"""Tests for download_atomic — atomic, timeout-bounded, sha256-verified fetch.

Network access is never used: sources are ``file://`` URLs pointing at files
under ``tmp_path``, which exercises the same ``urllib.request.urlopen`` code
path without any real socket I/O.
"""

import hashlib
from pathlib import Path

import pytest

from palmimo_sdk.download import download_atomic


def _file_url(path: Path) -> str:
    return path.as_uri()


def test_download_atomic_writes_dest_content(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello world")
    dest = tmp_path / "dest.bin"

    download_atomic(_file_url(src), dest)

    assert dest.read_bytes() == b"hello world"


def test_download_atomic_creates_parent_dir(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload")
    dest = tmp_path / "nested" / "sub" / "dest.bin"

    download_atomic(_file_url(src), dest)

    assert dest.read_bytes() == b"payload"


def test_download_atomic_succeeds_when_sha256_matches(tmp_path: Path) -> None:
    content = b"verified content"
    src = tmp_path / "src.bin"
    src.write_bytes(content)
    dest = tmp_path / "dest.bin"
    digest = hashlib.sha256(content).hexdigest()

    download_atomic(_file_url(src), dest, sha256=digest)

    assert dest.read_bytes() == content


def test_download_atomic_raises_oserror_when_sha256_mismatches(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"actual content")
    dest = tmp_path / "dest.bin"
    wrong_digest = hashlib.sha256(b"different content").hexdigest()

    with pytest.raises(OSError):
        download_atomic(_file_url(src), dest, sha256=wrong_digest)

    assert not dest.exists()
    assert list(dest.parent.glob(".*")) == []


def test_download_atomic_leaves_no_files_when_download_fails(tmp_path: Path) -> None:
    missing_src = tmp_path / "does-not-exist.bin"
    dest = tmp_path / "dest.bin"

    with pytest.raises(OSError):
        download_atomic(_file_url(missing_src), dest)

    assert not dest.exists()
    assert list(dest.parent.glob(".*")) == []
