import os
import asyncio
import logging
import secrets
import json
import hashlib
import torch
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Dict, Optional
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, File, UploadFile, BackgroundTasks, Form, Header
from pydantic import BaseModel
from dotenv import load_dotenv

from transcribe import transcribe_audio as run_transcription, get_model

load_dotenv()

# Configuration from environment
MAX_AUDIO_SIZE_MB = int(os.getenv("MAX_AUDIO_SIZE_MB", "256"))
ADMIN_BEARER_TOKEN = os.getenv("ADMIN_BEARER_TOKEN", "changeme")
API_KEYS_FILE = Path("api_keys.json")
JOB_TIMEOUT_SECONDS = int(os.getenv("JOB_TIMEOUT_SECONDS", "3600"))
JOB_TTL_SECONDS = 300  # 5 minutes

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory storage
jobs: Dict[str, dict] = {}  # job_id -> {status, result, error, created_at}
api_keys: Dict[str, dict] = {}  # prefix -> {hash, name, created_at}
active_job_id: Optional[str] = None


# ============================================================================
# LIFESPAN
# ============================================================================

async def cleanup_expired_jobs():
    """Periodically remove completed/failed jobs older than JOB_TTL_SECONDS."""
    while True:
        await asyncio.sleep(60)
        cutoff = datetime.utcnow() - timedelta(seconds=JOB_TTL_SECONDS)
        expired = [
            job_id for job_id, job in jobs.items()
            if job["status"] in ("completed", "failed") and job["created_at"] < cutoff
        ]
        for job_id in expired:
            del jobs[job_id]
        if expired:
            logger.info(f"Cleaned up {len(expired)} expired job(s)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global api_keys
    # startup
    api_keys = load_api_keys()
    logger.info(f"Loaded {len(api_keys)} API keys from {API_KEYS_FILE}")
    logger.info("Blurb started")
    logger.info(f"Admin bearer token: {ADMIN_BEARER_TOKEN}")
    logger.info("Create API keys at POST /api-keys with Authorization: Bearer <token>")
    # Pre-warm model so it's in VRAM before the first request arrives
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, get_model)
    cleanup_task = asyncio.create_task(cleanup_expired_jobs())
    yield
    cleanup_task.cancel()


app = FastAPI(title="Blurb", version="0.1.0", lifespan=lifespan)


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
                'created_at': key_data['created_at'].isoformat()
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

async def verify_admin_token(authorization: str = Header(...)) -> None:
    """Verify admin bearer token"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid authorization header")

    token = authorization[7:]  # Remove "Bearer " prefix
    if token != ADMIN_BEARER_TOKEN:
        raise HTTPException(401, "Invalid bearer token")

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

    return "api_user"


# Models
class JobSubmitResponse(BaseModel):
    job_id: str
    status: str
    created_at: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str  # "processing", "completed", "failed"
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: str


class HealthResponse(BaseModel):
    status: str
    jobs_active: int
    config: dict


# Background job processing
async def process_transcription(job_id: str):
    """Process transcription job - updates job status in memory"""
    global active_job_id
    jobs[job_id]["status"] = "processing"
    logger.info(f"Starting transcription for job {job_id}")

    try:
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, run_transcription, jobs[job_id]["audio_data"]),
            timeout=JOB_TIMEOUT_SECONDS
        )

        jobs[job_id]["status"] = "completed"
        jobs[job_id]["result"] = {
            "text": result["text"],
            "language": result.get("language"),
            "segments": result.get("segments", [])
        }
        logger.info(f"Completed transcription for job {job_id}")

    except asyncio.TimeoutError:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = f"Job timed out after {JOB_TIMEOUT_SECONDS}s"
        logger.error(f"Job {job_id} timed out after {JOB_TIMEOUT_SECONDS}s")

    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        logger.error(f"Job {job_id} failed: {e}")

    finally:
        active_job_id = None
        if job_id in jobs:
            jobs[job_id].pop("audio_data", None)  # free memory


# ============================================================================
# API KEY MANAGEMENT ENDPOINTS
# ============================================================================

@app.post("/api-keys")
async def create_api_key(
    name: str = Form(...),
    admin: None = Depends(verify_admin_token)
):
    """Create API key with admin bearer token"""
    full_key, prefix = generate_api_key()

    api_keys[prefix] = {
        "hash": hash_api_key(full_key),
        "name": name,
        "created_at": datetime.utcnow()
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
        "created_at": data["created_at"].isoformat()
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

@app.post("/jobs", response_model=JobSubmitResponse)
async def submit_transcription_job(
    background_tasks: BackgroundTasks,
    job_id: str = Form(...),
    file: UploadFile = File(...),
    user: str = Depends(verify_api_key)
):
    """Submit transcription job - starts processing immediately"""
    global active_job_id

    # Concurrency guard: single GPU = one job at a time
    if active_job_id is not None:
        raise HTTPException(503, f"Blurb is busy with job {active_job_id}")

    # Check if job_id already exists
    if job_id in jobs:
        raise HTTPException(400, f"Job ID {job_id} already exists")

    # Check file size
    file_content = await file.read()
    file_size_mb = len(file_content) / (1024 * 1024)

    if file_size_mb > MAX_AUDIO_SIZE_MB:
        raise HTTPException(413, f"File too large. Max size is {MAX_AUDIO_SIZE_MB}MB")

    # Create job entry
    created_at = datetime.utcnow()
    jobs[job_id] = {
        "audio_data": file_content,
        "status": "queued",
        "result": None,
        "error": None,
        "created_at": created_at
    }

    # Set active_job_id synchronously before returning to prevent race conditions
    active_job_id = job_id
    background_tasks.add_task(process_transcription, job_id)

    logger.info(f"Job {job_id} submitted ({file_size_mb:.1f}MB)")

    return JobSubmitResponse(
        job_id=job_id,
        status="queued",
        created_at=created_at.isoformat()
    )

@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str, user: str = Depends(verify_api_key)):
    """
    Get job status - for manual testing and debugging
    Returns job status and result if completed
    """
    if job_id not in jobs:
        raise HTTPException(404, f"Job {job_id} not found")

    job = jobs[job_id]
    return JobStatusResponse(
        job_id=job_id,
        status=job["status"],
        result=job.get("result"),
        error=job.get("error"),
        created_at=job["created_at"].isoformat()
    )

@app.get("/jobs/{job_id}/result")
async def get_job_result(job_id: str, user: str = Depends(verify_api_key)):
    """
    Get job result and DELETE the job from memory (cleanup)
    For manual testing and debugging
    """
    if job_id not in jobs:
        raise HTTPException(404, f"Job {job_id} not found")

    job = jobs[job_id]

    if job["status"] == "processing" or job["status"] == "queued":
        raise HTTPException(400, f"Job {job_id} is still {job['status']}")

    if job["status"] == "failed":
        error = job.get("error", "Unknown error")
        # Clean up failed job
        del jobs[job_id]
        raise HTTPException(500, f"Job failed: {error}")

    # Get result and clean up
    result = job["result"]
    del jobs[job_id]
    logger.info(f"Job {job_id} result fetched and cleaned up")

    return result

@app.delete("/jobs/{job_id}")
async def cancel_job(job_id: str, user: str = Depends(verify_api_key)):
    """Cancel/delete a job"""
    if job_id not in jobs:
        raise HTTPException(404, f"Job {job_id} not found")

    del jobs[job_id]
    logger.info(f"Job {job_id} cancelled")
    return {"message": f"Job {job_id} cancelled"}


@app.get("/status")
async def status():
    """Unauthenticated status endpoint for the local manager UI"""
    return {
        "active_job_id": active_job_id,
        "jobs_total": len(jobs)
    }


@app.get("/health", response_model=HealthResponse)
async def health_check(user: str = Depends(verify_api_key)):
    config = {
        "whisper_model": os.getenv("WHISPER_MODEL", "base"),
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "compute_type": os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
        "cuda_available": torch.cuda.is_available(),
        "max_audio_size_mb": MAX_AUDIO_SIZE_MB
    }

    return HealthResponse(
        status="healthy",
        jobs_active=len(jobs),
        config=config
    )
