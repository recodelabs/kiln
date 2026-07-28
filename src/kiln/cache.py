"""Content-addressed disk cache for fetched boundary attachments.

Admin boundaries change at most once a year; a country registry can carry
tens of thousands of externally-referenced ones. Caching the fetched bytes
by URL means every run after the first is near-instant, and gives
resumability as a side effect -- a run killed halfway does not re-fetch
what it already has.

Each cache entry is two files, both named after a sha256 hex digest of the
URL so an arbitrary URL always maps to a filesystem-safe name:

    <hash>.bin         the raw fetched bytes, exactly as received
    <hash>.meta.json   {"url": ..., "fetched_at": "<UTC ISO8601>", "sha256": "<hex digest of .bin>"}

The metadata sidecar exists so the cache directory is not a dead end for
debugging: a directory of bare hex-named files gives no way to tell what a
stale or wrong-looking entry came from. Given a suspicious boundary, `grep`
the `.meta.json` files for its URL to find the entry and see when it was
fetched. `sha256` isn't just for debugging, though -- it's checked on every
read (see `cache_read`) to catch a truncated or bit-flipped `.bin` file
before it's fed into the pipeline as if it were good data.

Deliberately NOT stored: response `Content-Type`. Storing it would mean
threading the `httpx.Response` (or at least its headers) through the
worker-thread return value into this module, for a field that's already
recoverable another way -- the resolved attachment's own `contentType` is
written into the output NDJSON right next to the base64 data, so it's not
lost, just not duplicated here.

Both files are written with a temp-file-then-`os.replace`, the same pattern
`write.py` uses to publish a partition (see `write.py::_finalize`): the
fetch path runs on a pool of worker threads, and two of them can race to
fill the same cache entry for the same URL. `os.replace` is atomic on both
POSIX and Windows, so a reader never observes a partially-written file. The
write order -- `.bin` before `.meta.json` -- makes the metadata file's
existence itself the commit marker: a reader that finds a `.meta.json`
alongside a `.bin` is guaranteed the `.bin` it names was fully written,
never a half-finished one from a write still in progress.

Nothing in this module raises. A cache is an optimization, not a
correctness requirement: an unwritable directory, a corrupt or truncated
entry, or any other OSError degrades to "no cache" for that entry (or
silently drops the write it can't make), reported back to the caller as an
error string rather than raised -- a full disk must not fail someone's
export. Callers (`extract.py`) are responsible for turning that string into
a `Report` entry; this module never touches `Report` itself; it runs on
worker threads, and `Report` is only ever mutated from the main thread (see
`extract.py::_resolve_boundary_work`) so report ordering stays
deterministic across runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "kiln" / "boundaries"


def cache_key(url: str) -> str:
    """sha256 hex digest of `url`, used as the cache entry's filename stem."""
    return hashlib.sha256(url.encode()).hexdigest()


def _entry_paths(cache_dir: Path, url: str) -> tuple[Path, Path]:
    key = cache_key(url)
    return cache_dir / f"{key}.bin", cache_dir / f"{key}.meta.json"


def cache_read(cache_dir: Path, url: str) -> tuple[bytes | None, str | None]:
    """Return `(payload, error)` for `url`.

    A plain miss (no entry yet) is `(None, None)` -- not reported, this is
    the expected, common case for a first run. Anything else that keeps
    this from being a clean hit -- unreadable metadata, metadata that
    doesn't parse, a `.bin` file whose content doesn't match the recorded
    sha256 (truncated or corrupted), or an OSError reading either file --
    is also treated as a miss (`payload is None`, so the caller re-fetches)
    but comes back with `error` set so the caller can report it.
    """
    bin_path, meta_path = _entry_paths(cache_dir, url)

    try:
        meta_raw = meta_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"could not read cache metadata for {url}: {exc}"

    try:
        meta = json.loads(meta_raw)
        expected_sha = meta["sha256"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return None, f"cache metadata for {url} is corrupt: {exc}"

    try:
        payload = bin_path.read_bytes()
    except OSError as exc:
        return None, f"could not read cache entry for {url}: {exc}"

    if hashlib.sha256(payload).hexdigest() != expected_sha:
        return None, f"cache entry for {url} failed its integrity check; re-fetching"

    return payload, None


def cache_write(cache_dir: Path, url: str, payload: bytes) -> str | None:
    """Write `payload` for `url`, best-effort. Returns an error string, or
    None on success.

    Callers must only call this after a successful fetch -- never cache a
    failure, since a 404 today may be a working URL tomorrow, and caching
    the failure would make a transient outage permanent.
    """
    bin_path, meta_path = _entry_paths(cache_dir, url)
    sha = hashlib.sha256(payload).hexdigest()
    meta = json.dumps(
        {
            "url": url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "sha256": sha,
        }
    ).encode("utf-8")

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(bin_path, payload)
        # Written last: its existence is the commit marker a reader relies
        # on to know the .bin next to it is complete (see module docstring).
        _atomic_write(meta_path, meta)
    except OSError as exc:
        return f"could not write cache entry for {url}: {exc}"

    return None


def _atomic_write(path: Path, data: bytes) -> None:
    """Write `data` to `path` via a same-directory temp file + `os.replace`.

    Same pattern as `write.py::_finalize`: the temp file lives in `path`'s
    own directory (so the final `os.replace` is guaranteed same-filesystem,
    hence atomic) and is only renamed into place once it holds the
    complete, correct content -- `path` itself is never opened for writing
    directly, which would let a concurrent reader (another worker thread
    racing on the same URL) observe a partial file.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
