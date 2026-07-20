"""
Audio download — runs on blurb, using cookies that live only on this
machine's local disk. Conductor hands us a video/post ID and never sees the
credentials used to fetch it.
"""

import asyncio
import glob
import logging
import os

from discovery import cookie_file_for, yt_dlp_path

logger = logging.getLogger(__name__)


class EpisodeTooShortError(Exception):
    """Raised when yt-dlp skips a video because it's below the minimum duration."""


class EpisodeUnavailableError(Exception):
    """Raised when yt-dlp reports the video is private or unavailable."""


class EpisodeRateLimitedError(Exception):
    """Raised when YouTube rate-limits the download request (transient — safe to retry later)."""


class EpisodeAgeRestrictedError(Exception):
    """Raised when the video requires age verification (needs YouTube cookies)."""


# Maps our exception classes to the error_type string reported to conductor.
ERROR_TYPE = {
    EpisodeUnavailableError: "unavailable",
    EpisodeAgeRestrictedError: "age_restricted",
    EpisodeRateLimitedError: "rate_limited",
    EpisodeTooShortError: "too_short",
}


async def download_audio(site: str, video_id: str, min_duration_seconds: int, workdir: str) -> tuple[str, str | None]:
    """
    Download audio for one episode using yt-dlp into `workdir`.
    Returns (file_path, publication_date_or_None).

    Raises EpisodeTooShortError / EpisodeUnavailableError / EpisodeRateLimitedError /
    EpisodeAgeRestrictedError on known failure modes; RuntimeError otherwise.
    """
    output_template = os.path.join(workdir, f"{video_id}.%(ext)s")
    video_url = (
        f"https://www.patreon.com/posts/{video_id}" if site == "patreon"
        else f"https://youtube.com/watch?v={video_id}"
    )

    logger.info(f"Downloading audio for {site}/{video_id}")

    cmd = [
        yt_dlp_path(),
        "-x",
        "--audio-format", "best",
        "-f", "bestaudio/best",
        "--print", "upload_date",
        "--no-simulate",
        "--js-runtimes", "node",
        "--cache-dir", "/tmp/yt-dlp-cache",
        "-o", output_template,
    ]

    cookie_file = cookie_file_for(site)
    if cookie_file:
        cmd += ["--cookies", cookie_file]

    if site != "patreon" and min_duration_seconds and min_duration_seconds > 0:
        cmd += ["--match-filter", f"duration >= {min_duration_seconds}"]

    cmd.append(video_url)

    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=3600)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"yt-dlp download timed out after 1 hour for {video_id}")

    if proc.returncode != 0:
        stderr = stderr_bytes.decode()
        logger.warning(f"yt-dlp stderr for {video_id}: {stderr[:1000]}")
        if any(phrase in stderr for phrase in ("Private video", "This video is private", "Video unavailable", "This post is")):
            raise EpisodeUnavailableError(f"Content {video_id} is private or unavailable")
        if "Sign in to confirm your age" in stderr:
            raise EpisodeAgeRestrictedError(f"Age-restricted content {video_id} — cookies required")
        if any(phrase in stderr for phrase in ("HTTP Error 429", "Too Many Requests")):
            raise EpisodeRateLimitedError(f"Rate limited for {video_id} — will retry later")
        raise RuntimeError(f"yt-dlp download failed for {video_id}: {stderr[:500]}")

    pattern = os.path.join(workdir, f"{video_id}.*")
    matches = glob.glob(pattern)
    if not matches:
        # yt-dlp exited 0 but wrote no file — filtered out by --match-filter
        raise EpisodeTooShortError(f"Skipped by yt-dlp: duration below {min_duration_seconds}s minimum")

    file_path = matches[0]
    logger.info(f"Downloaded to {file_path}")

    pub_date: str | None = None
    upload_date = stdout_bytes.decode().strip()
    if len(upload_date) == 8 and upload_date.isdigit():
        pub_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"

    return file_path, pub_date
