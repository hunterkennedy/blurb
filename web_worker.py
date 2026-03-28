"""
Pull-worker for Blurb.

Polls a remote service for pending transcription jobs, fetches the audio,
transcribes locally using the Whisper model, and posts the result back.
Runs as a long-lived process managed by blurb_manager.py.

Required env vars (loaded from .env):
  WEB_URL             e.g. https://palpal.app/api
  BLURB_API_KEY       shared secret for API authentication

Optional:
  POLL_INTERVAL       seconds to wait when no job is available (default: 5)
"""

import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

WEB_URL       = os.environ["WEB_URL"].rstrip("/")
API_KEY       = os.environ["BLURB_API_KEY"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "5"))

STATUS_FILE   = Path("/tmp/blurb_worker_status.json")
WORKER_ID_FILE = Path(__file__).parent / "worker_id.txt"


def _load_or_create_worker_id() -> str:
    """Return a stable UUID for this worker instance, creating one if needed."""
    if WORKER_ID_FILE.exists():
        wid = WORKER_ID_FILE.read_text().strip()
        if wid:
            return wid
    wid = str(uuid.uuid4())
    WORKER_ID_FILE.write_text(wid)
    logger.info(f"Generated new worker ID: {wid}")
    return wid


WORKER_ID = _load_or_create_worker_id()
HEADERS: dict[str, str] = {"X-API-Key": API_KEY, "X-Worker-ID": WORKER_ID}


def _write_status(state: str, job_id: str | None = None, error: str | None = None, next_poll_at: float | None = None) -> None:
    data: dict = {"state": state}
    if job_id is not None:
        data["job_id"] = job_id
    if error is not None:
        data["error"] = error
    if next_poll_at is not None:
        data["next_poll_at"] = next_poll_at
    try:
        STATUS_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def _claim_job() -> dict | None:
    """Poll remote for the next pending job. Returns job dict or None."""
    res = httpx.get(f"{WEB_URL}/worker/jobs/next", headers=HEADERS, timeout=30)
    if res.status_code == 204:
        return None
    res.raise_for_status()
    return res.json()


def _fetch_audio(episode_id: str) -> bytes:
    """Download the audio file for a claimed job. Retries on connection errors."""
    for attempt in range(3):
        try:
            res = httpx.get(
                f"{WEB_URL}/worker/audio/{episode_id}",
                headers=HEADERS,
                timeout=600,
            )
            res.raise_for_status()
            return res.content
        except Exception as exc:
            if attempt < 2:
                wait = 10 * (attempt + 1)
                logger.warning(f"Audio fetch attempt {attempt + 1} failed: {exc} — retrying in {wait}s")
                time.sleep(wait)
            else:
                raise


def _complete(job_id: str, result: dict) -> None:
    """Post transcript back to conductor. Retries on connection errors."""
    for attempt in range(3):
        try:
            httpx.post(
                f"{WEB_URL}/worker/jobs/{job_id}/complete",
                json=result,
                headers=HEADERS,
                timeout=60,
            ).raise_for_status()
            return
        except Exception as exc:
            if attempt < 2:
                wait = 10 * (attempt + 1)
                logger.warning(f"Complete attempt {attempt + 1} failed: {exc} — retrying in {wait}s")
                time.sleep(wait)
            else:
                raise


def _fail(job_id: str, error: str) -> None:
    for attempt in range(3):
        try:
            httpx.post(
                f"{WEB_URL}/worker/jobs/{job_id}/fail",
                json={"error": error},
                headers=HEADERS,
                timeout=10,
            ).raise_for_status()
            return
        except Exception as e:
            if attempt < 2:
                wait = 10 * (attempt + 1)
                logger.warning(f"Fail report attempt {attempt + 1} failed: {e} — retrying in {wait}s")
                time.sleep(wait)
            else:
                logger.warning(f"Could not report failure after 3 attempts: {e}")


_BACKOFF_MAX = 30 * 60  # 30 minutes in seconds
_stop_event  = threading.Event()


def _shutdown(signum, frame):
    _stop_event.set()

signal.signal(signal.SIGTERM, _shutdown)


def _sleep(seconds: float) -> bool:
    """Sleep for up to `seconds`. Returns True if a stop was requested."""
    return _stop_event.wait(timeout=seconds)


def run() -> None:
    # Import here so the model isn't loaded until the worker actually starts
    from transcribe import transcribe_audio

    logger.info(f"Pull worker started — polling {WEB_URL}")
    _write_status("polling")
    job_id:  str | None = None
    backoff: float      = POLL_INTERVAL

    while not _stop_event.is_set():
        try:
            job = _claim_job()
            if job is None:
                _write_status("polling", next_poll_at=time.time() + backoff)
                logger.debug(f"No jobs available, sleeping {backoff:.0f}s")
                if _sleep(backoff):
                    break
                backoff = min(backoff * 2, _BACKOFF_MAX)
                continue

            backoff    = POLL_INTERVAL  # reset on successful claim
            job_id     = job["id"]
            episode_id = job["episode_id"]
            logger.info(f"Claimed job {job_id} (episode {episode_id})")
            _write_status("transcribing", job_id=job_id)

            audio = _fetch_audio(episode_id)
            logger.info(f"Audio fetched ({len(audio):,} bytes), transcribing…")

            result = transcribe_audio(audio)
            logger.info(f"Transcription complete — {len(result.get('segments', []))} segments")

            _complete(job_id, result)
            logger.info(f"Job {job_id} posted back")
            job_id = None
            _write_status("polling")

        except KeyboardInterrupt:
            break

        except Exception as exc:
            logger.error(f"Worker error: {exc}")
            _write_status("error", job_id=job_id, error=str(exc))
            if job_id:
                _fail(job_id, str(exc))
                job_id = None
            if _sleep(POLL_INTERVAL):
                break

    logger.info("Worker stopped")
    _write_status("stopped")


if __name__ == "__main__":
    run()
