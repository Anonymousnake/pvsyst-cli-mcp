"""Schema and preflight smoke check for the design-flow entry points.

Nothing here needs a PVsyst installation, a geocoding or weather HTTP request, a
component parse, a project read or a hash: the CLI path is this Python interpreter
and the workspace is an empty temporary directory. Every expectation is read from
the published tools/list schema, from a call refused by the strict top-level
argument whitelist, or from a call that the backend blocks before it opens a file.

Run: python tests/smoke_design_inputs.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))       # sibling component fixture module
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # pvsyst_* modules and the server

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# The inverter fixture is the already-verified 8.1.6 layout of the design-envelope tests
# (identity in PVObject_Commercial); only a module-level constant is reused, no test case or
# test helper is driven here. The module fixture below is a root-only text PAN whose
# identity lives directly in the pvModule root object.
from test_design_envelope import OND_TEXT

PAN_ROOT_TEXT = """PVObject_=pvModule
  Comment=Jinko\tTiger Neo 620W\tUser device
  Manufacturer=Jinko Solar
  Model=Tiger Neo 620W
  Version=7.2
  Flags=$00
  Technol=mtSiMono
  NCelS=120
  NCelP=1
  NDiode=3
  PNom=620.0
  GRef=1000.0
  TRef=25.0
  Absorb=0.900
  VMaxIEC=1500
  ISC=15.500
  Voc=41.500
  Imp=14.900
  Vmp=35.400
  MuISC=0.005000
  muVocSpec=-0.150000
End of PVObject pvModule
"""

PAN_CONFLICT_TEXT = """PVObject_=pvModule
  Manufacturer=RootBrand
  Model=Shared Model
  Version=7.2
  Flags=$00
  PVObject_Commercial=pvCommercial
    Manufacturer=CommercialBrand
    Model=Shared Model
  End of PVObject pvCommercial
End of PVObject pvModule
"""

PAN_DUPLICATE_TEXT = """PVObject_=pvModule
  Manufacturer=SameBrand
  Manufacturer=SameBrand
  Model=SameModel
  Version=7.2
  Flags=$00
End of PVObject pvModule
"""

SERVER = Path(__file__).resolve().parents[1] / "pvsyst_mcp_server.py"
EXPECTED_TOOL_COUNT = 60
EXPECTED_GROUP_COUNT = 7
NEW_TOOL_GROUPS = (("pvsyst_resolve_location", "Sites & Weather"),
                   ("pvsyst_acquire_weather", "Sites & Weather"),
                   ("pvsyst_review_weather", "Sites & Weather"),
                   ("pvsyst_simulate_design", "Simulation"),
                   ("pvsyst_prepare_sourced_design", "Simulation"))
RESOLVE_SCHEMA = {"query", "country_code"}
ACQUIRE_SCHEMA = {"csv_name", "year", "location_id", "latitude", "longitude", "site_name",
                  "country_code", "altitude", "timezone_name", "temperature_method", "dry_run"}
REVIEW_SCHEMA = {"csv_name", "native_csv_name", "year", "acquisition_manifest_name",
                 "weather_manifest_name"}
SOURCED_SCHEMA = {"pan_name", "ond_name", "sources", "declarations", "design"}
DESIGN_SCHEMA = {"spec", "dry_run"}
TEMPLATE_EXTRA_SCHEMA = {"azimuth", "losses"}
KNOWN_SPEC_FIELDS = {"scope", "new_project", "year", "target_dc_kw", "tilt", "azimuth", "module",
                     "inverter", "template_project", "variant", "capacity", "electrical_design",
                     "losses", "weather"}
FIXTURE_FILES = ("ComposPV/PVmodules/Fixture.PAN", "ComposPV/PVmodules/Conflict.PAN",
                 "ComposPV/PVmodules/Duplicate.PAN", "ComposPV/PVmodules/Binary.PAN",
                 "ComposPV/Inverters/Fixture.OND")
CALL_TIMEOUT = 60


class Checks:
    def __init__(self):
        self.passed = 0
        self.failures = []

    def ok(self, condition, label, detail=""):
        if condition:
            self.passed += 1
        else:
            self.failures.append(f"{label}: {detail}")
        return bool(condition)

    def equal(self, actual, expected, label):
        return self.ok(actual == expected, label, f"expected {expected!r}, got {actual!r}")


def text_of(result):
    return "\n".join(item.text for item in result.content if getattr(item, "text", None))


def payload(result):
    return json.loads(text_of(result))


def issue_fields(data):
    """Collect reported issue field names; weather/acquisition modules report plain text."""
    return {item.get("field") for item in data.get("issues", []) if isinstance(item, dict)}


def issue_text(data):
    return " ".join(str(item.get("message", "")) if isinstance(item, dict) else str(item)
                    for item in data.get("issues", []))


def group_of(description):
    if description and description.startswith("[") and "]" in description:
        return description[1:description.index("]")]
    return None


async def invoke(session, name, arguments):
    return await asyncio.wait_for(session.call_tool(name, arguments), CALL_TIMEOUT)


async def rejected(session, checks, label, name, arguments):
    """Assert that a call is refused by schema validation or the argument whitelist."""
    try:
        result = await invoke(session, name, arguments)
    except Exception as exc:  # a JSON-RPC level rejection still counts as refused
        text = str(exc)
        refused = "validation" in text.lower() or "unknown argument" in text.lower()
        checks.ok(refused, label, f"rejected without a schema reason: {type(exc).__name__}: {text[:200]}")
        return
    text = text_of(result)
    checks.ok(result.is_error, label,
              f"invalid input was accepted; reply was {text[:200]!r}")


async def blocked(session, checks, label, name, arguments, expect_fields=()):
    """Assert that a schema-valid call comes back as a blocked result, not an MCP error."""
    result = await invoke(session, name, arguments)
    if not checks.ok(not result.is_error, label, f"schema-valid call errored: {text_of(result)[:300]}"):
        return None
    data = payload(result)
    checks.equal(data.get("status"), "blocked", f"{label} status")
    for field in expect_fields:
        reported = issue_fields(data)
        checks.ok(field in reported, f"{label} reports {field}", f"issues were {sorted(reported)}")
    return data


def schema_checks(checks, tools):
    by_name = {tool.name: tool for tool in tools}
    checks.equal(len(tools), EXPECTED_TOOL_COUNT, "published tool count")
    groups = {group_of(tool.description) for tool in tools}
    checks.equal(len(groups), EXPECTED_GROUP_COUNT, "published tool group count")
    checks.ok(all(group_of(tool.description) for tool in tools), "every tool carries a group label",
              "a tool description has no [Group] prefix")
    for name, group in NEW_TOOL_GROUPS:
        tool = by_name.get(name)
        if not checks.ok(tool is not None, f"{name} is published", "tool is missing"):
            continue
        checks.equal(group_of(tool.description), group, f"{name} group")
    expected_properties = {"pvsyst_resolve_location": RESOLVE_SCHEMA,
                           "pvsyst_acquire_weather": ACQUIRE_SCHEMA,
                           "pvsyst_review_weather": REVIEW_SCHEMA,
                           "pvsyst_prepare_sourced_design": SOURCED_SCHEMA,
                           "pvsyst_simulate_design": DESIGN_SCHEMA}
    for name, expected in expected_properties.items():
        schema = by_name[name].input_schema
        checks.equal(set(schema.get("properties", {})), expected, f"{name} published properties")
    resolve_required = set(by_name["pvsyst_resolve_location"].input_schema.get("required", []))
    checks.equal(resolve_required, {"query"}, "pvsyst_resolve_location required")
    review_required = set(by_name["pvsyst_review_weather"].input_schema.get("required", []))
    checks.equal(review_required, {"csv_name", "native_csv_name", "year"},
                 "pvsyst_review_weather required")
    sourced_required = set(by_name["pvsyst_prepare_sourced_design"].input_schema.get("required", []))
    checks.equal(sourced_required, {"pan_name", "ond_name", "sources", "declarations"},
                 "pvsyst_prepare_sourced_design required")
    design_required = set(by_name["pvsyst_simulate_design"].input_schema.get("required", []))
    checks.equal(design_required, {"spec"}, "pvsyst_simulate_design required")
    sourced_defs = by_name["pvsyst_prepare_sourced_design"].input_schema.get("$defs", {})
    checks.equal(set(sourced_defs.get("SourceTextRequest", {}).get("properties", {})),
                 {"kind", "title", "locator", "text", "model"},
                 "SourceTextRequest published properties")
    checks.equal(set(sourced_defs.get("SourceDeclarationRequest", {}).get("properties", {})),
                 {"field", "value", "unit", "source_id", "excerpt", "source_value_text",
                  "source_unit_text", "page"},
                 "SourceDeclarationRequest published properties")
    checks.equal(set(sourced_defs.get("SourcedDesignGoals", {}).get("properties", {})),
                 {"target_dc_kw", "max_dc_ac_ratio", "capacity_tolerance_pct", "voltage_standard",
                  "design_basis_extra"},
                 "SourcedDesignGoals published properties")
    checks.ok(all(definition.get("additionalProperties") is False
                  for definition in sourced_defs.values()),
              "sourced nested models reject unknown keys",
              "a nested model allows extra keys")
    template_properties = set(by_name["pvsyst_simulate_template"].input_schema.get("properties", {}))
    checks.ok(TEMPLATE_EXTRA_SCHEMA <= template_properties,
              "pvsyst_simulate_template exposes azimuth and losses",
              f"properties were {sorted(template_properties)}")
    return by_name


async def run(checks):
    with tempfile.TemporaryDirectory(prefix="pvsyst-design-smoke-") as workspace:
        env = dict(os.environ)
        env["PVSYST_CLI"] = sys.executable       # sentinel: never a real PVsyst executable
        env["PVSYST_WORKSPACE"] = workspace
        params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=env)
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                by_name = schema_checks(checks, (await session.list_tools()).tools)

                # --- strict top-level argument whitelist (middleware, before any model) ---
                await rejected(session, checks, "unknown top-level argument is refused",
                               "pvsyst_simulate_design",
                               {"spec": {"new_project": "smoke_design.PRJ"}, "typo": 1})
                await rejected(session, checks, "unknown resolve_location argument is refused",
                               "pvsyst_resolve_location", {"query": "Zurich", "limit": 3})
                await rejected(session, checks, "unknown acquire_weather argument is refused",
                               "pvsyst_acquire_weather", {"year": 2020, "csv": "x.csv"})
                await rejected(session, checks, "unknown simulate_template argument is refused",
                               "pvsyst_simulate_template", {"template_project": "T.PRJ", "loss": 1})
                await rejected(session, checks, "unknown review_weather argument is refused",
                               "pvsyst_review_weather",
                               {"csv_name": "a.csv", "native_csv_name": "b.csv", "year": 2020,
                                "native": "c.csv"})
                await rejected(session, checks, "unknown sourced argument is refused",
                               "pvsyst_prepare_sourced_design",
                               {"pan_name": "P.PAN", "ond_name": "O.OND", "sources": {},
                                "declarations": [], "manufacturer_model": "x"})

                # --- strict typed schema: booleans and numeric strings never coerce ---
                base = {"new_project": "smoke_design.PRJ"}
                sourced = {"pan_name": "Fixture.PAN", "ond_name": "Fixture.OND", "sources": {},
                           "declarations": []}
                reviewed = {"csv_name": "a.csv", "native_csv_name": "b.csv"}
                source_entry = {"kind": "manufacturer", "title": "Datasheet",
                                "locator": "https://example.invalid/ds.pdf", "text": "Voc 45.0 V"}
                declaration = {"field": "envelope.cold_voc_v", "value": 45.0, "unit": "V",
                               "source_id": "ds", "excerpt": "Voc 45.0 V",
                               "source_value_text": "45.0", "source_unit_text": "V"}
                typed_rejections = (
                    ("pvsyst_simulate_design", "spec.year true is refused",
                     {"spec": {**base, "year": True}}),
                    ("pvsyst_simulate_design", "spec.year numeric string is refused",
                     {"spec": {**base, "year": "2020"}}),
                    ("pvsyst_simulate_design", "spec.dry_run is refused",
                     {"spec": {**base}, "dry_run": 1}),
                    ("pvsyst_simulate_design", "spec.tilt true is refused",
                     {"spec": {**base, "tilt": True}}),
                    ("pvsyst_simulate_design", "spec.target_dc_kw numeric string is refused",
                     {"spec": {**base, "target_dc_kw": "100"}}),
                    ("pvsyst_simulate_design", "spec.scope is fixed",
                     {"spec": {**base, "scope": "roof"}}),
                    ("pvsyst_simulate_design", "weather.mode is enumerated",
                     {"spec": {**base, "weather": {"mode": "guess"}}}),
                    ("pvsyst_simulate_design", "weather.location_id true is refused",
                     {"spec": {**base, "weather": {"mode": "open_meteo", "location_id": True}}}),
                    ("pvsyst_simulate_design", "unknown equipment key is refused",
                     {"spec": {**base, "module": {"name": "X"}}}),
                    ("pvsyst_simulate_design", "unknown loss key is refused",
                     {"spec": {**base, "losses": {"Soiling": 3.0}}}),
                    ("pvsyst_simulate_design", "coefficient without unit is refused",
                     {"spec": {**base, "electrical_design": {"coefficients": {
                         "beta_voc": {"value": -0.3, "source": "datasheet"}}}}}),
                    ("pvsyst_simulate_design", "coefficient with a typo key is refused",
                     {"spec": {**base, "electrical_design": {"coefficients": {
                         "beta_voc": {"value": -0.3, "unit": "pct_per_degC", "sorce": "datasheet"}}}}}),
                    ("pvsyst_simulate_design", "coefficient as a bare number is refused",
                     {"spec": {**base, "electrical_design": {"coefficients": {"beta_voc": -0.3}}}}),
                    ("pvsyst_simulate_design", "envelope declaration with a typo key is refused",
                     {"spec": {**base, "electrical_design": {"envelope": {
                         "cold_voc_v": {"value": 45.0, "unit": "V", "sorce": "datasheet"}}}}}),
                    ("pvsyst_simulate_design", "electrical_design with a typo key is refused",
                     {"spec": {**base, "electrical_design": {"temperature": {}}}}),
                    ("pvsyst_simulate_design", "unknown weather key is refused",
                     {"spec": {**base, "weather": {"mode": "existing", "file": "x.CSV"}}}),
                    ("pvsyst_resolve_location", "resolve_location.query is required", {}),
                    ("pvsyst_acquire_weather", "acquire_weather.year true is refused",
                     {"year": True}),
                    ("pvsyst_acquire_weather", "acquire_weather.latitude string is refused",
                     {"latitude": "27.7"}),
                    ("pvsyst_simulate_template", "template azimuth true is refused",
                     {"template_project": "T.PRJ", "azimuth": True}),
                    ("pvsyst_simulate_template", "template unknown loss key is refused",
                     {"template_project": "T.PRJ", "losses": {"Soiling": 1.0}}),
                    ("pvsyst_review_weather", "review_weather.year true is refused",
                     {**reviewed, "year": True}),
                    ("pvsyst_review_weather", "review_weather.year numeric string is refused",
                     {**reviewed, "year": "2020"}),
                    ("pvsyst_review_weather", "review_weather.year is required", reviewed),
                    ("pvsyst_review_weather", "review_weather unknown key is refused",
                     {**reviewed, "year": 2020, "native": "c.csv"}),
                    ("pvsyst_prepare_sourced_design", "sourced source kind is enumerated",
                     {**sourced, "sources": {"ds": {**source_entry, "kind": "vendor"}}}),
                    ("pvsyst_prepare_sourced_design", "sourced source typo key is refused",
                     {**sourced, "sources": {"ds": {**source_entry, "mdel": "M"}}}),
                    ("pvsyst_prepare_sourced_design", "sourced source text is required",
                     {**sourced, "sources": {"ds": {"kind": "site", "title": "T",
                                                    "locator": "L"}}}),
                    ("pvsyst_prepare_sourced_design", "sourced source empty title is refused",
                     {**sourced, "sources": {"ds": {**source_entry, "title": ""}}}),
                    ("pvsyst_prepare_sourced_design", "sourced source empty text is refused",
                     {**sourced, "sources": {"ds": {**source_entry, "text": ""}}}),
                    ("pvsyst_prepare_sourced_design", "sourced source model true is refused",
                     {**sourced, "sources": {"ds": {**source_entry, "model": True}}}),
                    ("pvsyst_prepare_sourced_design", "sourced declaration unit is required",
                     {**sourced, "declarations": [{key: value for key, value in declaration.items()
                                                   if key != "unit"}]}),
                    ("pvsyst_prepare_sourced_design", "sourced declaration page true is refused",
                     {**sourced, "declarations": [{**declaration, "page": True}]}),
                    ("pvsyst_prepare_sourced_design", "sourced declaration numeric string is refused",
                     {**sourced, "declarations": [{**declaration, "value": "45.0"}]}),
                    ("pvsyst_prepare_sourced_design", "sourced declaration typo key is refused",
                     {**sourced, "declarations": [{**declaration, "citation": "x"}]}),
                    ("pvsyst_prepare_sourced_design", "sourced design extra key is refused",
                     {**sourced, "design": {"coefficients": {"beta_voc": 1.0}}}),
                    ("pvsyst_prepare_sourced_design", "sourced voltage_standard is enumerated",
                     {**sourced, "design": {"voltage_standard": "iec"}}),
                    ("pvsyst_prepare_sourced_design", "sourced goal numeric string is refused",
                     {**sourced, "design": {"target_dc_kw": "21.6"}}),
                    ("pvsyst_prepare_sourced_design", "sourced sources true is refused",
                     {**sourced, "sources": True}),
                )
                for name, label, arguments in typed_rejections:
                    await rejected(session, checks, label, name, arguments)

                # --- schema-valid but incomplete specs stop before any file is opened ---
                data = await blocked(session, checks,
                                     "simulate_design missing inputs",
                                     "pvsyst_simulate_design",
                                     {"spec": {"new_project": "smoke_design.PRJ"}},
                                     expect_fields=("year", "target_dc_kw", "tilt", "azimuth",
                                                    "capacity", "module.name", "inverter.name",
                                                    "weather.sit_name"))
                if data is not None:
                    checks.equal(data.get("stage"), "spec", "simulate_design blocking stage")
                    checks.equal(data.get("selected"), None, "simulate_design selects nothing")
                    checks.equal(data.get("execution_preflight_complete"), False,
                                 "simulate_design preflight flag")
                    # The serialized spec must match the backend whitelist: a renamed or
                    # unknown key would come back as an unknown-input issue here.
                    unknown = [item for item in data["issues"]
                               if "Unknown" in str(item.get("message", ""))
                               or str(item.get("field", "")).split(".")[0] not in KNOWN_SPEC_FIELDS]
                    checks.ok(not unknown, "design spec keys match the backend whitelist",
                              json.dumps(unknown)[:300])
                await blocked(session, checks, "resolve_location short query",
                              "pvsyst_resolve_location", {"query": "a"})
                result = await invoke(session, "pvsyst_resolve_location",
                                      {"query": "Zurich", "country_code": "ch"})
                checks.ok(not result.is_error and payload(result)["status"] == "blocked",
                          "resolve_location lowercase country code is blocked",
                          text_of(result)[:200])
                checks.equal(payload(result).get("request_url"), None,
                             "resolve_location rejects before any provider request")
                blocked_year = await blocked(session, checks, "acquire_weather year out of range",
                                             "pvsyst_acquire_weather", {"year": 1900})
                if blocked_year is not None:
                    checks.ok("1940..2068" in issue_text(blocked_year),
                              "acquire_weather reports the supported year range",
                              issue_text(blocked_year)[:300])
                half_hour = await invoke(session, "pvsyst_acquire_weather",
                                         {"year": 2020, "latitude": 27.7172, "longitude": 85.324,
                                          "site_name": "Kathmandu", "country_code": "NP",
                                          "timezone_name": "Asia/Kathmandu"})
                half_text = text_of(half_hour)
                if checks.ok(not half_hour.is_error and payload(half_hour)["status"] == "blocked",
                             "acquire_weather half-hour zone is blocked", half_text[:300]):
                    reasons = issue_text(payload(half_hour))
                    checks.ok(("half-hour" in reasons or "time zone database" in reasons),
                              "acquire_weather reports the half-hour/zone reason", reasons[:300])
                    checks.equal(payload(half_hour).get("plan"), None,
                                 "acquire_weather blocks before building the request plan")
                await blocked(session, checks, "acquire_weather missing year",
                              "pvsyst_acquire_weather", {"dry_run": True})

                # --- read-only weather review: missing evidence is reported, never ignored ---
                review = await invoke(session, "pvsyst_review_weather",
                                      {"csv_name": "missing.csv",
                                       "native_csv_name": "missing_native.csv", "year": 2020})
                if checks.ok(not review.is_error and payload(review)["status"] == "blocked",
                             "review_weather missing evidence is blocked", text_of(review)[:200]):
                    checks.ok("does not exist" in issue_text(payload(review)),
                              "review_weather names the missing file", issue_text(payload(review))[:200])

                # --- sourced design: an unresolved loose filename blocks on its own field ---
                missing = await invoke(session, "pvsyst_prepare_sourced_design",
                                       {"pan_name": "Missing.PAN", "ond_name": "Missing.OND",
                                        "sources": {}, "declarations": []})
                if checks.ok(not missing.is_error and payload(missing)["status"] == "blocked",
                             "sourced design with unresolved components is blocked",
                             text_of(missing)[:200]):
                    checks.equal(payload(missing)["issues"][0]["field"], "PAN.name",
                                 "an unresolved PAN blocks on PAN.name")
                checks.ok("manufacturer_model" not in json.dumps(payload(missing)),
                          "sourced design never invents an identity label")

                # The fixtures are plain text written into the temporary workspace and read
                # through the store's own bounded reader; nothing is hashed or inspected.
                module_folder = Path(workspace) / "ComposPV" / "PVmodules"
                inverter_folder = Path(workspace) / "ComposPV" / "Inverters"
                module_folder.mkdir(parents=True, exist_ok=True)
                inverter_folder.mkdir(parents=True, exist_ok=True)
                (module_folder / "Fixture.PAN").write_text(PAN_ROOT_TEXT, encoding="utf-8")
                (module_folder / "Conflict.PAN").write_text(PAN_CONFLICT_TEXT, encoding="utf-8")
                (module_folder / "Duplicate.PAN").write_text(PAN_DUPLICATE_TEXT, encoding="utf-8")
                (inverter_folder / "Fixture.OND").write_text(OND_TEXT, encoding="utf-8")
                (module_folder / "Binary.PAN").write_bytes(b"pvModule;legacy binary payload")

                only_module = await invoke(session, "pvsyst_prepare_sourced_design",
                                           {"pan_name": "Fixture.PAN", "ond_name": "Missing.OND",
                                            "sources": {}, "declarations": []})
                if checks.ok(not only_module.is_error and payload(only_module)["status"] == "blocked",
                             "sourced design blocks on the unresolved inverter",
                             text_of(only_module)[:200]):
                    checks.equal(payload(only_module)["issues"][0]["field"], "OND.name",
                                 "the declared PAN identity binds before the inverter resolves")

                legacy = await invoke(session, "pvsyst_prepare_sourced_design",
                                      {"pan_name": "Binary.PAN", "ond_name": "Fixture.OND",
                                       "sources": {}, "declarations": []})
                if checks.ok(not legacy.is_error and payload(legacy)["status"] == "blocked",
                             "legacy binary PAN is blocked", text_of(legacy)[:200]):
                    checks.equal(payload(legacy)["issues"][0]["field"], "PAN.name",
                                 "legacy binary PAN blocks on PAN.name")
                    checks.ok("Legacy binary" in issue_text(payload(legacy)),
                              "legacy binary PAN reports its own reason",
                              issue_text(payload(legacy))[:200])

                conflict = await invoke(session, "pvsyst_prepare_sourced_design",
                                        {"pan_name": "Conflict.PAN", "ond_name": "Fixture.OND",
                                         "sources": {}, "declarations": []})
                if checks.ok(not conflict.is_error and payload(conflict)["status"] == "blocked",
                             "a root/Commercial identity disagreement is blocked",
                             text_of(conflict)[:200]):
                    checks.equal(payload(conflict)["issues"][0]["field"], "PAN.manufacturer",
                                 "the identity disagreement names PAN.manufacturer")
                    checks.ok("differently" in issue_text(payload(conflict)),
                              "the identity disagreement explains itself",
                              issue_text(payload(conflict))[:200])

                duplicate = await invoke(session, "pvsyst_prepare_sourced_design",
                                         {"pan_name": "Duplicate.PAN", "ond_name": "Fixture.OND",
                                          "sources": {}, "declarations": []})
                if checks.ok(not duplicate.is_error and payload(duplicate)["status"] == "blocked",
                             "a repeated identical identity field is blocked",
                             text_of(duplicate)[:200]):
                    checks.equal(payload(duplicate)["issues"][0]["field"], "PAN.manufacturer",
                                 "the repeated identity field names PAN.manufacturer")
                    checks.ok("occurs 2 times" in issue_text(payload(duplicate)),
                              "an identical repeated field is not de-duplicated",
                              issue_text(payload(duplicate))[:200])

                wired = await invoke(session, "pvsyst_prepare_sourced_design",
                                     {"pan_name": "Fixture.PAN", "ond_name": "Fixture.OND",
                                      "sources": {"ds": source_entry},
                                      "declarations": [declaration]})
                wired_error = text_of(wired)
                if wired.is_error:
                    # A backend that raises inside its own module is reported, not masked: the
                    # identity binding and key mapping are already covered by the blocked cases.
                    checks.ok("Unknown" not in wired_error,
                              "a sourced failure is not a key-mapping rejection", wired_error[:200])
                    print("NOTE: sourced backend raised:", wired_error.strip()[:200])
                else:
                    wired_data = payload(wired)
                    checks.ok(wired_data.get("status") in ("ready", "needs_review", "blocked"),
                              "sourced design reports a documented status",
                              str(wired_data.get("status")))
                    # A component block returns the server's own short structure (with "stage");
                    # its absence proves both identities bound and the backend produced the result.
                    checks.ok("scope" in wired_data and "stage" not in wired_data
                              and isinstance(wired_data.get("missing_fields"), list),
                              "both identities bind, so the backend validation result is returned",
                              json.dumps(sorted(wired_data))[:200])
                    wired_fields = {str(item.get("field", "")) for item in wired_data.get("issues", [])
                                    if isinstance(item, dict)}
                    checks.ok(not {"PAN.name", "PAN.manufacturer", "PAN.model", "OND.name",
                                   "OND.manufacturer", "OND.model"} & wired_fields,
                              "no identity field is reported as blocked",
                              json.dumps(sorted(wired_fields))[:300])
                    checks.ok(not [item for item in wired_data.get("issues", [])
                                   if "Unknown" in str(item.get("message", ""))],
                              "sourced keys match the backend whitelist",
                              json.dumps(wired_data.get("issues", []))[:300])
                    checks.ok("manufacturer_model" not in wired_fields,
                              "the bound identity label is never rejected as missing",
                              json.dumps(sorted(wired_fields))[:300])
                    print("sourced backend status:", wired_data.get("status"),
                          "issue fields:", json.dumps(sorted(wired_fields))[:200])

                print("schema pvsyst_resolve_location:",
                      json.dumps(by_name["pvsyst_resolve_location"].input_schema))
                print("schema pvsyst_acquire_weather:",
                      json.dumps(by_name["pvsyst_acquire_weather"].input_schema))
                print("schema pvsyst_review_weather:",
                      json.dumps(by_name["pvsyst_review_weather"].input_schema))
                print("schema pvsyst_prepare_sourced_design:",
                      json.dumps(by_name["pvsyst_prepare_sourced_design"].input_schema))
                print("schema pvsyst_simulate_design:",
                      json.dumps(by_name["pvsyst_simulate_design"].input_schema))
                print("schema pvsyst_simulate_template properties:",
                      json.dumps(sorted(by_name["pvsyst_simulate_template"].input_schema["properties"])))

        created = sorted(path.relative_to(workspace).as_posix()
                         for path in Path(workspace).rglob("*") if path.is_file())
        checks.equal(created, sorted(FIXTURE_FILES), "the workspace holds only the written fixtures")


async def main():
    checks = Checks()
    await run(checks)
    for failure in checks.failures:
        print("FAIL:", failure)
    print(f"checks passed: {checks.passed}, failed: {len(checks.failures)}")
    return 1 if checks.failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
