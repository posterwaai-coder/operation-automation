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
from PIL import Image

# ── Configuration ─────────────────────────────────────────────────────────────

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-pro-image-preview")
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"

# Requested generation quality, per spec.
IMAGE_QUALITY = "2K"

# 1742x2528 is a ratio of 0.689, which is not one of the API's supported
# aspect ratios. 2:3 (0.667) is the nearest — 3:4 (0.75) is much further off —
# so we generate at 2:3 and resize to the exact target below.
ASPECT_RATIO = "2:3"
TARGET_SIZE = (1742, 2528)

# Two attempts total: one retry when the first produces no image.
MAX_ATTEMPTS = 2
REQUEST_TIMEOUT = 180          # generation is slow; well above typical latency

UPSCALED_DIR = "Upscaled framed Custom Posters"
NON_UPSCALED_DIR = "Non-Upscaled Custom posters"

FAILURE_REASON = "Upscaling failed"

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


# ── Gemini call ───────────────────────────────────────────────────────────────

def _request_upscale(image_bytes: bytes, mime_type: str, api_key: str):
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
            "aspect_ratio": ASPECT_RATIO,
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


def upscale_bytes(image_bytes: bytes, mime_type: str, api_key: str, log=print):
    """
    Upscale one image, retrying once. Returns the new bytes, or None if both
    attempts failed.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        data, reason = _request_upscale(image_bytes, mime_type, api_key)
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


def _save_at_target_size(image_bytes: bytes, dest_path: str):
    """
    Write the image at exactly TARGET_SIZE.

    The API returns 2K at the nearest supported aspect ratio, which is close to
    but not exactly 1742x2528, so the final resize pins it to the print size.
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        if img.mode != "RGB":
            img = img.convert("RGB")
        img = img.resize(TARGET_SIZE, Image.Resampling.LANCZOS)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        img.save(dest_path, "JPEG", quality=95, subsampling=0)


# ── Entry point used by the pipeline ──────────────────────────────────────────

def place_custom_poster(source_path: str, destination_root: str, quantity,
                        file_name: str, log=print):
    """
    Upscale a downloaded custom poster and file it under the right folder.

    Parameters
    ----------
    source_path      : the just-downloaded original.
    destination_root : the run's output directory.
    quantity         : ordered quantity, used for the ``N copy`` subfolder.
    file_name        : basename to save as.

    Returns
    -------
    (final_path, ok) : ok is False when the poster landed in the Non-Upscaled
                       folder, so the caller can record "Upscaling failed".
    """
    api_key = os.getenv("GEMINI_API_KEY")

    def _destination(folder: str) -> str:
        return os.path.join(destination_root, folder, f"{quantity} copy", file_name)

    def _keep_original(reason: str):
        target = _destination(NON_UPSCALED_DIR)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            os.replace(source_path, target)
        except OSError:
            with open(source_path, "rb") as src, open(target, "wb") as dst:
                dst.write(src.read())
            os.remove(source_path)
        log(f"    → {NON_UPSCALED_DIR} ({reason})")
        return target, False

    if not api_key:
        return _keep_original("GEMINI_API_KEY is not set")

    try:
        with open(source_path, "rb") as handle:
            original = handle.read()
    except OSError as exc:
        return _keep_original(f"could not read the download: {exc}")

    log(f"    Upscaling with {GEMINI_MODEL} at {IMAGE_QUALITY}…")
    upscaled = upscale_bytes(original, _mime_for(source_path), api_key, log)

    if not upscaled:
        return _keep_original(FAILURE_REASON)

    target = _destination(UPSCALED_DIR)
    try:
        _save_at_target_size(upscaled, target)
    except Exception as exc:        # noqa: BLE001
        if os.path.exists(target):
            os.remove(target)
        return _keep_original(f"{FAILURE_REASON} — could not save: {exc}")

    if os.path.exists(source_path):
        os.remove(source_path)

    log(f"    → {UPSCALED_DIR} at {TARGET_SIZE[0]}x{TARGET_SIZE[1]}")
    return target, True
