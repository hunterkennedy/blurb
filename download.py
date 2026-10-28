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
import hashlib
import logging
import os
import re
from urllib.parse import urlparse

import httpx

import discovery
import paths
from discovery import cookie_file_for, yt_dlp_path
from errors import (
    ERROR_TYPE,
    EpisodeAgeRestrictedError,
    EpisodeRateLimitedError,
    EpisodeTooShortError,
    EpisodeUnavailableError,
)

logger = logging.getLogger(__name__)

# Hard ceiling for an RSS enclosure. A malicious/broken feed can point at an
# endless stream; without this the worker would happily fill its disk.
_RSS_MAX_AUDIO_BYTES = int(os.environ.get("RSS_MAX_AUDIO_MB", "1024")) * 1024 * 1024

_AUDIO_EXT_BY_CONTENT_TYPE = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/aacp": "aac",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
}


def _safe_audio_stem(video_id: str) -> str:
    """Turn an episode ID into a safe filename stem.

    RSS GUIDs are not guaranteed to look like YouTube/Patreon IDs: plenty of
    feeds use a URL (or a slash-containing tag) as the guid. Using that
    verbatim as a filename either creates a nonexistent subdirectory or
    exceeds the filename limit, so sanitize it and append a short hash to keep
    distinct IDs distinct after sanitizing.
    """
    if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", video_id):
        return video_id
    digest = hashlib.sha256(video_id.encode("utf-8")).hexdigest()[:12]
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", video_id).strip("_-")[:80]
    return f"{cleaned or 'rss'}-{digest}"


def _guess_audio_ext(url: str, content_type: str | None) -> str:
    path_ext = os.path.splitext(urlparse(url).path)[1].lstrip(".").lower()
    if re.fullmatch(r"[a-z0-9]{1,4}", path_ext):
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

    try:
        enclosure_url = await discovery.find_rss_enclosure(feed_url, video_id)
    except discovery.RSSFeedError as exc:
        if exc.status_code in (401, 403, 404, 410):
            raise EpisodeUnavailableError(str(exc)) from exc
        if exc.status_code == 429:
            raise EpisodeRateLimitedError(str(exc)) from exc
        raise RuntimeError(str(exc)) from exc

    if not enclosure_url:
        raise EpisodeUnavailableError(f"RSS item {video_id} no longer in feed")

    # Don't log the full enclosure URL: feeds like Patreon's embed the same
    # private token in the enclosure path that the feed URL carries in ?auth=.
    logger.info(f"Downloading RSS audio for {video_id} from {urlparse(enclosure_url).hostname}")

    try:
        async with discovery.rss_stream(enclosure_url, timeout=120) as resp:
            if resp.status_code in (401, 403, 404, 410):
                raise EpisodeUnavailableError(f"RSS enclosure for {video_id} returned {resp.status_code}")
            if resp.status_code == 429:
                raise EpisodeRateLimitedError(f"RSS enclosure for {video_id} rate limited (429)")
            resp.raise_for_status()  # anything else unexpected — surfaces as a generic (retryable) failure

            content_length = resp.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > _RSS_MAX_AUDIO_BYTES:
                raise RuntimeError(
                    f"RSS enclosure for {video_id} exceeds the "
                    f"{_RSS_MAX_AUDIO_BYTES // (1024 * 1024)} MB limit"
                )

            ext = _guess_audio_ext(enclosure_url, resp.headers.get("content-type"))
            file_path = os.path.join(workdir, f"{_safe_audio_stem(video_id)}.{ext}")
            total = 0
            too_large = False
            with open(file_path, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _RSS_MAX_AUDIO_BYTES:
                        too_large = True
                        break
                    f.write(chunk)
    except discovery.RSSFeedError as exc:
        if exc.status_code in (401, 403, 404, 410):
            raise EpisodeUnavailableError(str(exc)) from exc
        if exc.status_code == 429:
            raise EpisodeRateLimitedError(str(exc)) from exc
        raise RuntimeError(str(exc)) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"RSS enclosure request failed: {type(exc).__name__}") from exc

    if too_large:
        os.remove(file_path)
        raise RuntimeError(
            f"RSS enclosure for {video_id} exceeds the "
            f"{_RSS_MAX_AUDIO_BYTES // (1024 * 1024)} MB limit"
        )

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
        "--cache-dir", str(paths.yt_dlp_cache_dir()),
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
