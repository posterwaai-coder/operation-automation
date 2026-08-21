"""
Offline fulfilment pipeline — Operation Automation.

Same flow as ``main.py`` but with Google Drive taken out of the loop:

  1. Pull unfulfilled orders from Shopify            (still needs internet)
  2. Index a LOCAL artwork folder                    (was: walk a Drive folder)
  3. Copy the matched artwork out of it              (was: download from Drive)
  4. Write not_found.csv for anything unmatched
  5. Build the sticker sheets
  6. Drop empty folders
  7. Leave the result as a plain FOLDER on disk      (was: zip → upload → email)

``run_offline()`` returns the absolute path of the folder it created.
"""

import csv
import os
import re
import shutil
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from sticker_processor import StickerProcessor

# script.py reads TOKEN/MERCHANT at import time, so the .env next to this repo
# has to be on the environment before it is imported — see _load_shopify().
load_dotenv(dotenv_path=os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
))

# ── Tunables ──────────────────────────────────────────────────────────────────

# SKU routing and artwork matching are shared with the online pipeline so the
# two can't drift apart — see sku_rules.py.
from sku_rules import (  # noqa: E402
    IGNORED_NAMES,
    index_file,
    normalize_sku,
    resolve_artwork,
    resolve_with_size_fallback,
    route_sku,
    unique_dest_path,
)

# Timeout (seconds) for pulling customer-supplied custom artwork by URL.
CUSTOM_ARTWORK_TIMEOUT = 60


def _load_shopify():
    """
    Import script.py late so a missing/incomplete .env surfaces as a readable
    message in the run log instead of an import error at server startup.
    """
    try:
        import script
    except ValueError as exc:
        raise RuntimeError(
            f"Shopify is not configured: {exc}. Add a .env file in the repo root "
            "with TOKEN='shpat_…' and MERCHANT='your-merchant-name'."
        ) from exc
    return script


# ── Local artwork index ───────────────────────────────────────────────────────

def build_local_index(source_folder: str, log=print) -> tuple[dict, dict]:
    """
    Walk ``source_folder`` recursively and return two lookup tables:

      by_name  : "KPOPSTIC271.png" → /abs/path/KPOPSTIC271.png
                 "KPOPSTIC271"     → /abs/path/KPOPSTIC271.png   (stem, no ext)
      by_lower : the same keys lower-cased, used as a case-insensitive fallback

    This is the local equivalent of ``main._build_drive_index``. Directory
    entries are sorted so that, when two files share a stem, the winner is
    always the same one from run to run.
    """
    by_name: dict[str, str] = {}
    by_lower: dict[str, str] = {}
    file_count = 0

    for root_dir, dirs, files in os.walk(source_folder):
        # Skip hidden/system directories (.git, .Trash, __MACOSX, …)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__MACOSX")

        for name in sorted(files):
            if index_file(by_name, by_lower, name, os.path.join(root_dir, name)):
                file_count += 1

    log(f"Indexed {file_count} file(s) in the local artwork folder.")
    return by_name, by_lower


# ── Output helpers ────────────────────────────────────────────────────────────

def unique_run_folder(output_folder: str) -> str:
    """
    Return the path for this run's output folder, e.g.
    ``<output_folder>/21082026onlineorder``.

    If that name is taken (a second run on the same day), a ``_2``, ``_3``, …
    suffix is added rather than overwriting yesterday's — or this morning's —
    work.
    """
    date_str = datetime.now().strftime("%d%m%Y")
    base = os.path.join(output_folder, f"{date_str}onlineorder")

    if not os.path.exists(base):
        return base

    counter = 2
    while os.path.exists(f"{base}_{counter}"):
        counter += 1
    return f"{base}_{counter}"


def zip_run_folder(run_dir: str, log=print) -> str:
    """
    Zip a finished run folder into a sibling ``<name>.zip``.

    Only needed when the result is going out by email — a mail draft needs one
    file, not a directory. The run folder itself is left exactly as it is.
    """
    zip_path = f"{run_dir}.zip"
    if os.path.exists(zip_path):
        os.remove(zip_path)

    base = os.path.basename(run_dir)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root_dir, _, files in os.walk(run_dir):
            for name in sorted(files):
                full = os.path.join(root_dir, name)
                # Store paths under the run folder's own name, so unzipping
                # produces one tidy folder rather than loose A3/A4/… dirs.
                zf.write(full, os.path.join(base, os.path.relpath(full, run_dir)))

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    log(f"Created {os.path.basename(zip_path)} ({size_mb:.1f} MB) for emailing.")
    return zip_path


def remove_empty_folders(path: str):
    """Bottom-up sweep that deletes any folder left with nothing in it."""
    for root_dir, dirs, _ in os.walk(path, topdown=False):
        for dir_name in dirs:
            dir_path = os.path.join(root_dir, dir_name)
            try:
                if not os.listdir(dir_path):
                    os.rmdir(dir_path)
            except OSError:
                pass


def process_sticker_folders(input_dir: Path, output_dir: Path, log=print) -> None:
    """Expand every ``N copy`` folder into N stickers, then lay out the sheets."""
    processor = StickerProcessor()
    output_dir.mkdir(parents=True, exist_ok=True)

    all_stickers = []
    for subfolder in sorted(input_dir.iterdir()):
        if not subfolder.is_dir():
            continue
        match = re.search(r"(\d+)\s*copy", subfolder.name.lower())
        if not match:
            continue
        copies = int(match.group(1))
        for sticker_path in sorted(subfolder.glob("*.*")):
            for _ in range(copies):
                all_stickers.append(sticker_path)

    if not all_stickers:
        return

    generated = processor.process_multi_sticker_order(all_stickers, output_dir)
    log(f"  Generated {len(generated)} sticker sheet(s) from {len(all_stickers)} sticker(s).")


# ── Path validation ───────────────────────────────────────────────────────────

def validate_paths(source_folder: str, output_folder: str) -> tuple[str, str]:
    """
    Normalise and sanity-check the two folders, raising ValueError with a
    message meant for the user rather than letting a bare OSError surface later.
    """
    if not source_folder:
        raise ValueError("No artwork folder given.")
    if not output_folder:
        raise ValueError("No output folder given.")

    source = os.path.realpath(os.path.expanduser(source_folder))
    output = os.path.realpath(os.path.expanduser(output_folder))

    if not os.path.isdir(source):
        raise ValueError(f"Artwork folder does not exist: {source}")
    if not os.access(source, os.R_OK):
        raise ValueError(f"Artwork folder is not readable: {source}")

    # Writing the run inside the artwork folder would make the next run index
    # its own output; the reverse nests the source under the output tree.
    if output == source:
        raise ValueError("The output folder cannot be the artwork folder itself.")
    if output.startswith(source + os.sep):
        raise ValueError("The output folder cannot sit inside the artwork folder.")
    if source.startswith(output + os.sep):
        raise ValueError("The artwork folder cannot sit inside the output folder.")

    os.makedirs(output, exist_ok=True)
    if not os.access(output, os.W_OK):
        raise ValueError(f"Output folder is not writable: {output}")

    return source, output


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_offline(source_folder: str, output_folder: str, log=print,
                make_zip: bool = False, on_zip_ready=None) -> str:
    """
    Run the offline fulfilment pipeline.

    Parameters
    ----------
    source_folder : Local folder holding all artwork, searched recursively.
    output_folder : Local folder the dated run folder is created inside.
    log           : Callable for progress messages (offline_api passes its
                    log appender so the messages show up in the UI).
    make_zip      : Also produce a sibling .zip of the run folder. Needed only
                    when the result is being emailed, since a mail draft can
                    only carry a file.
    on_zip_ready  : Optional callback handed the .zip path once it exists.

    Returns
    -------
    str : Absolute path of the run folder that was created.
    """
    source, output = validate_paths(source_folder, output_folder)

    final_dir = unique_run_folder(output)
    # Build into a hidden sibling first and rename on success, so a run that
    # dies halfway can never leave a half-filled folder that looks finished.
    staging_dir = os.path.join(output, f".{os.path.basename(final_dir)}.partial")
    shutil.rmtree(staging_dir, ignore_errors=True)
    os.makedirs(staging_dir)

    try:
        # ── Step 1: fetch unfulfilled orders ─────────────────────────────────
        log("Fetching unfulfilled orders from Shopify…")
        orders = _load_shopify().get_data("Order")

        unfulfilled_skus = []
        for order in orders:
            if order.fulfillment_status is None:
                for line_item in order.line_items:
                    if line_item.sku:
                        unfulfilled_skus.append(
                            (order.id, line_item.sku, line_item.quantity, line_item.properties)
                        )

        log(f"Found {len(unfulfilled_skus)} unfulfilled SKU(s).")

        # ── Step 2: index the local artwork folder ───────────────────────────
        log(f"Indexing local artwork folder: {source}")
        by_name, by_lower = build_local_index(source, log)

        # ── Step 3: sort SKUs and copy the artwork that's needed ─────────────
        log("Sorting SKUs and copying artwork…")

        not_found = []
        copied = 0
        custom_count = 0
        skipped = 0

        for order_id, sku, quantity, properties in unfulfilled_skus:
            if sku is None:
                continue

            routed = route_sku(sku)
            if routed is None:
                # No printable marker (gift wrap, shipping protection, …).
                # Still recorded, so a SKU can never vanish without a trace.
                skipped += 1
                not_found.append((order_id, sku, quantity,
                                  "SKU has no A3/A4/A5/PP/STIC marker"))
                continue

            filename, folder_name, is_sticker = routed
            target_folder = os.path.join(staging_dir, folder_name)

            subfolder_path = os.path.join(target_folder, f"{quantity} copy")
            os.makedirs(subfolder_path, exist_ok=True)

            source_file, filename = resolve_with_size_fallback(
                filename, by_name, by_lower, is_sticker)

            if source_file:
                ext = os.path.splitext(source_file)[1]
                dest_file = unique_dest_path(
                    os.path.join(subfolder_path, f"{filename}{ext}")
                )
                shutil.copy2(source_file, dest_file)
                copied += 1
                if os.path.basename(dest_file) != f"{filename}{ext}":
                    log(f"  Copied {filename}{ext} → {os.path.basename(dest_file)} "
                        f"(another order already needed this SKU at qty {quantity})")
                else:
                    log(f"  Copied {filename}{ext}")

            elif properties and len(properties) > 0:
                # Customer-supplied custom artwork, hosted on a URL by Shopify.
                url = getattr(properties[0], "value", None)
                if not url or not str(url).lower().startswith(("http://", "https://")):
                    not_found.append((order_id, sku, quantity, "custom artwork property is not a URL"))
                    log(f"  ⚠ Custom artwork property is not a URL: {sku}")
                    continue

                custom_count += 1
                dest_file = os.path.join(subfolder_path, f"{filename}_{custom_count}.jpg")
                log(f"  Downloading custom artwork for {sku}…")
                try:
                    with urllib.request.urlopen(url, timeout=CUSTOM_ARTWORK_TIMEOUT) as resp, \
                            open(dest_file, "wb") as out:
                        shutil.copyfileobj(resp, out)
                    copied += 1
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    if os.path.exists(dest_file):
                        os.remove(dest_file)
                    not_found.append((order_id, sku, quantity, f"custom artwork download failed: {exc}"))
                    log(f"  ⚠ Custom artwork download failed for {sku}: {exc}")

            else:
                not_found.append((order_id, sku, quantity, "no matching file in artwork folder"))
                log(f"  ⚠ Not found locally: {sku}")

        if skipped:
            log(f"{skipped} SKU(s) had no A3/A4/A5/PP/STIC marker — listed in not_found.csv.")

        # ── Step 4: write not_found report ───────────────────────────────────
        not_found_path = os.path.join(staging_dir, "not_found.csv")
        with open(not_found_path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Order ID", "SKU", "Quantity", "Reason"])
            writer.writerows(not_found)

        if not_found:
            log(f"⚠ {len(not_found)} SKU(s) unresolved — see not_found.csv in the output folder.")

        # ── Step 5: build the sticker sheets ─────────────────────────────────
        sticker_dir = Path(staging_dir) / "stickers"
        if sticker_dir.exists():
            log("Processing sticker sheets…")
            process_sticker_folders(sticker_dir, sticker_dir, log)

        # ── Step 6: drop empty folders ───────────────────────────────────────
        remove_empty_folders(staging_dir)

        # ── Step 7: publish the finished folder ──────────────────────────────
        os.rename(staging_dir, final_dir)
        log(f"✅  Done — {copied} file(s) written to {final_dir}")

        # ── Step 8: optional ZIP, for attaching to an email ───────────────────
        if make_zip:
            try:
                zip_path = zip_run_folder(final_dir, log)
                if on_zip_ready:
                    on_zip_ready(zip_path)
            except Exception as exc:        # noqa: BLE001
                # The folder is the real deliverable; a failed zip must not
                # cost the operator a finished run.
                log(f"⚠ Could not create the ZIP for emailing: {exc}")
                log("  The output folder itself is complete and usable.")

        return final_dir

    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
