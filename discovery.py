"""
Discovery scraping — runs entirely on blurb, using cookies that live only on
this machine's local disk. Talks to YouTube (via yt-dlp) and Patreon (via its
JSON:API) directly; conductor only ever receives the resulting entries, never
the credentials used to get them.

Entries are returned unfiltered (title_exclude, unavailable-title skipping,
duration-based blacklisting) — conductor owns all of that business logic
since it already has the source's filter config. Blurb is a dumb scraper.
"""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import sys

import httpx

logger = logging.getLogger(__name__)

_PATREON_AUDIO_POST_TYPES = {"podcast", "audio_file"}
_PATREON_API_URL = "https://www.patreon.com/api/posts"
_PATREON_COLLECTION_RE = re.compile(r"patreon\.com/collection/(\d+)")


def yt_dlp_path() -> str:
    """Return the yt-dlp executable path, checking PATH then the active venv."""
    found = shutil.which("yt-dlp")
    if found:
        return found
    venv_bin = os.path.join(os.path.dirname(sys.executable), "yt-dlp")
    if os.path.isfile(venv_bin):
        return venv_bin
    raise FileNotFoundError("yt-dlp not found in PATH or venv")


def cookie_file_for(site: str) -> str | None:
    """Path to the local Netscape-format cookies.txt for a site, if configured."""
    env_key = "YOUTUBE_COOKIES_FILE" if site == "youtube" else "PATREON_COOKIES_FILE"
    path = os.environ.get(env_key, "").strip()
    if path and os.path.isfile(path):
        return path
    return None


def _read_patreon_session_cookie() -> str:
    """Extract session_id from the local Patreon cookies.txt (Netscape format)."""
    path = cookie_file_for("patreon")
    if not path:
        return ""
    with open(path) as f:
        cookies_txt = f.read()
    for line in cookies_txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        if "patreon.com" in parts[0] and parts[5] == "session_id":
            return parts[6]
    return ""


async def _run_yt_dlp_json(url: str, cookie_file: str | None, flat_playlist: bool, timeout: int) -> dict | list[dict]:
    cmd = [yt_dlp_path(), "--no-warnings", "--cache-dir", "/tmp/yt-dlp-cache"]
    if flat_playlist:
        cmd += ["--flat-playlist", "--dump-json"]
    else:
        cmd += ["--dump-single-json", "--flat-playlist", "-I", ":0"]
    if cookie_file:
        cmd += ["--cookies", cookie_file]
    cmd.append(url)

    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"yt-dlp timed out after {timeout}s for {url}")
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed for {url}: {stderr_bytes.decode()[:500]}")

    text = stdout_bytes.decode()
    if flat_playlist and "--dump-json" in cmd:
        entries = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
    return json.loads(text)


async def discover_youtube_source(url: str) -> tuple[list[dict], dict | None]:
    """
    Scrape a YouTube channel/playlist with yt-dlp --flat-playlist.
    Returns (entries, icon). Entries are unfiltered — includes unavailable
    ("[Private video]"-titled) items so conductor can still track them for
    orphan detection without deleting real episode history.
    """
    cookie_file = cookie_file_for("youtube")

    raw_entries = await _run_yt_dlp_json(url, cookie_file, flat_playlist=True, timeout=300)

    entries: list[dict] = []
    for entry in raw_entries:
        video_id = entry.get("id")
        if not video_id:
            continue
        upload_date = entry.get("upload_date")  # "YYYYMMDD" or None
        pub_date = None
        if upload_date and len(upload_date) == 8:
            pub_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
        entries.append({
            "video_id": video_id,
            "title": entry.get("title") or "",
            "pub_date": pub_date,
            "duration": entry.get("duration"),
        })

    logger.info(f"Discovered {len(entries)} entries from {url}")

    icon = await _fetch_channel_icon(url, cookie_file)
    return entries, icon


async def _fetch_channel_icon(source_url: str, cookie_file: str | None) -> dict | None:
    """Fetch the channel avatar via yt-dlp metadata. Returns {url, content_type, bytes_b64} or None."""
    try:
        info = await _run_yt_dlp_json(source_url, cookie_file, flat_playlist=False, timeout=60)
        if not isinstance(info, dict) or info.get("_type") == "video":
            return None

        thumbnails: list[dict] = info.get("thumbnails") or []
        avatar = next((t for t in thumbnails if t.get("id") == "avatar_uncropped"), None)

        if not avatar:
            channel_url = info.get("channel_url") or info.get("uploader_url")
            if channel_url and channel_url != source_url:
                info2 = await _run_yt_dlp_json(channel_url, cookie_file, flat_playlist=False, timeout=60)
                if isinstance(info2, dict):
                    thumbnails2 = info2.get("thumbnails") or []
                    avatar = next((t for t in thumbnails2 if t.get("id") == "avatar_uncropped"), None)
                    if not avatar:
                        avatar = max(
                            (t for t in thumbnails2 if t.get("url")),
                            key=lambda t: t.get("preference", 0), default=None,
                        )

        if not avatar:
            avatar = max((t for t in thumbnails if t.get("url")), key=lambda t: t.get("preference", 0), default=None)
        if not avatar:
            return None

        thumbnail_url = avatar["url"]
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(thumbnail_url)
        if resp.status_code != 200:
            return None

        content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        return {
            "url": thumbnail_url,
            "content_type": content_type,
            "bytes_b64": base64.b64encode(resp.content).decode("ascii"),
        }
    except Exception as exc:
        logger.warning(f"fetch_channel_icon failed for {source_url}: {exc}")
        return None


async def _fetch_patreon_campaign_id(url: str, session_cookie: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; palpal/1.0)"}
    if session_cookie:
        headers["Cookie"] = f"session_id={session_cookie}"

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
    resp.raise_for_status()
    html = resp.text

    m = re.search(r'"campaign"\s*:\s*\{\s*"data"\s*:\s*\{\s*"id"\s*:\s*"(\d+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'/campaign/(\d+)/', html)
    if m:
        return m.group(1)
    m = re.search(r'"NavigationBar_(\d+)"', html)
    if m:
        return m.group(1)

    raise RuntimeError(f"Could not extract campaign_id from Patreon page: {url}")


async def discover_patreon_source(url: str) -> tuple[list[dict], None]:
    """Scrape a Patreon collection/campaign via the JSON:API. No icon fetch for Patreon."""
    session_cookie = _read_patreon_session_cookie()

    collection_match = _PATREON_COLLECTION_RE.search(url)
    collection_id = collection_match.group(1) if collection_match else None

    campaign_id = await _fetch_patreon_campaign_id(url, session_cookie)
    logger.info(f"Patreon source {url}: campaign_id={campaign_id}, collection_id={collection_id}")

    headers = {"User-Agent": "Mozilla/5.0 (compatible; palpal/1.0)"}
    if session_cookie:
        headers["Cookie"] = f"session_id={session_cookie}"

    params: dict = {
        "filter[campaign_id]": campaign_id,
        "filter[contains_exclusive_posts]": "true",
        "filter[is_draft]": "false",
        "sort": "collection_order" if collection_id else "-published_at",
        "fields[post]": "title,published_at,post_type",
        "json-api-version": "1.0",
        "page[count]": "50",
    }
    if collection_id:
        params["filter[collection_id]"] = collection_id

    entries: list[dict] = []
    cursor: str | None = None

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            if cursor:
                params["page[cursor]"] = cursor

            resp = await client.get(_PATREON_API_URL, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            for post in data.get("data", []):
                post_id = post.get("id")
                attrs = post.get("attributes", {})
                post_type = attrs.get("post_type") or ""
                if not post_id or post_type not in _PATREON_AUDIO_POST_TYPES:
                    continue

                published_at = attrs.get("published_at")  # ISO 8601 string
                pub_date = published_at[:10] if published_at else None

                entries.append({
                    "video_id": post_id,
                    "title": attrs.get("title") or "",
                    "pub_date": pub_date,
                    "duration": None,
                })

            cursor = data.get("meta", {}).get("pagination", {}).get("cursors", {}).get("next")
            if not cursor:
                break

    logger.info(f"Patreon source {url}: {len(entries)} audio entries found")
    return entries, None


async def discover(site: str, url: str) -> dict:
    """Entry point for a 'discover' job. Returns the job result payload."""
    if site == "patreon":
        entries, icon = await discover_patreon_source(url)
    else:
        entries, icon = await discover_youtube_source(url)
    return {"entries": entries, "icon": icon}
