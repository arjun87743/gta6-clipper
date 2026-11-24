"""Upload a finished clip to YouTube Shorts or Instagram Reels through the official APIs.

YouTube  (YouTube Data API v3): secrets YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN.
         Get the refresh token once with scripts/youtube_token.py. Videos uploaded by an app that
         has not passed Google's API audit are locked to private; post() reports that.
Instagram: secrets IG_USER_ID and IG_TOKEN. Default is Meta's "Unified login" setup (the only
         one new apps get, 2026): a system-user token that never expires, used on graph.facebook.com.
         Older apps with "Instagram login" can set IG_GRAPH_HOST=https://graph.instagram.com; that
         token lasts 60 days and is refreshed automatically, the new one kept in the vault.
X has no free posting API, so it is not here.
"""
import os
import time
from datetime import timedelta
from pathlib import Path

import requests

from pipeline import state, vault

IG_HOST = os.environ.get("IG_GRAPH_HOST") or "https://graph.facebook.com"
IG_API = f"{IG_HOST}/v21.0"
IG_UPLOAD = "https://rupload.facebook.com/ig-api-upload/v21.0"


class PostError(Exception):
    pass


# ---------- YouTube ----------

def youtube_post(video, title, description):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"):
        if not os.environ.get(k):
            raise PostError(f"{k} secret is not set")
    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
                        client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"],
                        token_uri="https://oauth2.googleapis.com/token",
                        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {"snippet": {"title": title[:100], "description": description[:4900], "categoryId": "20"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": False}}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    privacy = resp.get("status", {}).get("privacyStatus")
    note = None
    if privacy != "public":
        note = (f"YouTube made it {privacy}: the Google app has not passed the API audit yet. "
                "Open YouTube Studio and set it to Public (and request the audit, see SETUP.md).")
    return {"url": f"https://www.youtube.com/shorts/{vid}", "id": vid, "privacy": privacy, "note": note}


# ---------- Instagram ----------

def _ig_token():
    v = vault.load()
    tok = v.get("ig_token") or os.environ.get("IG_TOKEN")
    if not tok:
        raise PostError("IG_TOKEN secret is not set")
    if "graph.instagram.com" not in IG_HOST:
        return os.environ.get("IG_TOKEN") or tok  # system-user token: never expires, nothing to refresh
    last = v.get("ig_refreshed_at")
    # Long-lived tokens last 60 days; refresh after 7 so a few missed runs never let it expire.
    if not last or state.now() - state.parse(last) > timedelta(days=7):
        r = requests.get(f"{IG_HOST}/refresh_access_token",
                         params={"grant_type": "ig_refresh_token", "access_token": tok}, timeout=30)
        if r.ok and r.json().get("access_token"):
            tok = r.json()["access_token"]
            v.update(ig_token=tok, ig_refreshed_at=state.iso())
            vault.save(v)
        elif not last:
            v.update(ig_refreshed_at=state.iso())  # brand-new tokens cannot be refreshed for 24 h
            vault.save(v)
    return tok


def _ig(method, path, tok, **kw):
    r = requests.request(method, f"{IG_API}/{path}", params={"access_token": tok, **kw.pop("params", {})},
                         timeout=60, **kw)
    if not r.ok:
        raise PostError(f"Instagram {path}: {r.status_code} {r.text[:400]}")
    return r.json()


def instagram_post(video, caption):
    uid = os.environ.get("IG_USER_ID")
    if not uid:
        raise PostError("IG_USER_ID secret is not set")
    tok = _ig_token()
    c = _ig("POST", f"{uid}/media", tok, data={"media_type": "REELS", "upload_type": "resumable",
                                               "caption": caption[:2200], "share_to_feed": "true"})
    # Resumable upload goes to rupload.facebook.com for both API setups.
    cid = c["id"]
    size = Path(video).stat().st_size
    with open(video, "rb") as f:
        up = requests.post(f"{IG_UPLOAD}/{cid}", data=f, timeout=600,
                           headers={"Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)})
    if not up.ok:
        raise PostError(f"Instagram upload: {up.status_code} {up.text[:400]}")
    for _ in range(60):  # processing usually takes 30 s to 2 min
        st = _ig("GET", cid, tok, params={"fields": "status_code,status"})
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise PostError(f"Instagram processing failed: {st}")
        time.sleep(10)
    else:
        raise PostError("Instagram processing did not finish in 10 minutes")
    mid = _ig("POST", f"{uid}/media_publish", tok, data={"creation_id": cid})["id"]
    link = _ig("GET", mid, tok, params={"fields": "permalink"}).get("permalink")
    return {"url": link or f"https://www.instagram.com/", "id": mid, "privacy": "public", "note": None}


def post(platform, video, title, text):
    if platform == "youtube_shorts":
        return youtube_post(video, title, text)
    if platform == "instagram":
        return instagram_post(video, text)
    raise PostError(f"no auto-posting for {platform}")
