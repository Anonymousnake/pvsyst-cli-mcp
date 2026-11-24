"""Shared workspace paths and atomic file I/O helpers."""
from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable
from pathlib import Path


def safe_filename(name: str, extension: str, max_length: int = 255) -> str:
    if (not isinstance(name, str) or len(name) <= len(extension) or name in (".", "..")
            or len(name) > max_length or name.rstrip(" .") != name
            or re.search(r'[\\/:*?"<>|\x00-\x1f]', name)
            or not name.lower().endswith(extension.lower())
            or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", name.split(".")[0].rstrip(" "), re.I)):
        raise ValueError(f"Expected a {extension} filename without directories")
    return name


def battery_reference_name(name: str) -> str:
    """Validate a filename to be written into a native BatteryFile field."""
    name = safe_filename(name, ".BTR")
    if name != name.lstrip():
        raise ValueError("Battery references cannot start with whitespace; rename the BTR before using it")
    return name


def project_name(project: str) -> str:
    return safe_filename(project, ".PRJ", 180)


def variant_id(variant: str) -> str:
    if not isinstance(variant, str) or len(variant) > 40 or not re.fullmatch(r"VC[A-Za-z0-9]+", variant, re.I):
        raise ValueError("Variant must be a VC identifier without directories")
    return variant.upper()


def workspace_folder(workspace: Path, folder: str) -> Path:
    root = workspace.resolve(strict=True)
    parent = (root / folder).resolve()
    if parent != root and root not in parent.parents:
        raise ValueError("Workspace folder resolves outside the workspace")
    return parent


def workspace_file(workspace: Path, folder: str, filename: str, extension: str) -> Path:
    safe_filename(filename, extension)
    parent = workspace_folder(workspace, folder)
    target = (parent / filename).resolve()
    if target.parent != parent:
        raise ValueError("File resolves outside its workspace folder")
    return target


def project_root(workspace: Path) -> Path:
    root = workspace / "Projects"
    if root.is_symlink() or root.resolve().parent != workspace.resolve():
        raise ValueError("Projects directory resolves outside workspace")
    return root.resolve()


def project_path(workspace: Path, project: str, variant: str | None = None) -> Path:
    project_name(project)
    name = project if variant is None else f"{project[:-4]}.{variant_id(variant)}"
    root = project_root(workspace)
    path = root / name
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError("Symlink or escaping project files are not supported")
    return path


def stage(path: Path, data: bytes) -> Path:
    """Write `data` to a new hidden temporary file beside `path` and flush it to disk."""
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
    """Replace or create `path` with `data` in one rename."""
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
    """Replace `path` with `candidate` only while it still holds the inspected bytes `expected`."""
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
    """Create `path` with `data` but never overwrite it, like an exclusive "xb" open."""
    temporary = stage(path, data)
    try:
        (os.rename if os.name == "nt" else os.link)(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, record, first: bool = False) -> None:
    """Save `record` as an indented ASCII JSON checkpoint."""
    payload = json.dumps(record, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if first:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
        return
    atomic_replace(path, payload.encode("utf-8"))
