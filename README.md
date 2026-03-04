# Blurb

FastAPI transcription service using Faster-Whisper. Runs natively on the host machine (not in Docker) to access the GPU.

**Requirements:** NVIDIA GPU, Linux, Python 3.12

---

## Setup (first time)

### 1. Install Python 3.12 and FFmpeg

```bash
sudo dnf install -y python3.12 python3.12-tkinter ffmpeg
```

### 2. Create the virtual environment

```bash
cd /path/to/blurb
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

Edit `.env` and set at minimum:
```env
ADMIN_BEARER_TOKEN=your-secure-token
CONDUCTOR_URL=http://localhost:8000
BLURB_API_KEY=shared-secret-with-conductor
```

### 6. Create an API key

Start blurb once manually to create the conductor API key, then stop it:

```bash
venv-linux/bin/uvicorn main:app --host 0.0.0.0 --port 8001
```

```bash
curl -X POST http://localhost:8001/api-keys \
  -H "Authorization: Bearer your-secure-token" \
  -F "name=conductor"
# Save the returned api_key — set it as BLURB_API_KEY in .env on both sides
```

---

## Running

**Normal use — manager window:**

```bash
venv-linux/bin/python blurb_manager.py
```

This opens a small control panel that starts blurb automatically, shows live status and job stats, and has a Start/Stop button. The manager auto-starts on login via `~/.config/autostart/blurb-manager.desktop`.

**Manual / headless:**

```bash
venv-linux/bin/uvicorn main:app --host 0.0.0.0 --port 8001
```

**Logs:**

Blurb logs to stdout. When run via the manager, output goes to the terminal you launched the manager from. For persistent logging, redirect:

```bash
venv-linux/bin/python blurb_manager.py >> blurb.log 2>&1 &
```

---

## Autostart on login

The file `~/.config/autostart/blurb-manager.desktop` is already in place after setup. The manager (and blurb) will start automatically on next login.

To disable autostart:
```bash
rm ~/.config/autostart/blurb-manager.desktop
```

---

## Authentication

All job/health endpoints require an API key via `X-API-Key` header. API keys are stored in `api_keys.json` (SHA-256 hashed) and survive restarts.

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
| `GET` | `/jobs/{id}` | API key | Poll job status (for debugging) |
| `GET` | `/jobs/{id}/result` | API key | Fetch result and delete job |
| `DELETE` | `/jobs/{id}` | API key | Cancel a job |

`POST /jobs` accepts multipart form data: `job_id` (string) + `file` (audio).

On completion or failure, blurb POSTs the result to `{CONDUCTOR_URL}/blurb/webhook/{job_id}` automatically — conductor does not need to poll.

### Other

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/status` | none | Active job ID and job count (used by manager UI) |
| `GET` | `/health` | API key | GPU info, model config, job count |
| `POST` | `/api-keys` | Bearer token | Create API key |
| `GET` | `/api-keys` | API key | List keys |
| `DELETE` | `/api-keys/{prefix}` | API key | Delete key |

---

## Configuration

All settings in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `ADMIN_BEARER_TOKEN` | `changeme` | Token for creating API keys |
| `MAX_AUDIO_SIZE_MB` | `256` | Max upload size |
| `WHISPER_MODEL` | `distil-large-v3` | Whisper model variant |
| `WHISPER_COMPUTE_TYPE` | `float16` | Compute precision |
| `CONDUCTOR_URL` | `` | Base URL for conductor (leave empty to run standalone) |
| `BLURB_API_KEY` | `` | Shared secret sent to conductor in `Authorization` header |
| `JOB_TIMEOUT_SECONDS` | `3600` | Max seconds before a job is marked failed |
