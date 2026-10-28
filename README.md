<p align="center">
  <img src="blurb.png" alt="Blurb" width="128">
</p>

<h1 align="center">Blurb</h1>

<p align="center">
  GPU-accelerated audio transcription service built on
  <a href="https://github.com/SYSTRAN/faster-whisper">Faster-Whisper</a>
</p>

---

Blurb transcribes audio on-GPU using batched Whisper inference and returns timestamped text with word-level timing. It runs on your local machine as a pull-worker — polling a remote service for jobs, transcribing locally, and posting results back. No inbound connections required.

Blurb also owns discovery and download: it scrapes YouTube/Patreon sources with yt-dlp and downloads episode audio, both using cookies that live only in this directory. The remote service (conductor) never sees YouTube or Patreon credentials — it only ever hands blurb a source URL or a video/post ID, and gets back scraped entries or a finished transcript.

**Requirements:** Linux, Python 3.12, FFmpeg, Node.js (for yt-dlp's JS challenge solving)

An NVIDIA GPU with CUDA is recommended for speed, but not required — Blurb falls back to CPU otherwise. AMD/Intel GPUs can't be used either way: the Whisper inference engine ([CTranslate2](https://github.com/OpenNMT/CTranslate2)) only has CUDA and CPU backends, no ROCm support.

---

## Quick Start

```bash
# 1. Install system deps (Fedora example — see below for Ubuntu)
sudo dnf install -y python3.12 python3.12-tkinter ffmpeg nodejs

# 2. Run the installer
./install.sh

# 3. Launch
venv-linux/bin/python blurb_manager.py
# First run opens straight to Settings — paste in your Remote URL and API
# key there and hit Save. No manual .env editing required.
```

---

## Setup

### install.sh

Creates the venv, installs PyTorch with CUDA and all pip dependencies, and copies `.env.example` to `.env` if one doesn't already exist.

### Manual Steps

**1. System dependencies**

```bash
# Fedora
sudo dnf install -y python3.12 python3.12-tkinter ffmpeg nodejs

# Ubuntu / Debian
sudo apt install -y python3.12 python3.12-tk ffmpeg nodejs
```

**2. Create venv and install packages**

```bash
python3.12 -m venv venv-linux

# With an NVIDIA GPU:
venv-linux/bin/pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
# Without one (CPU mode, e.g. AMD/Intel GPUs or no GPU at all):
venv-linux/bin/pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu

venv-linux/bin/pip install -r requirements.txt
```

`install.sh` picks the right one automatically based on whether `nvidia-smi` is present.

Verify CUDA:

```bash
venv-linux/bin/python -c "import torch; print('CUDA:', torch.cuda.is_available())"
```

**3. Configure**

```bash
cp .env.example .env
```

Edit `.env` — see [Configuration](#configuration) for all options.

---

## Running

### Manager GUI (recommended)

```bash
venv-linux/bin/python blurb_manager.py
```

Opens a control panel with a Settings screen for pasting in your Remote URL and API key (saved straight to `.env`, no manual editing needed), a live status view (polling / discovering / transcribing / error, plus a heartbeat-based connection indicator), a Start/Stop toggle, and a system tray icon that reflects the same state when minimized.

### Headless

```bash
venv-linux/bin/python web_worker.py
```

Requires `WEB_URL` and `BLURB_API_KEY` to be set in `.env`.

---

## How It Works

Blurb makes only **outbound** connections — no inbound ports, no firewall changes, no port forwarding needed. Audio never crosses the network: blurb downloads it locally and deletes it after transcribing.

```
[blurb, home PC]         polls WEB_URL          [remote service, VPS]
  yt-dlp + cookies   ───────────────────────►
  Whisper model      ◄───────────────────────   source URLs / video IDs
                      ─── entries/transcript ──►
```

`web_worker.py` polls `GET /worker/jobs/next` and gets back one of two job kinds:

- **`discover`** — `{source_id, site, url, filters}`. Scrapes the source (`discovery.py`) and returns the entries it found. `site` is `youtube`, `patreon`, or `rss`.
- **`process`** — `{episode_id, site, video_id, min_duration_seconds, url}`. Downloads the audio (`download.py`) and transcribes it, returning the transcript. The audio file lives only in a temp dir on this machine for the duration of the job.

YouTube and Patreon go through yt-dlp using local cookies (see below). **RSS sources need no cookies at all** — `site: "rss"` treats `url` as a plain podcast feed URL and parses/downloads it directly with `httpx`. RSS 2.0, Atom, and RSS 1.0/RDF feeds are supported, including relative `<enclosure>` URLs. If the feed requires auth (e.g. a Patreon RSS export like `https://www.patreon.com/rss/<show>?auth=<token>&show=<id>`), the token is just part of the URL — conductor stores and passes it through exactly like any other source URL, no separate credential to configure here. GUIDs that aren't filename-safe (URL-style GUIDs are common outside Patreon) are sanitized/hashed for the local temp file only; the feed's original GUID is still what palpal stores and searches against.

Either way: `POST /worker/jobs/{id}/complete` on success, `POST /worker/jobs/{id}/fail` with `{error, error_type}` on failure (`error_type` is one of `unavailable`, `age_restricted`, `rate_limited`, `too_short`, `other` — conductor uses it to decide whether to blacklist, retry, or fail the episode). On no jobs available, blurb backs off exponentially (starting at `POLL_INTERVAL`, doubling each time, up to 12 hours), then retries.

Since that backoff can stretch to 12 hours while idle, job polling alone isn't a reliable "is blurb still there" signal. A separate background thread posts to `POST /worker/heartbeat` every `HEARTBEAT_INTERVAL` seconds (default 60), independent of job activity — that's what the admin panel's connected/disconnected indicator is based on.

**Setup:**

1. Set `WEB_URL` and `BLURB_API_KEY` — via the manager GUI's Settings screen, or directly in `.env` for headless use
2. Launch via the manager GUI, or run `web_worker.py` directly

---

## Configuration

All settings via `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_MODEL` | `distil-large-v3` | Faster-Whisper model name |
| `WHISPER_DEVICE` | `auto` | `auto` picks CUDA if available, else CPU. Force with `cuda` or `cpu` |
| `WHISPER_COMPUTE_TYPE` | `float16` (CUDA) / `int8` (CPU) | Compute precision (`float16`, `int8`, `float32`) |
| `WEB_URL` | _(empty)_ | Remote API base URL — required for pull-worker mode |
| `BLURB_API_KEY` | _(empty)_ | Shared API key for remote auth |
| `POLL_INTERVAL` | `5` | Initial seconds between polls; doubles on each empty response up to 12 hours |
| `YOUTUBE_COOKIES_FILE` | _(empty)_ | Path to a Netscape-format cookies.txt with your YouTube session — only needed for age-restricted videos |
| `PATREON_COOKIES_FILE` | _(empty)_ | Path to a Netscape-format cookies.txt with your Patreon session — required for any Patreon source |
| `RSS_MAX_FEED_BYTES` | `26214400` (25 MB) | Maximum decompressed RSS/Atom feed size; larger responses are rejected |
| `RSS_MAX_AUDIO_MB` | `1024` | Maximum size of a single RSS enclosure download |
| `RSS_MAX_ICON_MB` | `5` | Maximum size of a podcast artwork image fetched from RSS/Atom |
| `ALLOW_PRIVATE_RSS_URLS` | `false` | Allow feed/enclosure URLs on localhost or a private LAN (needed for local test feeds) |

Cookies are read from these files on every discover/download job — nothing is cached or sent anywhere except directly to YouTube/Patreon by yt-dlp itself.

---

## Tests

The RSS/Atom parser and filename handling have dependency-free unit tests:

```bash
venv-linux/bin/python -m unittest discover -s tests
```
