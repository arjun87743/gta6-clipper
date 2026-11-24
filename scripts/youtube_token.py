"""One-time: get a YouTube refresh token for the GitHub secrets. Run on the Mac:

  pip install google-auth-oauthlib
  python scripts/youtube_token.py path/to/client_secret.json

A browser opens: sign in as aiweblabs89@gmail.com and pick the @AsianJeffVaultClips channel.
It prints YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN to paste into GitHub secrets.
Nothing is saved to disk. Never commit client_secret.json (the repo is public).
"""
import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

if len(sys.argv) != 2:
    sys.exit(__doc__)
flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
info = json.load(open(sys.argv[1]))
app = info.get("installed") or info.get("web")
print("\nAdd these as GitHub repository secrets (Settings > Secrets and variables > Actions):\n")
print(f"YT_CLIENT_ID      {app['client_id']}")
print(f"YT_CLIENT_SECRET  {app['client_secret']}")
print(f"YT_REFRESH_TOKEN  {creds.refresh_token}")
