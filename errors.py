"""Shared download/process failure types.

Split out from download.py so test_mode.py can raise the same errors for
canned test-mode failures without importing download.py (which imports
discovery.py, which imports test_mode.py — a cycle).
"""


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
