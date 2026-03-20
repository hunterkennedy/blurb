<p align="center">
  <img src="blurb.png" alt="Blurb" width="128">
</p>

<h1 align="center">Blurb</h1>

<p align="center">
  GPU-accelerated audio transcription service built on
  <a href="https://github.com/SYSTRAN/faster-whisper">Faster-Whisper</a>
</p>

---

Blurb transcribes audio on-GPU using batched Whisper inference and returns timestamped text with word-level timing. It runs on your local machine and can operate in two modes:

- **Server mode** — exposes a REST API; a remote service submits audio and polls for results
- **Pull-worker mode** — polls a remote service for jobs, transcribes locally, posts results back; no inbound connections required

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

Opens a control panel that starts Blurb automatically, shows live status and job info, and minimizes to the system tray on close. If `WEB_URL` is set in `.env`, the manager also starts the pull worker and shows its status in a second panel.

### Headless — server only

```bash
venv-linux/bin/uvicorn main:app --host 0.0.0.0 --port 8001
```

### Headless — pull worker only

```bash
venv-linux/bin/python web_worker.py
```

Requires `WEB_URL` and `BLURB_API_KEY` to be set in `.env`.

---

## Pull-Worker Mode

In pull-worker mode, blurb makes only **outbound** connections — no inbound ports, no firewall changes, no port forwarding needed.

```
[blurb, home PC]  →  polls WEB_URL  →  [remote service, VPS]
                  ←  audio file        ←
                  →  transcript        →
```

**How it works:**

1. `web_worker.py` polls `GET /worker/jobs/next` on the remote service
2. On a job: downloads audio via `GET /worker/audio/{episode_id}`
3. Transcribes locally using the Whisper model
4. Posts the result to `POST /worker/jobs/{id}/complete`
5. On error: reports to `POST /worker/jobs/{id}/fail`
6. On no jobs: sleeps `POLL_INTERVAL` seconds and repeats

**Setup:**

1. Set `WEB_URL`, `BLURB_API_KEY`, and (if using Cloudflare Access) `CF_CLIENT_ID` + `CF_CLIENT_SECRET` in `.env`
2. Launch via the manager GUI, or run `web_worker.py` directly

If the remote service is behind Cloudflare Access, obtain a service token from the Cloudflare Zero Trust dashboard and set `CF_CLIENT_ID` / `CF_CLIENT_SECRET` accordingly.

---

## REST API (server mode)

### Authentication

All job and health endpoints require an API key via the `X-API-Key` header. Keys are stored in `api_keys.json` as SHA-256 hashes and persist across restarts.

**Create a key** (requires admin bearer token from `.env`):

```bash
curl -X POST http://localhost:8001/api-keys \
  -H "Authorization: Bearer <ADMIN_BEARER_TOKEN>" \
  -F "name=my-key"
```

**List / delete keys:**

```bash
curl http://localhost:8001/api-keys -H "X-API-Key: blurb_xxx"
curl -X DELETE http://localhost:8001/api-keys/<prefix> -H "X-API-Key: blurb_xxx"
```

### Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/jobs` | API key | Submit a transcription job |
| `GET` | `/jobs/{id}` | API key | Poll job status |
| `GET` | `/jobs/{id}/result` | API key | Fetch result (deletes the job) |
| `DELETE` | `/jobs/{id}` | API key | Cancel a job |
| `GET` | `/status` | None | Active job ID and job count |
| `GET` | `/health` | API key | GPU info, model config, job count |
| `POST` | `/api-keys` | Bearer | Create API key |
| `GET` | `/api-keys` | API key | List keys |
| `DELETE` | `/api-keys/{prefix}` | API key | Delete key |

### Submitting a Job

`POST /jobs` accepts multipart form data:

- `job_id` — a client-chosen string identifier
- `file` — audio file (any format FFmpeg can read)

Audio is downsampled to 16 kHz mono internally. Only one job runs at a time; submitting while busy returns `503`.

### Retrieving Results

Poll `GET /jobs/{id}` until `status` is `completed` or `failed`, then call `GET /jobs/{id}/result` to fetch the transcript and clean up. Results include full text, per-segment timestamps, and word-level timing with confidence scores.

---

## Configuration

All settings via `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `ADMIN_BEARER_TOKEN` | `changeme123` | Token for creating API keys |
| `MAX_AUDIO_SIZE_MB` | `256` | Max upload size in MB |
| `WHISPER_MODEL` | `distil-large-v3` | Faster-Whisper model name |
| `WHISPER_COMPUTE_TYPE` | `float16` | Compute precision (`float16`, `int8`, `float32`) |
| `JOB_TIMEOUT_SECONDS` | `3600` | Max seconds before a job is marked failed |
| `WEB_URL` | _(empty)_ | Remote API URL — enables pull-worker mode when set |
| `BLURB_API_KEY` | _(empty)_ | Shared API key for remote auth |
| `CF_CLIENT_ID` | _(empty)_ | Cloudflare Access service token ID |
| `CF_CLIENT_SECRET` | _(empty)_ | Cloudflare Access service token secret |
| `POLL_INTERVAL` | `5` | Seconds to wait between polls when idle |
