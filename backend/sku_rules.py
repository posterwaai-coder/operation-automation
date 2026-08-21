"""
SKU routing and artwork matching, shared by both pipelines.

``main.py`` (Google Drive) and ``offline_main.py`` (local folder) fetch artwork
in completely different ways, but they have to agree on two things: which
folder a SKU belongs in, and which file in the source answers to it. Keeping
that agreement in one module is what stops the two from drifting apart — the
case-sensitivity bug that dropped line items silently was fixed once here
rather than twice, slightly differently.

An "index" below is a plain dict mapping a lookup key to whatever the caller
needs to fetch the file: an absolute path offline, a ``(file_id, name)`` pair
for Drive. Nothing here cares which.
"""

import os

# Extensions tried when a SKU has no extension of its own, in priority order.
ARTWORK_EXTENSIONS = [".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".pdf"]

# Stickers are cut around an alpha channel, so transparent formats are tried
# first. A flat .jpg preview filed next to the real .png used to win purely on
# folder sort order, which produced a square sticker with no die-cut edge.
STICKER_EXTENSIONS = [".png", ".webp", ".tif", ".tiff", ".jpg", ".jpeg"]

# Suffixes that mark a print size and are not part of the artwork filename.
SIZE_SUFFIXES = ("A3", "A4", "A5", "PP")

# Files that are never artwork and would only pollute the index.
IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def normalize_sku(sku: str) -> str:
    """
    Shopify SKUs routinely arrive with stray whitespace and inconsistent
    casing. Routing and matching both work off this normalised form; the raw
    SKU is kept for reporting so it can still be found in the Shopify admin.
    """
    return (sku or "").strip().upper()


def route_sku(sku: str):
    """
    Decide where a SKU belongs. Returns ``(stem, folder, is_sticker)``, or None
    when the SKU carries no printable marker at all.

    The marker tests used to run against the raw SKU, so a lower-case ``stic``
    or a trailing space meant the line item matched nothing and was dropped
    without ever reaching not_found.csv.
    """
    key = normalize_sku(sku)
    if not key:
        return None
    if "STIC" in key:
        return key, "stickers", True
    for suffix in SIZE_SUFFIXES:
        if key.endswith(suffix):
            return key[:-len(suffix)], suffix, False
    return None


def index_file(by_name: dict, by_lower: dict, name: str, value):
    """
    Record one source file under every key a SKU might be written as.

    The full filename always wins over a stem claimed by an earlier file, and
    stems use setdefault so a later duplicate can't silently displace the entry
    an earlier one established.
    """
    if name in IGNORED_NAMES or name.startswith("."):
        return False

    stem = os.path.splitext(name)[0]
    by_name[name] = value
    by_name.setdefault(stem, value)
    by_lower.setdefault(name.lower(), value)
    by_lower.setdefault(stem.lower(), value)
    return True


def merge_indexes(sources):
    """
    Merge several ``(by_name, by_lower)`` index pairs into one.

    Earlier sources win every conflict. That matters once artwork is spread
    across more than one folder: when the same SKU exists in both, the run has
    to pick the same file every time, and "the first folder you listed" is a
    rule an operator can reason about — unlike "whichever the API paginated
    first", which is what an unordered merge would give.
    """
    merged_name, merged_lower = {}, {}
    for by_name, by_lower in sources:
        for key, value in by_name.items():
            merged_name.setdefault(key, value)
        for key, value in by_lower.items():
            merged_lower.setdefault(key, value)
    return merged_name, merged_lower


def resolve_artwork(filename: str, by_name: dict, by_lower: dict,
                    prefer_transparent: bool = False):
    """
    Find the source entry for a SKU stem, or None.

    Explicit extensions are tried before the bare stem, because the bare-stem
    entry is whichever file the source walk happened to reach first — that made
    the result depend on folder names. Each pass runs case-sensitively and then
    case-insensitively: artwork folders are rarely consistent about SKU casing.
    """
    extensions = STICKER_EXTENSIONS if prefer_transparent else ARTWORK_EXTENSIONS
    lowered = filename.lower()

    for ext in extensions:
        if f"{filename}{ext}" in by_name:
            return by_name[f"{filename}{ext}"]

    for ext in extensions:
        if f"{lowered}{ext}" in by_lower:
            return by_lower[f"{lowered}{ext}"]

    # Last resort: an entry stored under the bare stem (no extension of its own).
    if filename in by_name:
        return by_name[filename]
    if lowered in by_lower:
        return by_lower[lowered]

    return None


def resolve_with_size_fallback(filename: str, by_name: dict, by_lower: dict,
                               is_sticker: bool):
    """
    Resolve ``filename``, and for stickers retry without a trailing print-size
    suffix — some sticker SKUs carry one (KPOPSTIC271A4) while the file on disk
    is named without it.

    Returns ``(entry, stem_actually_matched)``; entry is None if nothing hit.
    """
    entry = resolve_artwork(filename, by_name, by_lower, prefer_transparent=is_sticker)
    if entry is not None or not is_sticker:
        return entry, filename

    for suffix in SIZE_SUFFIXES:
        if filename.endswith(suffix):
            stripped = filename[:-len(suffix)]
            entry = resolve_artwork(stripped, by_name, by_lower, prefer_transparent=True)
            if entry is not None:
                return entry, stripped
            break

    return None, filename


def unique_dest_path(dest_path: str) -> str:
    """
    Avoid clobbering a file already written to this ``N copy`` folder.

    Two separate orders for the same SKU at the same quantity both land in e.g.
    ``stickers/2 copy/KPOPSTIC271.png``. Overwriting would make the sticker
    processor see one design where two were ordered, and the batch would print
    short — so the second one becomes ``KPOPSTIC271__2.png``.
    """
    if not os.path.exists(dest_path):
        return dest_path

    stem, ext = os.path.splitext(dest_path)
    counter = 2
    while os.path.exists(f"{stem}__{counter}{ext}"):
        counter += 1
    return f"{stem}__{counter}{ext}"
