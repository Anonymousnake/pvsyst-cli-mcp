"""Headless DC cabling, soiling, string mismatch, spectral correction, IAM, and ageing models."""
from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Any

from pvsyst_auto.components.editor import ASSIGNMENT, Profile, direct_fields, finite, objects, one, pan_validation_fields, replace_value, root_block
from pvsyst_auto.energy.system import _layout
from pvsyst_auto.native.headless import strip_result_cache
from pvsyst_auto.project.circuit import inventory as grid_inventory
from pvsyst_auto.project.variant_updates import run_new_variant_transaction
from pvsyst_auto.versions import NATIVE_TEXT, NATIVE_TEXT_OR_SEED, SEED_LABEL, TEXT_LABEL

_VT_CELL = (1.380649e-23 * 298.15) / 1.602176634e-19
_HEX_FLAG = re.compile(r"\$[0-9A-Fa-f]{1,8}")
_RES_LINE = re.compile(r"Resistance=([^\s]+)[ \t]+m(?:\u03a9|\u2126)")


def _count_field(lines, key: str) -> int:
    return sum(m is not None and m[2] == key for line in lines for m in [ASSIGNMENT.fullmatch(line.rstrip("\r\n"))])


def _step_check(value, label: str, *, scale: int, minimum: float, maximum: float, step_err: str, strict_number: bool = False):
    if strict_number and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise ValueError(f"{label} must be a strict number")
    num = finite(value, label, minimum=minimum, maximum=maximum)
    scaled = Decimal(str(num)) * scale
    if scaled != scaled.to_integral_value():
        raise ValueError(step_err)
    return num


def _finite_entry(block, key: str, label: str | None = None) -> float:
    lbl = label or key
    try:
        return finite(float(one(block.fields, key)[1]), lbl)
    except (ValueError, OverflowError):
        raise ValueError(f"Require a finite native {lbl}") from None


def _try_state(fn, *args, key: str = "mode", **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        return {key: None, "issue": str(exc)}


# --- DC cabling ---

def _number(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("DC values must be finite and nonnegative")
    return result


def _first_float(fields: dict[str, list[str]], *names: str, default: float | None = None) -> float | None:
    for name in names:
        if vals := fields.get(name):
            try:
                if math.isfinite(num := float(vals[0].strip())):
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
    isc, voc = _first_float(fields, "ISC", "Isc"), _first_float(fields, "Voc", "VCo")
    imp, vmp, ncels = _first_float(fields, "Imp", "Impp"), _first_float(fields, "Vmp", "Vmpp"), _first_float(fields, "NCelS")
    rs, rsh = _first_float(fields, "RSerie", default=0.0), _first_float(fields, "RShunt", default=1e9)
    if None in (isc, voc, imp, vmp, ncels, rs, rsh) or ncels < 1 or not (0 < imp < isc) or not (0 < vmp < voc) or rs < 0 or rsh <= 0:
        return None
    fallback = {"vmp_stc": vmp, "imp_stc": imp, "source": "pan_nominal_fallback"}

    def diode_eval(gamma: float):
        vt = gamma * ncels * _VT_CELL
        if vt <= 0 or (denom := math.exp(min(voc / vt, 700.0)) - math.exp(min((isc * rs) / vt, 700.0))) <= 0:
            return None
        i0 = (isc * (1.0 + rs / rsh) - voc / rsh) / denom
        if i0 <= 0 or not math.isfinite(i0):
            return None
        il = i0 * (math.exp(min(voc / vt, 700.0)) - 1.0) + voc / rsh
        return il - i0 * (math.exp(min((vmp + imp * rs) / vt, 700.0)) - 1.0) - (vmp + imp * rs) / rsh - imp, i0, il, vt

    lo, hi, steps, bracket = 0.4, 3.5, 120, None
    prev_g, prev = lo, diode_eval(lo)
    for step in range(1, steps + 1):
        g = lo + (hi - lo) * (step / steps)
        if (cur := diode_eval(g)) is not None:
            if prev is not None and (prev[0] == 0 or (prev[0] < 0) != (cur[0] < 0)):
                bracket = (prev_g, g, prev[0])
                break
            prev_g, prev = g, cur
    if bracket is None:
        return fallback
    a, b, fa = bracket
    for _ in range(80):
        if (res := diode_eval(mid := 0.5 * (a + b))) is None or res[0] == 0:
            if res is not None:
                a = b = mid
            break
        a, fa, b = (mid, res[0], b) if (fa < 0) == (res[0] < 0) else (a, fa, mid)
    if (solved := diode_eval(gamma := 0.5 * (a + b))) is None:
        return fallback
    _, i0, il, vt = solved

    def current_at(v: float) -> float:
        i = max(0.0, isc * (1.0 - max(0.0, v / voc) ** 4))
        for _ in range(50):
            exp_val = math.exp(min((v + i * rs) / vt, 700.0))
            i -= (step := (il - i0 * (exp_val - 1.0) - (v + i * rs) / rsh - i) / (-i0 * exp_val * (rs / vt) - rs / rsh - 1.0))
            if abs(step) < 1e-12:
                break
        return max(0.0, i)

    v_lo, v_hi, scan_n = 0.45 * voc, 0.98 * voc, 200
    vs = [v_lo + (v_hi - v_lo) * (k / scan_n) for k in range(scan_n + 1)]
    best_idx = max(range(scan_n + 1), key=lambda idx: vs[idx] * current_at(vs[idx]))
    left, right, phi = vs[max(0, best_idx - 2)], vs[min(scan_n, best_idx + 2)], (math.sqrt(5.0) - 1.0) / 2.0
    c, d = right - phi * (right - left), left + phi * (right - left)
    fc, fd = c * current_at(c), d * current_at(d)
    for _ in range(60):
        if fc > fd:
            right, d, fd = d, c, fc
            fc = (c := right - phi * (right - left)) * current_at(c)
        else:
            left, c, fc = c, d, fd
            fd = (d := left + phi * (right - left)) * current_at(d)
    v_peak, i_peak = 0.5 * (left + right), current_at(0.5 * (left + right))
    return {"vmp_stc": v_peak, "imp_stc": i_peak, "gamma": gamma, "source": "one_diode_stc"} if (math.isfinite(v_peak) and math.isfinite(i_peak) and v_peak > 0 and i_peak > 0) else fallback


def _resolve_pan_stc(components, pan_name: str) -> dict | None:
    """Resolve readable workspace/builtin .PAN module STC MPP; return None for encrypted DB refs."""
    for library in ("workspace", "builtin"):
        if library == "builtin" and components.builtin is None:
            continue
        candidate = components._path("PAN", pan_name, library)
        if not candidate.is_file():
            continue
        inspected = components._inspect("PAN", data := components._read(candidate))
        if inspected["format"] != "text" or inspected["structural_errors"] or inspected["known_value_errors"]:
            return None
        if (solved := _solve_pan_stc_mpp(components._text("PAN", data)[1])) is None:
            return None
        return {**solved, "pan": pan_name, "library": library, "path": str(candidate)}
    return None


def _parse_string_node(lines, node):
    stack, spans, sub_id_line, sub_id = [], [], None, None
    for index in range(node.line, node.end_line - 1):
        if not (raw := lines[index].rstrip("\r\n")).strip():
            continue
        if not (match := ASSIGNMENT.fullmatch(raw)):
            raise ValueError("Unexpected record inside StringNode")
        key, value = match[2], match[4]
        if value == "Start":
            if key != "CableDefinition" or stack:
                raise ValueError("Only direct CableDefinition blocks are supported inside StringNode")
            stack.append((key, index))
        elif value == "End":
            if not stack or stack[-1][0] != key:
                raise ValueError("Unmatched block end inside StringNode")
            spans.append((stack.pop()[1], index))
        elif stack or key == "SubArrayName":
            continue
        elif key == "NElements":
            if not value.isdigit() or int(value) < 1:
                raise ValueError("StringNode NElements must be a positive integer")
        elif key == "SubArrayId":
            if sub_id_line is not None or not value.isdigit() or int(value) < 1:
                raise ValueError("StringNode requires one positive SubArrayId")
            sub_id_line, sub_id = index, int(value)
        else:
            raise ValueError(f"Unsupported StringNode field: {key}")
    if stack or len(spans) > 1 or sub_id_line is None or sub_id != 1:
        raise ValueError("Requires uniform StringNode geometry belonging to SubArrayId=1")
    if not spans:
        return {"node": node, "sub_id_line": sub_id_line, "res_line": None, "resistance": None}
    inner = [(i, lines[i].strip()) for i in range(spans[0][0] + 1, spans[0][1]) if lines[i].strip()]
    if len(inner) != 1:
        raise ValueError("StringNode CableDefinition must carry only Resistance")
    if not (res_match := _RES_LINE.fullmatch(inner[0][1])):
        raise ValueError("Requires native mOhm resistance records")
    return {"node": node, "sub_id_line": sub_id_line, "res_line": inner[0][0], "resistance": _number(res_match[1])}


def _state(lines):
    layout = _layout(lines)
    sub = layout[2]
    if not _HEX_FLAG.fullmatch(flags := sub.one("Flags")[1]):
        raise ValueError("Unsupported native DC mode flags")
    resistance, percent = _number(sub.one("RSCablageCh")[1]), _number(sub.one("RSCablagePC")[1])
    parsed_nodes, stack = [], list(layout[3])
    while stack:
        node = stack.pop(0)
        if node.kind == "StringNode":
            parsed_nodes.append(_parse_string_node(lines, node))
        stack[:0] = list(node.children)
    if not parsed_nodes:
        raise ValueError("Requires at least one StringNode in the circuit")
    has_cables = [item["resistance"] is not None for item in parsed_nodes]
    if any(has_cables) and not all(has_cables):
        raise ValueError("StringNode cable definitions must be uniform across the subarray")
    circuit_res = parsed_nodes[0]["resistance"] if all(has_cables) else None
    if circuit_res is not None and any(item["resistance"] != circuit_res for item in parsed_nodes):
        raise ValueError("StringNode resistances must agree across the subarray")
    state = {
        "mode": "percent" if (int(flags[1:], 16) & 0x01) else "resistance",
        "resistance_mohm": str(resistance), "loss_percent": str(percent),
        "circuit_resistance_mohm": str(circuit_res) if circuit_res is not None else None,
        "cable_definition_present": circuit_res is not None, "string_groups": len(parsed_nodes),
    }
    if circuit_res is not None and circuit_res.quantize(Decimal("0.001")) != resistance:
        state.update(resistance_difference_mohm=str(abs(circuit_res - resistance)), issues=["Native StringNode resistance and subarray summary must agree"])
    return state, layout, parsed_nodes


def dc_cabling_state(lines):
    try:
        return _state(lines)[0]
    except (ValueError, ArithmeticError, TypeError, KeyError, StopIteration) as exc:
        return {"mode": "unsupported", "issues": [str(exc)]}


def _dc_profile(store, lines):
    root = root_block(lines)
    if root.one("Version")[1] not in NATIVE_TEXT_OR_SEED:
        raise ValueError(f"Requires a native {SEED_LABEL} variant")
    if not _HEX_FLAG.fullmatch(root.one("Flags")[1]):
        raise ValueError("Root Flags must be hexadecimal")
    arrays, system = store._sections(lines)
    if system is None or len(arrays) != 1:
        raise ValueError("Requires one pvSystem and one subarray")
    sys_root = root_block(lines, system)
    if sys_root.one("SystemType")[1] != "Grid":
        raise ValueError("Requires Grid pvSystem for DC cabling")
    if "Flags" in sys_root.fields and not _HEX_FLAG.fullmatch(sys_root.one("Flags")[1]):
        raise ValueError("Requires hexadecimal Grid pvSystem Flags")
    if (main := store._main_array(lines)) is None:
        raise ValueError("A pvMainArray section is required")
    main_root = root_block(lines, main)
    m_flags, m_flags2 = main_root.one("Flags")[1], main_root.one("Flags2")[1]
    if not _HEX_FLAG.fullmatch(m_flags) or not _HEX_FLAG.fullmatch(m_flags2) or int(m_flags2[1:], 16) != 0x082A:
        raise ValueError("Main array requires native hex Flags and Flags2=$082A")
    sub_root = root_block(lines, next(iter(arrays.values())))
    s_flags2 = sub_root.one("Flags2")[1]
    if (sub_root.one("Version")[1] not in NATIVE_TEXT_OR_SEED or not _HEX_FLAG.fullmatch(sub_root.one("Flags")[1])
            or not _HEX_FLAG.fullmatch(s_flags2) or int(s_flags2[1:], 16) != 0x20):
        raise ValueError(f"Subarray requires native {SEED_LABEL} hex Flags and Flags2=$20")
    strings, mod_serie = sub_root.one("NStringCh")[1], sub_root.one("NModSerie")[1]
    if (sub_root.one("NoOrientation")[1] != "1" or not strings.isdigit() or int(strings) < 1
            or not mod_serie.isdigit() or int(mod_serie) < 1 or sub_root.one("NStrOrient1")[1] != strings):
        raise ValueError("Requires all strings on orientation 1 with positive NModSerie and NStrOrient1 == NStringCh")
    if any(k.startswith("NStrOrient") and k != "NStrOrient1" and sub_root.one(k)[1] != "0" for k in sub_root.fields):
        raise ValueError("Every other NStrOrient* must be zero")
    store._orientation_list(lines)
    store._ordinary_fixed_plane(lines, store._orientation(lines, 1), allow_missing_flags=True, allow_sheds=True)
    if any(b.object_type in ("pvLoad", "pvGridStorage") for b in objects("".join(lines))):
        raise ValueError("Load and storage objects are outside DC cabling scope")
    if any(re.match(r"\s*(?:P50_P90|PVObject_Horizon)\s*=", line) for line in lines):
        raise ValueError("P50/P90 and saved horizon objects are outside DC cabling scope")
    if grid_inventory(lines, arrays)["counts_valid"] is not True:
        raise ValueError("Requires complete consistent native grid counts")
    state, layout, parsed_nodes = _state(lines)
    if state.get("issues"):
        raise ValueError("; ".join(state["issues"]))
    if layout[6].one("Version")[1] not in NATIVE_TEXT_OR_SEED or layout[6].one("Flags")[1] != "$00":
        raise ValueError(f"Requires native {SEED_LABEL} circuit Flags=$00")
    return state, layout, parsed_nodes


def _format_res(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    return (text.rstrip("0").rstrip(".") if "." in text else text) or "0"


def create_dc_cabling_variant(store, *, project, source_variant, new_variant, dc, dry_run=True, variant_name=None, add_utf8_bom=False):
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
        if dc["mode"] == "percent" and (value != value.quantize(Decimal("0.01")) or value >= 99):
            raise ValueError("DC loss percent must be in [0, 99) with at most 0.01 precision (PVsystCLI simulation limit <99%)")
        if dc["mode"] == "resistance" and value != value.quantize(Decimal("0.001")):
            raise ValueError("DC resistance (mOhm) supports at most 0.001 precision")
        parsed.update(mode=dc["mode"], value=value)

    def build(*, text, lines, **_):
        mode, value = parsed["mode"], parsed["value"]
        _, layout, parsed_nodes = _dc_profile(store, lines)
        main, sub = layout[1], layout[2]
        n_mod_serie, n_string_ch, pan_name = int(sub.one("NModSerie")[1]), int(sub.one("NStringCh")[1]), sub.one("PVModule")[1]
        pan_info = _resolve_pan_stc(store.components, pan_name)
        warnings = []
        if pan_info is not None:
            r_array_stc_mohm = (n_mod_serie * pan_info["vmp_stc"] / (n_string_ch * pan_info["imp_stc"])) * 1000.0
            if mode == "percent":
                res_decimal = Decimal("0") if value == 0 else Decimal(f"{(float(value) / 100.0) * r_array_stc_mohm:.12g}")
                pct_decimal = value.quantize(Decimal("0.01"))
            else:
                res_decimal = value
                pct_decimal = Decimal(f"{(float(value) / r_array_stc_mohm) * 100.0:.2f}")
                if pct_decimal >= 99:
                    raise ValueError(f"Resistance {value} mOhm corresponds to {pct_decimal:.2f}% STC loss, which exceeds PVsystCLI simulation limit (<99%).")
        else:
            if mode == "percent":
                raise ValueError(f"Module {pan_name!r} is a built-in encrypted or unreadable .PAN reference: percent mode requires readable STC Vmp/Imp. Use mode='resistance' (mOhm) instead.")
            res_decimal, pct_decimal = value, Decimal("0.00")
            warnings.append(f"Module {pan_name!r} has no readable local text .PAN; RSCablagePC is stored as 0.00 and PVsystCLI evaluates DC ohmic loss directly from StringNode Resistance (mOhm).")
        res_str, ch_str, pc_str = _format_res(res_decimal), f"{res_decimal:.3f}", f"{pct_decimal:.2f}"
        main_flags_int = int(main.one("Flags")[1][1:], 16) | 0x01800000
        sub_flags_int = int(sub.one("Flags")[1][1:], 16)
        sub_flags_int = (sub_flags_int | 0x01800005) if mode == "percent" else ((sub_flags_int | 0x01800000) & ~0x00000005)
        newline = "\r\n" if "\r\n" in text else "\n"
        edits = [(idx, idx + 1, [replace_value(lines[idx], val)]) for idx, val in (
            (main.one("Flags")[0], f"${main_flags_int:08X}"), (sub.one("Flags")[0], f"${sub_flags_int:08X}"),
            (sub.one("RSCablageCh")[0], ch_str), (sub.one("RSCablagePC")[0], pc_str),
        )]
        for item in parsed_nodes:
            if item["res_line"] is not None:
                idx = item["res_line"]
                edits.append((idx, idx + 1, [replace_value(lines[idx], f"{res_str} m\u03a9")]))
            else:
                idx = item["sub_id_line"]
                indent = re.match(r"[ \t]*", lines[idx]).group(0)
                edits.append((idx, idx, [f"{indent}CableDefinition=Start{newline}", f"{indent}    Resistance={res_str} m\u03a9{newline}", f"{indent}CableDefinition=End{newline}"]))
        candidate_lines = list(lines)
        for start, end, replacement in sorted(edits, key=lambda pair: pair[0], reverse=True):
            candidate_lines[start:end] = replacement
        candidate_lines, _ = strip_result_cache(candidate_lines)

        def verify(checked, _, res):
            verified, _, _ = _dc_profile(store, checked)
            if (verified["mode"] != mode or not verified["cable_definition_present"]
                    or Decimal(verified["resistance_mohm"]) != res_decimal.quantize(Decimal("0.001"))
                    or Decimal(verified["loss_percent"]) != pct_decimal.quantize(Decimal("0.01"))):
                raise ValueError("Candidate DC cabling fields did not read back the requested values")
            res["dc"] = verified
            res["pan_stc"] = ({**{k: pan_info[k] for k in ("pan", "library", "source")}, "vmp_stc": round(pan_info["vmp_stc"], 6), "imp_stc": round(pan_info["imp_stc"], 6)} if pan_info is not None else None)
            res["warnings"].extend([*warnings, "Only pvMainArray/pvSubArray DC cabling flags, RSCablageCh/PC, and StringNode CableDefinition records change; other hardware, topology, shading, and source files stay inherited."])

        return {"candidate_lines": candidate_lines, "verify": verify}

    return run_new_variant_transaction(store, project=project, source_variant=source_variant, new_variant=new_variant, dry_run=dry_run, add_utf8_bom=add_utf8_bom, variant_name=variant_name, preflight=preflight, build=build, source_changed_error="DC source changed before creation", write_failure_warning="A partial new artifact may remain; inspect its disclosed path. Source retained.", extra_exceptions=(ArithmeticError,))


# --- Soiling ---

def _soiling_profile(lines, bounds):
    start, end = bounds
    end = next((i for i in range(start + 1, end) if lines[i].strip().startswith("PVObject_")), end)
    header = range(start + 1, end)
    assignments = {i: ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n")) for i in header}
    if any(m is not None and m[2] in ("SoilingLoss", "SoilingLossM") for m in assignments.values()):
        raise ValueError(f"Legacy soiling representation; resave with the native {TEXT_LABEL} GUI first")
    begins = [i for i, m in assignments.items() if m is not None and m[2] == "Soiling"]
    if _count_field(lines, "Soiling") != len(begins):
        raise ValueError("Non-owning Soiling blocks are not supported")
    if len(begins) > 1:
        raise ValueError("Require at most one owning Soiling block")
    if not begins:
        if any(lines[i].strip() == "EndTags" for i in header):
            raise ValueError("Unverified main-array tag block")
        return {"mode": "none"}, (end, end), "0.000"
    begin, = begins
    indent = lines[start][:len(lines[start]) - len(lines[start].lstrip())]
    if lines[begin].rstrip("\r\n") != indent + "Soiling=Soiling":
        raise ValueError("Require an owning native Soiling=Soiling block")
    finish = next((i for i in range(begin + 1, end) if lines[i].strip() == "EndTags"), None)
    if finish is None or any(lines[i].strip() == "EndTags" for i in header if not begin <= i <= finish):
        raise ValueError("Unclosed or unverified main-array tag block")
    values = {}
    for line in lines[begin + 1:finish]:
        match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if match is None or match[2] not in ("IsYearly", "IsMonthly", "IsFromFile", "YearlyValue", "MonthlyValue") or match[2] in values:
            raise ValueError("Unknown or duplicate native Soiling field")
        values[match[2]] = match[4]
    if any(values.get(k) not in ("True", "False") for k in ("IsYearly", "IsMonthly", "IsFromFile")):
        raise ValueError("Require explicit native Soiling mode booleans")
    if values["IsFromFile"] != "False":
        raise ValueError("File-based soiling is not verified by this editor")
    if values["IsYearly"] == values["IsMonthly"] == "True":
        raise ValueError("Native Soiling modes are mutually exclusive")
    try:
        yearly = float(values["YearlyValue"])
        monthly = [float(x) for x in values["MonthlyValue"].removesuffix(",").split(",")] if values.get("MonthlyValue") else None
    except (ValueError, KeyError):
        raise ValueError("Invalid native Soiling values") from None
    if not math.isfinite(yearly) or not 0 <= yearly <= 1 or (monthly is not None and (len(monthly) != 12 or any(not math.isfinite(x) or not 0 <= x <= 1 for x in monthly))):
        raise ValueError("Native Soiling values must be finite fractions in 0..1")
    mode = "constant" if values["IsYearly"] == "True" else "monthly" if values["IsMonthly"] == "True" else "none"
    if mode == "monthly" and monthly is None:
        raise ValueError("Monthly soiling requires twelve native values")
    state: dict[str, Any] = {"mode": mode}
    if mode == "constant":
        state["yearly_loss_pct"] = round(yearly * 100, 10)
    elif mode == "monthly":
        state["monthly_loss_pct"] = [round(x * 100, 10) for x in monthly]
    return state, (begin, finish + 1), values["YearlyValue"]


def soiling_state(lines, bounds):
    return _try_state(lambda: _soiling_profile(lines, bounds)[0])


def replace_soiling(lines, bounds, updates):
    if not isinstance(updates, dict) or updates.get("mode") not in ("none", "constant", "monthly"):
        raise ValueError("Require soiling mode none, constant or monthly")
    mode = updates["mode"]
    if set(updates) != ({"mode"} | ({"yearly_loss_pct"} if mode == "constant" else {"monthly_loss_pct"} if mode == "monthly" else set())):
        raise ValueError("Provide only the values required by the chosen soiling mode")
    supplied = [updates["yearly_loss_pct"]] if mode == "constant" else updates.get("monthly_loss_pct", [])
    if not isinstance(supplied, list) or (mode == "monthly" and len(supplied) != 12):
        raise ValueError("Monthly soiling requires exactly twelve percentage values")
    for val in supplied:
        _step_check(val, "Soiling percentages", scale=10, minimum=0, maximum=100, step_err="Use 0.1 percentage-point increments, matching native GUI save precision", strict_number=True)
    before, (begin, end), yearly_raw = _soiling_profile(lines, bounds)
    after: dict[str, Any] = {"mode": mode, **({"yearly_loss_pct": supplied[0]} if mode == "constant" else {"monthly_loss_pct": list(supplied)} if mode == "monthly" else {})}
    if before == after:
        return {"before": before, "after": before}
    indent = lines[bounds[0]][:len(lines[bounds[0]]) - len(lines[bounds[0]].lstrip())]
    newline = "\r\n" if lines[bounds[0]].endswith("\r\n") else "\n"
    block = []
    if mode != "none":
        fraction = lambda v: f"{Decimal(str(v)) / 100:.3f}"
        body = ["Soiling=Soiling", f"  IsYearly={mode == 'constant'}", f"  IsMonthly={mode == 'monthly'}", "  IsFromFile=False",
                "  YearlyValue=" + (fraction(supplied[0]) if mode == "constant" else yearly_raw),
                *(["  MonthlyValue=" + ",".join(map(fraction, supplied)) + ","] if mode == "monthly" else [])]
        block = [indent + text + newline for text in (*body, "EndTags")]
    candidate = list(lines)
    candidate[begin:end] = block
    checked = _soiling_profile(candidate, (bounds[0], bounds[1] + len(block) - (end - begin)))[0]
    if checked != after:
        raise ValueError("Candidate soiling state differs from the request")
    lines[:] = candidate
    return {"before": before, "after": checked}


# --- String mismatch ---

DEFAULT_BIT = 0x00020000
_MISMATCH_FIELD, _MISMATCH_ANCHOR, _MISMATCH_SIBLING = "PMismString", "PMismMPP", "PMismUFixe"


def _read_mismatch(lines, bounds):
    if not isinstance(bounds, tuple) or len(bounds) != 2:
        raise ValueError("Require one owning subarray bounds")
    fields = direct_fields(lines, bounds)
    if one(fields, "Version")[1] not in NATIVE_TEXT:
        raise ValueError(f"String mismatch editing requires subarray Version={TEXT_LABEL}")
    if not _HEX_FLAG.fullmatch(raw := one(fields, "Flags")[1]):
        raise ValueError("Require one hexadecimal owning subarray Flags")
    if _count_field(lines, _MISMATCH_FIELD) != len(fields.get(_MISMATCH_FIELD, [])):
        raise ValueError(f"Require {_MISMATCH_FIELD} only in the owning subarray")
    flags, entry = int(raw[1:], 16), one(fields, _MISMATCH_FIELD, False)
    if flags & DEFAULT_BIT:
        if entry is not None:
            raise ValueError("A defaulted string mismatch must not carry PMismString")
        return fields, flags, {"mode": "default", "loss_percent": None}
    if entry is None:
        raise ValueError("A manual string mismatch requires one finite PMismString")
    try:
        loss = _step_check(float(entry[1]), "loss_percent", scale=100, minimum=0.01, maximum=100, step_err="loss_percent must use 0.01 increments")
    except (ValueError, OverflowError):
        raise ValueError("Require a finite native PMismString") from None
    return fields, flags, {"mode": "constant", "loss_percent": loss}


def string_mismatch_state(lines, bounds):
    """Semantic state; unsupported layouts report mode None plus an issue and never a zero."""
    return _try_state(lambda: _read_mismatch(lines, bounds)[2])


def replace_string_mismatch(lines, bounds, updates):
    """Validate input and source, build the whole body, then replace once."""
    if not isinstance(updates, dict) or updates.get("mode") not in ("default", "constant"):
        raise ValueError("Require string mismatch mode default or constant")
    mode = updates["mode"]
    if set(updates) != ({"mode"} if mode == "default" else {"mode", "loss_percent"}):
        raise ValueError("default accepts only mode; constant requires loss_percent")
    value = None if mode == "default" else _step_check(
        updates["loss_percent"], "loss_percent", scale=100, minimum=0.01, maximum=100,
        step_err="loss_percent must use 0.01 increments", strict_number=True,
    )
    fields, flags, before = _read_mismatch(lines, bounds)
    after = {"mode": mode, "loss_percent": value}
    if before == after:
        return {"before": before, "after": after}
    target, entry = (flags | DEFAULT_BIT) if mode == "default" else (flags & ~DEFAULT_BIT), one(fields, _MISMATCH_FIELD, False)
    anchor = None
    if mode == "constant" and entry is None:
        anchor = one(fields, _MISMATCH_SIBLING, False) or one(fields, _MISMATCH_ANCHOR, False)
        if anchor is None:
            raise ValueError(f"Require an owning {_MISMATCH_SIBLING} or {_MISMATCH_ANCHOR} to place {_MISMATCH_FIELD} in GUI order")
    body, (flags_index, flags_raw) = [], fields["Flags"][0]
    field_index = entry[0] if entry is not None else None
    for i in range(bounds[0] + 1, bounds[1]):
        if i == flags_index:
            body.append(replace_value(lines[i], "$%0*X" % (len(flags_raw) - 1, target)) if target != flags else lines[i])
        elif i == field_index:
            if mode == "constant":
                body.append(replace_value(lines[i], f"{value:.2f}"))
        else:
            body.append(lines[i])
            if anchor is not None and anchor[0] == i:
                line = lines[i]
                indent, ending = line[:len(line) - len(line.lstrip())], ("\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else "")
                body.append(f"{indent}{_MISMATCH_FIELD}={value:.2f}{ending}")
    lines[bounds[0] + 1:bounds[1]] = body
    return {"before": before, "after": after}


# --- Spectral correction ---

COEFF_SETS = ("SiMono", "SiPoly", "CdTe", "CIS", "aSi", "Custom")


def spectral_state(lines: list[str], main: tuple[int, int] | None) -> dict[str, Any]:
    """Read current UseSpectralCorr / SpectralCorrection settings from pvMainArray."""
    if main is None:
        return {"configured": False, "enabled": False}
    enabled, use_default, coeff_set, use_rel_hum, in_block = False, True, None, None, False
    for idx in range(main[0] + 1, main[1]):
        s = lines[idx].strip()
        if s in ("SpectralCorrection=Start", "SpectralCorrection=End"):
            in_block = s == "SpectralCorrection=Start"
        elif in_block:
            if s.startswith("CoeffSet="):
                coeff_set = s.split("=", 1)[1].strip()
            elif s.startswith("UseRelHum="):
                use_rel_hum = s.split("=", 1)[1].strip() == "True"
        elif s.startswith("UseSpectralCorr="):
            enabled = s.split("=", 1)[1].strip() == "True"
        elif s.startswith("UseSpctCrrDefCoeffs="):
            use_default = s.split("=", 1)[1].strip() == "True"
    return {"configured": enabled or coeff_set is not None, "enabled": enabled, "use_default_coeffs": use_default, "coeff_set": coeff_set, "use_rel_hum": use_rel_hum}


def replace_spectral_correction(lines: list[str], main: tuple[int, int] | None, updates: dict[str, Any]) -> dict[str, Any]:
    """Enable, update, or disable native UseSpectralCorr + SpectralCorrection block in pvMainArray."""
    if not isinstance(updates, dict) or "enabled" not in updates:
        raise ValueError("spectral_updates requires an explicit boolean 'enabled'")
    allowed = {"enabled", "coeff_set", "use_default_coeffs", "use_rel_hum", "coefficients"}
    if not set(updates) <= allowed:
        raise ValueError("Unknown field in spectral_updates: " + ", ".join(sorted(set(updates) - allowed)))
    enabled, coeff_set = updates["enabled"], str(updates.get("coeff_set") or "SiMono")
    if coeff_set not in COEFF_SETS:
        raise ValueError(f"spectral_updates.coeff_set must be one of {COEFF_SETS}")
    use_default, use_rel_hum = updates.get("use_default_coeffs", coeff_set != "Custom"), updates.get("use_rel_hum", True)
    for flag_name, flag_val in (("enabled", enabled), ("use_default_coeffs", use_default), ("use_rel_hum", use_rel_hum)):
        if type(flag_val) is not bool:
            raise ValueError(f"spectral_updates.{flag_name} must be a strict boolean")
    coeffs = updates.get("coefficients")
    if coeff_set == "Custom":
        if not isinstance(coeffs, list) or len(coeffs) != 6 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in coeffs):
            raise ValueError("spectral_updates.coefficients must be 6 finite numbers when coeff_set='Custom'")
        use_default = False
    elif coeffs is not None:
        raise ValueError("spectral_updates.coefficients is only accepted when coeff_set='Custom'")
    if main is None:
        raise ValueError("Spectral correction requires a pvMainArray section")
    start, end = main
    before = spectral_state(lines, main)
    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    circ_idx = next((i for i in range(start + 1, end) if lines[i].strip() == "PVObject_SystemCircuit=pvCircuit"), end)
    kept, in_spec = [], False
    for idx in range(start, circ_idx):
        s = lines[idx].strip()
        if s in ("SpectralCorrection=Start", "SpectralCorrection=End"):
            in_spec = s == "SpectralCorrection=Start"
        elif not in_spec and not s.startswith(("UseSpectralCorr=", "UseSpctCrrDefCoeffs=")):
            kept.append(lines[idx])
    new_spec = [f"  UseSpectralCorr={'True' if enabled else 'False'}{eol}"]
    if enabled:
        new_spec.extend([f"  UseSpctCrrDefCoeffs={'True' if use_default else 'False'}{eol}", f"  SpectralCorrection=Start{eol}", f"    CoeffSet={coeff_set}{eol}"])
        if coeff_set == "Custom" and coeffs is not None:
            new_spec.append("    Coefficients=" + ",".join(f"{float(v):.6f}" for v in coeffs) + f",{eol}")
        new_spec.extend([f"    UseRelHum={'True' if use_rel_hum else 'False'}{eol}", f"  SpectralCorrection=End{eol}"])
    candidate = list(lines[:start]) + kept + new_spec + list(lines[circ_idx:])
    after = spectral_state(candidate, (start, end + len(candidate) - len(lines)))
    lines[:] = candidate
    return {"before": before, "after": after}


# --- IAM model ---

COMPONENT_BIT = 0x20
IAM_PATH, IAM_TYPE = "PVObject_IAM", "pvIAM"
PROFILE_PATH, PROFILE_TYPE = "PVObject_IAM/IAMProfile", "TCubicProfile"
ANCHOR_KEY, ANCHOR_VALUE = "WiringResistance", "TWiringResistance"
OPTIC_KEYS = ("N_Glass", "N_EVA", "N_ARCoating", "DGlass", "DArCoating")
FRESNEL_KEYS = ("Flags", "IAMMode") + OPTIC_KEYS


def _iam_curve_points(parsed, *, require_profile_instance: bool = False):
    children = [b for b in parsed if b.path.startswith(IAM_PATH + "/")]
    if len(children) != 1 or children[0].path != PROFILE_PATH or children[0].object_type != PROFILE_TYPE:
        raise ValueError(f"Require exactly one {PROFILE_PATH}/{PROFILE_TYPE} child")
    curve = children[0]
    if require_profile_instance and not isinstance(curve, Profile):
        raise ValueError("Require one owning IAMProfile/TCubicProfile")
    extra = set(curve.fields) - ({"NPtsMax", "NPtsEff", "LastCompile", "Mode"} | {f"Point_{i}" for i in range(1, 10)})
    if extra:
        raise ValueError(f"Refusing to drop unknown TCubicProfile field {sorted(extra)[0]}")
    pts = curve.points()
    for key in ("LastCompile", "Mode"):
        one(curve.fields, key)
    return [{"angle_deg": float(a), "factor": float(f)} for a, f in pts]


def _read_iam(lines, bounds):
    if not isinstance(bounds, tuple) or len(bounds) != 2:
        raise ValueError("Require one owning subarray bounds")
    parsed = objects("".join(lines[bounds[0]:bounds[1] + 1]))
    for b in parsed:
        if b.problems:
            raise ValueError(f"Unsupported {b.path}: {b.problems[0]}")
    owners = [b for b in parsed if b.path == ""]
    if len(owners) != 1:
        raise ValueError("Require one owning pvSubArray header")
    fields = owners[0].fields
    if one(fields, "Version")[1] not in NATIVE_TEXT:
        raise ValueError(f"IAM model editing requires subarray Version={TEXT_LABEL}")
    index, raw = one(fields, "Flags2")
    index += bounds[0]
    if not _HEX_FLAG.fullmatch(raw) or int(raw[1:], 16) not in (0x20, 0x00):
        raise ValueError("Require the verified subarray Flags2 $20 or $00")
    found = [b for b in parsed if b.path == IAM_PATH and b.object_type == IAM_TYPE]
    if len(found) > 1:
        raise ValueError("Require one owning pvIAM block")
    total = sum(m is not None and m[2].startswith("PVObject_") and m[4] == IAM_TYPE for line in lines for m in [ASSIGNMENT.fullmatch(line.rstrip("\r\n"))])
    if total != (1 if found else 0):
        raise ValueError("Require pvIAM only in the owning subarray")
    block = found[0] if found else None
    for key in ("IAMMode", "B0_IAM"):
        if _count_field(lines, key) != (len(block.fields.get(key, [])) if block is not None else 0):
            raise ValueError(f"Require {key} only in the owning pvIAM")
    component = bool(int(raw[1:], 16) & COMPONENT_BIT)
    if block is None:
        if not component:
            raise ValueError("Require a stored Ashrae pvIAM block when the component bit is clear")
        return fields, index, raw, None, {"effective": "component", "stored_model": None, "stored_ashrae_b0": None}
    model = one(block.fields, "IAMMode")[1]
    if model == "Ashrae":
        if set(block.fields) != {"Flags", "IAMMode", "B0_IAM"}:
            raise ValueError("Require exactly Flags/IAMMode/B0_IAM in the Ashrae pvIAM")
        if any(b.path.startswith(IAM_PATH + "/") for b in parsed):
            raise ValueError("Require no nested objects inside the Ashrae pvIAM")
        if one(block.fields, "Flags")[1] != "$00" or one(block.fields, "IAMMode")[1] != "Ashrae":
            raise ValueError("Require native Flags=$00 and IAMMode=Ashrae")
        try:
            b0 = _step_check(float(one(block.fields, "B0_IAM")[1]), "B0_IAM", scale=1000, minimum=0, maximum=1, step_err="Use 0.001 increments for B0_IAM")
        except (ValueError, OverflowError) as exc:
            raise ValueError("Require a finite native B0_IAM in 0..1" if "increments" not in str(exc) else str(exc)) from None
    elif model == "FresnelAR" and component:
        if set(block.fields) != set(FRESNEL_KEYS):
            raise ValueError("Require exactly Flags/IAMMode and the five optics fields in the FresnelAR pvIAM")
        if one(block.fields, "Flags")[1] != "$00" or one(block.fields, "IAMMode")[1] != "FresnelAR":
            raise ValueError("Require native Flags=$00 and IAMMode=FresnelAR")
        for key in OPTIC_KEYS:
            _finite_entry(block, key)
        _iam_curve_points(parsed, require_profile_instance=True)
        b0 = None
    elif model in ("FresnelSmooth", "UserProfile"):
        if component:
            raise ValueError(f"{model} cannot be the effective model while bit5 is set")
        expected = FRESNEL_KEYS if model == "FresnelSmooth" else ("Flags", "IAMMode")
        if set(block.fields) != set(expected):
            raise ValueError(f"Require exactly the recorded {model} pvIAM fields")
        if one(block.fields, "Flags")[1] != "$00":
            raise ValueError("Require native Flags=$00")
        for key in expected:
            if key not in ("Flags", "IAMMode", "IAMProfile"):
                _finite_entry(block, key)
        return fields, index, raw, block, {"effective": model, "stored_model": model, "stored_ashrae_b0": None, "points": _iam_curve_points(parsed)}
    else:
        raise ValueError("Require a verified Ashrae, component FresnelAR, FresnelSmooth or UserProfile pvIAM state")
    return fields, index, raw, block, {"effective": "component" if component else "ashrae", "stored_model": model, "stored_ashrae_b0": b0}


def iam_model_state(lines, bounds):
    return _try_state(lambda: _read_iam(lines, bounds)[4], key="effective")


def replace_iam_model(lines, bounds, updates):
    if not isinstance(updates, dict) or updates.get("mode") not in ("component", "ashrae"):
        raise ValueError("Require IAM model mode component or ashrae")
    mode = updates["mode"]
    if set(updates) != ({"mode"} if mode == "component" else {"mode", "ashrae_b0"}):
        raise ValueError("component accepts only mode; ashrae requires ashrae_b0")
    b0 = None if mode == "component" else _step_check(updates["ashrae_b0"], "ashrae_b0", scale=1000, minimum=0, maximum=1, step_err="Use 0.001 increments for ashrae_b0")
    fields, index, raw, block, before = _read_iam(lines, bounds)
    if before.get("stored_model") in ("FresnelSmooth", "UserProfile"):
        raise ValueError("The stored IAM table is read-only in this entry point; replace or regenerate it through the native software")
    anchor = None
    if mode == "ashrae" and block is None:
        indent = ASSIGNMENT.fullmatch(lines[bounds[0] + fields["Version"][0][0]].rstrip("\r\n"))[1]
        found = []
        for i in range(bounds[0] + 1, bounds[1]):
            if (m := ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))) is not None and m[1] == indent and m[2] == ANCHOR_KEY:
                if m[4] != ANCHOR_VALUE:
                    raise ValueError(f"Require the native {ANCHOR_KEY}={ANCHOR_VALUE} anchor")
                found.append(i)
        if len(found) != 1:
            raise ValueError(f"Require one owning {ANCHOR_KEY} anchor")
        anchor = found[0]
    first = lines[bounds[0]]
    indent, newline = first[:len(first) - len(first.lstrip())] + "  ", ("\r\n" if first.endswith("\r\n") else "\n")
    target = COMPONENT_BIT if mode == "component" else 0x00
    minimal = [f"{indent}{IAM_PATH}={IAM_TYPE}{newline}", f"{indent}  Flags=$00{newline}", f"{indent}  IAMMode=Ashrae{newline}", f"{indent}  B0_IAM={b0:.3f}{newline}", f"{indent}End of PVObject {IAM_TYPE}{newline}"] if mode == "ashrae" else []
    span, body = (None if block is None else (bounds[0] + block.line - 1, bounds[0] + block.end_line - 1)), []
    for i in range(bounds[0] + 1, bounds[1]):
        if i == index:
            body.append(lines[i] if target == int(raw[1:], 16) else replace_value(lines[i], "$%0*X" % (len(raw) - 1, target)))
        elif mode == "ashrae" and span is not None and span[0] <= i <= span[1]:
            if i == span[0]:
                body.extend(minimal)
        else:
            if mode == "ashrae" and span is None and i == anchor:
                body.extend(minimal)
            body.append(lines[i])
    after = _read_iam(lines[:bounds[0] + 1] + body + lines[bounds[1]:], (bounds[0], bounds[0] + 1 + len(body)))[4]
    if before != after:
        lines[bounds[0] + 1:bounds[1]] = body
    return {"before": before, "after": after}


YEAR_FIELD, MEAN_FIELD = "NoYearSimul", "ModuleDegradFactor"
KEEP_MC, RMS_ISC, RMS_VOC = "KeepMCValues", "ModulesRMSYearISC", "ModulesRMSYearVoc"
STORED_LOSS, STORED_MISMATCH = "DegradLossFactor", "DegradMismFactor"
AGE_KNOWN = frozenset(("Flags", KEEP_MC, "PlotKind", "CurveWarranty", "ProgressMode", "NSigmaLimit", MEAN_FIELD, "ImpDegradSharing", RMS_ISC, RMS_VOC, "ModuleYearWarr", "ModuleWarranty"))
AGE_FIXED = {"Flags": "$00", "ProgressMode": "1", "CurveWarranty": "1", "NSigmaLimit": "2.0", "ImpDegradSharing": "80.0"}
MAIN_FLAGS2_ON, MAIN_FLAGS2_OFF = "$0010082A", "$082A"
AGE_TEMPLATE_ORDER = ("Flags", KEEP_MC, "PlotKind", "CurveWarranty", "ProgressMode", "NSigmaLimit", "ImpDegradSharing", RMS_ISC, RMS_VOC, "ModuleYearWarr", "ModuleWarranty")
RMS_ISC_KEY, RMS_VOC_KEY = "modules_rms_year_isc_percent", "modules_rms_year_voc_percent"


def _age_year(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 30:
        raise ValueError("simulation_year must be a strict integer" if isinstance(value, bool) or not isinstance(value, int) else "simulation_year must be in 1..30")
    return value


def _age_pct(value, label: str, maximum: float):
    return _step_check(value, label, scale=100, minimum=0, maximum=maximum, step_err=f"{label} must use 0.01 increments", strict_number=True)


def _read_ageing(lines, bounds, require_enabled=True):
    if not isinstance(bounds, tuple) or len(bounds) != 2:
        raise ValueError("Require one owning main-array bounds")
    shift = bounds[0]
    blocks = objects("".join(lines[shift:bounds[1] + 1]))
    roots, ages = [b for b in blocks if not b.path], [b for b in blocks if b.object_type == "pvAgeing"]
    if len(roots) != 1 or roots[0].object_type != "pvMainArray":
        raise ValueError("Require one owning pvMainArray root object")
    if len(ages) != 1 or ages[0].path != "PVObject_Ageing":
        raise ValueError("Require exactly one direct PVObject_Ageing block")
    root, age = roots[0], ages[0]
    header_end = next((i for i, line in enumerate(lines[1:], 1) if line.strip().startswith("PVObject_")), shift)
    versions = [m[4] for i in range(1, header_end) if (m := ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))) is not None and m[2] == "Version"]
    if len(versions) != 1 or versions[0] not in NATIVE_TEXT:
        raise ValueError(f"Ageing model editing requires root Version={TEXT_LABEL}")
    raw_f2 = one(root.fields, "Flags2")[1]
    if not isinstance(raw_f2, str) or not _HEX_FLAG.fullmatch(raw_f2):
        raise ValueError("main Flags2 must be a hexadecimal $ value of 1..8 digits")
    if (f2_val := int(raw_f2[1:], 16)) not in (0x10082A, 0x82A):
        raise ValueError(f"Ageing editing requires the verified main Flags2={MAIN_FLAGS2_ON} or {MAIN_FLAGS2_OFF}")
    enabled = f2_val == 0x10082A
    if require_enabled and not enabled:
        raise ValueError("Ageing model editing requires the verified enabled template")
    one(root.fields, "Flags")
    subs = [b for b in blocks if b.object_type == "pvSubArray"]
    if len(subs) != 1 or one(subs[0].fields, "Flags2")[1] != "$20":
        raise ValueError("Require exactly one subarray block" if len(subs) != 1 else "Require subarray Flags2=20 for this verified template")
    if set(age.fields) != AGE_KNOWN:
        raise ValueError("Ageing template requires exactly the twelve verified fields")
    for key, req in AGE_FIXED.items():
        if one(age.fields, key)[1] != req:
            raise ValueError(f"Ageing template requires {key}={req}")
    if not re.fullmatch(r"[0-9]+", one(age.fields, "PlotKind")[1]):
        raise ValueError("PlotKind must be a native integer")
    if one(age.fields, KEEP_MC)[1] not in ("True", "False"):
        raise ValueError("KeepMCValues must be a native boolean")
    for key in (RMS_ISC, RMS_VOC):
        if not re.fullmatch(r"[0-9]+\.[0-9]{2}", raw_rms := one(age.fields, key)[1]):
            raise ValueError("Require the recorded two-decimal native " + key)
        try:
            _age_pct(float(raw_rms), key, 100)
        except (ValueError, OverflowError):
            raise ValueError("Require a finite native " + key) from None
    for key, owner in ((YEAR_FIELD, root.fields), (STORED_LOSS, root.fields), (STORED_MISMATCH, root.fields), (MEAN_FIELD, age.fields)):
        if _count_field(lines, key) != len(owner.get(key, [])):
            raise ValueError(f"Require {key} only in its owning object")
    if not re.fullmatch(r"[0-9]+", year_raw := one(root.fields, YEAR_FIELD)[1]):
        raise ValueError("Require a native integer NoYearSimul")
    stored = {k: finite(float(one(root.fields, k)[1]), k, minimum=0, maximum=100) for k in (STORED_LOSS, STORED_MISMATCH)}
    if stored[STORED_MISMATCH] != 0:
        raise ValueError("The recorded template requires stored mismatch factor 0")
    try:
        mean_pct = _age_pct(float(one(age.fields, MEAN_FIELD)[1]), "annual_module_degradation_percent", 1)
    except (ValueError, OverflowError):
        raise ValueError("Require a finite native ModuleDegradFactor") from None
    return shift, root, age, {
        "mode": "template", "enabled": enabled, "simulation_year": _age_year(int(year_raw)),
        "annual_module_degradation_percent": mean_pct, "stored_loss_factor_percent": stored[STORED_LOSS],
        "stored_mismatch_factor_percent": stored[STORED_MISMATCH], "template": {k: one(age.fields, k)[1] for k in AGE_TEMPLATE_ORDER},
    }


def ageing_model_state(lines, bounds):
    """Disabled templates stay readable; malformed shapes report mode None and an issue."""
    return _try_state(lambda: _read_ageing(lines, bounds, require_enabled=False)[3])


def replace_ageing_model(lines, bounds, updates):
    """Validate everything on a candidate copy, then commit the copy once."""
    if not isinstance(updates, dict):
        raise ValueError("Require a mapping of ageing model inputs")
    req, opt = ("simulation_year", "annual_module_degradation_percent"), ("keep_mc_values", RMS_ISC_KEY, RMS_VOC_KEY)
    if (set(updates) - set(req) - set(opt)) or not set(req) <= set(updates):
        raise ValueError(f"Require exactly simulation_year and annual_module_degradation_percent, with optional keep_mc_values, {RMS_ISC_KEY} and {RMS_VOC_KEY}")
    if (RMS_ISC_KEY in updates) != (RMS_VOC_KEY in updates):
        raise ValueError(f"{RMS_ISC_KEY} and {RMS_VOC_KEY} must be supplied together")
    plan = [(YEAR_FIELD, f"{_age_year(updates['simulation_year']):d}"), (MEAN_FIELD, format(_age_pct(updates["annual_module_degradation_percent"], "annual_module_degradation_percent", 1), ".2f"))]
    if "keep_mc_values" in updates:
        if not isinstance(updates["keep_mc_values"], bool):
            raise ValueError("keep_mc_values must be a strict boolean")
        plan.append((KEEP_MC, "True" if updates["keep_mc_values"] else "False"))
    if RMS_ISC_KEY in updates:
        plan.extend([(RMS_ISC, format(_age_pct(updates[RMS_ISC_KEY], RMS_ISC, 100), ".2f")), (RMS_VOC, format(_age_pct(updates[RMS_VOC_KEY], RMS_VOC, 100), ".2f"))])
    shift, root, age, before = _read_ageing(lines, bounds)
    owner = {YEAR_FIELD: root.fields, MEAN_FIELD: age.fields, KEEP_MC: age.fields, RMS_ISC: age.fields, RMS_VOC: age.fields}
    if all(one(owner[k], k)[1] == raw for k, raw in plan):
        return {"before": before, "after": before}
    candidate = list(lines)
    for key, raw in plan:
        candidate[shift + one(owner[key], key)[0]] = replace_value(candidate[shift + one(owner[key], key)[0]], raw)
    after = _read_ageing(candidate, bounds)[3]
    lines[:] = candidate
    return {"before": before, "after": after}


def replace_ageing_enabled(lines, bounds, enabled):
    """Flip only the verified enable bit, after proving the same template reads back."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a strict boolean")
    shift, root, _, before = _read_ageing(lines, bounds, require_enabled=False)
    candidate, entry = list(lines), one(root.fields, "Flags2")
    value = int(entry[1][1:], 16)
    if ((value | 0x100000) if enabled else (value & ~0x100000)) != value:
        candidate[shift + entry[0]] = replace_value(candidate[shift + entry[0]], MAIN_FLAGS2_ON if enabled else MAIN_FLAGS2_OFF)
    after = _read_ageing(candidate, bounds, require_enabled=False)[3]
    if after["enabled"] is not enabled:
        raise ValueError("The candidate template did not reach the requested enable state")
    lines[:] = candidate
    return {"before": before, "after": after}
