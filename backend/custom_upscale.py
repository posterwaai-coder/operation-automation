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
import tempfile
import threading

import requests
from PIL import Image, ImageOps

# ── Configuration ─────────────────────────────────────────────────────────────

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-pro-image-preview")
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"

# ── Upscaling switch ─────────────────────────────────────────────────────────
#
# PARKED. CPU inference was costing minutes per poster, which made a batch with
# many custom orders unworkable. Custom posters are still stood upright and
# framed to the print canvas — only the AI step is skipped, and they all land in
# one folder rather than being split by whether it succeeded.
#
# Everything below (routing, Real-ESRGAN, Gemini) is left intact and simply
# unreached, so this is one switch to reverse rather than a rewrite:
#
#     UPSCALING_ENABLED=1        in the environment, or flip the default here
#
UPSCALING_ENABLED = os.getenv("UPSCALING_ENABLED", "").strip().lower() in (
    "1", "true", "yes", "on",
)

# Where custom posters go while upscaling is parked. One bin: with no AI step
# there is nothing to sort them by, and UPSCALED_DIR / NON_UPSCALED_DIR would
# both be misleading.
CUSTOM_DIR = "Custom Posters"

# Final canvas: 308 x 447 mm at 300 DPI. Every custom poster is delivered at
# exactly this size — the image is fitted inside, the canvas never changes.
TARGET_SIZE = (3638, 5280)

# 3638x5280 is 19.2 MP. Gemini's 2K output is ~4.2 MP, which would need a 2.2x
# interpolation afterwards and throw away much of what was paid for; 4K is
# ~16.8 MP, a 1.09x finish. Gemini now only handles the worst inputs, where
# output quality matters most, so it runs at 4K. Override to "2K" to halve the
# per-image cost at the expense of detail.
IMAGE_QUALITY = os.getenv("GEMINI_IMAGE_SIZE", "4K")

# 3638/5280 = 0.689, which is not a supported aspect ratio; 2:3 (0.667) is the
# nearest. Generated at 2:3, then fitted to the canvas.
GEMINI_ASPECT = "2:3"

# Routing band for the local upscaler.
#
# Above 85% the enlargement needed is under 1.18x, where LANCZOS is visually
# indistinguishable — and since inference time scales with the SOURCE size,
# that upper slice was the most expensive and least useful work in the
# pipeline. Below 15% even repeated x2 passes leave a large gap to close.
ESRGAN_MIN_FRACTION = 0.15
ESRGAN_MAX_FRACTION = 0.85

# Print sizes whose custom posters are worth paying Gemini for when they fall
# below the local band. A3 is the largest sheet in the range, so a weak source
# shows most there; every other size is served by the local upscaler.
GEMINI_PRINT_SIZES = ("A3",)

# x2 passes allowed before the fit-to-canvas step finishes the job. Three
# covers artwork from about 12% of the canvas upward. Each pass quadruples the
# pixels the next one must process, so this stays bounded — but the inputs
# down here are small, so the passes themselves are cheap.
ESRGAN_MAX_PASSES = 3

# Real-ESRGAN x2, run on CPU through ONNX Runtime. x2 is deliberate: nothing in
# the 65-100% band needs more than 1.54x, and x4 would cost four times the
# inference for detail that is discarded in the fit-to-canvas step.
ESRGAN_MODEL_URL = os.getenv(
    "ESRGAN_MODEL_URL",
    "https://huggingface.co/SceneWorks/real-esrgan-onnx/resolve/main/real_esrgan_x2.onnx",
)
ESRGAN_MODEL_PATH = os.getenv(
    "ESRGAN_MODEL_PATH", os.path.join(tempfile.gettempdir(), "real_esrgan_x2.onnx")
)
ESRGAN_SCALE = 2
ESRGAN_TILE = 256          # measured sweet spot: throughput plateaus here
# The network pixel-shuffles by 2, so every tile it sees must have even sides.
ESRGAN_SIZE_MULTIPLE = 2
ESRGAN_OVERLAP = 16        # trimmed after inference, so tile seams don't show
ESRGAN_THREADS = int(os.getenv("ESRGAN_THREADS", "4"))

# Two attempts at Gemini: one retry when the first produces no image.
MAX_ATTEMPTS = 2
REQUEST_TIMEOUT = 300

UPSCALED_DIR = "Upscaled framed Custom Posters"
NON_UPSCALED_DIR = "Non-Upscaled Custom posters"

FAILURE_REASON = "Upscaling failed"

# Outcome of place_custom_poster(). Only FAILED writes a row to the error
# sheet — a poster that simply did not need the AI is a normal result.
STATUS_UPSCALED = "upscaled"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

# Landscape artwork is turned upright before anything else, so every poster
# leaves this module portrait. ROTATE_270 is 90 degrees clockwise: the
# original's left edge becomes the top.
ROTATE_TO_PORTRAIT = Image.Transpose.ROTATE_270

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
    "the reference exactly \u2014 only clearer, sharper, and higher resolution."
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
    Open the download and stand it upright.

    EXIF orientation is baked in first: a phone records a sideways photo plus a
    "rotate me" flag, so raw pixel dimensions can read landscape when the image
    is really portrait, and judging orientation before applying the flag would
    rotate exactly the wrong images.

    Returns ``(image, note)`` with the image in RGB, portrait or square.
    """
    with Image.open(source_path) as opened:
        image = ImageOps.exif_transpose(opened)
        if image.mode != "RGB":
            image = image.convert("RGB")
        image.load()

    width, height = image.size
    if width > height:
        image = image.transpose(ROTATE_TO_PORTRAIT)
        note = f"landscape {width}x{height} rotated 90 clockwise"
    elif width == height:
        note = f"square {width}x{height}"
    else:
        note = f"portrait {width}x{height}"
    return image, note


def canvas_fraction(size) -> float:
    """
    How much of the canvas the artwork fills once fitted, along whichever edge
    reaches the frame first.

    1.0 means it already fills the canvas; 0.65 means it must be enlarged 1.54x
    to do so. Using the limiting edge keeps the number meaningful for artwork
    whose aspect ratio differs from the canvas.
    """
    width, height = size
    return max(width / TARGET_SIZE[0], height / TARGET_SIZE[1])


def choose_route(size, size_folder: str = "") -> str:
    """
    Pick how a poster reaches the canvas, from its size and print size.

      >= 85%        fit only — under 1.18x, LANCZOS is indistinguishable
      15% .. 85%    Real-ESRGAN x2 locally, free
      < 15%         Gemini, but only for the print sizes worth paying for;
                    everything else still goes to the local upscaler

    Gating the paid route on print size is what keeps the bill down: a weak
    source is most visible on the largest sheet, so A3 is worth the call and
    the smaller sizes are not.
    """
    fraction = canvas_fraction(size)
    if fraction >= ESRGAN_MAX_FRACTION:
        return "fit"
    if fraction >= ESRGAN_MIN_FRACTION:
        return "esrgan"
    if (size_folder or "").strip().upper() in GEMINI_PRINT_SIZES:
        return "gemini"
    return "esrgan"


def frame_to_canvas(image: Image.Image) -> Image.Image:
    """
    Fit the artwork inside the canvas on white, centred, aspect preserved.

    Nothing is cropped and nothing exceeds TARGET_SIZE. A square, or anything
    else whose ratio differs from the canvas, gets white bars — which is what
    makes the output size uniform for printing.
    """
    fitted = ImageOps.contain(image, TARGET_SIZE, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", TARGET_SIZE, (255, 255, 255))
    canvas.paste(fitted, ((TARGET_SIZE[0] - fitted.width) // 2,
                          (TARGET_SIZE[1] - fitted.height) // 2))
    return canvas


# ── Real-ESRGAN (local, CPU) ──────────────────────────────────────────────────

_session = None
_session_lock = threading.Lock()


def _ensure_model() -> str:
    """Fetch the ONNX weights once per container, into a temp path."""
    if os.path.exists(ESRGAN_MODEL_PATH) and os.path.getsize(ESRGAN_MODEL_PATH) > 1_000_000:
        return ESRGAN_MODEL_PATH

    response = requests.get(ESRGAN_MODEL_URL, stream=True, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    partial = ESRGAN_MODEL_PATH + ".part"
    with open(partial, "wb") as handle:
        for chunk in response.iter_content(1 << 20):
            handle.write(chunk)
    os.replace(partial, ESRGAN_MODEL_PATH)
    return ESRGAN_MODEL_PATH


def _get_session():
    """
    Build the inference session once and keep it.

    Loading costs a couple of seconds and ~100 MB, so rebuilding it per poster
    would dominate a batch. It is imported lazily so that a deployment which
    never hits the 65-100% band pays neither the import nor the memory.
    """
    global _session
    with _session_lock:
        if _session is None:
            import onnxruntime as ort
            options = ort.SessionOptions()
            options.intra_op_num_threads = ESRGAN_THREADS
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            _session = ort.InferenceSession(
                _ensure_model(), options, providers=["CPUExecutionProvider"]
            )
    return _session


def release_session():
    """Drop the model so a long-lived worker isn't billed for idle memory."""
    global _session
    with _session_lock:
        _session = None


def esrgan_upscale(image: Image.Image, log=print) -> Image.Image:
    """
    Upscale x2 with Real-ESRGAN, tile by tile.

    Tiling keeps peak memory proportional to one tile rather than the whole
    image, which is what makes a 19 MP poster survivable inside a container.
    Tiles are inferred with an overlap that is trimmed afterwards, so the seams
    between them don't show.
    """
    import numpy as np

    session = _get_session()
    input_name = session.get_inputs()[0].name

    source = np.asarray(image, dtype=np.float32) / 255.0
    height, width = source.shape[:2]
    scale = ESRGAN_SCALE
    # uint8 output buffer: float32 here would cost four times the memory for
    # precision that is discarded on save anyway.
    out = np.zeros((height * scale, width * scale, 3), dtype=np.uint8)

    tiles = 0
    for top in range(0, height, ESRGAN_TILE):
        for left in range(0, width, ESRGAN_TILE):
            y0 = max(0, top - ESRGAN_OVERLAP)
            x0 = max(0, left - ESRGAN_OVERLAP)
            y1 = min(height, top + ESRGAN_TILE + ESRGAN_OVERLAP)
            x1 = min(width, left + ESRGAN_TILE + ESRGAN_OVERLAP)

            patch = source[y0:y1, x0:x1]

            # The network pixel-shuffles by 2, so it rejects an odd-sided
            # tile outright ("cannot be reshaped"). Edge tiles inherit the
            # image's own parity, so any poster with an odd width or height
            # had its last row and column fail. Pad by edge replication —
            # zeros would darken the border — and trim the result back.
            pad_h = patch.shape[0] % ESRGAN_SIZE_MULTIPLE
            pad_w = patch.shape[1] % ESRGAN_SIZE_MULTIPLE
            if pad_h or pad_w:
                patch = np.pad(patch, ((0, pad_h), (0, pad_w), (0, 0)), mode="edge")

            tensor = patch.transpose(2, 0, 1)[None]
            result = session.run(None, {input_name: tensor})[0][0].transpose(1, 2, 0)

            if pad_h or pad_w:
                result = result[:result.shape[0] - pad_h * scale,
                                :result.shape[1] - pad_w * scale]

            # Trim the overlap back off, and clip to the image edge.
            bottom = min(top + ESRGAN_TILE, height)
            right = min(left + ESRGAN_TILE, width)
            oy = (top - y0) * scale
            ox = (left - x0) * scale
            th = (bottom - top) * scale
            tw = (right - left) * scale

            out[top * scale:top * scale + th, left * scale:left * scale + tw] = (
                np.clip(result[oy:oy + th, ox:ox + tw] * 255.0, 0, 255).astype(np.uint8)
            )
            tiles += 1

    log(f"      {tiles} tile(s) at {ESRGAN_TILE}px, {width}x{height} -> "
        f"{width * scale}x{height * scale}")
    return Image.fromarray(out)


# ── Gemini call ───────────────────────────────────────────────────────────────

def _request_upscale(image_bytes: bytes, mime_type: str, api_key: str,
                     aspect_ratio: str = GEMINI_ASPECT):
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
                  aspect_ratio: str = GEMINI_ASPECT):
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


def esrgan_to_canvas(image: Image.Image, log=print) -> Image.Image:
    """
    Run x2 passes until the artwork spans the canvas, or the cap is reached.

    One pass is enough for the 65-85% band. The Gemini fallback can start much
    smaller, so a second pass is allowed there; beyond that the fit-to-canvas
    LANCZOS step finishes the job, which is far cheaper than a third inference.
    """
    result = image
    for _ in range(ESRGAN_MAX_PASSES):
        # Stop as soon as the remaining gap is one LANCZOS step — the same
        # 85% reasoning the router uses. Running a further doubling from
        # there costs four times the pixels to produce something that is
        # then scaled straight back down.
        if canvas_fraction(result.size) >= ESRGAN_MAX_FRACTION:
            break
        result = esrgan_upscale(result, log)
    return result


# ── Orchestration ─────────────────────────────────────────────────────────────

def _gemini_upscale(image: Image.Image, log=print):
    """Send the upright artwork to Gemini. Returns a PIL image, or None."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        log("      GEMINI_API_KEY is not set")
        return None

    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=95, subsampling=0)

    log(f"      {GEMINI_MODEL} at {IMAGE_QUALITY}, {GEMINI_ASPECT}")
    data = upscale_bytes(buffer.getvalue(), "image/jpeg", api_key, log, GEMINI_ASPECT)
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as generated:
            return generated.convert("RGB")
    except Exception as exc:            # noqa: BLE001
        log(f"      returned bytes were not a readable image: {exc}")
        return None


def place_custom_poster(source_path: str, destination_root: str, quantity,
                        file_name: str, log=print, size_folder: str = ""):
    """
    Take a downloaded custom poster to the finished canvas and file it.

    The route is decided by size alone — there is no switch. Artwork already
    filling the canvas is simply fitted; artwork within 1.54x of it is upscaled
    locally with Real-ESRGAN; anything smaller goes to Gemini, which
    reconstructs large factors far better than a 2x model.

    Parameters
    ----------
    size_folder : print size from the SKU (A3/A4/A5/PP), so custom posters sit
                  in the same folder shape as everything else.

    Returns
    -------
    (final_path, status) : only STATUS_FAILED belongs in the error sheet.
    """
    def _destination(folder: str) -> str:
        parts = [destination_root, folder]
        if size_folder:
            parts.append(size_folder)
        parts.append(f"{quantity} copy")
        parts.append(file_name)
        return os.path.join(*parts)

    def _save(image: Image.Image, folder: str, status: str, reason: str):
        target = _destination(folder)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        frame_to_canvas(image).save(target, "JPEG", quality=95, subsampling=0)
        if os.path.exists(source_path):
            os.remove(source_path)
        log(f"    → {folder}/{size_folder or '.'}/{quantity} copy "
            f"at {TARGET_SIZE[0]}x{TARGET_SIZE[1]} ({reason})")
        return target, status

    def _rescue(reason: str):
        """Preparation failed outright — hand over the raw download untouched."""
        target = _destination(NON_UPSCALED_DIR)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            os.replace(source_path, target)
        except OSError:
            with open(source_path, "rb") as src, open(target, "wb") as dst:
                dst.write(src.read())
            os.remove(source_path)
        log(f"    → {NON_UPSCALED_DIR} ({reason})")
        return target, STATUS_FAILED

    try:
        image, note = _prepare_source(source_path)
    except Exception as exc:            # noqa: BLE001
        return _rescue(f"could not read the download: {exc}")

    fraction = canvas_fraction(image.size)

    if not UPSCALING_ENABLED:
        # Framing only. Orientation, canvas and folder layout are unchanged;
        # the poster simply never reaches the AI step.
        log(f"    {note}, {fraction:.0%} of canvas, {size_folder or 'no size'}")
        return _save(image, CUSTOM_DIR, STATUS_SKIPPED,
                     "framed, upscaling parked")

    route = choose_route(image.size, size_folder)
    log(f"    {note}, {fraction:.0%} of canvas, {size_folder or 'no size'} → {route}")

    if route == "fit":
        return _save(image, UPSCALED_DIR, STATUS_SKIPPED,
                     "already fills the canvas, fitted only")

    if route == "esrgan":
        try:
            enlarged = esrgan_to_canvas(image, log)
            return _save(enlarged, UPSCALED_DIR, STATUS_UPSCALED, "Real-ESRGAN x2")
        except Exception as exc:        # noqa: BLE001
            # A local failure must never cost the poster; fall through to the
            # framed original rather than dropping the order.
            log(f"    ⚠ Real-ESRGAN failed: {exc}")
            return _save(image, NON_UPSCALED_DIR, STATUS_FAILED, FAILURE_REASON)

    # Gemini route. Two attempts inside upscale_bytes; if both come back
    # without an image, fall back to the local upscaler rather than shipping
    # an un-enlarged poster — a quota block or a refusal on Google's side
    # shouldn't decide the quality of the print.
    try:
        generated = _gemini_upscale(image, log)
    except Exception as exc:            # noqa: BLE001
        log(f"    ⚠ Gemini raised: {exc}")
        generated = None

    if generated is not None:
        return _save(generated, UPSCALED_DIR, STATUS_UPSCALED, f"Gemini {IMAGE_QUALITY}")

    log("    Gemini produced nothing after 2 attempts — falling back to Real-ESRGAN")
    try:
        enlarged = esrgan_to_canvas(image, log)
        return _save(enlarged, UPSCALED_DIR, STATUS_UPSCALED,
                     "Real-ESRGAN x2 after Gemini failed")
    except Exception as exc:            # noqa: BLE001
        log(f"    ⚠ Real-ESRGAN fallback also failed: {exc}")
        return _save(image, NON_UPSCALED_DIR, STATUS_FAILED, FAILURE_REASON)
