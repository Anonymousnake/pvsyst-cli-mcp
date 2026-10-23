"""Bounded native DC loss controls; PVsyst owns all electrical calculation."""
from __future__ import annotations

import re
import time
from decimal import Decimal
from pathlib import Path

from pvsyst_ac_cabling import _layout
from pvsyst_tracking import _require_scope, _root_block


def _number(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("DC values must be finite and nonnegative")
    return result


def _state(lines):
    layout = _layout(lines)
    sub = layout[2]
    flags = sub.one("Flags")[1]
    if flags not in ("$019E1002", "$019E1003"):
        raise ValueError("Unsupported native DC mode flags")
    resistance = _number(sub.one("RSCablageCh")[1])
    percent = _number(sub.one("RSCablagePC")[1])
    cables = []

    def walk(node):
        if node.kind == "StringNode":
            body = [line.strip() for line in lines[node.line:node.end_line - 1] if line.strip()]
            if (len(body) != 6 or body[1] != "CableDefinition=Start" or body[3] != "CableDefinition=End"
                    or not body[0].startswith("NElements=") or body[4] != "SubArrayId=1"
                    or not body[5].startswith("SubArrayName=")):
                raise ValueError("Requires the recorded direct StringNode resistance shape")
            match = re.fullmatch(r"Resistance=([^ ]+) m\u03a9", body[2])
            if not match:
                raise ValueError("Requires native mOhm resistance records")
            cables.append(_number(match[1]))
        for child in node.children:
            walk(child)

    for node in layout[3]:
        walk(node)
    if (not cables or any(value != cables[0] for value in cables)
            or cables[0].quantize(Decimal("0.001")) != resistance):
        raise ValueError("Native StringNode resistance and subarray summary must agree")
    return {"mode": "percent" if flags.endswith("3") else "resistance",
            "resistance_mohm": str(resistance), "loss_percent": str(percent),
            "circuit_resistance_mohm": str(cables[0]), "string_groups": len(cables)}, layout


def dc_cabling_state(lines):
    try:
        return _state(lines)[0]
    except (ValueError, ArithmeticError, TypeError, KeyError, StopIteration) as exc:
        return {"mode": "unsupported", "issues": [str(exc)]}


def _profile(store, lines):
    arrays, system = store._sections(lines)
    _require_scope(store, lines, arrays, system, system_flags=("$00",),
                   sub_flags=("$019E1002", "$019E1003"))
    store._ordinary_fixed_plane(lines, store._orientation(lines, 1))
    state, layout = _state(lines)
    if layout[0]["mode"] != "none" or layout[5] or layout[1].one("MVNbTransfos")[1] != "0":
        raise ValueError("Native DC GUI edits require inactive AC and no MV/HV")
    if layout[6].one("Version")[1] != "8.1.6" or layout[6].one("Flags")[1] != "$00":
        raise ValueError("Requires native 8.1.6 circuit Flags00")
    return state


def _invariants(lines):
    _, layout = _state(lines)
    ignored = {layout[2].one(key)[0] for key in ("Flags", "RSCablageCh", "RSCablagePC")}

    def walk(node):
        if node.kind == "StringNode":
            ignored.update(i for i in range(node.line, node.end_line - 1)
                           if lines[i].strip().startswith("Resistance="))
        for child in node.children:
            walk(child)

    for node in layout[3]:
        walk(node)
    # Native saves add BOM and may re-encode display comments; physical records stay exact.
    return tuple(line.strip() for i, line in enumerate(lines)
                 if i not in ignored and line.strip() and not line.strip().startswith("Comment="))


def _field(gui, root, rect):
    x, y, _, _ = gui._rect_of(root)
    wanted = [rect[0] + x, rect[1] + y, rect[2] + x, rect[3] + y]
    hits = [h for h in gui._kids(root) if gui._class_of(h) == "TEdit" and gui._rect_of(h) == wanted]
    if len(hits) != 1:
        raise ValueError("Native DC edit metrics differ from the recorded layout")
    return hits[0]


def _open(gui, project, variant):
    root = gui._wait("TProjectForm")
    if not gui._string_of(root).rsplit(":", 1)[-1].strip().casefold() == project.casefold():
        raise ValueError("The open GUI project does not match the requested project")
    gui._select_source_variant(root, variant)
    gui._click(gui._find(gui._wait("TProjectForm"), "TPngBitBtn", "\u635f\u5931"))
    root = gui._wait("TDetailedLossesForm")
    gui._dpi(root)
    x, y, right, bottom = gui._rect_of(root)
    if (right - x, bottom - y) != (1126, 884):
        raise ValueError("Native detailed loss window metrics are unsupported")
    page = gui._find(root, "TPageControl")
    rect = gui._rect_of(page)
    gui._click(page, rect[0] + 82, rect[1] + 12)
    end = time.monotonic() + 12
    while True:
        try:
            resistance = _field(gui, root, (270, 203, 321, 229))
            percent = _field(gui, root, (270, 231, 321, 257))
            _number(gui._get_text(resistance))
            _number(gui._get_text(percent))
            break
        except (ValueError, ArithmeticError):
            if time.monotonic() >= end:
                raise ValueError("Native DC input fields did not become ready")
            time.sleep(0.2)
    return root, resistance, percent


def create_native_dc_cabling_variant(store, cli, *, project, source_variant, dc, variant_name,
                                      dry_run=True, confirm=False):
    """Drive an already open Chinese 8.1.6 GUI; never patch VC/INI or start/exit it."""
    import pvsyst_scene_native as gui
    result = {"status": "blocked", "stage": "preflight", "issues": [], "artifacts": {},
              "variant_created": False, "native_consumption_verified": False, "native_actions": []}
    with store.lock:
        try:
            if type(dry_run) is not bool or type(confirm) is not bool:
                raise ValueError("Use strict booleans")
            if not isinstance(dc, dict) or set(dc) != {"mode", "value"} or dc["mode"] not in ("resistance", "percent"):
                raise ValueError("DC input requires only mode and value")
            if type(dc["value"]) not in (int, float):
                raise ValueError("DC value must be a strict number")
            value = _number(dc["value"])
            if value != value.quantize(Decimal("0.01")) or len(format(value, "f")) > 20:
                raise ValueError("DC input requires exact 0.01 precision and at most 20 characters")
            if dc["mode"] == "percent" and value > 100:
                raise ValueError("DC loss percent must be between 0 and 100")
            store._check_project(project)
            store._validate_display_name(variant_name, "variant_name")
            path = store._path(project, source_variant)
            original = store._read(path)
            project_path = store._path(project)
            project_original = store._read(project_path)
            text, _ = store._text(original)
            source_lines = text.splitlines(keepends=True)
            before = _profile(store, source_lines)
            unchanged = _invariants(source_lines)
            result.update(project=project, source_variant=source_variant, source_dc=before)
            result["cli_version"] = gui._check_cli(cli)
            with gui._physical_coordinates():
                if gui._ini_datapath().resolve() != Path(store.workspace).resolve():
                    raise ValueError("GUI DataPath does not match the MCP workspace")
                gui._PID = gui._resolve_pid(cli)
                root = gui._wait("TProjectForm", timeout=3, activate=False)
                gui._dpi(root)
                if not gui._string_of(root).rsplit(":", 1)[-1].strip().casefold() == project.casefold():
                    raise ValueError("Open the requested GUI project before using this bridge")
                if dry_run or not confirm:
                    result["status"] = "ready" if dry_run else "confirmation_required"
                    return result
                if store._read(path) != original or store._read(project_path) != project_original:
                    raise ValueError("Source changed after preflight")
                result["stage"] = "native_edit"
                result["native_actions"].append("begin_native_dc_navigation")
                root, resistance, percent = _open(gui, project, source_variant)
                captions = {"resistance": "\u76f4\u6d41\u7535\u7f06\u7684\u7b49\u6548\u963b\u503c",
                            "percent": "\u7ebf\u635f\u7387 (at STC)"}
                radio = gui._find(root, "TRadioButton", captions[dc["mode"]])
                if gui._send(radio, 0xF0) != 1:
                    gui._click(radio)
                edit = resistance if dc["mode"] == "resistance" else percent
                gui._click(edit)
                gui._set_text(edit, format(value, ".2f"))
                gui._key(9)
                if _number(gui._get_text(edit)) != value or gui._send(radio, 0xF0) != 1:
                    raise ValueError("PVsyst did not accept the exact requested DC input")
                result["native_actions"].append("select_source_set_native_dc")
                if store._read(path) != original or store._read(project_path) != project_original:
                    raise ValueError("Source changed before native SaveAs")
                gui._click(gui._find(root, "TPngBitBtn", "\u786e\u5b9a"))
                gui._wait("TProjectForm")
                created = gui._save_new(project, variant_name)
                result.update(stage="native_saved", variant_created=True, variant=created.suffix[1:],
                              artifacts={"variant": str(created)})
                deadline = time.monotonic() + 8
                while True:
                    try:
                        raw = store._read(created)
                        decoded, _ = store._text(raw)
                        state = _profile(store, decoded.splitlines(keepends=True))
                        break
                    except (ValueError, ArithmeticError):
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.2)
                key = "resistance_mohm" if dc["mode"] == "resistance" else "loss_percent"
                if _root_block(decoded.splitlines(keepends=True)).one("Comment")[1] != variant_name:
                    raise ValueError("Native saved display name differs from the request")
                if _invariants(decoded.splitlines(keepends=True)) != unchanged:
                    raise ValueError("Native SaveAs changed a non-DC physical record")
                if state["mode"] != dc["mode"] or _number(state[key]) != value:
                    raise ValueError("Native saved DC mode/value differs from the request")
                if store._read(path) != original or store._read(project_path) != project_original:
                    raise ValueError("Native operation changed a source file")
                result.update(status="created", stage="verified_native_save", dc=state)
                result["native_actions"].append("native_confirm_save_as_verify_owners")
        except (ValueError, ArithmeticError, TypeError, KeyError, OSError, RuntimeError) as exc:
            result["issues"].append(str(exc))
    return result
