import os
import uuid
import asyncio
import logging
import secrets
import json
import hashlib
from datetime import datetime, timedelta
from typing import Dict, Optional
from collections import deque
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Depends, File, UploadFile, BackgroundTasks, Form, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from transcribe import transcribe_audio as run_transcription

load_dotenv()

# Configuration from environment
MAX_AUDIO_SIZE_MB = int(os.getenv("MAX_AUDIO_SIZE_MB", "256"))
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "32"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
API_KEYS_FILE = Path("api_keys.json")

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory storage
jobs: Dict[str, dict] = {}
job_queue = deque()
current_transcription: Optional[str] = None
log_buffer = deque(maxlen=100)
api_keys: Dict[str, dict] = {}  # prefix -> {hash, name, created_at, last_used}

app = FastAPI(title="Blurb", version="0.1.0")


# Custom logging handler to capture logs
class BufferHandler(logging.Handler):
    def emit(self, record):
        log_buffer.append(self.format(record))


buffer_handler = BufferHandler()
buffer_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(buffer_handler)


# ============================================================================
# AUTHENTICATION & PERSISTENCE
# ============================================================================

def hash_api_key(key: str) -> str:
    """Hash API key using SHA-256"""
    return hashlib.sha256(key.encode()).hexdigest()

def load_api_keys() -> Dict[str, dict]:
    """Load API keys from disk"""
    if not API_KEYS_FILE.exists():
        return {}

    try:
        with open(API_KEYS_FILE, 'r') as f:
            data = json.load(f)
            # Convert ISO datetime strings back to datetime objects
            for key_data in data.values():
                key_data['created_at'] = datetime.fromisoformat(key_data['created_at'])
                if key_data.get('last_used'):
                    key_data['last_used'] = datetime.fromisoformat(key_data['last_used'])
            return data
    except Exception as e:
        logger.error(f"Failed to load API keys: {e}")
        return {}

def save_api_keys():
    """Save API keys to disk (hashes only, not the actual keys)"""
    try:
        # Convert datetime objects to ISO strings for JSON serialization
        data = {}
        for prefix, key_data in api_keys.items():
            data[prefix] = {
                'hash': key_data['hash'],
                'name': key_data['name'],
                'created_at': key_data['created_at'].isoformat(),
                'last_used': key_data['last_used'].isoformat() if key_data.get('last_used') else None
            }

        with open(API_KEYS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        logger.info(f"Saved {len(data)} API keys to {API_KEYS_FILE}")
    except Exception as e:
        logger.error(f"Failed to save API keys: {e}")

def generate_api_key() -> tuple[str, str]:
    """Generate API key. Returns (full_key, prefix)"""
    key = f"blurb_{secrets.token_urlsafe(32)}"
    prefix = key[:15]
    return key, prefix

def verify_admin_credentials(username: str, password: str) -> bool:
    """Verify admin username and password"""
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD

async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> str:
    """Verify API key and return user identifier"""
    if not x_api_key.startswith("blurb_"):
        raise HTTPException(401, "Invalid API key format")

    prefix = x_api_key[:15]
    if prefix not in api_keys:
        raise HTTPException(401, "Invalid API key")

    key_data = api_keys[prefix]
    if hash_api_key(x_api_key) != key_data["hash"]:
        raise HTTPException(401, "Invalid API key")

    # Update last used timestamp and save async (don't block the request)
    key_data["last_used"] = datetime.utcnow()
    asyncio.create_task(asyncio.to_thread(save_api_keys))

    return "api_user"


# Models
class TranscribeResponse(BaseModel):
    job_id: str
    queue_position: int  # 0 = processing now, 1+ = position in queue


class HealthResponse(BaseModel):
    status: str
    logs: list[str]
    queue_info: dict
    config: dict


# Background job processing
async def send_webhook(webhook_url: str, payload: dict):
    """Send webhook notification to client or save to disk if no webhook"""
    if not webhook_url:
        # Save to disk instead
        import json
        output_dir = "transcripts"
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, f"{payload['job_id']}.json")
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        logger.info(f"Transcript saved to {filepath}")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
            logger.info(f"Webhook sent successfully to {webhook_url}")
    except Exception as e:
        logger.error(f"Failed to send webhook to {webhook_url}: {str(e)}")


async def process_transcription(job_id: str):
    global current_transcription

    try:
        current_transcription = job_id
        webhook_url = jobs[job_id]["webhook_url"]
        audio_data = jobs[job_id]["audio_data"]

        logger.info(f"Starting transcription for job {job_id}")

        # Run transcription in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            run_transcription,
            audio_data
        )

        transcription_text = result["text"]
        logger.info(f"Completed transcription for job {job_id}")

        # Send webhook with result
        webhook_payload = {
            "job_id": job_id,
            "status": "completed",
            "transcription": transcription_text,
            "language": result.get("language"),
            "segments": result.get("segments", [])
        }
        await send_webhook(webhook_url, webhook_payload)

    except Exception as e:
        logger.error(f"Error processing job {job_id}: {str(e)}")

        # Send webhook for failure
        webhook_payload = {
            "job_id": job_id,
            "status": "failed",
            "error": str(e)
        }
        await send_webhook(jobs[job_id]["webhook_url"], webhook_payload)

    finally:
        # Delete job immediately after webhook sent
        del jobs[job_id]
        logger.info(f"Job {job_id} completed and cleaned up")

        current_transcription = None
        # Process next job in queue
        if job_queue:
            next_job_id = job_queue.popleft()
            asyncio.create_task(process_transcription(next_job_id))


@app.on_event("startup")
async def startup_event():
    global api_keys

    # Load API keys from disk
    api_keys = load_api_keys()
    logger.info(f"Loaded {len(api_keys)} API keys from {API_KEYS_FILE}")

    logger.info("Blurb started")
    logger.info(f"Admin credentials - username: {ADMIN_USERNAME}, password: {ADMIN_PASSWORD}")
    logger.info("Create API keys at POST /api-keys")


# ============================================================================
# API KEY MANAGEMENT ENDPOINTS
# ============================================================================

@app.post("/api-keys")
async def create_api_key(
    name: str = Form(...),
    username: str = Form(...),
    password: str = Form(...)
):
    """Create API key with admin credentials"""
    if not verify_admin_credentials(username, password):
        raise HTTPException(401, "Invalid admin credentials")

    full_key, prefix = generate_api_key()

    api_keys[prefix] = {
        "hash": hash_api_key(full_key),
        "name": name,
        "created_at": datetime.utcnow(),
        "last_used": None
    }

    # Persist to disk
    save_api_keys()

    return {
        "api_key": full_key,
        "prefix": prefix,
        "name": name,
        "message": "Save this key securely - it won't be shown again"
    }

@app.get("/api-keys")
async def list_api_keys(user: str = Depends(verify_api_key)):
    """List all API keys (requires existing API key)"""
    return [{
        "prefix": prefix,
        "name": data["name"],
        "created_at": data["created_at"].isoformat(),
        "last_used": data["last_used"].isoformat() if data["last_used"] else None
    } for prefix, data in api_keys.items()]

@app.delete("/api-keys/{prefix}")
async def delete_api_key(prefix: str, user: str = Depends(verify_api_key)):
    """Delete an API key (requires existing API key)"""
    if prefix not in api_keys:
        raise HTTPException(404, "API key not found")

    del api_keys[prefix]

    # Persist to disk
    save_api_keys()

    return {"message": "API key deleted"}


# ============================================================================
# TRANSCRIPTION ENDPOINTS
# ============================================================================
@app.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_audio(
    background_tasks: BackgroundTasks,
    job_id: str = Form(...),
    file: UploadFile = File(...),
    webhook_url: str = Form(None),
    user: str = Depends(verify_api_key)
):
    # Check if job_id already exists
    if job_id in jobs or job_id == current_transcription:
        raise HTTPException(
            status_code=400,
            detail=f"Job ID {job_id} already exists"
        )

    # Check queue capacity
    total_jobs = len(jobs) + (1 if current_transcription else 0)
    if total_jobs >= MAX_QUEUE_SIZE + 1:  # +1 for current processing
        raise HTTPException(
            status_code=503,
            detail=f"Queue is full. Max queue size is {MAX_QUEUE_SIZE}"
        )

    # Check file size
    file_content = await file.read()
    file_size_mb = len(file_content) / (1024 * 1024)

    if file_size_mb > MAX_AUDIO_SIZE_MB:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max size is {MAX_AUDIO_SIZE_MB}MB"
        )

    jobs[job_id] = {
        "audio_data": file_content,
        "filename": file.filename,
        "webhook_url": webhook_url  # Can be None for testing
    }

    # Queue or start processing
    if current_transcription is None and not job_queue:
        # No job running, start immediately
        background_tasks.add_task(process_transcription, job_id)
        queue_position = 0  # Processing now
    else:
        # Add to queue
        job_queue.append(job_id)
        queue_position = len(job_queue)  # Position in queue (1-based)

    logger.info(f"Job {job_id} created for file {file.filename} (queue position: {queue_position})")

    return TranscribeResponse(
        job_id=job_id,
        queue_position=queue_position
    )


@app.get("/health", response_model=HealthResponse)
async def health_check(user: str = Depends(verify_api_key)):
    import torch

    # Queue information
    queue_info = {
        "jobs_processing": 1 if current_transcription else 0,
        "jobs_queued": len(job_queue),
        "total_active_jobs": len(jobs),
        "queue_slots_available": MAX_QUEUE_SIZE - len(job_queue),
        "max_queue_size": MAX_QUEUE_SIZE,
        "current_job_id": current_transcription
    }

    # Configuration information
    config = {
        "whisper_model": os.getenv("WHISPER_MODEL", "base"),
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "compute_type": os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
        "cuda_available": torch.cuda.is_available(),
        "max_audio_size_mb": MAX_AUDIO_SIZE_MB
    }

    return HealthResponse(
        status="healthy",
        logs=list(log_buffer),
        queue_info=queue_info,
        config=config
    )
