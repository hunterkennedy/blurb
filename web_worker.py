"""
Pull-worker for Blurb.

Polls a remote service for jobs, does the work locally, and posts results
back. Two job kinds:
  'discover' — scrape a YouTube/Patreon/RSS source (YouTube/Patreon use local
               cookies; RSS needs none — any auth is embedded in the feed URL)
  'process'  — download audio (using local cookies for YouTube/Patreon, a
               plain HTTP stream for RSS) + transcribe it
             — or, with payload.duration_only=True, just probe a Patreon
               post's duration via yt-dlp metadata (no download/transcribe),
               used to backfill episodes processed before duration capture existed

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
import queue
import signal
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

import paths

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# httpx logs every request URL at INFO. RSS feed/enclosure URLs carry private
# auth tokens (a Patreon feed's ?auth=.../u/<token>/...), so don't let those
# reach the worker logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

WEB_URL       = os.environ["WEB_URL"].rstrip("/")
API_KEY       = os.environ["BLURB_API_KEY"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "5"))

# Job-claim polling backs off up to 12h when idle, so it's a poor connectivity
# signal on its own — this runs independently on a short fixed interval so
# the admin panel's "blurb connected" indicator stays fresh even when there's
# no work.
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "60"))

STATUS_FILE   = paths.status_file()
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


def _read_status() -> dict:
    """Best-effort snapshot of the on-disk status file (used by heartbeats)."""
    with _status_lock:
        try:
            return json.loads(STATUS_FILE.read_text())
        except Exception:
            return {}


_last_progress_at = 0.0
_last_progress_stage = ""
_progress_queue: "queue.Queue[tuple[str, dict] | None]" = queue.Queue(maxsize=16)


def _progress_sender() -> None:
    """Posts queued progress updates off the worker thread.

    Progress is best-effort and must never stall a download or transcription.
    A blocking post (or a conductor outage) belongs here, not in the hot path.
    """
    while True:
        item = _progress_queue.get()
        try:
            if item is None:
                return
            job_id, payload = item
            httpx.post(
                f"{WEB_URL}/worker/jobs/{job_id}/progress",
                json=payload,
                headers=HEADERS,
                timeout=5,
            )
        except Exception as exc:
            logger.debug(f"Progress report failed: {exc}")
        finally:
            _progress_queue.task_done()


def _progress(
    job_id: str | None,
    stage: str,
    percent: float | None = None,
    detail: str | None = None,
    *,
    force: bool = False,
) -> None:
    """Best-effort progress report to conductor + local manager status.

    Network delivery is queued to a background thread so a slow/unreachable
    conductor can never throttle the actual download or transcription.
    """
    global _last_progress_at, _last_progress_stage

    now = time.monotonic()
    # Stage changes always go through; percentage updates at most once a second.
    if not force and stage == _last_progress_stage and now - _last_progress_at < 1.0:
        return
    _last_progress_at = now
    _last_progress_stage = stage

    payload = {"stage": stage}
    if percent is not None:
        payload["percent"] = round(max(0.0, min(100.0, percent)), 1)
    if detail:
        payload["detail"] = detail

    _update_status(stage=stage, progress=payload)
    if not job_id:
        return
    try:
        _progress_queue.put_nowait((job_id, payload))
    except queue.Full:
        # Drop the oldest pending update rather than blocking the worker.
        try:
            _progress_queue.get_nowait()
            _progress_queue.task_done()
            _progress_queue.put_nowait((job_id, payload))
        except (queue.Empty, queue.Full):
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
            status = _read_status()
            payload = {
                "state": status.get("state"),
                "stage": status.get("stage"),
                "job_id": status.get("job_id"),
                "detail": status.get("error") or status.get("detail"),
                "progress": status.get("progress") or {},
            }
            httpx.post(
                f"{WEB_URL}/worker/heartbeat",
                json=payload,
                headers=HEADERS,
                timeout=10,
            ).raise_for_status()
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


def _handle_discover(payload: dict, job_id: str | None = None) -> dict:
    import discovery
    _progress(job_id, "discovering", None, "scraping source", force=True)
    result = asyncio.run(discovery.discover(payload["site"], payload["url"]))
    _progress(job_id, "finishing", 99, "saving discovered entries", force=True)
    return result


def _handle_process(payload: dict, job_id: str | None = None) -> dict:
    import discovery
    import download
    import test_mode
    from transcribe import transcribe_audio  # deferred so the model isn't loaded until first needed

    site = payload["site"]
    video_id = payload["video_id"]

    # Lightweight backfill mode: probe duration via yt-dlp metadata only, no
    # download/transcribe. Used to fill in duration on episodes that were
    # already processed before duration capture existed.
    if payload.get("duration_only"):
        _progress(job_id, "probing duration", None, "reading metadata", force=True)
        duration = asyncio.run(discovery.fetch_patreon_post_duration(video_id))
        return {"duration_seconds": duration}

    min_duration = payload.get("min_duration_seconds", 0)

    if test_mode.is_test_video(video_id):
        try:
            _progress(job_id, "transcribing", 99, "using test transcript", force=True)
            return test_mode.load_transcript(video_id)
        except tuple(download.ERROR_TYPE.keys()) as exc:
            raise _JobError(str(exc), download.ERROR_TYPE[type(exc)]) from exc

    with tempfile.TemporaryDirectory(prefix="blurb_") as workdir:
        try:
            def _download_progress(percent: float | None, detail: str | None = None) -> None:
                _progress(job_id, "downloading", percent, detail)

            _progress(job_id, "downloading", 0, "starting download", force=True)
            file_path, pub_date, duration = asyncio.run(
                download.download_audio(
                    site, video_id, min_duration, workdir,
                    source_url=payload.get("url"),
                    progress_cb=_download_progress,
                )
            )
        except tuple(download.ERROR_TYPE.keys()) as exc:
            raise _JobError(str(exc), download.ERROR_TYPE[type(exc)]) from exc

        with open(file_path, "rb") as f:
            audio_bytes = f.read()

        logger.info(f"Audio downloaded ({len(audio_bytes):,} bytes), transcribing…")
        _progress(job_id, "transcribing", 0, "audio downloaded", force=True)
        transcript = transcribe_audio(
            audio_bytes,
            on_progress=lambda pct, detail=None: _progress(job_id, "transcribing", pct, detail),
        )
        logger.info(f"Transcription complete — {len(transcript.get('segments', []))} segments")
        _progress(job_id, "finishing", 99, "uploading transcript", force=True)

    return {"transcript": transcript, "publication_date": pub_date, "duration_seconds": duration}


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
    threading.Thread(target=_progress_sender, name="progress", daemon=True).start()
    _update_status(state="polling", job_id=_CLEAR, site=_CLEAR, error=_CLEAR,
                    stage=_CLEAR, progress=_CLEAR)
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
                            stage="claimed", progress={"stage": "claimed"},
                            error=_CLEAR, next_poll_at=_CLEAR)

            result = (
                _handle_discover(payload, job_id)
                if kind == "discover"
                else _handle_process(payload, job_id)
            )

            _complete(job_id, result)
            logger.info(f"Job {job_id} posted back")
            job_id = None
            _update_status(state="polling", job_id=_CLEAR, site=_CLEAR,
                            stage=_CLEAR, progress=_CLEAR)

        except KeyboardInterrupt:
            break

        except _JobError as exc:
            logger.error(f"Job {job_id} failed ({exc.error_type}): {exc}")
            _update_status(state="error", job_id=job_id, error=str(exc),
                            stage=_CLEAR, progress=_CLEAR)
            if job_id:
                _fail(job_id, str(exc), error_type=exc.error_type)
                job_id = None
            if _sleep(POLL_INTERVAL):
                break

        except Exception as exc:
            logger.error(f"Worker error: {exc}")
            _update_status(state="error", job_id=job_id, error=str(exc),
                            stage=_CLEAR, progress=_CLEAR)
            if job_id:
                _fail(job_id, str(exc), error_type="other")
                job_id = None
            if _sleep(POLL_INTERVAL):
                break

    logger.info("Worker stopped")
    _update_status(state="stopped", job_id=_CLEAR, error=_CLEAR, next_poll_at=_CLEAR)


if __name__ == "__main__":
    run()
