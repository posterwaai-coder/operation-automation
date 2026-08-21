import os
import shutil
import csv
import smtplib
import tempfile
import urllib.request
from email.message import EmailMessage

import script
import zipfile
from datetime import datetime
from google.oauth2 import service_account, credentials as google_credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError, GoogleAuthError
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError
from dotenv import load_dotenv
from sticker_processor import StickerProcessor
from sku_rules import (
    index_file,
    resolve_with_size_fallback,
    route_sku,
    unique_dest_path,
)
import re
from pathlib import Path

# ── Environment ───────────────────────────────────────────────────────────────

def get_repo_root():
    """Always returns the repo root regardless of working directory."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

load_dotenv(dotenv_path=os.path.join(get_repo_root(), ".env"))

# ── Google Drive service (shared, built once per run) ─────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",    # upload / create
    "https://www.googleapis.com/auth/drive.readonly", # read / download source
]

import json


# Actionable message shown to the user (via the UI) when Google rejects our
# credentials — replaces the cryptic ``('invalid_grant: Bad Request', ...)`` tuple.
_AUTH_HELP = (
    "Google Drive authentication failed. The credentials are expired, revoked, "
    "or don't match. To fix:\n"
    "  • Service account (recommended, never expires): set GOOGLE_SERVICE_ACCOUNT_JSON "
    "to the key JSON, or place credentials.json in the repo root, and share the Drive "
    "folder(s) with the service-account email.\n"
    "  • OAuth: regenerate the token with `python get_refresh_token.py` and update "
    "GOOGLE_OAUTH_REFRESH_TOKEN. Publish the OAuth consent screen to Production so "
    "tokens stop expiring after 7 days (the usual cause of invalid_grant)."
)


def _service_account_credentials():
    """
    Return service-account credentials from GOOGLE_SERVICE_ACCOUNT_JSON (inline
    JSON, ideal for deployment secrets) or a credentials.json file at the repo
    root — or None if neither is present.
    """
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        try:
            info = json.loads(sa_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is set but is not valid JSON."
            ) from exc
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    sa_path = os.path.join(get_repo_root(), "credentials.json")
    if os.path.exists(sa_path):
        return service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)

    return None


def _oauth_credentials():
    """Return OAuth user credentials from the GOOGLE_OAUTH_* env vars, or None."""
    client_id     = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN")

    if not (client_id and client_secret and refresh_token):
        return None

    return google_credentials.Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )


def _get_drive_service():
    """
    Build and return an authenticated Drive service.

    Auth is resolved in priority order:
      1. Service account (GOOGLE_SERVICE_ACCOUNT_JSON or credentials.json) — preferred,
         since service-account keys don't expire like OAuth refresh tokens do.
      2. OAuth user credentials (GOOGLE_OAUTH_CLIENT_ID / _SECRET / _REFRESH_TOKEN).
    """
    creds = _service_account_credentials()
    if creds is None:
        creds = _oauth_credentials()

    if creds is None:
        raise RuntimeError(
            "No Google Drive credentials configured. Set GOOGLE_SERVICE_ACCOUNT_JSON "
            "(or add credentials.json), or set GOOGLE_OAUTH_CLIENT_ID, "
            "GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REFRESH_TOKEN."
        )

    # OAuth credentials need an explicit refresh to obtain an access token; this is
    # where a dead refresh token surfaces as invalid_grant. Service-account creds
    # refresh lazily on first API call, so we let that error surface at call sites.
    if not creds.valid:
        try:
            creds.refresh(Request())
        except (RefreshError, GoogleAuthError) as exc:
            raise RuntimeError(f"{_AUTH_HELP}\n\nUnderlying error: {exc}") from exc

    return build("drive", "v3", credentials=creds)


# ── Drive helpers ─────────────────────────────────────────────────────────────

def _build_drive_index(service, folder_id: str) -> tuple[dict, dict]:
    """
    Recursively walk a Drive folder (including all subfolders) and return two
    lookup tables mapping a SKU-shaped key to ``(file_id, drive_filename)``.

    The filename is carried alongside the id because the destination file needs
    the real extension: a bare-stem match used to record no extension at all,
    and the sticker processor's ``glob("*.*")`` then skipped the extension-less
    file it produced, so those stickers vanished from the sheets.

    Nothing is downloaded here — this only decides what is worth fetching.
    """
    by_name, by_lower = {}, {}
    _walk_drive_folder(service, folder_id, by_name, by_lower)
    return by_name, by_lower


def _walk_drive_folder(service, folder_id: str, by_name: dict, by_lower: dict):
    """Recursively populate the two indexes from a Drive folder tree."""
    page_token = None
    subfolders, files = [], []

    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType)",
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        for item in response.get("files", []):
            if item["mimeType"] == "application/vnd.google-apps.folder":
                subfolders.append(item)
            else:
                files.append(item)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    # Sorted, files before subfolders: when two files share a stem the winner
    # has to be the same one on every run, not whichever page returned first.
    for item in sorted(files, key=lambda f: f["name"]):
        index_file(by_name, by_lower, item["name"], (item["id"], item["name"]))

    for item in sorted(subfolders, key=lambda f: f["name"]):
        _walk_drive_folder(service, item["id"], by_name, by_lower)


def _download_file(service, file_id: str, dest_path: str):
    """Download a single Drive file to dest_path."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dest_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


# ── Pipeline helpers (unchanged logic) ───────────────────────────────────────

def zip_folder(folder_path, zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root_dir, _, files in os.walk(folder_path):
            for file in files:
                full_path = os.path.join(root_dir, file)
                arcname = os.path.relpath(full_path, os.path.join(folder_path, "../.."))
                zipf.write(full_path, arcname)


def remove_empty_folders(path):
    for root_dir, dirs, _ in os.walk(path, topdown=False):
        for dir_name in dirs:
            dir_path = os.path.join(root_dir, dir_name)
            if not os.listdir(dir_path):
                os.rmdir(dir_path)


def upload_to_drive(file_path, folder_id):
    try:
        service = _get_drive_service()
        file_metadata = {
            "name": os.path.basename(file_path),
            "parents": [folder_id],
        }
        media = MediaFileUpload(file_path, mimetype="application/zip", resumable=True)
        print(f"Starting upload of {file_path} to Google Drive...")
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        ).execute()
        service.permissions().create(
            fileId=file.get("id"),
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
        ).execute()
        print(f"Upload successful! Web View Link: {file.get('webViewLink')}")
        return file.get("webViewLink")
    except FileNotFoundError as e:
        print(f"Error: File not found - {e}")
        raise
    except HttpError as e:
        print(f"Error: Google Drive API error - {e}")
        raise
    except Exception as e:
        print(f"Error: Unexpected error - {e}")
        raise


# How long to wait on the SMTP conversation before giving up.
SMTP_TIMEOUT = 30

_SMTP_AUTH_HELP = (
    "Gmail rejected the login. SENDER_PASSWORD has to be a 16-character Google "
    "app password — generate one at https://myaccount.google.com/apppasswords "
    "with 2-Step Verification switched on. Google stopped accepting plain "
    "account passwords for SMTP in May 2022, so the normal mailbox password "
    "will always fail here."
)


def send_email(shared_link, recipient_email, cc_email=None, log=print):
    """
    Send the notification through Gmail's SMTP server.

    Reads SENDER_EMAIL and SENDER_PASSWORD (a Google app password). SMTP_HOST
    and SMTP_PORT can point this at a different provider; port 465 switches to
    implicit SSL, anything else uses STARTTLS.
    """
    sender_email = os.getenv("SENDER_EMAIL")
    # App passwords are shown in groups of four — the spaces are cosmetic and
    # get pasted into .env more often than not.
    password = (os.getenv("SENDER_PASSWORD") or "").replace(" ", "")
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))

    if not sender_email or not password:
        raise RuntimeError(
            "Email is not configured. Set SENDER_EMAIL and SENDER_PASSWORD "
            "(a Google app password) in .env."
        )

    message = EmailMessage()
    message["From"] = sender_email
    message["To"] = recipient_email
    if cc_email:
        message["Cc"] = cc_email
    message["Subject"] = "Shared Google Drive Link"
    message.set_content(
        f"Here is the shared link to the uploaded file:\n\n{shared_link}\n"
    )
    message.add_alternative(
        f"<p>Here is the shared link to the uploaded file: "
        f"<a href='{shared_link}'>{shared_link}</a></p>",
        subtype="html",
    )

    recipients = [recipient_email] + ([cc_email] if cc_email else [])

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT)
        else:
            server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)

        with server:
            if port != 465:
                server.starttls()
            server.login(sender_email, password)
            server.send_message(message, to_addrs=recipients)

    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(f"{_SMTP_AUTH_HELP}\n\nServer said: {exc}") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise RuntimeError(f"Could not send mail via {host}:{port} — {exc}") from exc

    log(f"  Email sent to {', '.join(recipients)}.")


def process_sticker_folders(input_dir: Path, output_dir: Path) -> None:
    processor = StickerProcessor()
    output_dir.mkdir(exist_ok=True)
    all_stickers = []
    for subfolder in input_dir.iterdir():
        if subfolder.is_dir():
            match = re.search(r'(\d+)\s*copy', subfolder.name.lower())
            if match:
                copies = int(match.group(1))
                for sticker_path in subfolder.glob("*.*"):
                    for _ in range(copies):
                        all_stickers.append(sticker_path)
    if all_stickers:
        generated_files = processor.process_multi_sticker_order(all_stickers, output_dir)
        print(f"Generated sticker sheets: {generated_files}")


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_script(source_folder_id: str, recipient_email: str, cc_email: str,
               log=print, on_zip_ready=None) -> str:
    """
    Run the full fulfilment pipeline.

    Parameters
    ----------
    source_folder_id : Google Drive folder ID for the artwork source folder.
    recipient_email  : Primary email to notify when done.
    cc_email         : Optional CC email.
    log              : Callable used for progress messages (default: print).
                       api.py passes _append_log so messages appear in the UI.
    on_zip_ready     : Optional callback invoked with the ZIP path as soon as
                       the archive exists, before the upload and email steps.
                       api.py uses it to arm the download button early, so the
                       artwork stays reachable even if a later step fails or
                       the run dies outright.

    Returns
    -------
    str : Local path to the generated ZIP file (inside the temp directory).

    Raises
    ------
    Exception : re-raised from the gathering phase, but only after the partial
                harvest has been zipped and handed to on_zip_ready.
    """
    # Create a self-cleaning temp workspace for this run
    tmp_dir     = tempfile.mkdtemp(prefix="operation_automation_")
    destination = os.path.join(tmp_dir, "output")
    os.makedirs(destination)

    # Anything raised while gathering artwork is held here rather than thrown
    # straight out: whatever made it to disk still gets zipped and handed over
    # first, so a failure halfway through a 200-order run doesn't cost the
    # operator the 150 files that did come down.
    gather_error = None
    warnings = []

    try:
        # ── Step 1: fetch unfulfilled orders ─────────────────────────────────
        log("Fetching unfulfilled orders from Shopify…")
        orders = script.get_data("Order")
        unfulfilled_skus = []
        not_found = []
        custom_count = 0
        skipped = 0

        for order in orders:
            if order.fulfillment_status is None:
                for line_item in order.line_items:
                    if line_item.sku:
                        unfulfilled_skus.append(
                            (order.id, line_item.sku, line_item.quantity, line_item.properties)
                        )

        log(f"Found {len(unfulfilled_skus)} unfulfilled SKU(s).")

        # ── Step 2: build Drive index (one API walk, no downloads yet) ───────
        log("Indexing artwork source folder on Google Drive…")
        service = _get_drive_service()
        by_name, by_lower = _build_drive_index(service, source_folder_id)
        log(f"Indexed {len(by_name)} lookup key(s) in the Drive source folder.")

        # ── Step 3: sort SKUs and download only what's needed ────────────────
        log("Sorting SKUs and downloading required artwork files…")

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
            target_folder = os.path.join(destination, folder_name)

            os.makedirs(target_folder, exist_ok=True)
            subfolder_path = os.path.join(target_folder, f"{quantity} copy")
            os.makedirs(subfolder_path, exist_ok=True)

            entry, filename = resolve_with_size_fallback(
                filename, by_name, by_lower, is_sticker)

            if entry:
                file_id, drive_name = entry
                ext = os.path.splitext(drive_name)[1]
                dest_file = unique_dest_path(
                    os.path.join(subfolder_path, f"{filename}{ext}")
                )
                log(f"  Downloading {filename}{ext}…")
                _download_file(service, file_id, dest_file)
                if os.path.basename(dest_file) != f"{filename}{ext}":
                    log(f"    saved as {os.path.basename(dest_file)} "
                        f"(another order already needed this SKU at qty {quantity})")

            elif properties and len(properties) > 0:
                # Customer-supplied custom artwork, hosted on a URL by Shopify.
                url = getattr(properties[0], "value", None)
                if not url or not str(url).lower().startswith(("http://", "https://")):
                    not_found.append((order_id, sku, quantity,
                                      "custom artwork property is not a URL"))
                    log(f"  ⚠ Custom artwork property is not a URL: {sku}")
                    continue

                custom_count += 1
                dest_file = os.path.join(subfolder_path, f"{filename}_{custom_count}.jpg")
                log(f"  Downloading custom artwork for {sku}…")
                try:
                    urllib.request.urlretrieve(url, dest_file)
                except Exception as exc:
                    # One dead customer-upload URL shouldn't abort the whole run.
                    if os.path.exists(dest_file):
                        os.remove(dest_file)
                    not_found.append((order_id, sku, quantity,
                                      f"custom artwork download failed: {exc}"))
                    log(f"  ⚠ Custom artwork download failed for {sku}: {exc}")

            else:
                not_found.append((order_id, sku, quantity,
                                  "no matching file in Drive source folder"))
                log(f"  ⚠ Not found in Drive: {sku}")

        if skipped:
            log(f"{skipped} SKU(s) had no A3/A4/A5/PP/STIC marker — listed in not_found.csv.")

        # ── Step 4: write not_found report ───────────────────────────────────
        not_found_path = os.path.join(destination, "not_found.csv")
        with open(not_found_path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Order ID", "SKU", "Quantity", "Reason"])
            writer.writerows(not_found)

        if not_found:
            log(f"⚠ {len(not_found)} SKU(s) unresolved — see not_found.csv in the ZIP.")

        # ── Step 5: process stickers ─────────────────────────────────────────
        sticker_dir = Path(destination) / "stickers"
        if sticker_dir.exists():
            log("Processing sticker sheets…")
            process_sticker_folders(sticker_dir, sticker_dir)

        # ── Step 6: clean up empty folders ───────────────────────────────────
        remove_empty_folders(destination)

    except Exception as exc:            # noqa: BLE001 — deliberately broad
        gather_error = exc
        log(f"❌ Error while gathering artwork: {exc}")
        log("Packaging whatever was collected before the failure…")

    # ── Step 7: zip (always attempted, even after a failed harvest) ───────────
    zip_filepath = None
    has_content = os.path.isdir(destination) and bool(os.listdir(destination))

    if has_content:
        try:
            log("Creating ZIP archive…")
            date_str     = datetime.now().strftime("%d%m%Y")
            suffix       = "_PARTIAL" if gather_error else ""
            zip_filename = f"{date_str}onlineorder{suffix}.zip"
            zip_filepath = os.path.join(tmp_dir, zip_filename)
            zip_folder(destination, zip_filepath)

            # Hand the path over immediately. From here on the download button
            # works no matter what else goes wrong.
            if on_zip_ready:
                on_zip_ready(zip_filepath)
            log(f"ZIP ready: {zip_filename}")
        except Exception as exc:        # noqa: BLE001
            zip_filepath = None
            log(f"❌ Could not create the ZIP archive: {exc}")
    else:
        log("Nothing was collected, so there is no ZIP to create.")

    # A failed harvest stops here — but with the partial ZIP already published.
    if gather_error:
        if zip_filepath:
            log("⚠ Run failed, but the partial ZIP is available to download.")
        else:
            # Nothing salvageable — don't leave an empty temp dir behind.
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise gather_error

    # ── Step 8: upload ZIP to Drive (best effort) ─────────────────────────────
    shared_link = None
    if zip_filepath:
        try:
            log("Uploading ZIP to Google Drive…")
            output_folder_id = "1c28UVUhoxkpAjZyd9vsXoR5pXjaDlZZn"
            shared_link = upload_to_drive(zip_filepath, output_folder_id)
        except Exception as exc:        # noqa: BLE001
            warnings.append(f"Drive upload failed: {exc}")
            log(f"⚠ Drive upload failed: {exc}")
            log("  The ZIP is still available from the download button below.")
    else:
        warnings.append("Upload skipped — there was no ZIP to upload.")
        log("⚠ Upload skipped — there was no ZIP to upload.")

    # ── Step 9: send email (best effort, needs the link) ──────────────────────
    if shared_link:
        try:
            log("Sending email notification…")
            send_email(shared_link, recipient_email, cc_email, log=log)
        except Exception as exc:        # noqa: BLE001
            warnings.append(f"Email failed: {exc}")
            log(f"⚠ Email failed: {exc}")
            log("  The ZIP is still available from the download button below.")
    else:
        warnings.append("Email skipped — there was no Drive link to send.")
        log("⚠ Email skipped — the upload produced no link to send.")

    if warnings:
        log("✅  Finished with warnings — the ZIP is ready to download.")
    else:
        log("✅  All done!")

    return zip_filepath
