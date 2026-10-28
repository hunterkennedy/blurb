"""Unit tests for RSS/Atom feed parsing and RSS download path handling.

Run from the blurb directory:

    python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discovery  # noqa: E402
import download  # noqa: E402


FEED_URL = "https://feeds.example.com/show.xml"


def _root(xml: str) -> ElementTree.Element:
    return ElementTree.fromstring(xml)


class RssParsingTests(unittest.TestCase):
    def test_rss_numeric_guid_and_relative_enclosure(self):
        root = _root("""
            <rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
              <channel>
                <item>
                  <title>Episode One</title>
                  <guid>12345</guid>
                  <pubDate>Tue, 27 Oct 2026 12:00:00 GMT</pubDate>
                  <enclosure url="/audio/ep1.mp3" length="123" type="audio/mpeg"/>
                  <itunes:duration>01:02:03</itunes:duration>
                </item>
              </channel>
            </rss>
        """)

        entry = discovery._rss_entry_from_item(discovery._rss_items(root)[0], FEED_URL)

        self.assertEqual(entry["video_id"], "12345")
        self.assertEqual(entry["title"], "Episode One")
        self.assertEqual(entry["pub_date"], "2026-10-27")
        self.assertEqual(entry["duration"], 3723.0)
        self.assertEqual(entry["enclosure_url"], "https://feeds.example.com/audio/ep1.mp3")

    def test_url_guid_is_preserved_and_filename_is_safe(self):
        root = _root("""
            <rss version="2.0"><channel><item>
              <title>URL GUID</title>
              <guid>https://example.com/posts/abcdef</guid>
              <enclosure url="https://cdn.example.com/abcdef.mp3" type="audio/mpeg"/>
            </item></channel></rss>
        """)

        entry = discovery._rss_entry_from_item(discovery._rss_items(root)[0], FEED_URL)

        self.assertEqual(entry["video_id"], "https://example.com/posts/abcdef")
        stem = download._safe_audio_stem(entry["video_id"])
        self.assertNotIn("/", stem)
        self.assertNotIn(":", stem)
        self.assertLessEqual(len(stem), 100)

    def test_missing_guid_uses_stable_hashed_id(self):
        root = _root("""
            <rss version="2.0"><channel><item>
              <title>No GUID</title>
              <enclosure url="https://cdn.example.com/private.mp3?token=secret" type="audio/mpeg"/>
            </item></channel></rss>
        """)

        item = discovery._rss_items(root)[0]
        first = discovery._rss_entry_from_item(item, FEED_URL)
        second = discovery._rss_entry_from_item(item, FEED_URL)

        self.assertTrue(first["video_id"].startswith("rss-"))
        self.assertEqual(first["video_id"], second["video_id"])
        self.assertNotIn("secret", first["video_id"])

    def test_atom_entry(self):
        root = _root("""
            <feed xmlns="http://www.w3.org/2005/Atom">
              <title>Atom Show</title>
              <logo>https://example.com/logo.png</logo>
              <entry>
                <id>tag:example.com,2026:ep-1</id>
                <title>Atom Episode</title>
                <published>2026-10-25T12:00:00Z</published>
                <link rel="enclosure" href="/audio/ep1.mp3" type="audio/mpeg"/>
              </entry>
            </feed>
        """)

        entries = discovery._rss_items(root)
        parsed = discovery._rss_entry_from_item(entries[0], FEED_URL)

        self.assertEqual(len(entries), 1)
        self.assertEqual(parsed["video_id"], "tag:example.com,2026:ep-1")
        self.assertEqual(parsed["pub_date"], "2026-10-25")
        self.assertEqual(parsed["enclosure_url"], "https://feeds.example.com/audio/ep1.mp3")
        self.assertEqual(discovery._rss_image_url(root), "https://example.com/logo.png")

    def test_rdf_item_uses_rdf_about_as_id(self):
        root = _root("""
            <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
                     xmlns="http://purl.org/rss/1.0/"
                     xmlns:dc="http://purl.org/dc/elements/1.1/">
              <channel rdf:about="https://example.com/feed">
                <title>RDF Show</title>
                <items><rdf:Seq><rdf:li rdf:resource="https://example.com/ep1"/></rdf:Seq></items>
              </channel>
              <item rdf:about="https://example.com/ep1">
                <title>RDF Episode</title>
                <dc:date>2026-10-24T12:00:00Z</dc:date>
                <enclosure url="https://cdn.example.com/ep1.mp3" type="audio/mpeg"/>
              </item>
            </rdf:RDF>
        """)

        parsed = discovery._rss_entry_from_item(discovery._rss_items(root)[0], FEED_URL)

        self.assertEqual(parsed["video_id"], "https://example.com/ep1")
        self.assertEqual(parsed["pub_date"], "2026-10-24")

    def test_media_rss_audio_fallback(self):
        root = _root("""
            <rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
              <channel><item>
                <title>Media RSS</title>
                <guid>m-1</guid>
                <media:content url="https://cdn.example.com/m1.mp3" type="audio/mpeg"/>
              </item></channel>
            </rss>
        """)

        parsed = discovery._rss_entry_from_item(discovery._rss_items(root)[0], FEED_URL)

        self.assertEqual(parsed["enclosure_url"], "https://cdn.example.com/m1.mp3")

    def test_items_without_audio_are_skipped(self):
        root = _root("""
            <rss version="2.0"><channel>
              <item><title>Text post</title><guid>t-1</guid></item>
            </channel></rss>
        """)

        self.assertIsNone(discovery._rss_entry_from_item(discovery._rss_items(root)[0], FEED_URL))

    def test_feed_root_detection(self):
        self.assertTrue(discovery._is_feed_root(_root("<rss version='2.0'><channel/></rss>")))
        self.assertTrue(discovery._is_feed_root(_root("<feed xmlns='http://www.w3.org/2005/Atom'/>")))
        self.assertFalse(discovery._is_feed_root(_root("<html><body>not a feed</body></html>")))


class RssDateAndDurationTests(unittest.TestCase):
    def test_rfc2822_date(self):
        self.assertEqual(discovery._parse_rss_pub_date("Tue, 27 Oct 2026 12:00:00 GMT"), "2026-10-27")

    def test_iso_date(self):
        self.assertEqual(discovery._parse_rss_pub_date("2026-10-27T12:00:00Z"), "2026-10-27")

    def test_bad_date(self):
        self.assertIsNone(discovery._parse_rss_pub_date("not a date"))

    def test_duration_formats(self):
        self.assertEqual(discovery._parse_itunes_duration("3723"), 3723.0)
        self.assertEqual(discovery._parse_itunes_duration("62:03"), 3723.0)
        self.assertEqual(discovery._parse_itunes_duration("1:02:03"), 3723.0)
        self.assertIsNone(discovery._parse_itunes_duration("bogus"))


class RssUrlValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_non_http_scheme(self):
        with self.assertRaises(discovery.RSSFeedError):
            await discovery.validate_rss_url("file:///etc/passwd")

    async def test_rejects_loopback_and_private_literals(self):
        for url in (
            "http://127.0.0.1/feed.xml",
            "http://localhost/feed.xml",
            "http://10.0.0.1/feed.xml",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/feed.xml",
        ):
            with self.assertRaises(discovery.RSSFeedError, msg=url):
                await discovery.validate_rss_url(url)

    async def test_allows_public_literal(self):
        await discovery.validate_rss_url("https://8.8.8.8/feed.xml")

    async def test_private_override(self):
        old = discovery._ALLOW_PRIVATE_RSS_URLS
        discovery._ALLOW_PRIVATE_RSS_URLS = True
        try:
            await discovery.validate_rss_url("http://127.0.0.1/feed.xml")
        finally:
            discovery._ALLOW_PRIVATE_RSS_URLS = old


class FilenameTests(unittest.TestCase):
    def test_numeric_and_uuid_ids_are_unchanged(self):
        self.assertEqual(download._safe_audio_stem("123456789"), "123456789")
        self.assertEqual(
            download._safe_audio_stem("9b361da8-ea35-4978-9344-a4e45f457bb0"),
            "9b361da8-ea35-4978-9344-a4e45f457bb0",
        )

    def test_url_and_path_like_ids_are_sanitized(self):
        for video_id in (
            "https://example.com/episodes/abcdef",
            "../../etc/passwd",
            "tag:example.com,2026:ep-1",
            "x" * 300,
        ):
            stem = download._safe_audio_stem(video_id)
            self.assertNotIn("/", stem)
            self.assertNotIn("\\", stem)
            self.assertNotIn("..", stem)
            self.assertLessEqual(len(stem), 100)

    def test_sanitized_ids_do_not_collide(self):
        self.assertNotEqual(
            download._safe_audio_stem("a/b"),
            download._safe_audio_stem("a_b"),
        )


if __name__ == "__main__":
    unittest.main()
