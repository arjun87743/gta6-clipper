# Clip ranking rules

Used by whoever ranks candidates: Claude in a Claude Code session, or the scheduled GitHub run
(`pipeline/autorank.py`, which runs Claude Code headless with these rules).
Input: `rank_packet.md` (transcript + chat per candidate). Output: `rankings.json` in the same folder,
in the format below. `python pipeline/rankings.py --campaign <c> --vod-id <id>` validates it.

Clips ranked by the scheduled run are posted with no human check, so when in doubt, reject.

## What makes a good clip (score 1 to 10)
- A clear moment a stranger understands in 2 seconds with no context: a fail, a chase, a funny line,
  a big reaction, a rant, a "chat is backseating him" struggle, a surprise.
- The streamer's own voice and reaction carry it. Clips that are mostly game dialogue score low.
- A payoff inside the clip (the escape works, the joke lands, the reaction happens).
- Chat reacting (W spam, LMAO, "no way") confirms something happened, but is not enough alone.
- 8 to 10: post-worthy as is. 6 to 7: usable with a strong hook. 5 or below: reject.

## Chat is never shown in the clip
The render crops the stream's chat overlay out of the frame, and the "### Chat" lines in the packet
come from the Twitch chat log, not from the video. Viewers of the clip never see chat. So:
- Use chat only as a signal that something happened and how big it was.
- Never reject or lower a score because of what chat says: troll lines, insults, spoilers, sexual or
  violent comments typed by viewers do not appear in the clip.
- Judge only what the viewer will see and hear: the streamer's own words and reaction, and the game.

## Reject (keep = false) when
- Story spoilers said by the streamer or shown in the game: names of characters dying or betraying,
  mission outcomes, ending talk.
- Mostly cutscene or in-game TV/radio, cheats, exploits or mod menus (Rockstar policy).
- Nothing happens: menus, idle driving, reading chat out loud with no reaction, greetings, sponsor or
  merch talk.
- Talk about leaks or unreleased content.
- The streamer himself makes sexual comments about characters or encourages violence against a person;
  in-game radio music.
- The streamer in a clearly negative light: real rage at someone, drama, slurs. Self-deprecating jokes
  about his own driving or aim are fine and often the best clips.
- Score 5 or below.

## Boundaries
- 15 to 60 s; aim for 20 to 40 s. Start 1 to 3 s before the setup line, end just after the payoff.
- Use the transcript timestamps (absolute VOD time). Stay inside the transcribed range.
- YouTube rule: no graphic violence in the first 7 s. If the moment is a shootout or kill, start on
  the streamer's setup or reaction, and set `violence_early_risk: true` (the clip then skips YouTube).

## Hooks
- 2 to 3 per clip, each under 50 characters, all different formats (question, statement, quote from
  the clip). No clickbait about things that do not happen in the clip. No spoilers. Never show the
  streamer in a negative light (no mocking his skill, no rage bait or drama framing); his own words make the safest hooks. Hooks are on-screen
  text; the "Ad" disclosure is added separately at render time.

## Edit fields (kept clips only, used by `render.py --edit`)
- `hook_text`: shown large for the first 1.5 s. Under 50 characters, normally the best of `hooks`.
- `punchline_ts`: absolute VOD second where the payoff lands (the word or the reaction, not the
  setup). Gets a zoom, or a short freeze for fail and shock, plus a sound. Not in the first 1.5 s,
  and not in the first 7 s when `violence_early_risk` is true. Use null if there is no clear payoff.
- `mood`: funny, fail, shock, hype or wholesome. Picks the sound and the beat style.
- `outro_type`: reaction_freeze, reaction_replay, reaction_zoom or none. Vary it across a batch.
- `reaction_ts` (optional): the facecam moment used by the outro, default `punchline_ts`. Pick a
  frame with the face visible.
- `outro_text` (optional): up to 28 characters. It must be true of the clip and spoiler free. Tease the moment, never the streamer.
- `angle` (optional): a 1 to 3 word label for the joke type (for example "controller struggle").
  The scheduler avoids posting two clips with the same angle back to back.
Missing or invalid values never block a render: the effect they control is switched off
(`hook_text` falls back to the first hook).

## rankings.json format
```json
{
  "vod_id": "2933231152",
  "campaign": "asianjeff",
  "ranked_by": "claude-code-session | claude-code-headless | claude-api:<model>",
  "ranked_at": "YYYY-MM-DD",
  "clips": [
    {
      "candidate_rank": 1,
      "keep": true,
      "score": 8,
      "start": 7812.4,
      "end": 7840.0,
      "summary": "One line on what happens",
      "reason": "Why it scores this, or why rejected",
      "hooks": ["...", "..."],
      "hook_text": "...",
      "punchline_ts": 7833.9,
      "mood": "hype",
      "outro_type": "reaction_replay",
      "reaction_ts": 7833.9,
      "outro_text": "...",
      "angle": "controller struggle",
      "violence_early_risk": false,
      "spoiler_risk": false
    }
  ]
}
```
Every candidate gets an entry (kept or rejected) so decisions are traceable.
