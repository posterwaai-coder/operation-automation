"""
Gemini (Nano Banana Pro) upscaling for customer-supplied custom posters.

Self-contained on purpose: everything this feature needs lives here, and the
existing pipeline only calls ``place_custom_poster()``. Nothing else in
main.py's fulfilment logic changes.

Dependencies are ``requests`` and ``Pillow``, both already in
requirements.txt — no new packages, so container start-up time is unchanged.

Custom posters are filed into one of two folders depending on how upscaling
went:

    Upscaled framed Custom Posters/<N> copy/<name>.jpg
    Non-Upscaled Custom posters/<N> copy/<name>.jpg

Set GEMINI_API_KEY to switch upscaling on. Without it every custom poster is
still delivered — just unmodified, into the Non-Upscaled folder — so a missing
key degrades the output rather than breaking the run.
"""

import base64
import io
import os

import requests
from PIL import Image, ImageOps

# ── Configuration ─────────────────────────────────────────────────────────────

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-pro-image-preview")
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"

# Requested generation quality, per spec.
IMAGE_QUALITY = "2K"

# 1742x2528 is a ratio of 0.689, which is not one of the API's supported
# aspect ratios. 2:3 (0.667) is the nearest — 3:4 (0.75) is much further off —
# so we generate at 2:3 and resize to the exact target below.
PORTRAIT_ASPECT = "2:3"
PORTRAIT_SIZE = (1742, 2528)

# Square artwork keeps its ratio rather than being cropped or letterboxed into
# the portrait shape. 2528 matches the portrait's long edge.
SQUARE_ASPECT = "1:1"
SQUARE_SIZE = (2528, 2528)

# Landscape artwork is turned upright before upscaling, so every custom poster
# leaves this module either portrait or square — never landscape.
# ROTATE_270 is a 270 degree counter-clockwise turn, i.e. 90 degrees clockwise:
# the original's left edge becomes the top.
ROTATE_TO_PORTRAIT = Image.Transpose.ROTATE_270

# Two attempts total: one retry when the first produces no image.
MAX_ATTEMPTS = 2
REQUEST_TIMEOUT = 180          # generation is slow; well above typical latency

UPSCALED_DIR = "Upscaled framed Custom Posters"
NON_UPSCALED_DIR = "Non-Upscaled Custom posters"

FAILURE_REASON = "Upscaling failed"

# Outcome of place_custom_poster(). Only FAILED writes a row to the error
# sheet — a poster skipped on purpose (upscaler switched off, or already
# sharp enough) is a normal result, not something to triage.
STATUS_UPSCALED = "upscaled"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

# Artwork already at or above this size is left alone: it is detailed enough to
# print, and an upscale would cost a call and a couple of minutes to produce
# something no better. Measured after the image is stood upright, so "height"
# and "width" mean the same thing whatever way round it arrived.
SKIP_ABOVE_HEIGHT = 1200
SKIP_ABOVE_WIDTH = 900

UPSCALE_PROMPT = (
    "Ultra-high-resolution 4K enhancement based strictly on the provided "
    "reference image.\n"
    "Absolute fidelity to the original subject, composition, and visual "
    "identity. Preserve framing, camera angle, and perspective with zero "
    "deviation.\n"
    "All structural elements, colors, textures, and background details must "
    "remain unchanged in placement and design. Recover fine-grain detail with "
    "natural realism. Enhance surface textures, material edges, and fine "
    "structural details without introducing stylization.\n"
    "Maintain original color science, white balance, and tonal relationships "
    "exactly as captured. Lighting direction, intensity, contrast, and shadow "
    "behavior must match the source image precisely, with only improved "
    "clarity and expanded dynamic range. No relighting, no reshaping.\n"
    "Remove any grain. Apply controlled sharpening and high-frequency detail "
    "reconstruction, remove compression artifacts and noise while retaining "
    "authentic texture. No artificial gloss, no over-processing.\n"
    "All edges and structural lines must remain consistent across the entire "
    "image with coherent geometry.\n"
    "Negative constraints: no warping, no altered proportions, no added or "
    "missing elements, no distortions, no perspective shift, no text or "
    "graphics overlaid, no hallucinated details, no stylized or illustrated "
    "rendering.\n"
    "Output must read as a true-to-life, photorealistic upscale that matches "
    "the reference exactly - only clearer, sharper, and higher resolution."
)


# ── Response parsing ──────────────────────────────────────────────────────────

def _looks_like_image_payload(value) -> bool:
    """A base64 image is a long string; short ones are ids, mime types, etc."""
    return isinstance(value, str) and len(value) > 512


def _extract_image_bytes(payload):
    """
    Pull the generated image out of a response, tolerating shape differences.

    Google has moved image generation between response formats (the Interactions
    API's ``output_image.data`` and the older ``candidates[].content.parts[]
    .inlineData.data``), so rather than binding to one layout this walks the
    JSON for the first plausible base64 image. It keeps working if the wire
    format shifts again, which matters for a preview-stage model.
    """
    found = []

    def walk(node):
        if found:
            return
        if isinstance(node, dict):
            for key in ("data", "b64_json", "bytesBase64Encoded"):
                if _looks_like_image_payload(node.get(key)):
                    found.append(node[key])
                    return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    if not found:
        return None

    try:
        return base64.b64decode(found[0], validate=False)
    except Exception:
        return None


def _describe_failure(payload) -> str:
    """Best-effort one-line reason, for the log when no image came back."""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:200]
        for key in ("output_text", "text"):
            if isinstance(payload.get(key), str) and payload[key].strip():
                return payload[key].strip()[:200]
    return "no image in response"


def _prepare_source(source_path: str):
    """
    Normalise the downloaded poster before it is sent for upscaling.

    Returns ``(jpeg_bytes, aspect_ratio, target_size, note, upright_size)``.

    Three things happen here:

    * EXIF orientation is baked in first. Phone cameras record a sideways photo
      plus a "rotate me" flag, so the raw pixel dimensions can say landscape
      when the image is really portrait — orientation has to be judged after
      the flag is applied, or the rotation below fires on the wrong images.
    * Landscape artwork is rotated 90 degrees clockwise to stand upright.
    * The aspect ratio and final size are chosen from the resulting shape:
      square stays square, everything else is portrait.
    """
    with Image.open(source_path) as opened:
        image = ImageOps.exif_transpose(opened)
        if image.mode != "RGB":
            image = image.convert("RGB")

        width, height = image.size
        if width > height:
            image = image.transpose(ROTATE_TO_PORTRAIT)
            note = f"landscape {width}x{height} rotated 90 clockwise to portrait"
            aspect, target = PORTRAIT_ASPECT, PORTRAIT_SIZE
        elif width == height:
            note = f"square {width}x{height} kept square"
            aspect, target = SQUARE_ASPECT, SQUARE_SIZE
        else:
            note = f"portrait {width}x{height}"
            aspect, target = PORTRAIT_ASPECT, PORTRAIT_SIZE

        upright_size = image.size
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=95, subsampling=0)

    return buffer.getvalue(), aspect, target, note, upright_size


def is_already_high_resolution(size) -> bool:
    """
    True when the artwork is big enough that upscaling adds nothing.

    Deliberately an OR: either dimension clearing its threshold is taken as
    "already sharp enough", which errs towards skipping and so towards not
    spending a paid call on artwork that does not need one.
    """
    width, height = size
    return height > SKIP_ABOVE_HEIGHT or width > SKIP_ABOVE_WIDTH


# ── Gemini call ───────────────────────────────────────────────────────────────

def _request_upscale(image_bytes: bytes, mime_type: str, api_key: str,
                     aspect_ratio: str = PORTRAIT_ASPECT):
    """One call to Gemini. Returns (image_bytes, None) or (None, reason)."""
    body = {
        "model": GEMINI_MODEL,
        "input": [
            {"type": "text", "text": UPSCALE_PROMPT},
            {
                "type": "image",
                "mime_type": mime_type,
                "data": base64.b64encode(image_bytes).decode("ascii"),
            },
        ],
        "response_format": {
            "type": "image",
            "mime_type": "image/jpeg",
            "aspect_ratio": aspect_ratio,
            "image_size": IMAGE_QUALITY,
        },
    }

    try:
        response = requests.post(
            GEMINI_ENDPOINT,
            json=body,
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        return None, f"request failed: {exc}"

    if response.status_code != 200:
        detail = response.text[:200].replace("\n", " ")
        return None, f"HTTP {response.status_code}: {detail}"

    try:
        payload = response.json()
    except ValueError:
        return None, "response was not JSON"

    data = _extract_image_bytes(payload)
    if not data:
        return None, _describe_failure(payload)

    return data, None


def upscale_bytes(image_bytes: bytes, mime_type: str, api_key: str, log=print,
                  aspect_ratio: str = PORTRAIT_ASPECT):
    """
    Upscale one image, retrying once. Returns the new bytes, or None if both
    attempts failed.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        data, reason = _request_upscale(image_bytes, mime_type, api_key, aspect_ratio)
        if data:
            if attempt > 1:
                log(f"    Upscale succeeded on attempt {attempt}.")
            return data
        if attempt < MAX_ATTEMPTS:
            log(f"    Upscale attempt {attempt} produced no image ({reason}) — retrying…")
        else:
            log(f"    ⚠ Upscale attempt {attempt} failed ({reason}).")
    return None


# ── Image handling ────────────────────────────────────────────────────────────

def _mime_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(ext, "image/jpeg")


def _save_at_target_size(image_bytes: bytes, dest_path: str, target_size):
    """
    Write the image at exactly ``target_size``.

    The API returns 2K at the nearest supported aspect ratio, which is close to
    but not exactly the print dimensions, so this final resize pins it.
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        if img.mode != "RGB":
            img = img.convert("RGB")
        img = img.resize(target_size, Image.Resampling.LANCZOS)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        img.save(dest_path, "JPEG", quality=95, subsampling=0)


# ── Entry point used by the pipeline ──────────────────────────────────────────

def place_custom_poster(source_path: str, destination_root: str, quantity,
                        file_name: str, log=print, enabled: bool = False):
    """
    Normalise a downloaded custom poster, optionally upscale it, and file it.

    Landscape artwork is stood upright and square artwork keeps its ratio, so
    everything delivered here is portrait or square regardless of whether the
    upscaler ran.

    Parameters
    ----------
    enabled : whether the operator ticked the upscaler for this run. Off by
              default, so upscaling never happens unless it was asked for.

    Returns
    -------
    (final_path, status) : status is STATUS_UPSCALED, STATUS_SKIPPED or
                           STATUS_FAILED. Only STATUS_FAILED should be recorded
                           in the error sheet — a skip is a normal outcome.
    """
    api_key = os.getenv("GEMINI_API_KEY")

    def _destination(folder: str) -> str:
        return os.path.join(destination_root, folder, f"{quantity} copy", file_name)

    def _file_unupscaled(reason: str, status: str, payload: bytes = None):
        """Deliver the poster without an upscale, stood upright where relevant."""
        target = _destination(NON_UPSCALED_DIR)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if payload is not None:
            with open(target, "wb") as handle:
                handle.write(payload)
            if os.path.exists(source_path):
                os.remove(source_path)
        else:
            # Preparation itself failed, so hand over the raw download intact.
            try:
                os.replace(source_path, target)
            except OSError:
                with open(source_path, "rb") as src, open(target, "wb") as dst:
                    dst.write(src.read())
                os.remove(source_path)
        log(f"    → {NON_UPSCALED_DIR} ({reason})")
        return target, status

    # Normalise first: orientation decides both the aspect ratio requested and
    # the size finished at, and gives the upright dimensions the size check
    # below is measured against.
    try:
        prepared, aspect, target_size, note, upright = _prepare_source(source_path)
        log(f"    {note}")
    except Exception as exc:        # noqa: BLE001
        return _file_unupscaled(f"could not read the download: {exc}",
                                STATUS_FAILED)

    if not enabled:
        return _file_unupscaled("upscaler is switched off for this run",
                                STATUS_SKIPPED, prepared)

    if is_already_high_resolution(upright):
        return _file_unupscaled(
            f"already {upright[0]}x{upright[1]}, above the "
            f"{SKIP_ABOVE_WIDTH}x{SKIP_ABOVE_HEIGHT} threshold",
            STATUS_SKIPPED, prepared)

    if not api_key:
        return _file_unupscaled("GEMINI_API_KEY is not set", STATUS_FAILED, prepared)

    log(f"    Upscaling with {GEMINI_MODEL} at {IMAGE_QUALITY}, {aspect}…")
    upscaled = upscale_bytes(prepared, "image/jpeg", api_key, log, aspect)

    if not upscaled:
        return _file_unupscaled(FAILURE_REASON, STATUS_FAILED, prepared)

    target = _destination(UPSCALED_DIR)
    try:
        _save_at_target_size(upscaled, target, target_size)
    except Exception as exc:        # noqa: BLE001
        if os.path.exists(target):
            os.remove(target)
        return _file_unupscaled(f"{FAILURE_REASON} — could not save: {exc}",
                                STATUS_FAILED, prepared)

    if os.path.exists(source_path):
        os.remove(source_path)

    log(f"    → {UPSCALED_DIR} at {target_size[0]}x{target_size[1]}")
    return target, STATUS_UPSCALED
