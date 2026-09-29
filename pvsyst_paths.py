"""Shared Unicode filename and resolved workspace boundary checks."""
from pathlib import Path
import re


def safe_filename(name: str, extension: str, max_length: int = 255) -> str:
    if (not isinstance(name, str) or len(name) <= len(extension) or name in (".", "..")
            or len(name) > max_length or name.rstrip(" .") != name
            or re.search(r'[\\/:*?"<>|\x00-\x1f]', name)
            or not name.lower().endswith(extension.lower())
            or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])",
                            name.split(".")[0].rstrip(" "), re.I)):
        raise ValueError(f"Expected a {extension} filename without directories")
    return name


def project_name(project: str) -> str:
    return safe_filename(project, ".PRJ", 180)


def variant_id(variant: str) -> str:
    if (not isinstance(variant, str) or len(variant) > 40
            or not re.fullmatch(r"VC[A-Za-z0-9]+", variant, re.I)):
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
    if root.is_symlink() or root.resolve().parent != workspace:
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
