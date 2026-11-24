# One-time setup

After this, the clipper runs on GitHub with nothing to do day to day. You only hear from it when
something needs you (a new post to submit on Content Rewards, a YouTube video stuck private, or a failure).

All secrets go in **GitHub repo > Settings > Secrets and variables > Actions > New repository secret**.
The repo is public: secrets are encrypted by GitHub and never appear in the code, logs or state branch.

## 1. Claude (ranking the moments): `CLAUDE_CODE_OAUTH_TOKEN`
On the Mac, in Terminal:
```
claude setup-token
```
Sign in with your Claude account. Paste the token it prints as the secret `CLAUDE_CODE_OAUTH_TOKEN`.
This uses your Claude subscription, so ranking costs nothing extra.

## 2. State key: `STATE_KEY`
Encrypts the Instagram token that the runs refresh. On the Mac:
```
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
(If that fails: `pip3 install cryptography` first.) Paste the output as `STATE_KEY`.

## 3. YouTube: `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`
Signed in as **aiweblabs89@gmail.com**:
1. https://console.cloud.google.com : create a project "clipper".
2. APIs & Services > Library: enable **YouTube Data API v3**.
3. OAuth consent screen: External, app name "clipper", your email. Scopes: add `youtube.upload`.
   Then **Publish app** (status "In production"). Skipping this makes the token die after 7 days.
   Google will show "unverified app" when you sign in; that is fine for your own channel.
4. Credentials > Create credentials > OAuth client ID > **Desktop app**. Download the JSON.
5. On the Mac:
   ```
   pip3 install google-auth-oauthlib
   python3 scripts/youtube_token.py ~/Downloads/client_secret_XXXX.json
   ```
   Pick the **@AsianJeffVaultClips** channel. Paste the three printed values as secrets.
   Delete the downloaded JSON afterwards.

**Known limit:** YouTube locks videos uploaded by an app that has not passed its API audit to
*private*. Until the audit is approved, each YouTube post arrives as a notification asking you to
flip it to Public in YouTube Studio (one tap). Request the audit (free, takes a few weeks):
https://support.google.com/youtube/contact/yt_api_form

## 4. Instagram: `IG_USER_ID`, `IG_TOKEN`
@asianjeffvaultclips must stay a Creator or Business account (it is).
New Meta apps use "API setup with Unified login": a system-user token that never expires.
1. https://developers.facebook.com/apps : Create app "clipper", use case "Manage messaging & content
   on Instagram", then connect a business portfolio (ours: "AsianJeff Vault").
2. Use cases > Customize > API setup with Unified login: create a configuration (system-user token,
   Instagram account asset, `instagram_basic` + `instagram_content_publish`) and register a redirect URL.
3. Add the Instagram account and generate the token. Copy the Instagram account ID as `IG_USER_ID`
   and the token as `IG_TOKEN`.

## 5. Post the 8 clips you already approved
GitHub > Actions > **Process new streams** > Run workflow:
- vod_id: `2933231152`
- mode: `seed`

This renders your 8 approved versions (no re-ranking) and queues them. From then on it all runs
by itself: new GTA 6 streams are checked every 2 hours, and clips post at 20:00 and 07:00 IST
(US evening and night), up to 2 a day per platform.

## Notifications
Install the **GitHub** app on your phone and sign in as arjun87743. Each "Submit now" issue is a
push notification with the post links. Submit them on Content Rewards within 30 minutes, then
close the issue. (Content Rewards has no API, so this one step stays with you.)

## Handy controls
- Post one clip right now: Actions > Post clips > Run workflow > tick "now".
- See what's happened: the `state` branch (`posts.json`, `queue.json`, `events.jsonl`).
- Change posting hours or daily count: `autopost:` in `campaigns/asianjeff.yaml`.
- Stop everything: Actions > each workflow > "..." > Disable workflow.
