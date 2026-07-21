"""Deterministically (re)generates the test-mode fixtures under testdata/fixtures/.

Nothing here is random — every value is a fixed function of its index, so
rerunning this script produces byte-identical output. That matters because
these fixtures are used to test things like orphan detection: the "shrink"/
"grow" scenarios must share the same video_ids as "default" for entries that
overlap, or conductor can't tell "this episode disappeared" from "this is an
unrelated episode".

The fixtures are meant to be hand-edited afterward — this script is just a
starting point / a way to regenerate from scratch if you want a bigger or
differently-shaped baseline set. Run it with:

    venv-linux/bin/python testdata/generate.py
"""

import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Arbitrary fixed anchor so pub_dates never depend on when this script runs.
_ANCHOR_DATE = (2024, 1, 7)  # a Sunday

_TOPICS = [
    "Season Opener", "Behind the Scenes", "Fan Mailbag", "Guest Interview",
    "Year in Review", "Deep Dive", "Live Q&A", "Bonus Round", "Rapid Fire",
    "Director's Commentary",
]

_TRANSCRIPT_WORDS = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
    "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
    "oscar", "papa", "quebec", "romeo", "sierra", "tango",
]


def _date_str(days_before_anchor: int) -> str:
    import datetime
    anchor = datetime.date(*_ANCHOR_DATE)
    return str(anchor - datetime.timedelta(days=days_before_anchor))


def _entries(prefix: str, count: int, with_duration: bool) -> list[dict]:
    entries = []
    for i in range(1, count + 1):
        entries.append({
            "video_id": f"test-{prefix}-{i:02d}",
            "title": f"Synthetic Episode {i:02d} — {_TOPICS[(i - 1) % len(_TOPICS)]}",
            "pub_date": _date_str(days_before_anchor=(i - 1) * 7),
            "duration": 1200 + i * 137 if with_duration else None,
        })
    return entries


# 1x1 transparent PNG — enough to exercise the icon field's shape without a real fetch.
_FAKE_ICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY"
    "42YAAAAASUVORK5CYII="
)


def _write(name: str, data: dict | list) -> None:
    path = FIXTURES_DIR / name
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"wrote {path}")


def _make_transcript(word_offset: int, n_words: int, words_per_segment: int, pub_date: str) -> dict:
    words = []
    for i in range(n_words):
        w = _TRANSCRIPT_WORDS[(i + word_offset) % len(_TRANSCRIPT_WORDS)]
        start = round(i * 0.4, 2)
        words.append({"start": start, "end": round(start + 0.35, 2), "word": f" {w}", "probability": round(0.80 + (i % 5) * 0.04, 2)})

    segments = []
    for chunk_start in range(0, len(words), words_per_segment):
        chunk = words[chunk_start:chunk_start + words_per_segment]
        segments.append({
            "start": chunk[0]["start"],
            "end": chunk[-1]["end"],
            "text": "".join(w["word"] for w in chunk),
            "words": chunk,
        })

    return {
        "transcript": {
            "text": "".join(w["word"] for w in words).strip(),
            "segments": segments,
            "language": "en",
        },
        "publication_date": pub_date,
    }


def main() -> None:
    (FIXTURES_DIR / "transcripts").mkdir(parents=True, exist_ok=True)

    yt_default = _entries("yt", count=8, with_duration=True)
    pat_default = _entries("pat", count=8, with_duration=False)

    _write("discovery_youtube_default.json", {"entries": yt_default, "icon": {
        "url": "https://example.com/fake-channel-icon.png",
        "content_type": "image/png",
        "bytes_b64": _FAKE_ICON_B64,
    }})
    _write("discovery_youtube_shrink.json", {"entries": yt_default[:-1], "icon": None})
    _write("discovery_youtube_grow.json", {
        "entries": yt_default + _entries("yt", count=9, with_duration=True)[-1:],
        "icon": None,
    })
    _write("discovery_youtube_empty.json", {"entries": [], "icon": None})

    _write("discovery_patreon_default.json", {"entries": pat_default, "icon": None})
    _write("discovery_patreon_shrink.json", {"entries": pat_default[:-1], "icon": None})
    _write("discovery_patreon_grow.json", {
        "entries": pat_default + _entries("pat", count=9, with_duration=False)[-1:],
        "icon": None,
    })
    _write("discovery_patreon_empty.json", {"entries": [], "icon": None})

    default_transcript = _make_transcript(word_offset=0, n_words=60, words_per_segment=15, pub_date=_date_str(0))
    _write("transcripts/default.json", default_transcript)
    _write("transcripts/test-ok-1.json", default_transcript)
    _write("transcripts/test-ok-2.json", _make_transcript(word_offset=7, n_words=120, words_per_segment=20, pub_date=_date_str(7)))


if __name__ == "__main__":
    main()
