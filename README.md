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
# ---- Shopify (required, both builds) --------------------------------------
TOKEN='shpat_...'
MERCHANT='merchant-name'

# ---- Google Drive (online build only) -------------------------------------
# Option A, preferred: service account
GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account", ...}'
# Option B: OAuth user credentials, instead of Option A
GOOGLE_OAUTH_CLIENT_ID='...'
GOOGLE_OAUTH_CLIENT_SECRET='...'
GOOGLE_OAUTH_REFRESH_TOKEN='...'

# ---- Frontend origins (online build only) ---------------------------------
# Comma-separated. Without it, only localhost and the original Vercel URL.
ALLOWED_ORIGINS='https://your-site.vercel.app,http://localhost:5173'

# ---- Email (online build only, blocked on Railway) ------------------------
SENDER_EMAIL='ops@yourdomain.com'
SENDER_PASSWORD='abcd efgh ijkl mnop'   # Google APP password
# SMTP_HOST='smtp.gmail.com'
# SMTP_PORT='587'                       # 465 switches to implicit SSL

# ---- Custom posters (both builds, all optional) ---------------------------
UPSCALING_ENABLED=''        # '1' to un-park the AI step; off by default
GEMINI_API_KEY=''           # only used when upscaling is enabled
# GEMINI_MODEL='gemini-3-pro-image-preview'
# GEMINI_IMAGE_SIZE='4K'    # '2K' halves the cost, at the expense of detail
# ESRGAN_THREADS='4'        # local upscaler threads
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
because the connection never leaves the container.

The SMTP code in `main.py` still works anywhere port 587 is open (a laptop, a
VPS, most office networks) — it is simply unreachable from Railway. It needs
`SENDER_EMAIL` and `SENDER_PASSWORD`, where the password must be a **Google app
password** from <https://myaccount.google.com/apppasswords> with 2-Step
Verification on; Google stopped accepting mailbox passwords for SMTP in May
2022. `SMTP_HOST` / `SMTP_PORT` can point it elsewhere; 465 uses implicit SSL,
anything else STARTTLS.

An email failure never fails a run — see the failsafe table below.

**The offline build does not send email at all.** Its output is a folder on
disk; there is nothing to deliver.

## Online build

The backend runs on Railway and auto-deploys from `main`. The frontend is a
Vite app in `frontend/`, deployed separately on Vercel with **Root Directory**
set to `frontend` and `VITE_API_URL` pointing at the Railway service.

### Two Drive source folders

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

### Allowed browser origins

The API only answers browsers whose origin it recognises. `ALLOWED_ORIGINS` is
a comma-separated list read from the environment:

```
ALLOWED_ORIGINS='https://your-site.vercel.app,http://localhost:5173'
```

It is an environment variable rather than a constant because the frontend URL
is a deployment detail — it should not take a code release to move the site.
Unset, it falls back to localhost plus the original Vercel URL.

Vercel gives every deployment its own hostname
(`project-abc123-team.vercel.app`), so an allowed `*.vercel.app` entry also
admits that project's preview builds. Pinning only the production URL blocks
every preview for no visible reason.

`GET /api/health` reports the list currently in force — the quickest way to
tell whether a new frontend URL has actually been configured:

```json
{"ok": true, "allowed_origins": ["https://your-site.vercel.app", "..."]}
```

If the site loads but every request fails, this is almost always why.

## Custom poster upscaling

> **Status: parked.** CPU inference was costing minutes per poster, which made
> a batch with many custom orders unworkable. Custom posters are still stood
> upright and framed to the print canvas — only the AI step is skipped.
>
> The routing, Real-ESRGAN and Gemini code below is intact and simply
> unreached. To bring it back:
>
> ```
> UPSCALING_ENABLED=1
> ```
>
> set in the environment, or flip the default in `custom_upscale.py`. The rest
> of this section describes what happens when it is switched on.

Customer-supplied custom posters — the ones Shopify hosts on a URL in the line
item's properties — are brought to a fixed print canvas. All of it lives in
**`backend/custom_upscale.py`**; both pipelines import the same module.

### While parked

Every custom poster is stood upright, framed to 3638 × 5280, and filed in a
single folder:

```
Custom Posters/A3/1 copy/CUSTOM_1.jpg
Custom Posters/A4/2 copy/CUSTOM_2.jpg
```

One bin, because with no AI step there is nothing to sort them by and
`Upscaled framed` / `Non-Upscaled` would both be misleading. Nothing is written
to the error sheet either — not upscaling is no longer a failure.

### The canvas

**308 × 447 mm at 300 DPI = 3638 × 5280 px.** Every custom poster is delivered
at exactly this size. The artwork is fitted inside, never cropped, and never
exceeds it.

| Source | Handling |
|---|---|
| Portrait | fitted to the canvas |
| Landscape | rotated 90° clockwise, then fitted |
| Square | fitted to width, white space above and below |

EXIF orientation is applied before any of this — a phone records a sideways
photo plus a "rotate me" flag, so raw dimensions can read landscape when the
image is really portrait.

### Routing

`canvas_fraction()` is how much of the canvas the artwork fills once fitted,
measured on whichever edge reaches the frame first:

```
fraction = max(width / 3638, height / 5280)
```

| Fraction | Print size | Route |
|---|---|---|
| ≥ 85% | any | **fit only**, no AI |
| 15% – 85% | any | **Real-ESRGAN x2**, local, free |
| < 15% | **A3** | **Gemini**, falling back to Real-ESRGAN |
| < 15% | A4 / A5 / PP / none | **Real-ESRGAN x2** |

Two things keep the bill down. The 85% ceiling: inference time scales with the
**source** size, so the 85–100% slice was the most expensive and least useful
work in the pipeline — minutes of compute for an enlargement approaching 1.0×.
And the A3 gate: below 15% the local upscaler has a lot of ground to make up,
which is worth paying Gemini for on the largest sheet in the range, where a
weak source shows most. On A4 and smaller it is not, so those stay local and
free.

In practice Gemini now fires on one case only — a custom A3 order whose
artwork is under 15% of the canvas.

### Real-ESRGAN

Runs on CPU through ONNX Runtime — no GPU, so it works in the container.
Upscaling is x2 because nothing in the 65–100% band needs more, and x4 would
cost four times the inference for detail discarded when fitting to the canvas.

The 63 MB model is fetched once per container into a temp path (override with
`ESRGAN_MODEL_URL` / `ESRGAN_MODEL_PATH`). Inference is tiled at 256 px with a
16 px overlap that is trimmed afterwards, which keeps peak memory proportional
to one tile rather than the whole image — that is what makes a 19 MP poster
survivable inside a container.

**It is slow at the top of the band.** Measured at roughly 76k px/s on four
threads of an M-series Mac: a poster near 85% (13.9 MP) takes about 3 minutes,
one at 50% (4.8 MP) about a minute, and small artwork down near 15% runs in
seconds even across three passes, because each pass starts from very little.
Railway's shared vCPUs will be slower. `ESRGAN_MIN_FRACTION` and
`ESRGAN_MAX_FRACTION` are the levers if runs get too long.

`ESRGAN_THREADS` sets thread count, default 4.

### Gemini

Runs at **4K**, not 2K. The canvas is 19.2 MP; Gemini's 2K output is ~4.2 MP,
which would need a 2.2× interpolation afterwards and throw away most of what
was paid for. 4K is ~16.8 MP, a 1.09× finish. Gemini now only sees the worst
inputs, where output quality matters most. Set `GEMINI_IMAGE_SIZE=2K` to halve
the per-image cost at the expense of detail.

Two attempts. If both come back without an image, the poster **falls back to
Real-ESRGAN** rather than shipping un-enlarged — a quota block or a refusal on
Google's side shouldn't decide print quality. Artwork routed locally from below the band can
need several doublings, so up to `ESRGAN_MAX_PASSES` (3) are allowed, covering
anything from about 12% up, with the fit-to-canvas step finishing the rest. Only if that also fails does the framed original ship with
`Upscaling failed` in the error sheet.

### Output folders when enabled

Custom posters carry the print size from their SKU, matching normal automation:

```
Upscaled framed Custom Posters/A4/2 copy/CUSTOM_1.jpg
Non-Upscaled Custom posters/PP/3 copy/CUSTOM_2.jpg
```

`Non-Upscaled` means the AI step failed — the poster is still delivered, framed
at full canvas size, and a row reading `Upscaling failed` goes to
`not_found.csv`. A poster that simply did not need AI is *not* an error and
lands in `Upscaled framed Custom Posters`.

The poster is never lost: a failed Gemini call, a failed local inference, a
missing API key or an unreadable download all still produce a framed file.

### Known problem, unresolved

Real-ESRGAN on Railway's shared CPU takes **over two hours for 100 posters**,
and Gemini at $0.24/image costs **$24 per 100-poster run**. Neither is
workable, which is why the feature is parked.

The likely fix is a dedicated upscaler on serverless GPU (Replicate runs the
same Real-ESRGAN at roughly $0.0025/image and ~12 s, parallelisable to minutes
for a full batch) rather than either CPU inference or a general-purpose
generative model. Not implemented.

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

`main.py`, `api.py` and the React app are untouched by any of this.

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

Only `TOKEN` and `MERCHANT` are required in `.env`. `GEMINI_API_KEY` is
optional; without it a poster that would have gone to Gemini takes the local
route instead, and nothing fails.

### Picking folders

Both path fields have a **Browse…** button that opens the operating system's
own folder chooser — `FolderBrowserDialog` on Windows, `choose folder` on
macOS, `zenity` on Linux. This shells out to the platform dialog rather than
bundling a Tk one, because the server answers from a worker thread and Tk is
unreliable off the main thread on Windows.

Typing or pasting a path still works, and both fields are validated as you
type, so a typo surfaces before a run rather than 30 seconds into one.

**The output folder defaults to your Downloads folder** until you choose
somewhere else. Both paths are remembered in
`~/.operation_automation_offline_config.json`.

### Windows package

For a machine without a development setup, the offline build ships as a
self-contained folder:

```
OperationAutomation-Offline/
├── install.bat        creates the venv, installs dependencies
├── run.bat            starts the server and opens the browser
├── build-exe.bat      optional: PyInstaller single .exe
├── README.txt         plain-text instructions
├── env.example        copied to .env by the installer
├── backend/
└── frontend/
```

Python 3.12+ must be installed first with **"Add python.exe to PATH"** ticked —
`install.bat` detects when it isn't and explains the fix.

A Windows `.exe` has to be built **on Windows**; PyInstaller does not
cross-compile, which is why `build-exe.bat` runs there rather than being
shipped pre-built.

### Custom posters

The offline build runs the **same** custom-poster pipeline as the online one —
both import `custom_upscale.py`, so there is one implementation and no chance
of the two drifting. Canvas, orientation and folder layout are identical; see
*Custom poster upscaling* above, including its parked status.

`UPSCALING_ENABLED` is read from the environment in both places, so the two
builds can be switched independently.

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

## Shared behaviour

These apply to both builds — the logic lives in `sku_rules.py` and
`sticker_processor.py`, which both pipelines import.

### SKU matching

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

### Duplicate designs

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
