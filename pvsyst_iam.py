"""Native 8.1.6 IAM model selection for the owning pvSubArray.

``_read`` / ``iam_model_state`` / ``replace_iam_model`` take ``(lines, bounds)`` and
only ever touch the owning subarray. Blocks are located through the shared object
parser by object path, never by flattened line scanning. No curve, Fresnel, cosine
or interpolation value is computed, and no other component's cache is copied.
Accepted source shapes are limited to the verified component-state forms (no
``pvIAM`` block, a retained Ashrae block, a preserved ``FresnelAR`` optics cache)
and the stored Ashrae state; the ``ashrae_b0`` adapter domain is 0..1.
"""
from __future__ import annotations

import re
from decimal import Decimal

from pvsyst_component_editor import ASSIGNMENT, Profile, objects, replace_value
from pvsyst_generator import finite, one

COMPONENT_BIT = 0x20
ALLOWED_FLAGS2 = (0x20, 0x00)
IAM_PATH = "PVObject_IAM"
IAM_TYPE = "pvIAM"
PROFILE_PATH = "PVObject_IAM/IAMProfile"
PROFILE_TYPE = "TCubicProfile"
ANCHOR_KEY = "WiringResistance"
ANCHOR_VALUE = "TWiringResistance"
ASHRAE_KEYS = ("Flags", "IAMMode", "B0_IAM")
OPTIC_KEYS = ("N_Glass", "N_EVA", "N_ARCoating", "DGlass", "DArCoating")
# IAMProfile is parsed as a child object, so it is not among the pvIAM root fields
FRESNEL_KEYS = ("Flags", "IAMMode") + OPTIC_KEYS
CACHE_KEYS = ("NPtsMax", "NPtsEff", "LastCompile", "Mode")
SCOPE = ("Owning subarray only; native IAM component bit5 and the pvIAM IAMMode/B0_IAM fields. "
         "Accepted source shapes: component with no pvIAM block, component with a retained Ashrae "
         "block, component with a preserved FresnelAR optics cache, and the stored Ashrae state. "
         "A retained cache is reported as stored metadata and is never treated as an effective "
         "ASHRAE model while bit5 is set. No curve evaluation.")


def _blocks(lines, bounds):
    """Parse the whole subarray slice with the shared parser, then reject any nesting problem."""
    parsed = objects("".join(lines[bounds[0]:bounds[1] + 1]))
    for block in parsed:
        if block.problems:
            raise ValueError(f"Unsupported {block.path}: {block.problems[0]}")
    return parsed


def _count(lines, key):
    return sum(m is not None and m[2] == key for line in lines
               for m in [ASSIGNMENT.fullmatch(line.rstrip("\r\n"))])


def _children(parsed):
    return [block for block in parsed if block.path.startswith(IAM_PATH + "/")]


def _owning_pviam(lines, bounds, parsed):
    found = [b for b in parsed if b.path == IAM_PATH and b.object_type == IAM_TYPE]
    if len(found) > 1:
        raise ValueError("Require one owning pvIAM block")
    total = sum(m is not None and m[2].startswith("PVObject_") and m[4] == IAM_TYPE
                for line in lines for m in [ASSIGNMENT.fullmatch(line.rstrip("\r\n"))])
    if total != (1 if found else 0):
        raise ValueError("Require pvIAM only in the owning subarray")
    return found[0] if found else None


def _step(number, label):
    scaled = Decimal(str(number)) * 1000
    if scaled != scaled.to_integral_value():
        raise ValueError(f"Use 0.001 increments for {label}")
    return number


def _finite_entry(block, key, label):
    entry = one(block.fields, key)
    try:
        return finite(float(entry[1]), label)
    except (ValueError, OverflowError):
        raise ValueError(f"Require a finite native {label}") from None


def _ashrae_shape(parsed, block):
    if set(block.fields) != set(ASHRAE_KEYS):
        raise ValueError("Require exactly Flags/IAMMode/B0_IAM in the Ashrae pvIAM")
    if _children(parsed):
        raise ValueError("Require no nested objects inside the Ashrae pvIAM")
    if one(block.fields, "Flags")[1] != "$00" or one(block.fields, "IAMMode")[1] != "Ashrae":
        raise ValueError("Require native Flags=$00 and IAMMode=Ashrae")
    entry = one(block.fields, "B0_IAM")
    try:
        number = finite(float(entry[1]), "B0_IAM", minimum=0, maximum=1)
    except (ValueError, OverflowError):
        raise ValueError("Require a finite native B0_IAM in 0..1") from None
    return _step(number, "B0_IAM")


def _fresnel_shape(parsed, block):
    """Verified optics cache only; Profile.points() does format/finite/count work, not evaluation."""
    if set(block.fields) != set(FRESNEL_KEYS):
        raise ValueError("Require exactly Flags/IAMMode and the five optics fields in the FresnelAR pvIAM")
    if one(block.fields, "Flags")[1] != "$00" or one(block.fields, "IAMMode")[1] != "FresnelAR":
        raise ValueError("Require native Flags=$00 and IAMMode=FresnelAR")
    for key in OPTIC_KEYS:
        _finite_entry(block, key, key)
    children = _children(parsed)
    if len(children) != 1 or children[0].path != PROFILE_PATH or children[0].object_type != PROFILE_TYPE:
        raise ValueError("Require exactly one IAMProfile/TCubicProfile child in the FresnelAR pvIAM")
    curve = children[0]
    if not isinstance(curve, Profile):
        raise ValueError("Require one owning IAMProfile/TCubicProfile")
    extra = set(curve.fields) - (set(CACHE_KEYS) | {f"Point_{i}" for i in range(1, 10)})
    if extra:
        raise ValueError(f"Refusing to drop unknown TCubicProfile field {sorted(extra)[0]}")
    curve.points()
    for key in ("LastCompile", "Mode"):
        one(curve.fields, key)
    return True


def _read(lines, bounds):
    if not isinstance(bounds, tuple) or len(bounds) != 2:
        raise ValueError("Require one owning subarray bounds")
    parsed = _blocks(lines, bounds)
    owners = [block for block in parsed if block.path == ""]
    if len(owners) != 1:
        raise ValueError("Require one owning pvSubArray header")
    fields = owners[0].fields
    if one(fields, "Version")[1] != "8.1.6":
        raise ValueError("IAM model editing requires subarray Version=8.1.6")
    index, raw = one(fields, "Flags2")
    # objects() reports line numbers relative to the slice, so rebase before matching
    index += bounds[0]
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", raw) or int(raw[1:], 16) not in ALLOWED_FLAGS2:
        raise ValueError("Require the verified subarray Flags2 $20 or $00")
    block = _owning_pviam(lines, bounds, parsed)
    # these keys exist only in the owning block, so a blockless source must have none at all
    for key in ("IAMMode", "B0_IAM"):
        if _count(lines, key) != (len(block.fields.get(key, [])) if block is not None else 0):
            raise ValueError(f"Require {key} only in the owning pvIAM")
    component = bool(int(raw[1:], 16) & COMPONENT_BIT)
    if block is None:
        if not component:
            raise ValueError("Require a stored Ashrae pvIAM block when the component bit is clear")
        state = {"effective": "component", "stored_model": None, "stored_ashrae_b0": None}
    else:
        model = one(block.fields, "IAMMode")[1]
        if model == "Ashrae":
            b0 = _ashrae_shape(parsed, block)
        elif model == "FresnelAR" and component:
            _fresnel_shape(parsed, block)
            b0 = None
        else:
            raise ValueError("Require a verified Ashrae or component FresnelAR pvIAM state")
        state = {"effective": "component" if component else "ashrae",
                 "stored_model": model, "stored_ashrae_b0": b0}
    return fields, index, raw, block, state


def iam_model_state(lines, bounds):
    try:
        return _read(lines, bounds)[4]
    except ValueError as exc:
        return {"effective": None, "issue": str(exc)}


def _anchor(lines, bounds, fields):
    """The verified insertion point: the sibling WiringResistance header at the exact owner indent."""
    version = ASSIGNMENT.fullmatch(lines[bounds[0] + fields["Version"][0][0]].rstrip("\r\n"))
    indent = version[1]
    found = []
    for i in range(bounds[0] + 1, bounds[1]):
        match = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))
        if match is None or match[1] != indent or match[2] != ANCHOR_KEY:
            continue
        if match[4] != ANCHOR_VALUE:
            raise ValueError(f"Require the native {ANCHOR_KEY}={ANCHOR_VALUE} anchor")
        found.append(i)
    if len(found) != 1:
        raise ValueError(f"Require one owning {ANCHOR_KEY} anchor")
    return found[0]


def _minimal_block(indent, newline, b0):
    # indent already carries the subarray indent plus two spaces (the pvIAM block header)
    return [f"{indent}{IAM_PATH}={IAM_TYPE}{newline}", f"{indent}  Flags=$00{newline}",
            f"{indent}  IAMMode=Ashrae{newline}", f"{indent}  B0_IAM={b0:.3f}{newline}",
            f"{indent}End of PVObject {IAM_TYPE}{newline}"]


def _body(lines, bounds, index, raw, block, anchor, mode, b0):
    first = lines[bounds[0]]
    indent = first[:len(first) - len(first.lstrip())] + "  "
    newline = "\r\n" if first.endswith("\r\n") else "\n"
    target = COMPONENT_BIT if mode == "component" else 0x00
    minimal = _minimal_block(indent, newline, b0) if mode == "ashrae" else []
    span = None if block is None else (bounds[0] + block.line - 1, bounds[0] + block.end_line - 1)
    body = []
    for i in range(bounds[0] + 1, bounds[1]):
        if i == index:
            body.append(lines[i] if target == int(raw[1:], 16)
                        else replace_value(lines[i], "$%0*X" % (len(raw) - 1, target)))
            continue
        if mode == "ashrae" and span is not None and span[0] <= i <= span[1]:
            if i == span[0]:
                body.extend(minimal)
            continue
        # a missing block goes in before the WiringResistance sibling, matching the native order
        if mode == "ashrae" and span is None and i == anchor:
            body.extend(minimal)
        body.append(lines[i])
    return body


def replace_iam_model(lines, bounds, updates):
    if not isinstance(updates, dict) or updates.get("mode") not in ("component", "ashrae"):
        raise ValueError("Require IAM model mode component or ashrae")
    mode = updates["mode"]
    keys = {"mode"} if mode == "component" else {"mode", "ashrae_b0"}
    if set(updates) != keys:
        raise ValueError("component accepts only mode; ashrae requires ashrae_b0")
    b0 = None
    if mode == "ashrae":
        b0 = _step(finite(updates["ashrae_b0"], "ashrae_b0", minimum=0, maximum=1), "ashrae_b0")
    fields, index, raw, block, before = _read(lines, bounds)
    body = _body(lines, bounds, index, raw, block, _anchor(lines, bounds, fields), mode, b0)
    candidate = lines[:bounds[0] + 1] + body + lines[bounds[1]:]
    # the body may change the block height, so re-read the candidate with its own bounds
    after = _read(candidate, (bounds[0], bounds[0] + 1 + len(body)))[4]
    if before == after:
        return {"before": before, "after": after, "scope": SCOPE}
    lines[bounds[0] + 1:bounds[1]] = body
    return {"before": before, "after": after, "scope": SCOPE}
