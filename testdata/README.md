# Test mode

Lets conductor exercise discover/process jobs against blurb without hitting
real YouTube/Patreon or running yt-dlp/Whisper. Useful for testing conductor's
state machine — orphan detection, new-episode detection, download failure
handling — against known, repeatable data.

Logic lives in `test_mode.py` at the repo root; this folder only holds the
JSON fixtures it reads.

## Discover jobs

Point a source's `url` at `pal-test` (or `pal-test-<scenario>`) instead of a
real channel/collection URL. `site` stays `youtube` or `patreon` as normal —
that's what picks which fixture file loads.

| url | scenario | what you get |
|---|---|---|
| `pal-test` | `default` | 8 entries (`test-yt-01`..`08` / `test-pat-01`..`08`) |
| `pal-test-shrink` | `shrink` | same 8 minus the last one — poll this after `default` to trigger orphan detection |
| `pal-test-grow` | `grow` | the 8 plus a 9th (`test-yt-09` / `test-pat-09`) — poll this after `default` to trigger new-episode detection |
| `pal-test-empty` | `empty` | zero entries |

Scenario ids are shared across variants on purpose (`shrink` and `grow` both
build on the same `default` ids) — that's what lets conductor tell "this
episode disappeared" apart from "this is an unrelated episode". If you add a
new scenario, keep doing this: derive it from `default`'s entries rather than
inventing fresh ids.

To simulate a source changing over time, just edit the test source's `url` in
conductor between polls (e.g. `pal-test` → `pal-test-shrink`).

## Process jobs

Send a `process` job with a `video_id` starting with `test-` (any id from a
discover fixture works, e.g. `test-yt-01`) to get back a premade transcript
instead of a real download+transcription. Blurb sleeps for ~30s first so
conductor's "processing" state isn't instant.

A few ids are reserved to trigger a canned failure instead, exercising
conductor's `error_type` handling:

| video_id | error_type |
|---|---|
| `test-unavailable` | `unavailable` |
| `test-age-restricted` | `age_restricted` |
| `test-rate-limited` | `rate_limited` |
| `test-too-short` | `too_short` |

Any other `test-*` id (including ones straight out of a discover fixture)
falls back to `fixtures/transcripts/default.json`. Add
`fixtures/transcripts/<video_id>.json` if you want a specific episode to
return its own transcript instead.

## Fixtures

Everything under `fixtures/` is plain, hand-editable JSON — the exact shape
blurb posts back to conductor for a real job. Edit these directly for
one-off tweaks (change a title, add an entry, shorten a transcript).

```
fixtures/
  discovery_<site>_<scenario>.json   # {entries, icon}
  transcripts/<video_id>.json        # {transcript, publication_date}
```

To regenerate the whole default set from scratch:

```bash
venv-linux/bin/python testdata/generate.py
```

`generate.py` is fully deterministic (no `random` — every value is a fixed
function of its index), so reruns produce byte-identical output. Edit it if
you want a bigger baseline set or different scenario shapes; it's a starting
point, not something that needs to run again for normal use.
