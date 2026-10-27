"""Atomic file writes shared by the project, component and workflow stores.

Replacements and no-overwrite publications are staged in a hidden temporary file beside
the destination, flushed to disk and only then renamed into place, so a failed or
interrupted write never leaves a truncated destination behind. The temporary file is
always removed.

The stores' process locks do not cover the PVsyst GUI or other processes. An edit of an
inspected file therefore goes through `replace_checked`, which re-reads the file before
staging and again just before the rename.
"""
from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path


def stage(path: Path, data: bytes) -> Path:
    """Write `data` to a new hidden temporary file beside `path` and flush it to disk.

    The caller publishes the returned file with one rename and removes it afterwards.
    A failed write removes it here.
    """
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            # Persist the bytes before any rename, so a crash cannot publish an empty file.
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def atomic_replace(path: Path, data: bytes, verify: Callable[[], None] | None = None) -> None:
    """Replace or create `path` with `data` in one rename.

    `verify` runs once the copy is staged, just before the rename, and aborts it by raising.
    """
    temporary = stage(path, data)
    try:
        if verify is not None:
            verify()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def replace_checked(path: Path, expected: bytes, candidate: bytes, read: Callable[[Path], bytes],
                    message: str, *, prepare: Callable[[], object] | None = None,
                    verify: Callable[[], None] | None = None):
    """Replace `path` with `candidate` only while it still holds the inspected bytes `expected`.

    `path` is re-read with `read` before staging and again just before the rename. A change
    raises ValueError(message). `prepare` runs after the first check and before staging (for
    example to take a backup, so a file that already changed gets none); its result is
    returned. `verify` adds checks right after the second re-read.
    """
    def unchanged() -> None:
        if read(path) != expected:
            raise ValueError(message)

    def recheck() -> None:
        unchanged()
        if verify is not None:
            verify()

    unchanged()
    prepared = prepare() if prepare is not None else None
    atomic_replace(path, candidate, recheck)
    return prepared


def publish_new(path: Path, data: bytes) -> None:
    """Create `path` with `data` but never overwrite it, like an exclusive "xb" open.

    The bytes are staged first, so a failed write leaves no partial file blocking the name.
    A Windows rename and a POSIX hard link both refuse a destination created meanwhile with
    FileExistsError, where os.replace would overwrite it.
    """
    temporary = stage(path, data)
    try:
        (os.rename if os.name == "nt" else os.link)(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, record, first: bool = False) -> None:
    """Save `record` as an indented ASCII JSON checkpoint.

    The first save creates the file exclusively. A later save replaces it only with a
    complete flushed copy, so a failed write never leaves a truncated checkpoint.
    """
    payload = json.dumps(record, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if first:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
        return
    atomic_replace(path, payload.encode("utf-8"))
