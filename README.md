# Cliper

Paste a **YouTube** or **Twitch** URL → see the most-watched parts on a heatmap → generate
10–60 vertical clips with burned-in captions, ready for **YouTube Shorts / Reels / TikTok**.

Free, open source, runs entirely on your own computer. No API keys, no accounts, no uploads.
Works on Windows, macOS and Linux.

## Install & run

You need Python 3.10+ and [uv](https://docs.astral.sh/uv/getting-started/installation/)
(or `pipx`). ffmpeg is picked up from your system if present, otherwise a static build is
downloaded automatically the first time you run it.

```sh
uv tool install git+https://github.com/YOUR_USER/cliper
cliper
```

That's it — the UI opens in your browser at http://localhost:8000.
(With pipx: `pipx install git+https://github.com/YOUR_USER/cliper`.)

Try it without installing:

```sh
uvx --from git+https://github.com/YOUR_USER/cliper cliper
```

Update later with `uv tool upgrade cliper`. YouTube changes things often, and that fix usually
lands in yt-dlp — upgrading is the first thing to try if a URL stops working.

### Optional: local captions for Twitch

Twitch has no subtitles, so captions for Twitch clips need a local Whisper model (free, runs on CPU):

```sh
uv tool install "git+https://github.com/YOUR_USER/cliper[whisper]"
```

### Docker

```sh
docker compose up --build     # http://localhost:8000, clips saved in ./data
```

## How it works

**Finding the best parts**

| Source | Signal |
|---|---|
| YouTube video | The official **"Most replayed"** graph — the same one YouTube shows above the seek bar |
| Twitch VOD | **Chat activity** (messages/second), sampled from Twitch's public chat replay. An 8-hour stream profiles in ~40 s |
| YouTube stream VOD | **Chat activity**, sampled the same way (streams have no "most replayed" graph) |
| Anything else | Audio loudness (videos ≤ 3 h) |

The signal is smoothed, peaks are found, and each clip grows outward from a peak while the
signal stays hot, clamped to your min/max length. Clips never overlap, and when captions exist the
edges snap to sentence boundaries so nothing starts mid-word.

**Choosing what to cut**

With a transcript (YouTube auto-subs, or local Whisper on the hottest stretches), clips start and
end on phrase boundaries and are ranked by interest × hook value (questions, "wait", "no way",
…), so a clip is a complete thought rather than 15 s around a spike. Default length 8–15 s.

**Video type → framing**

Cliper guesses the video type from metadata (Twitch category per stream segment, YouTube
category/tags/title) and looks at the frames of each clip with a free face detector (OpenCV YuNet):

| Type | Framing |
|---|---|
| Talking / IRL / vlog | 9:16 crop that follows the speaker's face (smoothed, dead-banded) |
| Podcast with two people | both faces stacked top / bottom |
| Gameplay with a facecam | game (4:3 crop) on top, the webcam zoomed in below |
| Gameplay without a cam, music | full frame over a blurred background |

**Edits (per type, all toggleable)**

Punch-in zooms on phrase changes · hook title card for the first 3 s · word-by-word highlighted
captions · progress bar · colour grade + sharpen · loudness normalised to −14 LUFS (the
Shorts/Reels target) · 1.05× speed and +2 % pitch by default (off for music) · optional mirror.

Only the seconds you need are streamed from the source; nothing is downloaded in full.

**Copyright check tab**

An *estimate* of whether a clip would be flagged by automated matching (Content ID and friends are
proprietary, so nobody can promise more). It checks the two things behind most claims:
recognisable **music** (Shazam) and **how close the clip still is to its source** — perceptual
video hashes that survive crops and mirroring, and audio correlation that survives speed and pitch
changes. Works on generated clips (one click) or any uploaded file, optionally compared against a
source URL + start time.

Clips are saved in your user data folder (shown in the terminal on startup) and can be downloaded
individually or as a zip from the UI.

## Options

```
cliper [--host 127.0.0.1] [--port 8000] [--no-browser]
```

| Env var | Default | Meaning |
|---|---|---|
| `CLIPER_DATA` | OS user-data dir | where jobs and clips are stored |
| `CLIPER_MAX_HEIGHT` | 1080 | max source resolution to pull |
| `CLIPER_RENDER_WORKERS` | 2 | parallel ffmpeg renders per job |
| `CLIPER_JOB_WORKERS` | 2 | parallel jobs |
| `FFMPEG_BIN` | auto | force a specific ffmpeg binary |
| `CLIPER_COOKIES` | – | Netscape cookies.txt for YouTube (servers) |
| `CLIPER_PASSWORD` | – | require a password (HTTP basic auth) |

## Troubleshooting

- **"Sign in to confirm you're not a bot" / 403 from YouTube** — YouTube rate-limits some
  networks. Upgrade first (`uv tool upgrade cliper`); if it persists, the yt-dlp
  [cookies guide](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp) applies.
- **Twitch player is black in the UI** — open the site as `localhost`, not `127.0.0.1`
  (Twitch embeds require a hostname).
- **Captions missing** — your system ffmpeg lacks libass. Set `FFMPEG_BIN` to a full build, or
  remove ffmpeg from PATH so the bundled static build is used.
- **Nothing happens / no progress** — every stage is listed under the progress bar in the UI and
  in the terminal. Run `cliper -v` for debug output.

## Hosting it on a server

A free VPS (Oracle Cloud's always-free ARM instance, a Hetzner box, a home server) works well —
4 cores render a clip in well under a minute. Things to know:

- **YouTube and datacenter IPs.** YouTube often answers cloud IPs with *"Sign in to confirm you're
  not a bot"*. The fix is to export cookies from a logged-in browser (Netscape `cookies.txt` format,
  e.g. the "Get cookies.txt LOCALLY" extension — ideally from a throwaway Google account) and point
  Cliper at them: `CLIPER_COOKIES=/path/cookies.txt`. Twitch does not have this problem.
- **Put a password on it.** The app has no accounts; anyone who can reach it can use your CPU.
  Set `CLIPER_PASSWORD=something` (HTTP basic auth, any username), or keep it private behind
  Tailscale / a Cloudflare Tunnel.
- **Not** a free PaaS (Render/Railway/Fly free tiers): those cap CPU and kill long requests.

```sh
CLIPER_PASSWORD=secret CLIPER_COOKIES=~/cookies.txt cliper --host 0.0.0.0 --port 8000 --no-browser
```

or with Docker, add `CLIPER_PASSWORD` / `CLIPER_COOKIES` to `compose.yaml`'s `environment:`.

## Development

```sh
git clone https://github.com/YOUR_USER/cliper && cd cliper
uv sync --dev
uv run cliper          # dev server
uv run pytest          # tests
```

```
cliper/sources.py      yt-dlp metadata, heatmap / chat / audio signals, stream selection
cliper/chat_signal.py  Twitch / YouTube chat-activity samplers
cliper/analysis.py     peak detection and clip-window selection
cliper/modes.py        video-type detection and per-type edit defaults
cliper/framing.py      face detection → crop / split / game+cam plans
cliper/captions.py     json3 → word timings → ASS (captions, title, progress bar), optional Whisper
cliper/clipper.py      ffmpeg rendering (direct stream, yt-dlp fallback)
cliper/copyright.py    music recognition + source similarity → risk estimate
cliper/jobs.py         on-disk job manager
cliper/main.py         FastAPI app         cliper/web/  the single-page UI
```

## API

- `POST /api/analyze {url}` → job
- `GET /api/jobs/{id}` → state (signal, highlights, clips, progress)
- `POST /api/jobs/{id}/generate {count, min_len, max_len, layout, mode, captions, punch, title, progress, grade, mirror, speed, pitch}`
- `GET /api/jobs/{id}/clips/clip_01.mp4` · `GET /api/jobs/{id}/clips.zip` · `DELETE /api/jobs/{id}`
- `POST /api/jobs/{id}/clips/clip_01.mp4/check` · `POST /api/check` (multipart: file, source_url?, source_start?, speed?)

Posting the clips is up to you — respect the original creators' rights and each platform's rules.

MIT licensed.
