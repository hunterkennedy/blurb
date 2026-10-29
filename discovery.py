"""
Discovery scraping — runs entirely on blurb, using cookies that live only on
this machine's local disk. Talks to YouTube (via yt-dlp) and Patreon (via its
JSON:API) directly; conductor only ever receives the resulting entries, never
the credentials used to get them.

RSS sources are different: there's no separate credential at all — any auth
needed (e.g. a Patreon RSS feed's "?auth=..." token) is embedded directly in
the feed URL, which conductor already stores and hands back to us verbatim,
same as a YouTube channel URL. Nothing local to configure for it.

Entries are returned unfiltered (title_exclude, unavailable-title skipping,
duration-based blacklisting) — conductor owns all of that business logic
since it already has the source's filter config. Blurb is a dumb scraper.
"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree  # nosec B405 - type hints only; parsing uses defusedxml

import httpx
from defusedxml.ElementTree import fromstring as safe_fromstring
from defusedxml.common import DefusedXmlException

import paths
import test_mode

logger = logging.getLogger(__name__)

_PATREON_AUDIO_POST_TYPES = {"podcast", "audio_file"}
_PATREON_API_URL = "https://www.patreon.com/api/posts"
_PATREON_COLLECTION_RE = re.compile(r"patreon\.com/collection/(\d+)")

_ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
_RDF_NS = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
_RSS_REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; palpal/1.0)"}
# A feed larger than this is almost certainly malicious or broken. The cap is
# applied to the decompressed stream, so a gzip bomb can't get around it.
_RSS_MAX_FEED_BYTES = int(os.environ.get("RSS_MAX_FEED_BYTES", str(25 * 1024 * 1024)))
_RSS_MAX_ICON_BYTES = int(os.environ.get("RSS_MAX_ICON_MB", "5")) * 1024 * 1024
# Keep SVG and other scriptable image formats out of the stored channel icon,
# which conductor serves back on a public endpoint.
_RSS_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
# RSS feeds and enclosures are fetched from the worker's network. Block
# private/loopback destinations by default (blind SSRF), with an explicit
# escape hatch for local/self-hosted feeds.
_ALLOW_PRIVATE_RSS_URLS = os.environ.get("ALLOW_PRIVATE_RSS_URLS", "").strip().lower() in ("1", "true", "yes")
_RSS_MAX_REDIRECTS = 5


def _is_global_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # is_global is false for loopback, RFC1918, link-local, CGNAT, ULA,
    # multicast, unspecified, and documentation ranges.
    return ip.is_global


async def validate_rss_url(url: str, what: str = "RSS URL") -> None:
    """Reject non-HTTP(S) and private/loopback RSS destinations.

    Call this before every request and before every redirect hop. Set
    ALLOW_PRIVATE_RSS_URLS=true when deliberately using a feed on localhost or
    a LAN host (e.g. a local test feed).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise RSSFeedError(f"{what} must use http or https")

    host = parsed.hostname
    if not host:
        raise RSSFeedError(f"{what} is missing a hostname")
    if _ALLOW_PRIVATE_RSS_URLS:
        return

    try:
        if not _is_global_ip(ipaddress.ip_address(host)):
            raise RSSFeedError(f"{what} points to a private/loopback address")
        return
    except ValueError:
        pass

    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise RSSFeedError(f"{what} points to a loopback address")

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError:
        # Let the actual request surface DNS failures with its normal error.
        return

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not _is_global_ip(ip):
            raise RSSFeedError(f"{what} resolves to a private/loopback address")


async def _stream_with_redirects(
    client: httpx.AsyncClient, url: str, *, headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Open a streaming GET response, validating every redirect hop.

    httpx's follow_redirects would happily follow a public enclosure URL to
    http://127.0.0.1; validating only the initial URL isn't enough.
    """
    for _ in range(_RSS_MAX_REDIRECTS + 1):
        await validate_rss_url(url)
        request = client.build_request("GET", url, headers=headers)
        response = await client.send(request, stream=True, follow_redirects=False)
        if not response.is_redirect:
            return response

        location = response.headers.get("location")
        await response.aclose()
        if not location:
            raise RSSFeedError("RSS redirect did not include a Location header")
        url = urljoin(str(response.url), location)

    raise RSSFeedError(f"RSS URL redirected more than {_RSS_MAX_REDIRECTS} times")


@asynccontextmanager
async def rss_stream(
    url: str, *, headers: dict[str, str] | None = None, timeout: float = 30,
):
    """Async context manager yielding a validated streaming HTTP response."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await _stream_with_redirects(client, url, headers=headers)
        try:
            yield response
        finally:
            await response.aclose()


class RSSFeedError(RuntimeError):
    """A feed could not be fetched/parsed. Kept separate so download.py can
    map HTTP auth/rate-limit responses to the right episode outcome."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def yt_dlp_path() -> str:
    """Return the yt-dlp executable path.

    In the AppImage, yt-dlp runs inside the frozen Blurb executable via a
    small wrapper next to it (see packaging/appimage/AppRun), because there is
    no separate Python interpreter to run the console script with.
    """
    override = os.environ.get("BLURB_YTDLP")
    if override and os.path.isfile(override):
        return override

    if getattr(sys, "frozen", False):
        wrapper = os.path.join(os.path.dirname(sys.executable), "blurb-yt-dlp")
        if os.path.isfile(wrapper):
            return wrapper

    found = shutil.which("yt-dlp")
    if found:
        return found
    venv_bin = os.path.join(os.path.dirname(sys.executable), "yt-dlp")
    if os.path.isfile(venv_bin):
        return venv_bin
    raise FileNotFoundError("yt-dlp not found in PATH, venv, or AppImage bundle")


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
    cmd = [yt_dlp_path(), "--no-warnings", "--cache-dir", str(paths.yt_dlp_cache_dir())]
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


async def fetch_patreon_post_duration(video_id: str) -> float | None:
    """
    Probe a single Patreon post's duration via yt-dlp metadata — no audio is
    downloaded. Used to backfill duration on episodes discovered before we
    started capturing it at download time (see download_audio).
    """
    cookie_file = cookie_file_for("patreon")
    url = f"https://www.patreon.com/posts/{video_id}"
    info = await _run_yt_dlp_json(url, cookie_file, flat_playlist=False, timeout=60)
    if not isinstance(info, dict):
        return None
    return info.get("duration")


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


# --------------------------------------------------------------------------- #
# RSS — generic podcast feeds, e.g. a Patreon RSS export or any other private #
# feed URL with the auth baked into the query string.                        #
# --------------------------------------------------------------------------- #

def _parse_itunes_duration(raw: str | None) -> float | None:
    """Parse an <itunes:duration> value — either plain seconds or H:MM:SS/MM:SS."""
    if not raw or not raw.strip():
        return None
    parts = raw.strip().split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    return None


def _parse_rss_pub_date(raw: str | None) -> str | None:
    """Parse an RFC 2822 <pubDate> or an ISO-8601 Atom/RSS 1.0 date into 'YYYY-MM-DD'."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _local_name(tag: str) -> str:
    """Tag name without its XML namespace, lowercased."""
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _first_child(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    name = name.lower()
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    child = _first_child(element, name)
    if child is None:
        return None
    text = "".join(child.itertext()).strip()
    return text or None


def _stable_rss_id(enclosure_url: str) -> str:
    """Stable ID for feeds whose items have no guid/id. Hash the enclosure URL
    rather than storing it directly — enclosure URLs often contain the same
    private token as the feed URL, and episode IDs are exposed by the public API."""
    return "rss-" + hashlib.sha256(enclosure_url.encode("utf-8")).hexdigest()[:32]


async def _fetch_rss_feed(url: str) -> ElementTree.Element:
    try:
        async with rss_stream(url, headers=_RSS_REQUEST_HEADERS, timeout=30) as resp:
            if resp.status_code >= 400:
                raise RSSFeedError(
                    f"RSS feed request failed with HTTP {resp.status_code}",
                    status_code=resp.status_code,
                )

            content_length = resp.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > _RSS_MAX_FEED_BYTES:
                raise RSSFeedError(
                    f"RSS feed is larger than the {_RSS_MAX_FEED_BYTES // (1024 * 1024)} MB limit"
                )

            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _RSS_MAX_FEED_BYTES:
                    raise RSSFeedError(
                        f"RSS feed is larger than the {_RSS_MAX_FEED_BYTES // (1024 * 1024)} MB limit"
                    )
                chunks.append(chunk)
            content = b"".join(chunks)
    except httpx.RequestError as exc:
        # Deliberately omit the URL here: RSS feed URLs can carry private auth
        # tokens (e.g. Patreon's ?auth=...) and this message is shown in the
        # admin panel and stored on the job row.
        raise RSSFeedError(f"RSS feed request failed: {type(exc).__name__}") from None

    try:
        # defusedxml blocks entity expansion / external-entity attacks that
        # stdlib ElementTree is documented as vulnerable to.
        return safe_fromstring(content)
    except (ElementTree.ParseError, DefusedXmlException, ValueError) as exc:
        raise RSSFeedError(
            f"RSS feed is not valid XML ({exc}); the URL may be an HTML login/error page"
        ) from None


def _is_feed_root(root: ElementTree.Element) -> bool:
    # RSS 2.0 <rss>, Atom <feed>, and RSS 1.0/RDF <rdf:RDF> all use one of these.
    return _local_name(root.tag) in ("rss", "feed", "rdf")


def _rss_items(root: ElementTree.Element) -> list[ElementTree.Element]:
    """Return RSS <item> or Atom <entry> elements, regardless of namespace."""
    if _local_name(root.tag) == "feed":
        return [child for child in root if _local_name(child.tag) == "entry"]

    channel = _first_child(root, "channel")
    scope = channel if channel is not None else root
    items = [child for child in scope if _local_name(child.tag) == "item"]
    if items:
        return items

    # RSS 1.0 (RDF) and namespace-heavy feeds: fall back to a recursive scan.
    return [el for el in root.iter() if _local_name(el.tag) in ("item", "entry")]


def _rss_enclosure_url(item: ElementTree.Element, feed_url: str) -> str | None:
    enclosure = _first_child(item, "enclosure")
    url = enclosure.get("url") if enclosure is not None else None

    if not url:
        # Atom: <link rel="enclosure" href="..."/>
        for child in item:
            if _local_name(child.tag) == "link" and (child.get("rel") or "").lower() == "enclosure":
                url = child.get("href")
                if url:
                    break

    if not url:
        # Some feeds put the audio in Media RSS instead of <enclosure>.
        media = _first_child(item, "content")
        if media is not None and media.get("url"):
            media_type = (media.get("type") or "").lower()
            if media_type.startswith("audio/") or (media.get("medium") or "").lower() == "audio":
                url = media.get("url")

    # Relative enclosures are legal and do appear in the wild.
    return urljoin(feed_url, url) if url else None


def _rss_entry_from_item(item: ElementTree.Element, feed_url: str) -> dict | None:
    """Parse one item/entry into {video_id, title, pub_date, duration, enclosure_url}.
    None if the item has no audio enclosure (nothing to download)."""
    enclosure_url = _rss_enclosure_url(item, feed_url)
    if not enclosure_url:
        return None

    # RSS 2.0 <guid>, Atom <id>, or RSS 1.0's rdf:about attribute.
    guid = (
        _child_text(item, "guid")
        or _child_text(item, "id")
        or item.get(f"{_RDF_NS}about")
    )
    video_id = guid.strip() if guid else _stable_rss_id(enclosure_url)
    if not video_id:
        return None

    title = _child_text(item, "title") or ""

    pub_date = _parse_rss_pub_date(
        _child_text(item, "pubdate") or _child_text(item, "published") or _child_text(item, "date")
    )

    # itunes:duration is the normal case; RSS 1.0 feeds sometimes use a plain
    # <duration> in a different namespace.
    duration_el = item.find(f"{_ITUNES_NS}duration")
    duration_raw = duration_el.text if duration_el is not None else _child_text(item, "duration")
    duration = _parse_itunes_duration(duration_raw)

    return {
        "video_id": video_id,
        "title": title,
        "pub_date": pub_date,
        "duration": duration,
        "enclosure_url": enclosure_url,
    }


def _rss_image_url(root: ElementTree.Element, feed_url: str | None = None) -> str | None:
    channel = _first_child(root, "channel")
    scope = channel if channel is not None else root
    image_url = None

    # <itunes:image href="..."/> (RSS) — and the same element name appears in
    # some feeds as a plain <image href="..."/>.
    for child in scope:
        if _local_name(child.tag) == "image":
            if child.get("href"):
                image_url = child.get("href")
                break

    # RSS <image><url>...</url></image>
    if not image_url:
        image_el = _first_child(scope, "image")
        if image_el is not None:
            url_el = _first_child(image_el, "url")
            if url_el is not None and url_el.text:
                image_url = url_el.text.strip()

    # Atom <logo> / <icon>
    if not image_url:
        for name in ("logo", "icon"):
            text = _child_text(scope, name)
            if text:
                image_url = text
                break

    return urljoin(feed_url, image_url) if feed_url and image_url else image_url


async def _fetch_rss_icon(root: ElementTree.Element, feed_url: str | None = None) -> dict | None:
    """Fetch the channel/podcast artwork. Returns {url, content_type, bytes_b64} or None."""
    image_url = _rss_image_url(root, feed_url)
    if not image_url:
        return None

    try:
        async with rss_stream(image_url, headers=_RSS_REQUEST_HEADERS, timeout=30) as resp:
            if resp.status_code != 200:
                return None

            content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip().lower()
            if content_type not in _RSS_IMAGE_CONTENT_TYPES:
                logger.warning(f"fetch_rss_icon skipped unsupported content type: {content_type}")
                return None

            content_length = resp.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > _RSS_MAX_ICON_BYTES:
                logger.warning("fetch_rss_icon skipped oversized image")
                return None

            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _RSS_MAX_ICON_BYTES:
                    logger.warning("fetch_rss_icon skipped oversized image")
                    return None
                chunks.append(chunk)
            content = b"".join(chunks)
    except Exception as exc:
        logger.warning(f"fetch_rss_icon failed: {type(exc).__name__}")
        return None

    return {
        "url": image_url,
        "content_type": content_type,
        "bytes_b64": base64.b64encode(content).decode("ascii"),
    }


async def discover_rss_source(url: str) -> tuple[list[dict], dict | None]:
    """Scrape a podcast RSS/Atom feed directly — no cookies, no yt-dlp. Any
    auth needed is already part of `url` (e.g. a Patreon RSS feed's token)."""
    root = await _fetch_rss_feed(url)
    if not _is_feed_root(root):
        raise RSSFeedError(
            f"URL did not return an RSS/Atom feed (root element <{_local_name(root.tag)}>)"
        )

    items = _rss_items(root)
    entries: list[dict] = []
    no_enclosure = 0
    for item in items:
        parsed = _rss_entry_from_item(item, url)
        if not parsed:
            no_enclosure += 1
            continue
        entries.append({
            "video_id": parsed["video_id"],
            "title": parsed["title"],
            "pub_date": parsed["pub_date"],
            "duration": parsed["duration"],
        })

    logger.info(
        f"Discovered {len(entries)} entries from RSS feed ({len(items)} items"
        + (f", {no_enclosure} without audio enclosures" if no_enclosure else "")
        + ")"
    )

    icon = await _fetch_rss_icon(root, url)
    return entries, icon


async def find_rss_enclosure(feed_url: str, video_id: str) -> str | None:
    """Re-fetch a feed and return the enclosure (audio) URL for one item, by
    the same video_id discovery reported. Used at download time so we don't
    need to persist a separate per-episode audio URL anywhere."""
    root = await _fetch_rss_feed(feed_url)
    for item in _rss_items(root):
        parsed = _rss_entry_from_item(item, feed_url)
        if parsed and parsed["video_id"] == video_id:
            return parsed["enclosure_url"]
    return None


async def discover(site: str, url: str) -> dict:
    """Entry point for a 'discover' job. Returns the job result payload."""
    if test_mode.is_test_source(url):
        return test_mode.load_discovery(site, url)
    if site == "patreon":
        entries, icon = await discover_patreon_source(url)
    elif site == "rss":
        entries, icon = await discover_rss_source(url)
    else:
        entries, icon = await discover_youtube_source(url)
    return {"entries": entries, "icon": icon}
