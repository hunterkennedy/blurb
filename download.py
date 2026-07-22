"""
Audio download — runs on blurb, using cookies that live only on this
machine's local disk. Conductor hands us a video/post ID and never sees the
credentials used to fetch it.

RSS episodes skip yt-dlp entirely: the feed's <enclosure> already points
straight at an audio file, so we just stream it down with httpx. Any auth
needed lives in the feed URL itself (see discovery.py's module docstring).
"""

import asyncio
import glob
import logging
import os
from urllib.parse import urlparse

import httpx

import discovery
from discovery import cookie_file_for, yt_dlp_path
from errors import (
    ERROR_TYPE,
    EpisodeAgeRestrictedError,
    EpisodeRateLimitedError,
    EpisodeTooShortError,
    EpisodeUnavailableError,
)

logger = logging.getLogger(__name__)

_AUDIO_EXT_BY_CONTENT_TYPE = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}


def _guess_audio_ext(url: str, content_type: str | None) -> str:
    path_ext = os.path.splitext(urlparse(url).path)[1].lstrip(".").lower()
    if path_ext and len(path_ext) <= 4:
        return path_ext
    if content_type:
        return _AUDIO_EXT_BY_CONTENT_TYPE.get(content_type.split(";")[0].strip().lower(), "mp3")
    return "mp3"


async def _download_rss_audio(video_id: str, feed_url: str | None, workdir: str) -> tuple[str, None, None]:
    """Resolve `video_id` (the item's guid) back to its enclosure URL by
    re-fetching the feed, then stream the audio straight to disk. Publication
    date and duration are already known from discovery — nothing new to
    report here, so conductor's COALESCE-on-write leaves them untouched."""
    if not feed_url:
        raise RuntimeError(f"RSS download for {video_id} missing source feed URL")

    enclosure_url = await discovery.find_rss_enclosure(feed_url, video_id)
    if not enclosure_url:
        raise EpisodeUnavailableError(f"RSS item {video_id} no longer in feed")

    logger.info(f"Downloading RSS audio for {video_id}: {enclosure_url}")

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        async with client.stream("GET", enclosure_url) as resp:
            if resp.status_code in (401, 403, 404, 410):
                raise EpisodeUnavailableError(f"RSS enclosure for {video_id} returned {resp.status_code}")
            resp.raise_for_status()  # anything else unexpected — surfaces as a generic (retryable) failure
            ext = _guess_audio_ext(enclosure_url, resp.headers.get("content-type"))
            file_path = os.path.join(workdir, f"{video_id}.{ext}")
            with open(file_path, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)

    logger.info(f"Downloaded to {file_path}")
    return file_path, None, None


async def download_audio(
    site: str, video_id: str, min_duration_seconds: int, workdir: str, source_url: str | None = None,
) -> tuple[str, str | None, float | None]:
    """
    Download audio for one episode into `workdir`. YouTube/Patreon go through
    yt-dlp; RSS is a direct HTTP stream of the feed's enclosure (`source_url`
    is required in that case — the source's feed URL).
    Returns (file_path, publication_date_or_None, duration_seconds_or_None).

    Raises EpisodeTooShortError / EpisodeUnavailableError / EpisodeRateLimitedError /
    EpisodeAgeRestrictedError on known failure modes; RuntimeError otherwise.
    """
    if site == "rss":
        return await _download_rss_audio(video_id, source_url, workdir)

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
        "--print", "duration",
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

    print_lines = stdout_bytes.decode().strip().splitlines()
    upload_date = print_lines[0] if print_lines else ""
    duration_str = print_lines[1] if len(print_lines) > 1 else ""

    pub_date: str | None = None
    if len(upload_date) == 8 and upload_date.isdigit():
        pub_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"

    duration: float | None = None
    try:
        duration = float(duration_str)
    except ValueError:
        pass

    return file_path, pub_date, duration
