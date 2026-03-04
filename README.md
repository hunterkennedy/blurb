# Blurb

GPU-accelerated audio transcription service built on [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper). Exposes a simple REST API that accepts audio files, transcribes them on-GPU, and returns timestamped text with word-level timing. Jobs run one at a time (single GPU) and results are delivered via webhook or polling.

Comes with a small desktop manager GUI (Tkinter + system tray) for starting/stopping the service and monitoring job status.

**Requirements:** NVIDIA GPU with CUDA, Linux, Python 3.12, FFmpeg

---

## Setup

### 1. Install system dependencies

Python 3.12 and FFmpeg are required. Install them with your package manager, e.g.:

```bash
# Fedora
sudo dnf install -y python3.12 python3.12-tkinter ffmpeg

# Ubuntu/Debian
sudo apt install -y python3.12 python3.12-tk ffmpeg
```

### 2. Create the virtual environment

```bash
python3.12 -m venv venv-linux
```

### 3. Install PyTorch with CUDA

```bash
venv-linux/bin/pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
```

Verify CUDA is working:
```bash
venv-linux/bin/python -c "import torch; print('CUDA:', torch.cuda.is_available())"
```

### 4. Install dependencies

```bash
venv-linux/bin/pip install -r requirements.txt
```

### 5. Configure

```bash
cp .env.example .env
```

Edit `.env` and set `ADMIN_BEARER_TOKEN` to something secure. The other values have sensible defaults — see [Configuration](#configuration) for the full list.

---

## Running

**Manager GUI:**

```bash
venv-linux/bin/python blurb_manager.py
```

Opens a small control panel that starts blurb automatically, shows live status and job info, and minimizes to the system tray on close.

**Headless:**

```bash
venv-linux/bin/uvicorn main:app --host 0.0.0.0 --port 8001
```

---

## Authentication

All job and health endpoints require an API key via the `X-API-Key` header. Keys are stored in `api_keys.json` as SHA-256 hashes and persist across restarts.

**Create a key** (requires `ADMIN_BEARER_TOKEN`):
```bash
curl -X POST http://localhost:8001/api-keys \
  -H "Authorization: Bearer <admin-token>" \
  -F "name=my-key"
```

**List keys:**
```bash
curl http://localhost:8001/api-keys -H "X-API-Key: blurb_xxx"
```

**Delete a key:**
```bash
curl -X DELETE http://localhost:8001/api-keys/<prefix> -H "X-API-Key: blurb_xxx"
```

---

## Endpoints

### Jobs

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/jobs` | API key | Submit a transcription job |
| `GET` | `/jobs/{id}` | API key | Get job status |
| `GET` | `/jobs/{id}/result` | API key | Fetch result and delete job |
| `DELETE` | `/jobs/{id}` | API key | Cancel a job |

`POST /jobs` accepts multipart form data: `job_id` (string) + `file` (audio file). Audio is downsampled to 16kHz mono internally via FFmpeg before transcription.

If `CONDUCTOR_URL` is configured, blurb automatically POSTs results to `{CONDUCTOR_URL}/blurb/webhook/{job_id}` on completion or failure.

### Other

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/status` | None | Active job ID and job count |
| `GET` | `/health` | API key | GPU info, model config, job count |
| `POST` | `/api-keys` | Bearer token | Create API key |
| `GET` | `/api-keys` | API key | List keys |
| `DELETE` | `/api-keys/{prefix}` | API key | Delete key |

---

## Configuration

All settings via `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `ADMIN_BEARER_TOKEN` | `changeme` | Token for creating API keys |
| `MAX_AUDIO_SIZE_MB` | `256` | Max upload size |
| `WHISPER_MODEL` | `distil-large-v3` | Faster-Whisper model name |
| `WHISPER_COMPUTE_TYPE` | `float16` | Compute precision |
| `CONDUCTOR_URL` | | Webhook target URL (leave empty to run standalone) |
| `BLURB_API_KEY` | | Auth token sent with webhook requests |
| `JOB_TIMEOUT_SECONDS` | `3600` | Max seconds before a job is marked failed |
