[![Python application test with Github Actions](https://github.com/perceptronq/operation-automation/actions/workflows/actions.yml/badge.svg)](https://github.com/perceptronq/operation-automation/actions/workflows/actions.yml)

## Google Drive authentication

The app authenticates to the Google Drive API with **either** a service account
(recommended) **or** OAuth user credentials. It tries the service account first,
then falls back to OAuth. Configure one of them.

### Option A — Service account (recommended, never expires)

 a. Go to https://console.cloud.google.com/ <br>
 b. Create a new project <br>
 c. Enable the Google Drive API for your project <br>
 d. Create a service account and give it a name and description. <br>
 e. Open the service account, go to the `Keys` tab, click Add key → JSON, and download it. <br>
 f. Provide the key to the app in **one** of two ways: <br>
 &nbsp;&nbsp;• set `GOOGLE_SERVICE_ACCOUNT_JSON` to the full JSON contents (best for Railway/Vercel secrets), **or** <br>
 &nbsp;&nbsp;• save the file as `credentials.json` in the repo root. <br>
 g. Share your Drive source folder (and the upload/output folder) with the service-account email. <br>

> ⚠️ A service account has no Drive storage of its own. Reading shared folders
> works everywhere, but **uploading** the result ZIP requires the output folder to
> live in a **Shared Drive** (or use Option B for uploads). The Drive calls already
> pass `supportsAllDrives=True`.

### Option B — OAuth user credentials

If you can't use a service account, generate an OAuth refresh token:

 a. In Google Cloud Console create an OAuth 2.0 Client ID of type "Desktop app". <br>
 b. Download its client-secrets JSON and save it as `oauth_client.json` in the repo root. <br>
 c. **Publish the OAuth consent screen to "Production"** — otherwise Google expires the refresh token after 7 days, which produces the `invalid_grant: Bad Request` error. <br>
 d. Run `python get_refresh_token.py` and authorize in the browser. <br>
 e. Paste the printed `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and `GOOGLE_OAUTH_REFRESH_TOKEN` into your `.env` / secrets. <br>

> Seeing `invalid_grant: Bad Request`? The refresh token is expired or revoked —
> re-run `python get_refresh_token.py` and update `GOOGLE_OAUTH_REFRESH_TOKEN`, or
> switch to a service account.

### .env file

Create a `.env` file in the repo root. Include the Shopify/email settings plus the
Drive credentials for whichever option you chose above:

```
# Shopify
TOKEN='shopify-app-api'
MERCHANT='merchant-name'

# Email — Gmail SMTP
SENDER_EMAIL='ops@yourdomain.com'
SENDER_PASSWORD='abcd efgh ijkl mnop'   # Google APP password, not the mailbox password
# SMTP_HOST='smtp.gmail.com'            # optional override
# SMTP_PORT='587'                       # optional; 465 switches to implicit SSL

# Google Drive — Option A (service account)
GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account", ...}'

# Google Drive — Option B (OAuth), instead of Option A
GOOGLE_OAUTH_CLIENT_ID='...'
GOOGLE_OAUTH_CLIENT_SECRET='...'
GOOGLE_OAUTH_REFRESH_TOKEN='...'
```


Instructions to run the app:

```
git clone https://github.com/perceptronq/operation-automation.git

cd operation-automation

python -m venv venv

(for linux)
source venv/bin/activate

(for windows)
venv\Scripts\activate

pip install -r requirements.txt

python main.py
 
```
Generate a spec file
`pyinstaller main.py --name Automate --onefile --windowed --specpath .`

Add this to Automate.spec
```
datas=[
        ('credentials.json', '.'),
        ('.env', '.'),
    ],
```

Build executable
`pyinstaller main.py --name Automate --onefile --windowed --add-data "credentials.json:." --add-data ".env:."`

---

## Email

### Why the server can't send mail

Railway blocks outbound SMTP — ports 25, 465 and 587 are null-routed to stop
spam abuse. A send from the deployed backend fails with
`[Errno 101] Network is unreachable` no matter how the credentials are set up,
because the connection never leaves the container. This is almost certainly why
the project used an HTTP email API originally.

The SMTP code in `main.py` is still there and works fine anywhere that permits
port 587 (a laptop, a VPS, most office networks). It is simply unreachable from
Railway. It needs `SENDER_EMAIL` and `SENDER_PASSWORD`, where the password must
be a **Google app password** from <https://myaccount.google.com/apppasswords>
with 2-Step Verification on — Google stopped accepting mailbox passwords for
SMTP in May 2022. `SMTP_HOST` / `SMTP_PORT` can point it elsewhere; 465 uses
implicit SSL, anything else STARTTLS.

An email failure never fails a run — see the failsafe table below.

### Sending from the offline build instead

The offline build sidesteps the block entirely by keeping a human in the loop:
it writes the folder locally, zips it, and opens a pre-written Gmail draft in
the browser.

Fill in **Recipient** (and optionally **CC**) before running. That, and only
that, turns on two extra things:

 a. a `.zip` of the run folder, written next to it — a mail draft can only
    carry a file, not a directory <br>
 b. a **Compose Email in Gmail** button once the run finishes <br>

Leave Recipient empty and the run behaves exactly as before: folder only, no
ZIP, no email step.

Pressing the button does three things at once:

 a. opens Gmail compose with recipient, CC, subject and body already written <br>
 b. reveals the `.zip` in Finder/Explorer with the file already selected <br>
 c. copies the `.zip`'s full path to the clipboard as a backup <br>

**You still drag the file into the compose window yourself.** That step cannot
be automated: a web page has no way to be handed a local file except by the
person using it, which is a browser sandbox rule rather than a missing feature.
`mailto:` links can't carry attachments either — RFC 6068 forbids the `attach`
header and every modern client ignores it, because it was abused to deliver
malware. Driving a desktop client (Apple Mail via AppleScript, Outlook via COM)
*can* attach for real; that is a different option if the browser step ever
becomes annoying.

If the ZIP is over Gmail's 25 MB attachment limit, the button says so — Gmail
will offer to upload it to Drive and send a link instead.

## Failsafe: the ZIP survives a failed run

The pipeline used to delete its temp directory whenever anything went wrong, so
a failure in the last two steps — a Drive quota error, a rejected SMTP login —
threw away artwork that had already been fetched successfully.

Now the archive is built as early as it can be and handed to the API the moment
it exists, before the upload and email are attempted:

| What failed | Run outcome | Download button |
|---|---|---|
| Nothing | success | ✅ `…onlineorder.zip` |
| Email | success, with warning | ✅ `…onlineorder.zip` |
| Drive upload | success, with warning | ✅ `…onlineorder.zip` |
| Both | success, with warnings | ✅ `…onlineorder.zip` |
| Crash while gathering artwork | **error** | ✅ `…onlineorder_PARTIAL.zip` |
| Crash before anything was fetched | **error** | — nothing to offer |

Upload and email are best-effort: neither can fail the run any more. A crash
during the gathering phase still surfaces as an error, but only after whatever
was collected has been zipped — that archive is suffixed `_PARTIAL` so a
half-complete batch can't be mistaken for a finished one.

The download button in the UI is driven by `zip_ready`, which is independent of
the error state, so it appears on failed runs too. The archive is no longer
deleted after being served — a cancelled download or a second copy is fine. It
is cleared on the next run.

---

## Offline build

An offline variant of the same pipeline lives alongside the online one. It keeps
the Shopify step (orders have to come from somewhere) and replaces everything
Google Drive touched:

| Step | Online (`main.py`) | Offline (`offline_main.py`) |
|---|---|---|
| Artwork source | walks a Drive folder by ID | walks a **local folder**, recursively |
| Fetching artwork | downloads each match from Drive | **copies** each match off disk |
| Result | ZIP → upload to Drive → email link | **plain folder** at a path you choose |

Nothing is zipped, uploaded or emailed, so no Google credentials and no Resend
key are needed — only `TOKEN` and `MERCHANT` in `.env`.

### Files

```
backend/offline_main.py          the pipeline
backend/offline_api.py           Flask server + serves the UI
backend/requirements-offline.txt reduced dependency set
frontend/offline.html            single-file UI, no build step
```

The online `main.py`, `api.py` and the React app are untouched and still work.

### Running it

```
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r backend/requirements-offline.txt

cd backend
python offline_api.py
```

Then open <http://127.0.0.1:8000>. The server binds to loopback only — these
endpoints read and write the local filesystem, so it is deliberately not
reachable from the network. Set `PORT` to use a different port.

Fill in the two fields (both are validated as you type, so a typo shows up
before you start a run rather than 30 seconds into one):

- **Artwork Folder** — absolute path to the folder holding all artwork.
  Subfolders are searched, so the existing category/product structure works as-is.
- **Destination Folder** — where the run folder gets created.

Both are remembered in `~/.operation_automation_offline_config.json`.

### Output

Each run creates a dated folder inside the destination:

```
<destination>/21082026onlineorder/
├── A3/         └── 1 copy/ …
├── A4/         └── 3 copy/ …
├── A5/         └── 2 copy/ …
├── PP/         └── 1 copy/ …
├── stickers/
│   ├── 2 copy/ …
│   ├── order_10x10_sheet_1.png
│   └── order_10x8_sheet_1.png
└── not_found.csv
```

A second run on the same day becomes `21082026onlineorder_2` rather than
overwriting the first. The run is assembled in a hidden `.…partial` folder and
renamed into place only once it finishes, so a run that fails halfway never
leaves behind a folder that looks complete.

### Two Drive source folders (online)

Artwork is split across more than one Drive folder, so the online build takes
two. **Artwork Folder 1** is required; **Artwork Folder 2** is optional — leave
it empty and the run behaves exactly as it did with one folder.

Both are walked recursively, subfolders included, and merged into a single
lookup. **If the same SKU exists in both, Folder 1 wins.** The order is the
rule, so the same run always picks the same file — with an unordered merge the
winner would come down to whichever folder the Drive API happened to paginate
first, which is not something an operator can reason about or rely on.

Either field accepts a bare ID or a full `drive.google.com/drive/folders/…`
URL; the ID is extracted automatically.

The log reports each folder separately, so it's obvious when one of them is
returning nothing:

```
Indexing 2 artwork source folders on Google Drive…
  Folder 1: 4812 lookup key(s) from 1a2B3c…
  Folder 2: 1190 lookup key(s) from 9zY8x7…
Indexed 5794 lookup key(s) across 2 folders.
```

A folder that can't be read fails the run immediately, before anything is
downloaded, naming which of the two it was — the usual cause is a wrong ID or a
folder that was never shared with the service account.

### SKU matching (both pipelines)

SKU routing and artwork matching live in **`backend/sku_rules.py`** and are
imported by both `main.py` and `offline_main.py`, so the two can't drift apart.

Routing is unchanged in intent: `…A3` / `…A4` / `…A5` / `…PP` have the
suffix stripped and go to the matching folder, anything containing `STIC` goes
to `stickers/`.

Both tests now run against a **normalised** copy of the SKU — whitespace
stripped, upper-cased. Shopify SKUs routinely arrive as `kpopstic271` or
`"POSTER1182A4 "`, and the old case-sensitive tests matched neither, so those
line items were dropped without ever reaching `not_found.csv`.

Matching the stem to a file tries explicit extensions first (case-sensitively,
then case-insensitively), and only then a bare stem with no extension. Stickers
prefer transparent formats — `.png .webp .tif .tiff` before `.jpg .jpeg` —
because they are cut around an alpha channel. Posters keep the original
`.jpg`-first order. A sticker SKU that also carries a size suffix
(`KPOPSTIC271A4`) falls back to looking for the file without it.

`not_found.csv` has a **Reason** column, and now lists *every* unresolved SKU,
including ones with no printable marker at all:

| Reason | Meaning |
|---|---|
| `no matching file in artwork folder` | Routed fine, no file matched |
| `SKU has no A3/A4/A5/PP/STIC marker` | Not routable — usually gift wrap or shipping protection, but worth a glance |
| `custom artwork download failed: …` | Customer-supplied artwork URL failed |

### Duplicate designs (both pipelines)

When the same design is ordered by more than one customer at the same quantity,
both land in the same `N copy` folder. Both used to write to the same path, so the
second silently overwrote the first and the batch printed short. Both now write
the second as `KPOPSTIC271__2.png` and log it, so a design ordered by three
customers at qty 2 contributes six stickers rather than two. To restore the old
behaviour, drop the `unique_dest_path()` call in the relevant pipeline.

### Online-only: extension-less downloads

The Drive index recorded only the file id, and a match found via the bare stem
carried no extension — so the download was written as `ANIMESTIC010` with no
suffix. `process_sticker_folders()` collects stickers with `glob("*.*")`, which
does not match a name without a dot, so those files were fetched and then
silently left off every sheet. Any artwork whose extension was outside the old
`.jpg/.jpeg/.png` list hit this: `.tif`, `.webp`, even `.PNG`. The index now
carries `(file_id, filename)` so the real extension always survives.

### Flat artwork no longer kills a run

`StickerProcessor.add_bleeding()` pasted a sticker using itself as the
transparency mask, which raises `ValueError: bad transparency mask` on any
image without an alpha channel — a `.jpg` preview filed next to the real `.png`
was enough to abort the whole run. It now converts to RGBA first: a flat source
still prints, it just bleeds to a rectangular edge instead of following a
cut-out shape.
