# Blurb

Webhook-based FastAPI microservice for audio transcription with Faster-Whisper.

**Requirements:** NVIDIA GPU with CUDA support (this service will not run on CPU).

## Quick Start

### 1. Setup Virtual Environment (Recommended)

```powershell
# Create virtual environment
python -m venv venv

# Activate virtual environment
# On Windows (PowerShell):
.\venv\Scripts\Activate.ps1

# On Windows (CMD):
.\venv\Scripts\activate.bat

# On Linux/Mac:
source venv/bin/activate
```

### 2. Install CUDA Dependencies

For **NVIDIA RTX 30-series** (3060, 3070, 3080, 3090) or newer:

```bash
# Install PyTorch with CUDA 11.8 support
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118

# Install cuBLAS and cuDNN for faster-whisper
pip install nvidia-cublas-cu11 nvidia-cudnn-cu11
```

Verify CUDA is working:
```bash
python -c "import torch; print(f'CUDA available: {torch.cuda.is_available()}')"
```

### 3. Install FFmpeg

FFmpeg is required for audio preprocessing (downsampling to 16kHz mono):

```powershell
# Option 1: Using winget
winget install FFmpeg

# Option 2: Using chocolatey
choco install ffmpeg

# Option 3: Download from https://ffmpeg.org/download.html and add to PATH
```

Verify installation:
```bash
ffmpeg -version
```

### 4. Install Application

1. Copy `.env.example` to `.env` and configure:
   ```powershell
   copy .env.example .env
   ```

2. Edit `.env` with your credentials:
   ```env
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=your-secure-password
   SECRET_KEY=generate-random-key-here
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

### 5. Run the Service

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

API will be available at `http://localhost:8000`

**API docs:** `http://localhost:8000/docs`

## Authentication

All endpoints require an API key via the `X-API-Key` header.

**Persistence:** API keys are stored in `api_keys.json` (SHA-256 hashed, not plain text). Keys survive server restarts.

### Creating API Keys

Create an API key using admin credentials (from `.env`):

```bash
curl -X POST http://localhost:8000/api-keys \
  -F "name=Production Server" \
  -F "username=admin" \
  -F "password=yourpassword"

# Response:
# {
#   "api_key": "blurb_xxxxxxxxxxxxx",
#   "prefix": "blurb_12345678",
#   "name": "Production Server",
#   "message": "Save this key securely - it won't be shown again"
# }
```

### Using API Keys

Include the API key in all requests:

```bash
curl -X POST http://localhost:8000/transcribe \
  -H "X-API-Key: blurb_xxxxxxxxxxxxx" \
  -F "job_id=test123" \
  -F "file=@audio.mp3"
```

### Managing API Keys

List all API keys (requires existing API key):
```bash
curl -X GET http://localhost:8000/api-keys \
  -H "X-API-Key: blurb_xxxxxxxxxxxxx"
```

Delete an API key (requires existing API key):
```bash
curl -X DELETE http://localhost:8000/api-keys/blurb_12345678 \
  -H "X-API-Key: blurb_xxxxxxxxxxxxx"
```

## Architecture

**Webhook-based design**: Transcription results are delivered via webhooks when provided. For testing without a webhook, omit the `webhook_url` parameter and results will be saved to `transcripts/{job_id}.json`. Jobs are immediately deleted after webhook delivery or file save.

**Audio preprocessing**: All audio is automatically downsampled to 16kHz mono before transcription (matches Groq's preprocessing pipeline).

**Model**: Uses `distil-large-v3` (Whisper 3 Turbo equivalent) with `BatchedInferencePipeline` and batch size 8 for optimal GPU performance on RTX 30-series.

## Endpoints

### API Key Management

#### POST /api-keys
Create a new API key using admin credentials
- **Auth**: Admin username and password (no API key required)
- **Body**:
  - `name`: Descriptive name for the API key (e.g., "Production Server")
  - `username`: Admin username from `.env`
  - `password`: Admin password from `.env`
- **Returns**: `{"api_key": "blurb_...", "prefix": "blurb_123...", "name": "...", "message": "..."}`
- **Note**: The full API key is only shown once. Save it securely.

#### GET /api-keys
List all API keys
- **Auth**: API Key (header `X-API-Key`)
- **Returns**: Array of API key metadata (without the actual keys)

#### DELETE /api-keys/{prefix}
Delete/revoke an API key
- **Auth**: API Key (header `X-API-Key`)
- **Path**: `prefix` - The API key prefix (e.g., "blurb_12345678")
- **Returns**: `{"message": "API key deleted"}`

### Transcription

#### POST /transcribe
Upload audio file for transcription with optional webhook callback
- **Auth**: API Key (header `X-API-Key`)
- **Body**:
  - `job_id`: **REQUIRED** - Unique identifier for this transcription job (supplied by caller)
  - `file`: Audio file (multipart/form-data)
  - `webhook_url`: **OPTIONAL** - Your callback URL for results. If omitted, saves to `transcripts/{job_id}.json`
- **Returns**: `{"job_id": "...", "queue_position": 0}`
- **Webhook payload (success)**:
  ```json
  {
    "job_id": "abc-123",
    "status": "completed",
    "transcription": "Full transcription text...",
    "language": "en",
    "segments": [...]
  }
  ```
- **Webhook payload (failure)**:
  ```json
  {
    "job_id": "abc-123",
    "status": "failed",
    "error": "error message"
  }
  ```

#### GET /health
Service health check with queue status, configuration, and recent logs
- **Auth**: API Key (header `X-API-Key`)
- **Returns**:
  ```json
  {
    "status": "healthy",
    "logs": ["..."],
    "queue_info": {
      "jobs_processing": 1,
      "jobs_queued": 3,
      "queue_slots_available": 29,
      "max_queue_size": 32,
      "current_job_id": "abc-123"
    },
    "config": {
      "whisper_model": "base",
      "device": "cuda",
      "compute_type": "int8",
      "cuda_available": true,
      "max_audio_size_mb": 256
    }
  }
  ```

## Configuration

All settings in `.env`:

**Authentication:**
- `ADMIN_USERNAME`: Admin username for creating API keys (default: admin)
- `ADMIN_PASSWORD`: Admin password for creating API keys (default: changeme)

**Service:**
- `MAX_AUDIO_SIZE_MB`: Max upload size in MB (default: 256)
- `MAX_QUEUE_SIZE`: Max queued jobs (default: 32)

**Whisper:**
- `WHISPER_MODEL`: Model size - tiny, base, small, medium, large-v2, large-v3, distil-large-v3 (default: distil-large-v3)
- `WHISPER_COMPUTE_TYPE`: Computation type - int8, float16, float32 (default: float16)

## Example Usage

### Quick Start Workflow

1. **Create an API key** (first time setup):
```bash
curl -X POST http://localhost:8000/api-keys \
  -F "name=My Server" \
  -F "username=admin" \
  -F "password=changeme"

# Save the returned api_key value
```

2. **Transcribe audio**:
```bash
curl -X POST http://localhost:8000/transcribe \
  -H "X-API-Key: blurb_xxxxxxxxxxxxx" \
  -F "job_id=unique-id-123" \
  -F "file=@audio.mp3"
```

### Production (with webhook and API key)
```bash
curl -X POST http://localhost:8000/transcribe \
  -H "X-API-Key: blurb_xxxxxxxxxxxxx" \
  -F "job_id=my-unique-job-123" \
  -F "file=@audio.mp3" \
  -F "webhook_url=https://yourserver.com/webhook"
```

Your webhook endpoint will receive the transcription when complete.

### Testing (without webhook - saves to disk)
```bash
curl -X POST http://localhost:8000/transcribe \
  -H "X-API-Key: blurb_xxxxxxxxxxxxx" \
  -F "job_id=test-job-456" \
  -F "file=@audio.mp3"
```

Result will be saved to `transcripts/test-job-456.json`

### Health Check
```bash
curl -X GET http://localhost:8000/health \
  -H "X-API-Key: blurb_xxxxxxxxxxxxx"
```
