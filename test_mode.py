"""Test mode — canned responses for discover/process jobs, no network calls.

Lets conductor exercise the pipeline (including things like orphan detection,
which needs entries to disappear between polls) without touching real YouTube
or Patreon data:

  - A discover job with url == "pal-test" or "pal-test-<scenario>" returns a
    premade entry list instead of scraping. Point a test source's url at
    different scenario suffixes to change what comes back on the next poll
    (e.g. "pal-test-shrink" drops an entry conductor already saw — orphan
    case; "pal-test-grow" adds a new one).
  - A process job whose video_id starts with "test-" returns a premade
    transcript (after a fixed fake-transcribing delay, so conductor's
    "processing" state isn't instant), or — for a handful of reserved ids —
    raises the same exception a real failed download would, so conductor's
    error handling can be tested deterministically.

Neither "pal-test" nor "test-*" can collide with real data: real source URLs
always contain a site domain, real YouTube ids are exactly 11 characters, and
real Patreon ids are purely numeric.

All fixture data lives in testdata/fixtures/ as plain JSON — hand-editable,
nothing generated at request time. testdata/generate.py (re)creates the
default set of fixtures.
"""

import json
import time
from pathlib import Path

from errors import (
    EpisodeAgeRestrictedError,
    EpisodeRateLimitedError,
    EpisodeTooShortError,
    EpisodeUnavailableError,
)

_FIXTURES_DIR = Path(__file__).parent / "testdata" / "fixtures"

_SOURCE_PREFIX = "pal-test"
_VIDEO_PREFIX = "test-"

# Real transcription isn't instant — sleep here so conductor's "processing"
# state/UI gets exercised the same way it would against a real job.
_FAKE_TRANSCRIBE_SECONDS = 30

# Reserved video_ids that raise a canned failure instead of returning a transcript.
_TEST_ERRORS = {
    "test-unavailable": EpisodeUnavailableError,
    "test-age-restricted": EpisodeAgeRestrictedError,
    "test-rate-limited": EpisodeRateLimitedError,
    "test-too-short": EpisodeTooShortError,
}


def is_test_source(url: str) -> bool:
    return url == _SOURCE_PREFIX or url.startswith(_SOURCE_PREFIX + "-")


def is_test_video(video_id: str) -> bool:
    return video_id.startswith(_VIDEO_PREFIX)


def _scenario_from_url(url: str) -> str:
    if url == _SOURCE_PREFIX:
        return "default"
    return url[len(_SOURCE_PREFIX) + 1:]  # "pal-test-shrink" -> "shrink"


def load_discovery(site: str, url: str) -> dict:
    """Return the {entries, icon} payload for a test discover job."""
    scenario = _scenario_from_url(url)
    path = _FIXTURES_DIR / f"discovery_{site}_{scenario}.json"
    if not path.is_file():
        path = _FIXTURES_DIR / f"discovery_{site}_default.json"
    return json.loads(path.read_text())


def load_transcript(video_id: str) -> dict:
    """Return the {transcript, publication_date} payload for a test process job.

    Raises one of the errors.Episode*Error classes for reserved failure ids.
    """
    if video_id in _TEST_ERRORS:
        raise _TEST_ERRORS[video_id](f"Canned test-mode failure for {video_id}")

    time.sleep(_FAKE_TRANSCRIBE_SECONDS)

    path = _FIXTURES_DIR / "transcripts" / f"{video_id}.json"
    if not path.is_file():
        path = _FIXTURES_DIR / "transcripts" / "default.json"
    return json.loads(path.read_text())
