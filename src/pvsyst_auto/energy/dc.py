"""Headless DC cabling model: STC one-diode resistance conversion and circuit/subarray edits."""
from __future__ import annotations

import math
import re
from decimal import Decimal

from pvsyst_auto.components.editor import ASSIGNMENT, objects, replace_value, root_block
from pvsyst_auto.components.pan import validation_fields as pan_validation_fields
from pvsyst_auto.energy.ac import _layout
from pvsyst_auto.native.headless import strip_result_cache
from pvsyst_auto.project.circuit import inventory as grid_inventory
from pvsyst_auto.project.variant_writer import run_new_variant_transaction
from pvsyst_auto.versions import NATIVE_TEXT_OR_SEED, SEED_LABEL

_KB = 1.380649e-23
_QE = 1.602176634e-19
_T_STC = 298.15
_VT_CELL = (_KB * _T_STC) / _QE

_HEX_FLAG = re.compile(r"\$[0-9A-Fa-f]{1,8}")
_RES_LINE = re.compile(r"Resistance=([^\s]+)[ \t]+m(?:\u03a9|\u2126)")


def _number(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("DC values must be finite and nonnegative")
    return result


def _first_float(fields: dict[str, list[str]], *names: str, default: float | None = None) -> float | None:
    for name in names:
        vals = fields.get(name)
        if vals:
            try:
                num = float(vals[0].strip())
                if math.isfinite(num):
                    return num
            except ValueError:
                return None
    return default


def _solve_pan_stc_mpp(pan_text: str) -> dict | None:
    """Solve PVsyst's 3-point one-diode model at STC (1000 W/m^2, 25 C) for true (Vmp, Imp)."""
    try:
        fields = pan_validation_fields(pan_text)
    except (ValueError, KeyError):
        return None
    isc = _first_float(fields, "ISC", "Isc")
    voc = _first_float(fields, "Voc", "VCo")
    imp = _first_float(fields, "Imp", "Impp")
    vmp = _first_float(fields, "Vmp", "Vmpp")
    ncels = _first_float(fields, "NCelS")
    rs = _first_float(fields, "RSerie", default=0.0)
    rsh = _first_float(fields, "RShunt", default=1e9)
    if (isc is None or voc is None or imp is None or vmp is None or ncels is None
            or rs is None or rsh is None):
        return None
    if ncels < 1 or not (0 < imp < isc) or not (0 < vmp < voc) or rs < 0 or rsh <= 0:
        return None

    def diode_eval(gamma: float):
        vt = gamma * ncels * _VT_CELL
        if vt <= 0:
            return None
        e_oc = math.exp(min(voc / vt, 700.0))
        e_sc = math.exp(min((isc * rs) / vt, 700.0))
        denom = e_oc - e_sc
        if denom <= 0:
            return None
        i0 = (isc * (1.0 + rs / rsh) - voc / rsh) / denom
        if i0 <= 0 or not math.isfinite(i0):
            return None
        il = i0 * (e_oc - 1.0) + voc / rsh
        f = (il - i0 * (math.exp(min((vmp + imp * rs) / vt, 700.0)) - 1.0)
             - (vmp + imp * rs) / rsh - imp)
        return f, i0, il, vt

    lo, hi = 0.4, 3.5
    steps = 120
    prev_g = lo
    prev = diode_eval(prev_g)
    bracket = None
    for step in range(1, steps + 1):
        g = lo + (hi - lo) * (step / steps)
        cur = diode_eval(g)
        if prev is not None and cur is not None and (prev[0] == 0 or (prev[0] < 0) != (cur[0] < 0)):
            bracket = (prev_g, g, prev[0], cur[0])
            break
        if cur is not None:
            prev_g, prev = g, cur
    if bracket is None:
        return {"vmp_stc": vmp, "imp_stc": imp, "source": "pan_nominal_fallback"}

    a, b, fa, _ = bracket
    for _ in range(80):
        mid = 0.5 * (a + b)
        res = diode_eval(mid)
        if res is None:
            break
        fm = res[0]
        if fm == 0:
            a = b = mid
            break
        if (fa < 0) == (fm < 0):
            a, fa = mid, fm
        else:
            b = mid
    gamma = 0.5 * (a + b)
    solved = diode_eval(gamma)
    if solved is None:
        return {"vmp_stc": vmp, "imp_stc": imp, "source": "pan_nominal_fallback"}
    _, i0, il, vt = solved

    def current_at(v: float) -> float:
        i = max(0.0, isc * (1.0 - max(0.0, v / voc) ** 4))
        for _ in range(50):
            arg = min((v + i * rs) / vt, 700.0)
            exp_val = math.exp(arg)
            g = il - i0 * (exp_val - 1.0) - (v + i * rs) / rsh - i
            dg = -i0 * exp_val * (rs / vt) - rs / rsh - 1.0
            step = g / dg
            i -= step
            if abs(step) < 1e-12:
                break
        return max(0.0, i)

    v_lo, v_hi = 0.45 * voc, 0.98 * voc
    scan_n = 200
    best_idx, best_p = 0, -1.0
    vs = [v_lo + (v_hi - v_lo) * (k / scan_n) for k in range(scan_n + 1)]
    for idx, vv in enumerate(vs):
        p = vv * current_at(vv)
        if p > best_p:
            best_p, best_idx = p, idx
    left = vs[max(0, best_idx - 2)]
    right = vs[min(scan_n, best_idx + 2)]
    phi = (math.sqrt(5.0) - 1.0) / 2.0
    c = right - phi * (right - left)
    d = left + phi * (right - left)
    fc, fd = c * current_at(c), d * current_at(d)
    for _ in range(60):
        if fc > fd:
            right, d, fd = d, c, fc
            c = right - phi * (right - left)
            fc = c * current_at(c)
        else:
            left, c, fc = c, d, fd
            d = left + phi * (right - left)
            fd = d * current_at(d)
    v_peak = 0.5 * (left + right)
    i_peak = current_at(v_peak)
    if not (math.isfinite(v_peak) and math.isfinite(i_peak) and v_peak > 0 and i_peak > 0):
        return {"vmp_stc": vmp, "imp_stc": imp, "source": "pan_nominal_fallback"}
    return {"vmp_stc": v_peak, "imp_stc": i_peak, "gamma": gamma, "source": "one_diode_stc"}


def _resolve_pan_stc(components, pan_name: str) -> dict | None:
    """Resolve readable workspace/builtin .PAN module STC MPP; return None for encrypted DB refs."""
    for library in ("workspace", "builtin"):
        if library == "builtin" and components.builtin is None:
            continue
        candidate = components._path("PAN", pan_name, library)
        if not candidate.is_file():
            continue
        data = components._read(candidate)
        inspected = components._inspect("PAN", data)
        if inspected["format"] != "text":
            return None
        if inspected["structural_errors"] or inspected["known_value_errors"]:
            return None
        _, pan_text = components._text("PAN", data)
        solved = _solve_pan_stc_mpp(pan_text)
        if solved is None:
            return None
        return {**solved, "pan": pan_name, "library": library, "path": str(candidate)}
    return None


def _parse_string_node(lines, node):
    """Inspect a StringNode body and return its CableDefinition/SubArrayId line anchors."""
    stack, spans = [], []
    sub_id_line = None
    sub_id = None
    for index in range(node.line, node.end_line - 1):
        raw = lines[index].rstrip("\r\n")
        stripped = raw.strip()
        if not stripped:
            continue
        match = ASSIGNMENT.fullmatch(raw)
        if not match:
            raise ValueError("Unexpected record inside StringNode")
        key, value = match[2], match[4]
        if value == "Start":
            if key != "CableDefinition" or stack:
                raise ValueError("Only direct CableDefinition blocks are supported inside StringNode")
            stack.append((key, index))
        elif value == "End":
            if not stack or stack[-1][0] != key:
                raise ValueError("Unmatched block end inside StringNode")
            _, start = stack.pop()
            spans.append((start, index))
        elif stack:
            continue
        elif key == "NElements":
            if not value.isdigit() or int(value) < 1:
                raise ValueError("StringNode NElements must be a positive integer")
        elif key == "SubArrayId":
            if sub_id_line is not None or not value.isdigit() or int(value) < 1:
                raise ValueError("StringNode requires one positive SubArrayId")
            sub_id_line = index
            sub_id = int(value)
        elif key == "SubArrayName":
            continue
        else:
            raise ValueError(f"Unsupported StringNode field: {key}")
    if stack or len(spans) > 1 or sub_id_line is None or sub_id != 1:
        raise ValueError("Requires uniform StringNode geometry belonging to SubArrayId=1")
    if not spans:
        return {"node": node, "sub_id_line": sub_id_line, "res_line": None, "resistance": None}
    start, end = spans[0]
    inner = [(i, lines[i].strip()) for i in range(start + 1, end) if lines[i].strip()]
    if len(inner) != 1:
        raise ValueError("StringNode CableDefinition must carry only Resistance")
    res_line, token = inner[0]
    res_match = _RES_LINE.fullmatch(token)
    if not res_match:
        raise ValueError("Requires native mOhm resistance records")
    return {"node": node, "sub_id_line": sub_id_line,
            "res_line": res_line, "resistance": _number(res_match[1])}


def _state(lines):
    layout = _layout(lines)
    sub = layout[2]
    flags = sub.one("Flags")[1]
    if not _HEX_FLAG.fullmatch(flags):
        raise ValueError("Unsupported native DC mode flags")
    resistance = _number(sub.one("RSCablageCh")[1])
    percent = _number(sub.one("RSCablagePC")[1])
    parsed_nodes = []

    def walk(node):
        if node.kind == "StringNode":
            parsed_nodes.append(_parse_string_node(lines, node))
        for child in node.children:
            walk(child)

    for node in layout[3]:
        walk(node)
    if not parsed_nodes:
        raise ValueError("Requires at least one StringNode in the circuit")
    has_cables = [item["resistance"] is not None for item in parsed_nodes]
    if any(has_cables) and not all(has_cables):
        raise ValueError("StringNode cable definitions must be uniform across the subarray")
    if all(has_cables):
        first = parsed_nodes[0]["resistance"]
        if any(item["resistance"] != first for item in parsed_nodes):
            raise ValueError("StringNode resistances must agree across the subarray")
        circuit_res = first
    else:
        circuit_res = None
    mode = "percent" if (int(flags[1:], 16) & 0x01) else "resistance"
    state = {"mode": mode,
             "resistance_mohm": str(resistance),
             "loss_percent": str(percent),
             "circuit_resistance_mohm": str(circuit_res) if circuit_res is not None else None,
             "cable_definition_present": circuit_res is not None,
             "string_groups": len(parsed_nodes)}
    if circuit_res is not None and circuit_res.quantize(Decimal("0.001")) != resistance:
        state["resistance_difference_mohm"] = str(abs(circuit_res - resistance))
        state["issues"] = ["Native StringNode resistance and subarray summary must agree"]
    return state, layout, parsed_nodes


def dc_cabling_state(lines):
    try:
        return _state(lines)[0]
    except (ValueError, ArithmeticError, TypeError, KeyError, StopIteration) as exc:
        return {"mode": "unsupported", "issues": [str(exc)]}


def _profile(store, lines):
    root = root_block(lines)
    if root.one("Version")[1] not in NATIVE_TEXT_OR_SEED:
        raise ValueError(f"Requires a native {SEED_LABEL} variant")
    root_flags = root.one("Flags")[1]
    if not _HEX_FLAG.fullmatch(root_flags):
        raise ValueError("Root Flags must be hexadecimal")
    arrays, system = store._sections(lines)
    if system is None or len(arrays) != 1:
        raise ValueError("Requires one pvSystem and one subarray")
    system_root = root_block(lines, system)
    if not set(system_root.fields) <= {"Flags", "SystemType"} or system_root.one("SystemType")[1] != "Grid":
        raise ValueError("Requires Grid pvSystem declaring only Flags and SystemType")
    if "Flags" in system_root.fields and system_root.one("Flags")[1] != "$00":
        raise ValueError("Requires native Grid pvSystem Flags=$00")
    main = store._main_array(lines)
    if main is None:
        raise ValueError("A pvMainArray section is required")
    main_root = root_block(lines, main)
    main_flags = main_root.one("Flags")[1]
    main_flags2 = main_root.one("Flags2")[1]
    if (not _HEX_FLAG.fullmatch(main_flags)
            or not _HEX_FLAG.fullmatch(main_flags2)
            or int(main_flags2[1:], 16) != 0x082A):
        raise ValueError("Main array requires native hex Flags and Flags2=$082A")
    sub_root = root_block(lines, next(iter(arrays.values())))
    sub_flags2 = sub_root.one("Flags2")[1]
    if (sub_root.one("Version")[1] not in NATIVE_TEXT_OR_SEED
            or not _HEX_FLAG.fullmatch(sub_root.one("Flags")[1])
            or not _HEX_FLAG.fullmatch(sub_flags2)
            or int(sub_flags2[1:], 16) != 0x20):
        raise ValueError(f"Subarray requires native {SEED_LABEL} hex Flags and Flags2=$20")
    strings = sub_root.one("NStringCh")[1]
    mod_serie = sub_root.one("NModSerie")[1]
    if (sub_root.one("NoOrientation")[1] != "1"
            or not strings.isdigit() or int(strings) < 1
            or not mod_serie.isdigit() or int(mod_serie) < 1
            or sub_root.one("NStrOrient1")[1] != strings):
        raise ValueError("Requires all strings on orientation 1 with positive NModSerie and NStrOrient1 == NStringCh")
    if any(key.startswith("NStrOrient") and key != "NStrOrient1" and sub_root.one(key)[1] != "0"
           for key in sub_root.fields):
        raise ValueError("Every other NStrOrient* must be zero")
    store._orientation_list(lines)
    store._ordinary_fixed_plane(lines, store._orientation(lines, 1))
    if any(block.object_type in ("pvLoad", "pvGridStorage") for block in objects("".join(lines))):
        raise ValueError("Load and storage objects are outside DC cabling scope")
    if any(re.match(r"\s*(?:P50_P90|PVObject_Horizon)\s*=", line) for line in lines):
        raise ValueError("P50/P90 and saved horizon objects are outside DC cabling scope")
    if grid_inventory(lines, arrays)["counts_valid"] is not True:
        raise ValueError("Requires complete consistent native grid counts")
    state, layout, parsed_nodes = _state(lines)
    if state.get("issues"):
        raise ValueError("; ".join(state["issues"]))
    if layout[5] or (int(main_flags[1:], 16) & 0x02000000):
        raise ValueError("Native DC cabling edits exclude MV/HV transformers")
    if layout[6].one("Version")[1] not in NATIVE_TEXT_OR_SEED or layout[6].one("Flags")[1] != "$00":
        raise ValueError(f"Requires native {SEED_LABEL} circuit Flags=$00")
    return state, layout, parsed_nodes


def _format_res(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def create_dc_cabling_variant(store, *, project, source_variant, new_variant, dc,
                              dry_run=True, variant_name=None, add_utf8_bom=False):
    """Preview or create a NEW VC configuring DC cabling resistance (mOhm) or STC loss (%)."""
    parsed = {}

    def preflight():
        if not isinstance(dc, dict) or set(dc) != {"mode", "value"} or dc["mode"] not in ("resistance", "percent"):
            raise ValueError("DC input requires only mode ('resistance' or 'percent') and value")
        if type(dc["value"]) not in (int, float):
            raise ValueError("DC value must be a strict finite number")
        value = _number(dc["value"])
        if len(format(value, "f")) > 20:
            raise ValueError("DC value exceeds 20 characters")
        if dc["mode"] == "percent":
            if value != value.quantize(Decimal("0.01")) or value >= 99:
                raise ValueError("DC loss percent must be in [0, 99) with at most 0.01 precision (PVsystCLI simulation limit <99%)")
        else:
            if value != value.quantize(Decimal("0.001")):
                raise ValueError("DC resistance (mOhm) supports at most 0.001 precision")
        parsed["mode"] = dc["mode"]
        parsed["value"] = value

    def build(*, text, lines, **_):
        mode = parsed["mode"]
        value = parsed["value"]
        _, layout, parsed_nodes = _profile(store, lines)
        main, sub = layout[1], layout[2]
        n_mod_serie = int(sub.one("NModSerie")[1])
        n_string_ch = int(sub.one("NStringCh")[1])
        pan_name = sub.one("PVModule")[1]
        pan_info = _resolve_pan_stc(store.components, pan_name)

        warnings = []
        if pan_info is not None:
            r_array_stc_mohm = (n_mod_serie * pan_info["vmp_stc"]
                                / (n_string_ch * pan_info["imp_stc"])) * 1000.0
            if mode == "percent":
                r_mohm = (float(value) / 100.0) * r_array_stc_mohm
                res_decimal = Decimal("0") if value == 0 else Decimal(f"{r_mohm:.12g}")
                pct_decimal = value.quantize(Decimal("0.01"))
            else:
                res_decimal = value
                pct_float = (float(value) / r_array_stc_mohm) * 100.0
                pct_decimal = Decimal(f"{pct_float:.2f}")
                if pct_decimal >= 99:
                    raise ValueError(
                        f"Resistance {value} mOhm corresponds to {pct_decimal:.2f}% STC loss, "
                        "which exceeds PVsystCLI simulation limit (<99%)."
                    )
        else:
            if mode == "percent":
                raise ValueError(
                    f"Module {pan_name!r} is a built-in encrypted or unreadable .PAN reference: "
                    "percent mode requires readable STC Vmp/Imp. Use mode='resistance' (mOhm) instead."
                )
            res_decimal = value
            pct_decimal = Decimal("0.00")
            warnings.append(
                f"Module {pan_name!r} has no readable local text .PAN; RSCablagePC is stored as 0.00 "
                "and PVsystCLI evaluates DC ohmic loss directly from StringNode Resistance (mOhm)."
            )

        res_str = _format_res(res_decimal)
        ch_str = f"{res_decimal:.3f}"
        pc_str = f"{pct_decimal:.2f}"

        main_flags_int = int(main.one("Flags")[1][1:], 16) | 0x01800000
        sub_flags_int = int(sub.one("Flags")[1][1:], 16)
        if mode == "percent":
            sub_flags_int = (sub_flags_int | 0x01800005)
        else:
            sub_flags_int = (sub_flags_int | 0x01800000) & ~0x00000005
        main_flags_str = f"${main_flags_int:08X}"
        sub_flags_str = f"${sub_flags_int:08X}"

        newline = "\r\n" if "\r\n" in text else "\n"
        edits = []

        def replace_line(index, val):
            edits.append((index, index + 1, [replace_value(lines[index], val)]))

        replace_line(main.one("Flags")[0], main_flags_str)
        replace_line(sub.one("Flags")[0], sub_flags_str)
        replace_line(sub.one("RSCablageCh")[0], ch_str)
        replace_line(sub.one("RSCablagePC")[0], pc_str)

        for item in parsed_nodes:
            if item["res_line"] is not None:
                replace_line(item["res_line"], f"{res_str} m\u03a9")
            else:
                idx = item["sub_id_line"]
                indent = re.match(r"[ \t]*", lines[idx]).group(0)
                block_lines = [
                    f"{indent}CableDefinition=Start{newline}",
                    f"{indent}    Resistance={res_str} m\u03a9{newline}",
                    f"{indent}CableDefinition=End{newline}",
                ]
                edits.append((idx, idx, block_lines))

        candidate_lines = list(lines)
        for start, end, replacement in sorted(edits, key=lambda pair: pair[0], reverse=True):
            candidate_lines[start:end] = replacement
        candidate_lines, _ = strip_result_cache(candidate_lines)

        def verify(checked, _, res):
            verified, _, _ = _profile(store, checked)
            if (verified["mode"] != mode
                    or not verified["cable_definition_present"]
                    or Decimal(verified["resistance_mohm"]) != res_decimal.quantize(Decimal("0.001"))
                    or Decimal(verified["loss_percent"]) != pct_decimal.quantize(Decimal("0.01"))):
                raise ValueError("Candidate DC cabling fields did not read back the requested values")
            res["dc"] = verified
            res["pan_stc"] = (
                {"pan": pan_info["pan"], "library": pan_info["library"],
                 "vmp_stc": round(pan_info["vmp_stc"], 6), "imp_stc": round(pan_info["imp_stc"], 6),
                 "source": pan_info["source"]}
                if pan_info is not None else None
            )
            res["warnings"].extend(warnings)
            res["warnings"].append(
                "Only pvMainArray/pvSubArray DC cabling flags, RSCablageCh/PC, and StringNode CableDefinition "
                "records change; other hardware, topology, shading, and source files stay inherited."
            )

        return {"candidate_lines": candidate_lines, "verify": verify}

    return run_new_variant_transaction(
        store,
        project=project,
        source_variant=source_variant,
        new_variant=new_variant,
        dry_run=dry_run,
        add_utf8_bom=add_utf8_bom,
        variant_name=variant_name,
        preflight=preflight,
        build=build,
        source_changed_error="DC source changed before creation",
        write_failure_warning="A partial new artifact may remain; inspect its disclosed path. Source retained.",
        extra_exceptions=(ArithmeticError,),
    )
