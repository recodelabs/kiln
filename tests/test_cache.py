import json
from pathlib import Path

import kiln.cache as cache_module
from kiln.cache import DEFAULT_CACHE_DIR, cache_key, cache_read, cache_write


def test_cache_key_is_a_stable_sha256_hex_digest():
    key = cache_key("https://files.test/a.geojson")

    assert key == cache_key("https://files.test/a.geojson")
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


def test_cache_key_differs_for_different_urls():
    assert cache_key("https://files.test/a.geojson") != cache_key("https://files.test/b.geojson")


def test_default_cache_dir_is_under_the_users_cache_home():
    assert DEFAULT_CACHE_DIR.parts[-3:] == (".cache", "kiln", "boundaries")


def test_write_then_read_round_trips_the_payload(tmp_path):
    url = "https://files.test/a.geojson"
    payload = b'{"type":"Polygon"}'

    error = cache_write(tmp_path, url, payload)
    read_payload, read_error = cache_read(tmp_path, url)

    assert error is None
    assert read_error is None
    assert read_payload == payload


def test_reading_a_missing_entry_is_a_silent_miss(tmp_path):
    payload, error = cache_read(tmp_path, "https://files.test/never-fetched.geojson")

    assert payload is None
    assert error is None


def test_metadata_sidecar_records_url_and_timestamp_and_hash(tmp_path):
    url = "https://files.test/a.geojson"
    payload = b"hello world"

    cache_write(tmp_path, url, payload)

    key = cache_key(url)
    meta = json.loads((tmp_path / f"{key}.meta.json").read_text())

    assert meta["url"] == url
    assert "fetched_at" in meta
    import hashlib

    assert meta["sha256"] == hashlib.sha256(payload).hexdigest()


def test_corrupt_metadata_json_is_reported_and_treated_as_a_miss(tmp_path):
    url = "https://files.test/a.geojson"
    cache_write(tmp_path, url, b"good bytes")
    meta_path = tmp_path / f"{cache_key(url)}.meta.json"
    meta_path.write_text("not json at all {{{")

    payload, error = cache_read(tmp_path, url)

    assert payload is None
    assert error is not None
    assert "corrupt" in error


def test_truncated_bin_file_fails_integrity_check_and_is_treated_as_a_miss(tmp_path):
    url = "https://files.test/a.geojson"
    cache_write(tmp_path, url, b"the complete original payload")
    bin_path = tmp_path / f"{cache_key(url)}.bin"
    bin_path.write_bytes(b"the complete origi")  # truncated

    payload, error = cache_read(tmp_path, url)

    assert payload is None
    assert error is not None
    assert "integrity" in error


def test_bin_file_missing_but_meta_present_is_reported_and_treated_as_a_miss(tmp_path):
    url = "https://files.test/a.geojson"
    cache_write(tmp_path, url, b"payload")
    (tmp_path / f"{cache_key(url)}.bin").unlink()

    payload, error = cache_read(tmp_path, url)

    assert payload is None
    assert error is not None


def test_write_to_an_unwritable_directory_is_reported_not_raised(tmp_path):
    unwritable = tmp_path / "no-perms"
    unwritable.mkdir()
    unwritable.chmod(0o444)
    try:
        error = cache_write(unwritable / "nested", "https://files.test/a.geojson", b"payload")
        assert error is not None
        assert "could not write" in error
    finally:
        unwritable.chmod(0o755)  # so tmp_path cleanup can remove it


def test_temp_file_is_created_inside_the_cache_directory_not_tmp(tmp_path, monkeypatch):
    """os.replace is only atomic when the source and destination are on the
    same filesystem -- a temp file created under /tmp (a different
    filesystem than the cache dir on many systems) makes the final
    os.replace raise EXDEV. This exact bug has bitten this codebase before,
    so pin the temp file's parent to the cache directory itself."""
    seen_dirs = []
    real_mkstemp = cache_module.tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        seen_dirs.append(kwargs.get("dir"))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(cache_module.tempfile, "mkstemp", spy_mkstemp)

    cache_write(tmp_path, "https://files.test/a.geojson", b"payload")

    assert seen_dirs  # mkstemp was actually invoked (twice: .bin, .meta.json)
    assert all(Path(d) == tmp_path for d in seen_dirs)


def test_no_partial_file_is_left_behind_after_a_write(tmp_path):
    """The write is temp-file-then-os.replace: only the final .bin/.meta.json
    names should ever exist in the directory, never a stray .tmp file."""
    cache_write(tmp_path, "https://files.test/a.geojson", b"payload")

    names = {p.name for p in tmp_path.iterdir()}
    assert all(not name.endswith(".tmp") for name in names)
    assert len(names) == 2  # .bin and .meta.json only
