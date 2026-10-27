"""Read-only discovery of accurate loose components and one eligible template variant.

Locates PAN/OND loose files by exact Manufacturer+Model or by explicit filename in the
workspace and the installed library, then selects one workspace PRJ/VC member that passes
the single-subarray, single-fixed-plane, Grid, PRCorrTemp=False and no-active-geometry
gates. Nothing is created, copied or edited, no identity is guessed, and the encrypted
built-in component database is never enumerated.
"""
from __future__ import annotations

from pvsyst_auto.components.store import electrical_inventory
from pvsyst_auto.components.pan import validation_fields as pan_validation_fields
from pvsyst_auto.project.circuit import positive_integer
from pvsyst_auto.project.paths import variant_id
from pvsyst_auto.engineering.sizing import plan_capacity

SELECTION_RULE = (
    "Components: exact case-insensitive Manufacturer+Model equality over workspace and installed loose files "
    "(or one explicit filename); workspace loose files win over the installed library. One usable match is "
    "selected, two or more usable matches in the winning library are reported as ambiguous and block the "
    "discovery; no fuzzy, approximate or proxy component substitution and no encrypted-database enumeration. "
    "Templates: only workspace PRJ/VC members that pass one subarray, one ordinary fixed plane, Grid, "
    "PRCorrTemp=False and no-active-geometry checks are eligible. With capacity every eligible member must "
    "return a ready plan_capacity result, and members are ordered by fewest inverter changes from the "
    "template counts, then fewest parameter steps, then capacity error, then the stable project/variant name."
)


def _public(candidate: dict) -> dict:
    return {key: value for key, value in candidate.items() if not key.startswith("_")}


def _loose_files(components, kind: str, library: str, query: str) -> list[dict]:
    items, offset = [], 0
    while True:
        page = components.list(kind, library, query=query, offset=offset, limit=200)
        items.extend(page["items"])
        offset += len(page["items"])
        if not page["items"] or offset >= page["total"]:
            return items


def _resolve(components, kind: str, name: str, manufacturer: str, model: str, result: dict) -> list[dict]:
    candidates = []
    for library in ("workspace", "builtin"):
        if name:
            try:
                path = components._path(kind, name, library)
            except ValueError as exc:
                if library == "builtin" and components.builtin is None:
                    result["warnings"].append(
                        f"{kind}: no installed component library is configured; only workspace loose files were searched")
                    continue
                result["issues"].append({"field": f"{kind}.name", "message": str(exc)})
                return []
            if path.is_file():
                candidates.append({"library": library, "name": name})
            continue
        try:
            found = _loose_files(components, kind, library, model)
        except (ValueError, OSError) as exc:
            if library == "builtin" and components.builtin is None:
                result["warnings"].append(
                    f"{kind}: no installed component library is configured; only workspace loose files were searched")
                continue
            result["warnings"].append(
                f"{kind}: {library} loose files are unavailable ({exc}); the encrypted built-in database is not enumerable")
            continue
        for item in found:
            if ((item.get("manufacturer") or "").casefold() == manufacturer.casefold()
                    and (item.get("model") or "").casefold() == model.casefold()):
                candidates.append({"library": library, "name": item["name"]})
    candidates.sort(key=lambda item: (item["library"] != "workspace", item["name"].casefold()))
    if not candidates:
        target = f"file {name}" if name else f"{manufacturer} {model}"
        result["issues"].append({"field": f"{kind}.name", "message":
            f"No loose file matches {target} in the workspace or the installed library; the encrypted built-in "
            "database cannot be enumerated and no approximate identity is substituted"})
        return []
    for candidate in candidates:
        _verify(components, kind, candidate, manufacturer, model, result)
    return candidates


def _verify(components, kind: str, candidate: dict, manufacturer: str, model: str, result: dict) -> None:
    errors = []
    try:
        path = components._path(kind, candidate["name"], candidate["library"])
        raw = components._read(path)
        info = components._inspect(kind, raw)
        components._require_valid(info, complete=True)
        candidate.update(format=info["format"], manufacturer=info.get("manufacturer"),
                         model=info.get("model"), path=str(path), _text=raw.decode("utf-8-sig"))
    except (ValueError, OSError) as exc:
        candidate.update(format=None, manufacturer=None, model=None, path=None, valid=False, errors=[str(exc)])
        return
    if manufacturer and (candidate["manufacturer"] or "").casefold() != manufacturer.casefold():
        errors.append(f"Declared manufacturer {manufacturer!r} does not match {candidate['manufacturer']!r}")
    if model and (candidate["model"] or "").casefold() != model.casefold():
        errors.append(f"Declared model {model!r} does not match {candidate['model']!r}")
    candidate.update(valid=not errors, errors=errors)
    if candidate["library"] == "builtin":
        try:
            shadow = components._path(kind, candidate["name"]).is_file()
        except ValueError:
            shadow = False
        if shadow:
            # Only this candidate is unusable; a usable workspace identity must stay selectable.
            result["warnings"].append(
                f"{kind}: workspace file {candidate['name']} shadows the installed-library match; "
                "the installed identity is not substituted")
            candidate.update(valid=False, errors=[*candidate["errors"],
                "Shadowed by a workspace file with the same name; identity is not substituted"])


def _bound(kind: str, candidate: dict, result: dict) -> dict:
    """Attach declared reference-point fields for the selected component only."""
    try:
        candidate["electrical"] = electrical_inventory(kind, candidate["_text"])
    except (ValueError, KeyError) as exc:
        candidate["electrical"] = {"status": "unreadable", "issues": [str(exc)]}
        result["warnings"].append(f"{kind}: declared electrical fields are unreadable ({exc})")
    return candidate


def _select(kind: str, candidates: list[dict], result: dict) -> dict | None:
    usable = [item for item in candidates if item.get("valid")]
    workspace = [item for item in usable if item["library"] == "workspace"]
    installed = [item for item in usable if item["library"] == "builtin"]
    if len(workspace) > 1:
        result["issues"].append({"field": f"{kind}.name", "message":
            f"{len(workspace)} usable workspace files share this identity; pass one filename explicitly: "
            + ", ".join(item["name"] for item in workspace)})
        return None
    if workspace:
        if installed:
            result["warnings"].append(f"{kind}: workspace {workspace[0]['name']} is used and takes precedence "
                                      "over the installed-library match")
        return _bound(kind, workspace[0], result)
    if len(installed) > 1:
        result["issues"].append({"field": f"{kind}.name", "message":
            f"{len(installed)} usable installed-library files share this identity; pass one filename explicitly: "
            + ", ".join(item["name"] for item in installed)})
        return None
    if installed:
        return _bound(kind, installed[0], result)
    if candidates:
        result["issues"].append({"field": f"{kind}.name", "message": "No usable component: " + "; ".join(
            f"{item['name']} ({item['library']}): " + "; ".join(item.get("errors") or ["unusable"])
            for item in candidates)})
    return None


def _report(project: str, variant: str, reasons: list[str]) -> dict:
    return {"project": project, "variant": variant, "valid": False, "reasons": reasons, "subarray_id": None,
            "orientation_id": None, "counts": {}, "capacity_plan": None,
            "_key": (project.casefold(), variant)}


def _pairs(store, template_project: str, variant: str, result: dict) -> tuple[list[tuple[str, str]], list[dict]]:
    """Eligible (project, variant) pairs plus rejected scan records.

    An explicit template_project keeps hard errors; an automatic scan records unusable
    projects as rejected candidates instead of blocking every other eligible template.
    """
    try:
        root = store._root()
    except ValueError as exc:
        result["issues"].append({"field": "template_project", "message": str(exc)})
        return [], []
    if template_project:
        names = [template_project]
    else:
        try:
            names = sorted((path.name for path in root.iterdir() if path.is_file() and not path.is_symlink()
                            and path.suffix.upper() == ".PRJ"), key=str.casefold)
        except OSError as exc:
            result["issues"].append({"field": "template_project",
                                     "message": f"Workspace projects are unreadable: {exc}"})
            return [], []
        if not names:
            result["issues"].append({"field": "template_project",
                                     "message": "No readable workspace PRJ with a variant was found"})
            return [], []
    wanted = variant_id(variant) if variant else None
    pairs, rejected = [], []
    for name in names:
        try:
            info = store.inspect_project(name)
        except (ValueError, OSError) as exc:
            if template_project:
                result["issues"].append({"field": "template_project", "message": f"{name}: {exc}"})
            else:
                rejected.append(_report(name, wanted or "", [f"Project is not readable: {exc}"]))
            continue
        members = sorted(info["variants"], key=str.casefold)
        if not members:
            if template_project:
                result["issues"].append({"field": "variant", "message": f"{name}: no workspace variant"})
            else:
                rejected.append(_report(name, wanted or "", ["Project has no workspace variant member"]))
            continue
        if wanted is not None:
            if wanted not in [member.upper() for member in members]:
                if template_project:
                    result["issues"].append({"field": "variant", "message": f"{name}: variant {wanted} is missing"})
                else:
                    rejected.append(_report(name, wanted, [f"Variant {wanted} is missing from this project"]))
                continue
            members = [wanted]
        pairs.extend((name, member.upper()) for member in members)
    return pairs, rejected


def _preflight(store, project: str, variant: str, capacity, pan_text, ond_text) -> dict:
    report = _report(project, variant, [])
    try:
        parameters = store.inspect_parameters(project, variant)
        structure = store.validate_structure(project, variant)
        if structure["valid"] is not True or structure["system_type"] != "Grid":
            raise ValueError("Require a completely checked Grid template: "
                             + "; ".join(structure["issues"] + structure["unchecked_checks"]))
        if len(parameters["subarrays"]) != 1 or len(parameters["orientations"]) != 1:
            raise ValueError("Require exactly one subarray and one fixed plane")
        data = store._read(store._path(project, variant))
        _, lines = store._text(data)
        child = next(index for index in range(1, len(lines)) if lines[index].strip().startswith("PVObject_"))
        corrected = store._field_indices(lines, (0, child), "PRCorrTemp")
        if len(corrected) != 1 or lines[corrected[0]].split("=", 1)[1].strip() != "False":
            raise ValueError("Template must explicitly declare PRCorrTemp=False")
        store._require_no_geometry(lines)
        array = parameters["subarrays"][0]
        plane = parameters["orientations"][0]["id"]
        store._ordinary_fixed_plane(lines, store._orientation(lines, plane), allow_missing_flags=True)
        counts = {key: array["values"].get(key) for key in ("NModSerie", "NStringCh", "NInvMPPT", "NInverter")}
        report.update(subarray_id=array["id"], orientation_id=plane, counts=counts)
    except (ValueError, OSError, KeyError, StopIteration) as exc:
        report["reasons"].append(str(exc))
        return report
    if capacity is None:
        report["valid"] = True
        return report
    if pan_text is None or ond_text is None:
        report["reasons"].append("Capacity planning requires the resolved PAN and OND components")
        return report
    try:
        plan = plan_capacity(store, lines, array["id"], pan_validation_fields(pan_text),
                             electrical_inventory("OND", ond_text), capacity)
    except (ValueError, OSError, KeyError) as exc:
        report["reasons"].append(str(exc))
        return report
    report["capacity_plan"] = plan
    if plan["status"] != "ready":
        report["reasons"].extend(f"{item['field']}: {item['message']}" for item in plan["issues"])
        return report
    try:
        # The ordering key compares against the template count; an unreadable count is never guessed.
        current = positive_integer(counts.get("NInverter"), "Template NInverter")
    except ValueError as exc:
        report["reasons"].append(str(exc))
        return report
    report["valid"] = True
    report["_key"] = (abs(plan["inverter_count"] - current), len(plan["steps"]),
                      round(abs(plan["capacity_error_pct"]), 9), project.casefold(), variant)
    return report


def discover_design_inputs(store, *, pan_name="", ond_name="", module_manufacturer="", module_model="",
                           inverter_manufacturer="", inverter_model="", template_project="",
                           variant="", capacity=None, tilt=None):
    """Locate one exact PAN/OND pair and one eligible template variant without writing or guessing.

    Returns status, selected, resolved_components, candidates, issues, warnings and selection_rule.
    resolved_components holds the selected PAN/OND files whenever both were resolved exactly, even
    when no eligible template leaves status blocked, so missing template input never hides resolved
    components; it is None while any component is ambiguous, unusable or missing. status is ready
    only when both components and one template are selected with an empty issue list.
    tilt is accepted for the design-flow caller only and never filters, orders or blocks templates:
    that caller validates it and its configuration step writes it onto the selected fixed plane.
    """
    components = getattr(store, "components", store)
    result = {"status": "blocked", "selected": None, "resolved_components": None,
              "candidates": {"PAN": [], "OND": [], "templates": []},
              "issues": [], "warnings": [], "selection_rule": SELECTION_RULE}

    def issue(field, message):
        result["issues"].append({"field": field, "message": str(message)})

    if not hasattr(store, "_root"):
        issue("store", "Discovery requires a VariantStore for workspace projects and component paths")

    chosen = {}
    for kind, name, manufacturer, model in (("PAN", pan_name, module_manufacturer, module_model),
                                            ("OND", ond_name, inverter_manufacturer, inverter_model)):
        if not name and not (manufacturer and model):
            issue(f"{kind}.name", "Provide one exact Manufacturer+Model pair or an explicit loose filename")
            continue
        try:
            candidates = _resolve(components, kind, name, manufacturer, model, result)
        except (ValueError, OSError) as exc:
            issue(f"{kind}.name", exc)
            continue
        result["candidates"][kind] = [_public(item) for item in candidates]
        if selected := _select(kind, candidates, result):
            chosen[kind] = selected

    reports = []
    eligible = []
    if not result["issues"]:
        try:
            pairs, reports = _pairs(store, template_project, variant, result)
        except ValueError as exc:
            issue("variant", exc)
            pairs, reports = [], []
        for project, member in pairs:
            reports.append(_preflight(store, project, member, capacity,
                                      (chosen.get("PAN") or {}).get("_text"),
                                      (chosen.get("OND") or {}).get("_text")))
        result["candidates"]["templates"] = [_public(report) for report in reports]
        eligible = [report for report in reports if report["valid"]]
        for report in reports:
            if report["valid"]:
                continue
            reason = "; ".join(report["reasons"])
            if eligible:
                result["warnings"].append(f"{report['project']}/{report['variant']} is not eligible: {reason}")
            else:
                issue(f"template {report['project']}/{report['variant']}", reason)

    template = min(eligible, key=lambda report: report["_key"]) if eligible else None
    if len(chosen) == 2:
        result["resolved_components"] = {"PAN": _public(chosen["PAN"]), "OND": _public(chosen["OND"])}
    if template is not None and len(chosen) == 2 and not result["issues"]:
        result["selected"] = {"PAN": _public(chosen["PAN"]), "OND": _public(chosen["OND"]),
                              "template": _public(template)}
        result["status"] = "ready"
    return result
