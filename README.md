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
# Shopify / email
TOKEN='shopify-app-api'
MERCHANT='merchant-name'
SENDER_EMAIL='sender-email-address'
RESEND_API_KEY='resend-api-key'

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
