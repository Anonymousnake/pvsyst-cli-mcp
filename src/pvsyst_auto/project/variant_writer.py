"""Shared transaction runner for creating new variants from inspected templates."""
from __future__ import annotations

import difflib
from pathlib import Path

from pvsyst_auto.components.editor import replace_value

BOM = b"\xef\xbb\xbf"


def apply_variant_name(store, candidate_lines: list[str], variant_name: str | None):
    """Rewrite header Comment when `variant_name` is given and return (lines, name_change)."""
    if variant_name is None:
        return candidate_lines, None
    store._validate_display_name(variant_name, "variant_name")
    index, = store._display_name_fields(candidate_lines, complete=True)
    before = candidate_lines[index].partition("=")[2].strip()
    candidate_lines[index] = replace_value(candidate_lines[index], variant_name)
    return candidate_lines, {"before": before, "after": variant_name}


def resolve_bom_prefix(store, original: bytes, candidate_text: str, *,
                       variant_name: str | None = None,
                       add_utf8_bom: bool = False,
                       bom_display_target: str | None = None) -> bytes:
    """Compute UTF-8 BOM prefix so BOM-less sources carrying non-ASCII text stay valid."""
    if variant_name is not None:
        return store._display_name_prefix(
            original, variant_name, add_utf8_bom, "variant_name", content=candidate_text
        )
    if bom_display_target is not None:
        return store._display_name_prefix(
            original, bom_display_target, add_utf8_bom, "variant_name", content=candidate_text
        )
    if original.startswith(BOM) or add_utf8_bom or not candidate_text.isascii():
        return BOM
    return b""


def run_new_variant_transaction(
    store,
    *,
    project: str,
    source_variant: str,
    new_variant: str,
    dry_run: bool,
    build,
    add_utf8_bom: bool | None = None,
    variant_name: str | None = None,
    bom_display_target: str | None = None,
    preflight=None,
    initial_fields: dict | None = None,
    exists_prefix: str = "Choose a new variant: ",
    source_changed_error: str = "Source variant changed before creation",
    write_failure_warning: str = (
        "The new variant may contain a partial artifact; inspect the disclosed path "
        "before retrying. Original template is retained."
    ),
    extra_exceptions: tuple[type[Exception], ...] = (),
    include_bom_added: bool = True,
    uppercase_variant: bool = True,
) -> dict:
    """Preview or atomically create a new VC variant under `store.lock`."""
    result = {
        "status": "blocked",
        "stage": "preflight",
        "dry_run": dry_run,
        "issues": [],
        "warnings": [],
        "artifacts": {},
        "variant_created": False,
        "native_consumption_verified": False,
    }
    if initial_fields:
        result.update(initial_fields)
    caught = (ValueError, OSError, UnicodeError, KeyError, TypeError, StopIteration, *extra_exceptions)
    with store.lock:
        try:
            if type(dry_run) is not bool or (add_utf8_bom is not None and type(add_utf8_bom) is not bool):
                raise ValueError("Use strict booleans")
            if preflight is not None:
                preflight()
            store._check_project(project)
            source = store._path(project, source_variant)
            target = store._path(project, new_variant)
            if any(path.name.casefold() == target.name.casefold() for path in target.parent.iterdir()):
                raise FileExistsError(exists_prefix + target.name)
            original = store._read(source)
            text, lines = store._text(original)

            prepared = build(
                text=text,
                lines=lines,
                original=original,
                source=source,
                target=target,
                result=result,
            )
            if "candidate_text" in prepared:
                candidate_text = prepared["candidate_text"]
                name_change = prepared.get("name_change")
            else:
                candidate_lines, name_change = apply_variant_name(
                    store, list(prepared["candidate_lines"]), variant_name
                )
                candidate_text = "".join(candidate_lines)

            bom_prefix = resolve_bom_prefix(
                store,
                original,
                candidate_text,
                variant_name=variant_name,
                add_utf8_bom=bool(add_utf8_bom),
                bom_display_target=bom_display_target,
            )
            candidate = bom_prefix + candidate_text.encode("utf-8")
            _, checked = store._text(candidate)
            if prepared.get("verify") is not None:
                prepared["verify"](checked, candidate, result)

            if name_change is not None:
                result["name_change"] = name_change
            diff = "".join(
                difflib.unified_diff(
                    text.splitlines(keepends=True),
                    candidate_text.splitlines(keepends=True),
                )
            )
            result.update(
                status="ready",
                project=project,
                source_variant=source_variant,
                variant=target.suffix[1:].upper() if uppercase_variant else new_variant,
                diff=diff[:32000],
                diff_truncated=len(diff) > 32000,
            )
            result.setdefault("artifacts", {})["variant"] = str(target)
            if include_bom_added:
                result["bom_added"] = bool(bom_prefix) and not original.startswith(BOM)

            if store._read(source) != original:
                raise ValueError(source_changed_error)
            if dry_run:
                return result

            result["stage"] = "write"
            with target.open("xb") as stream:
                stream.write(candidate)
            result.update(status="created", stage="created", variant_created=True)
        except caught as exc:
            result["issues"].append(str(exc))
            result["status"] = "failed" if result["stage"] == "write" else "blocked"
            if result["status"] == "failed":
                result["warnings"].append(write_failure_warning)
    return result
