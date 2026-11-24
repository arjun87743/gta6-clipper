# gta6-clipper

Finds the best moments in a streamer's GTA 6 VODs, edits them into vertical clips, and posts them
to YouTube Shorts and Instagram Reels on a schedule, for paid clipping campaigns. Runs on GitHub
Actions with no human in the loop.

```
every 2 h   process.yml   new VOD? -> download (160p + chat) -> detect peaks (chat + audio, cutscenes out)
                          -> transcribe (faster-whisper) -> rank (Claude Code headless, rank_rules.md)
                          -> render edited 1080x1920 clips -> queue (state branch) + artifact
hourly      post.yml      inside posting hours: best queued clip per platform -> YouTube / Instagram API
                          -> GitHub issue "Submit now" with the links
```

Setup: see [SETUP.md](SETUP.md). Campaign settings: `campaigns/<campaign>.yaml`.
Ranking rules (what gets kept or rejected): `pipeline/rank_rules.md`.

The repo is public, so it holds code only. Videos travel as workflow artifacts, run state lives
on the `state` branch, and every token is either a GitHub secret or encrypted (`pipeline/vault.py`).

Run locally (Mac uses mlx-whisper automatically, Linux uses faster-whisper):
```
python pipeline/cloud.py process --campaign asianjeff --vod-id <id>
python pipeline/cloud.py status
```
