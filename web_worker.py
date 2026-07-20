"""
Pull-worker for Blurb.

Polls a remote service for jobs, does the work locally, and posts results
back. Two job kinds:
  'discover' — scrape a YouTube/Patreon source (using local cookies)
  'process'  — download audio (using local cookies) + transcribe it

Runs as a long-lived process managed by blurb_manager.py.

Required env vars (loaded from .env):
  WEB_URL             e.g. https://palpal.app/api
  BLURB_API_KEY       shared secret for API authentication

Optional:
  POLL_INTERVAL           seconds to wait when no job is available (default: 5)
  YOUTUBE_COOKIES_FILE    path to a Netscape-format cookies.txt for YouTube
  PATREON_COOKIES_FILE    path to a Netscape-format cookies.txt for Patreon

Cookies never leave this machine — conductor only ever sends us a source URL
or a video/post ID and receives back scraped entries or a transcript.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
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

# Job-claim polling backs off up to 12h when idle, so it's a poor connectivity
# signal on its own — this runs independently on a short fixed interval so
# the admin panel's "blurb connected" indicator stays fresh even when there's
# no work.
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "60"))

STATUS_FILE   = Path("/tmp/blurb_worker_status.json")
WORKER_ID_FILE = Path(__file__).parent / "worker_id.txt"

_CLEAR = object()  # sentinel: remove this key from the status file
_status_lock = threading.Lock()


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


def _update_status(**changes) -> None:
    """Merge `changes` into the on-disk status file.

    A value of _CLEAR removes that key; None is ignored (leaves it as-is).
    Both the main loop and the heartbeat thread write here, so this does a
    locked read-modify-write rather than each owning separate fields.
    """
    with _status_lock:
        try:
            data = json.loads(STATUS_FILE.read_text())
        except Exception:
            data = {}
        for key, value in changes.items():
            if value is _CLEAR:
                data.pop(key, None)
            elif value is not None:
                data[key] = value
        try:
            STATUS_FILE.write_text(json.dumps(data))
        except Exception:
            pass


def _claim_job() -> dict | None:
    """Poll remote for the next pending job. Returns {id, kind, payload} or None."""
    res = httpx.get(f"{WEB_URL}/worker/jobs/next", headers=HEADERS, timeout=30)
    if res.status_code == 204:
        return None
    res.raise_for_status()
    return res.json()


def _complete(job_id: str, result: dict) -> None:
    """Post a job result back to conductor. Retries on connection errors."""
    for attempt in range(3):
        try:
            httpx.post(
                f"{WEB_URL}/worker/jobs/{job_id}/complete",
                json=result,
                headers=HEADERS,
                timeout=120,
            ).raise_for_status()
            return
        except Exception as exc:
            if attempt < 2:
                wait = 10 * (attempt + 1)
                logger.warning(f"Complete attempt {attempt + 1} failed: {exc} — retrying in {wait}s")
                time.sleep(wait)
            else:
                raise


def _fail(job_id: str, error: str, error_type: str = "other") -> None:
    for attempt in range(3):
        try:
            httpx.post(
                f"{WEB_URL}/worker/jobs/{job_id}/fail",
                json={"error": error, "error_type": error_type},
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


def _heartbeat_loop() -> None:
    """Runs in its own thread so it keeps firing even while a job is in progress."""
    while not _stop_event.is_set():
        try:
            httpx.post(f"{WEB_URL}/worker/heartbeat", headers=HEADERS, timeout=10).raise_for_status()
            _update_status(last_heartbeat_at=time.time(), last_heartbeat_ok=True)
        except Exception as exc:
            logger.debug(f"Heartbeat failed: {exc}")
            _update_status(last_heartbeat_at=time.time(), last_heartbeat_ok=False)
        _stop_event.wait(timeout=HEARTBEAT_INTERVAL)


class _JobError(Exception):
    """Carries a structured error_type up to the fail-reporting call."""

    def __init__(self, message: str, error_type: str = "other"):
        super().__init__(message)
        self.error_type = error_type


def _handle_discover(payload: dict) -> dict:
    import discovery
    return asyncio.run(discovery.discover(payload["site"], payload["url"]))


def _handle_process(payload: dict) -> dict:
    import download
    from transcribe import transcribe_audio  # deferred so the model isn't loaded until first needed

    site = payload["site"]
    video_id = payload["video_id"]
    min_duration = payload.get("min_duration_seconds", 0)

    with tempfile.TemporaryDirectory(prefix="blurb_") as workdir:
        try:
            file_path, pub_date = asyncio.run(
                download.download_audio(site, video_id, min_duration, workdir)
            )
        except tuple(download.ERROR_TYPE.keys()) as exc:
            raise _JobError(str(exc), download.ERROR_TYPE[type(exc)]) from exc

        with open(file_path, "rb") as f:
            audio_bytes = f.read()

        logger.info(f"Audio downloaded ({len(audio_bytes):,} bytes), transcribing…")
        transcript = transcribe_audio(audio_bytes)
        logger.info(f"Transcription complete — {len(transcript.get('segments', []))} segments")

    return {"transcript": transcript, "publication_date": pub_date}


_BACKOFF_MAX = 30 * 60  # 30 minutes in seconds
_stop_event  = threading.Event()


def _shutdown(signum, frame):
    _stop_event.set()

signal.signal(signal.SIGTERM, _shutdown)


def _sleep(seconds: float) -> bool:
    """Sleep for up to `seconds`. Returns True if a stop was requested."""
    return _stop_event.wait(timeout=seconds)


def run() -> None:
    logger.info(f"Pull worker started — polling {WEB_URL}")
    threading.Thread(target=_heartbeat_loop, name="heartbeat", daemon=True).start()
    _update_status(state="polling", job_id=_CLEAR, site=_CLEAR, error=_CLEAR)
    job_id:  str | None = None
    backoff: float      = POLL_INTERVAL

    while not _stop_event.is_set():
        try:
            job = _claim_job()
            if job is None:
                _update_status(state="polling", next_poll_at=time.time() + backoff)
                logger.debug(f"No jobs available, sleeping {backoff:.0f}s")
                if _sleep(backoff):
                    break
                backoff = min(backoff * 2, _BACKOFF_MAX)
                continue

            backoff = POLL_INTERVAL  # reset on successful claim
            job_id  = job["id"]
            kind    = job["kind"]
            payload = job["payload"]
            logger.info(f"Claimed {kind} job {job_id}")
            _update_status(state=kind, job_id=job_id, site=payload.get("site"),
                            error=_CLEAR, next_poll_at=_CLEAR)

            result = _handle_discover(payload) if kind == "discover" else _handle_process(payload)

            _complete(job_id, result)
            logger.info(f"Job {job_id} posted back")
            job_id = None
            _update_status(state="polling", job_id=_CLEAR, site=_CLEAR)

        except KeyboardInterrupt:
            break

        except _JobError as exc:
            logger.error(f"Job {job_id} failed ({exc.error_type}): {exc}")
            _update_status(state="error", job_id=job_id, error=str(exc))
            if job_id:
                _fail(job_id, str(exc), error_type=exc.error_type)
                job_id = None
            if _sleep(POLL_INTERVAL):
                break

        except Exception as exc:
            logger.error(f"Worker error: {exc}")
            _update_status(state="error", job_id=job_id, error=str(exc))
            if job_id:
                _fail(job_id, str(exc), error_type="other")
                job_id = None
            if _sleep(POLL_INTERVAL):
                break

    logger.info("Worker stopped")
    _update_status(state="stopped", job_id=_CLEAR, error=_CLEAR, next_poll_at=_CLEAR)


if __name__ == "__main__":
    run()
