import os
import shutil
import csv
import tempfile
import urllib.request

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

def _build_drive_index(service, folder_id: str) -> dict:
    """
    Recursively walk a Drive folder (including all subfolders) and return a
    flat dict mapping  filename_without_extension → file_id.

    This mirrors what os.walk() did over the local all_in_one folder, but
    works entirely against the Drive API so nothing is downloaded until we
    know exactly which files are needed.
    """
    index = {}
    _walk_drive_folder(service, folder_id, index)
    return index


def _walk_drive_folder(service, folder_id: str, index: dict):
    """Recursively populate index from a Drive folder tree."""
    page_token = None
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
                # Recurse into subfolders (matches your category/product structure)
                _walk_drive_folder(service, item["id"], index)
            else:
                # Store by name and by name-without-extension so SKU matching works
                name = item["name"]
                stem = os.path.splitext(name)[0]
                index[name] = item["id"]
                index[stem] = item["id"]  # allows lookup by SKU without extension

        page_token = response.get("nextPageToken")
        if not page_token:
            break


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


def send_email(shared_link, recipient_email, cc_email=None):
    import resend

    resend.api_key = os.getenv("RESEND_API_KEY")
    sender_email   = os.getenv("SENDER_EMAIL")

    params = {
        "from":    sender_email,
        "to":      [recipient_email],
        "subject": "Shared Google Drive Link",
        "html":    f"<p>Here is the shared link to the uploaded file: "
                   f"<a href='{shared_link}'>{shared_link}</a></p>",
    }
    if cc_email:
        params["cc"] = [cc_email]

    try:
        response = resend.Emails.send(params)
        print(f"Email sent successfully. ID: {response['id']}")
    except Exception as e:
        print(f"Error: Unable to send email - {e}")
        raise


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
               log=print) -> str:
    """
    Run the full fulfilment pipeline.

    Parameters
    ----------
    source_folder_id : Google Drive folder ID for the artwork source folder.
    recipient_email  : Primary email to notify when done.
    cc_email         : Optional CC email.
    log              : Callable used for progress messages (default: print).
                       api.py passes _append_log so messages appear in the UI.

    Returns
    -------
    str : Local path to the generated ZIP file (inside the temp directory).
    """
    # Create a self-cleaning temp workspace for this run
    tmp_dir     = tempfile.mkdtemp(prefix="operation_automation_")
    destination = os.path.join(tmp_dir, "output")
    os.makedirs(destination)

    try:
        # ── Step 1: fetch unfulfilled orders ─────────────────────────────────
        log("Fetching unfulfilled orders from Shopify…")
        orders = script.get_data("Order")
        unfulfilled_skus = []
        not_found = []
        custom_count = 0

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
        drive_index = _build_drive_index(service, source_folder_id)
        log(f"Indexed {len(drive_index)} files in Drive source folder.")

        # ── Step 3: sort SKUs and download only what's needed ────────────────
        log("Sorting SKUs and downloading required artwork files…")

        for order_id, sku, quantity, properties in unfulfilled_skus:
            if sku is None:
                continue

            if "STIC" in sku:
                filename = sku
                target_folder = os.path.join(destination, "stickers")
            elif sku.endswith("A3"):
                filename = sku[:-2]
                target_folder = os.path.join(destination, "A3")
            elif sku.endswith("A4"):
                filename = sku[:-2]
                target_folder = os.path.join(destination, "A4")
            elif sku.endswith("A5"):
                filename = sku[:-2]
                target_folder = os.path.join(destination, "A5")
            elif sku.endswith("PP"):
                filename = sku[:-2]
                target_folder = os.path.join(destination, "PP")
            else:
                continue

            os.makedirs(target_folder, exist_ok=True)
            subfolder_path = os.path.join(target_folder, f"{quantity} copy")
            os.makedirs(subfolder_path, exist_ok=True)

            # Look up in Drive index — try with common image extensions first,
            # then fall back to the bare SKU stem (index stores both)
            file_id = None
            matched_ext = ""
            for ext in [".jpg", ".jpeg", ".png"]:
                candidate = f"{filename}{ext}"
                if candidate in drive_index:
                    file_id = drive_index[candidate]
                    matched_ext = ext
                    break
            if file_id is None and filename in drive_index:
                # Index has the stem without extension — get the full name
                file_id = drive_index[filename]
                matched_ext = ""  # extension already in the Drive filename

            if file_id:
                dest_file = os.path.join(subfolder_path, f"{filename}{matched_ext}")
                log(f"  Downloading {filename}{matched_ext}…")
                _download_file(service, file_id, dest_file)
            elif len(properties) > 0:
                custom_count += 1
                dest_file = os.path.join(subfolder_path, f"{filename}_{custom_count}.jpg")
                log(f"  Downloading {filename}.jpg…")
                urllib.request.urlretrieve(properties[0].value, dest_file)
            else:
                not_found.append((order_id, sku, quantity))
                log(f"  ⚠ Not found in Drive: {sku}")

        # ── Step 4: write not_found report ───────────────────────────────────
        not_found_path = os.path.join(destination, "not_found.csv")
        with open(not_found_path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Order ID", "SKU", "Quantity"])
            for row in not_found:
                writer.writerow(row)

        if not_found:
            log(f"⚠ {len(not_found)} SKU(s) not found — see not_found.csv in the ZIP.")

        # ── Step 5: process stickers ─────────────────────────────────────────
        sticker_dir = Path(destination) / "stickers"
        if sticker_dir.exists():
            log("Processing sticker sheets…")
            process_sticker_folders(sticker_dir, sticker_dir)

        # ── Step 6: clean up empty folders ───────────────────────────────────
        remove_empty_folders(destination)

        # ── Step 7: zip ───────────────────────────────────────────────────────
        log("Creating ZIP archive…")
        date_str     = datetime.now().strftime("%d%m%Y")
        zip_filename = f"{date_str}onlineorder.zip"
        zip_filepath = os.path.join(tmp_dir, zip_filename)
        zip_folder(destination, zip_filepath)

        # ── Step 8: upload ZIP to Drive ───────────────────────────────────────
        log("Uploading ZIP to Google Drive…")
        output_folder_id = "1c28UVUhoxkpAjZyd9vsXoR5pXjaDlZZn"
        shared_link = upload_to_drive(zip_filepath, output_folder_id)

        # ── Step 9: send email ────────────────────────────────────────────────
        log("Sending email notification…")
        send_email(shared_link, recipient_email, cc_email)

        log("✅  All done!")
        return zip_filepath

    except Exception:
        # Clean up temp dir on failure so Railway disk doesn't fill up
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    # Note: on success we intentionally do NOT delete tmp_dir here —
    # api.py keeps zip_filepath in _run_state so /api/download can serve it.
    # api.py is responsible for cleanup after the download.