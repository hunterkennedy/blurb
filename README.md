<p align="center">
  <img src="blurb.png" alt="Blurb" width="128">
</p>

<h1 align="center">Blurb</h1>

<p align="center">
  GPU-accelerated audio transcription service built on
  <a href="https://github.com/SYSTRAN/faster-whisper">Faster-Whisper</a>
</p>

---

Blurb exposes a simple REST API that accepts audio files, transcribes them on-GPU using batched inference, and returns timestamped text with word-level timing. Jobs run one at a time (single GPU) and results are retrieved by polling.

Comes with a desktop manager GUI (Tkinter + system tray) for starting/stopping the service and monitoring job status.

**Requirements:** NVIDIA GPU with CUDA, Linux, Python 3.12, FFmpeg

---

## Quick Start

```bash
# 1. Install system deps (Fedora example — see below for Ubuntu)
sudo dnf install -y python3.12 python3.12-tkinter ffmpeg

# 2. Run the installer
./install.sh

# 3. Configure
#    Edit .env and set ADMIN_BEARER_TOKEN to something secure

# 4. Launch
venv-linux/bin/python blurb_manager.py
```

---

## Setup

### install.sh

The install script creates the venv, installs PyTorch with CUDA and all pip dependencies, and copies `.env.example` to `.env` if one doesn't already exist.

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

Edit `.env` and set `ADMIN_BEARER_TOKEN`. The other values have sensible defaults — see [Configuration](#configuration) for the full list.

---

## Running

### Manager GUI

```bash
venv-linux/bin/python blurb_manager.py
```

Opens a control panel that starts Blurb automatically, shows live status and job info, and minimizes to the system tray on close.

### Headless

```bash
venv-linux/bin/uvicorn main:app --host 0.0.0.0 --port 8001
```

---

## API

### Authentication

All job and health endpoints require an API key via the `X-API-Key` header. Keys are stored in `api_keys.json` as SHA-256 hashes and persist across restarts.

**Create a key** (requires admin bearer token):

```bash
curl -X POST http://localhost:8001/api-keys \
  -H "Authorization: Bearer <admin-token>" \
  -F "name=my-key"
```

**List keys:**

```bash
curl http://localhost:8001/api-keys \
  -H "X-API-Key: blurb_xxx"
```

**Delete a key:**

```bash
curl -X DELETE http://localhost:8001/api-keys/<prefix> \
  -H "X-API-Key: blurb_xxx"
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

`POST /jobs` accepts multipart form data with two fields:

- `job_id` — a client-chosen string identifier
- `file` — the audio file (any format FFmpeg can read)

Audio is downsampled to 16 kHz mono internally before transcription. Only one job runs at a time; submitting while busy returns `503`.

### Retrieving Results

Poll `GET /jobs/{id}` until `status` is `completed` or `failed`, then call `GET /jobs/{id}/result` to fetch the transcript and clean up the job from memory. The result includes full text, per-segment timestamps, and word-level timing with confidence scores.

---

## Configuration

All settings via `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `ADMIN_BEARER_TOKEN` | `changeme` | Token for creating API keys |
| `MAX_AUDIO_SIZE_MB` | `256` | Max upload size in MB |
| `WHISPER_MODEL` | `distil-large-v3` | Faster-Whisper model name |
| `WHISPER_COMPUTE_TYPE` | `float16` | Compute precision |
| `JOB_TIMEOUT_SECONDS` | `3600` | Max seconds before a job is marked failed |
