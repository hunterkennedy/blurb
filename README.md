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

**Requirements:** NVIDIA GPU with CUDA, Linux, Python 3.12, FFmpeg

---

## Quick Start

```bash
# 1. Install system deps (Fedora example — see below for Ubuntu)
sudo dnf install -y python3.12 python3.12-tkinter ffmpeg

# 2. Run the installer
./install.sh

# 3. Configure
cp .env.example .env
# Edit .env — see Configuration below

# 4. Launch
venv-linux/bin/python blurb_manager.py
```

---

## Setup

### install.sh

Creates the venv, installs PyTorch with CUDA and all pip dependencies, and copies `.env.example` to `.env` if one doesn't already exist.

### Manual Steps

**1. System dependencies**

```bash
# Fedora
sudo dnf install -y python3.12 python3.12-tkinter ffmpeg

# Ubuntu / Debian
sudo apt install -y python3.12 python3.12-tk ffmpeg
```

**2. Create venv and install packages**

```bash
python3.12 -m venv venv-linux
venv-linux/bin/pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
venv-linux/bin/pip install -r requirements.txt
```

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

Opens a control panel that starts the pull-worker automatically, shows live status (polling / transcribing / error), and minimizes to the system tray on close.

### Headless

```bash
venv-linux/bin/python web_worker.py
```

Requires `WEB_URL` and `BLURB_API_KEY` to be set in `.env`.

---

## How It Works

Blurb makes only **outbound** connections — no inbound ports, no firewall changes, no port forwarding needed.

```
[blurb, home PC]  →  polls WEB_URL  →  [remote service, VPS]
                  ←  audio file     ←
                  →  transcript     →
```

1. `web_worker.py` polls `GET /worker/jobs/next` on the remote service
2. On a job: downloads audio via `GET /worker/audio/{episode_id}`
3. Transcribes locally using the Whisper model
4. Posts the result to `POST /worker/jobs/{id}/complete`
5. On error: reports to `POST /worker/jobs/{id}/fail`
6. On no jobs: backs off exponentially (starting at `POLL_INTERVAL`, doubling each time, up to 12 hours), then retries

**Setup:**

1. Set `WEB_URL`, `BLURB_API_KEY`, and (if using Cloudflare Access) `CF_CLIENT_ID` + `CF_CLIENT_SECRET` in `.env`
2. Launch via the manager GUI, or run `web_worker.py` directly

If the remote service is behind Cloudflare Access, obtain a service token from the Cloudflare Zero Trust dashboard and set `CF_CLIENT_ID` / `CF_CLIENT_SECRET` accordingly.

---

## Configuration

All settings via `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_MODEL` | `distil-large-v3` | Faster-Whisper model name |
| `WHISPER_COMPUTE_TYPE` | `float16` | Compute precision (`float16`, `int8`, `float32`) |
| `WEB_URL` | _(empty)_ | Remote API base URL — required for pull-worker mode |
| `BLURB_API_KEY` | _(empty)_ | Shared API key for remote auth |
| `CF_CLIENT_ID` | _(empty)_ | Cloudflare Access service token ID |
| `CF_CLIENT_SECRET` | _(empty)_ | Cloudflare Access service token secret |
| `POLL_INTERVAL` | `5` | Initial seconds between polls; doubles on each empty response up to 12 hours |
