"""
Pull-worker for Blurb.

Polls a remote service for pending transcription jobs, fetches the audio,
transcribes locally using the Whisper model, and posts the result back.
Runs as a long-lived process managed by blurb_manager.py.

Required env vars (loaded from .env):
  WEB_URL          e.g. https://api.example.com
  BLURB_API_KEY       shared secret for API authentication

Optional:
  CF_CLIENT_ID        Cloudflare Access service token ID
  CF_CLIENT_SECRET    Cloudflare Access service token secret
  POLL_INTERVAL       seconds to wait when no job is available (default: 5)
"""

import logging
import os
import sys
import time

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
API_KEY          = os.environ["BLURB_API_KEY"]
CF_CLIENT_ID     = os.getenv("CF_CLIENT_ID", "")
CF_CLIENT_SECRET = os.getenv("CF_CLIENT_SECRET", "")
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL", "5"))

HEADERS: dict[str, str] = {"X-API-Key": API_KEY}
if CF_CLIENT_ID:
    HEADERS["CF-Access-Client-Id"] = CF_CLIENT_ID
    HEADERS["CF-Access-Client-Secret"] = CF_CLIENT_SECRET


def _claim_job() -> dict | None:
    """Poll remote for the next pending job. Returns job dict or None."""
    res = httpx.get(f"{WEB_URL}/worker/jobs/next", headers=HEADERS, timeout=30)
    if res.status_code == 204:
        return None
    res.raise_for_status()
    return res.json()


def _fetch_audio(episode_id: str) -> bytes:
    """Download the audio file for a claimed job."""
    res = httpx.get(
        f"{WEB_URL}/worker/audio/{episode_id}",
        headers=HEADERS,
        timeout=600,
    )
    res.raise_for_status()
    return res.content


def _complete(job_id: str, result: dict) -> None:
    httpx.post(
        f"{WEB_URL}/worker/jobs/{job_id}/complete",
        json=result,
        headers=HEADERS,
        timeout=60,
    ).raise_for_status()


def _fail(job_id: str, error: str) -> None:
    try:
        httpx.post(
            f"{WEB_URL}/worker/jobs/{job_id}/fail",
            json={"error": error},
            headers=HEADERS,
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Could not report failure: {e}")


def run() -> None:
    # Import here so the model isn't loaded until the worker actually starts
    from transcribe import transcribe_audio

    logger.info(f"Pull worker started — polling {WEB_URL}")
    job_id: str | None = None

    while True:
        try:
            job = _claim_job()
            if job is None:
                time.sleep(POLL_INTERVAL)
                continue

            job_id     = job["id"]
            episode_id = job["episode_id"]
            logger.info(f"Claimed job {job_id} (episode {episode_id})")

            audio = _fetch_audio(episode_id)
            logger.info(f"Audio fetched ({len(audio):,} bytes), transcribing…")

            result = transcribe_audio(audio)
            logger.info(f"Transcription complete — {len(result.get('segments', []))} segments")

            _complete(job_id, result)
            logger.info(f"Job {job_id} posted back")
            job_id = None

        except KeyboardInterrupt:
            logger.info("Worker stopped")
            break

        except Exception as exc:
            logger.error(f"Worker error: {exc}")
            if job_id:
                _fail(job_id, str(exc))
                job_id = None
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
