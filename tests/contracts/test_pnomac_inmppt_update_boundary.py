"""Public stdio-MCP contract for the PNomAC_InMPPT subarray edit (round19-A).

External, persisted behaviour only. A synthetic self-contained workspace and a
fake ``PVsystCLI.exe`` are used, so ZERO PVsyst CLI/Native/GUI runs: the tool is
reached exactly as a caller reaches it, through the MCP server.

Pinned behaviour of ``pvsyst_update_variant_parameters(subarray_updates=...)``:

- ``PNomAC_InMPPT`` is a new supported key. It writes the existing per-MPPT
  nominal AC power declaration (kW) as a positive finite number, and nothing
  else in that subarray changes. A loop over several subarrays, each already
  bound to a different ``GInverter`` and starting at the shared source value
  7.50, edits each one independently to its own per-MPPT value (same-PAN /
  different-OND authoring, different MPP).
- The value must be strictly positive and finite *in its stored 2-dp form*. Zero,
  negative, a JSON boolean, a numeric string, NaN and infinity are refused before
  any byte is written, and a tiny positive that would serialize to 0.00 is refused
  too (the saved declaration never becomes zero); a subarray without the declaration
  is refused as well (it is an edit, not a create).
- It is a declaration write only: no OND PNomConv/NbMPPT derivation, no MPPT
  resize, no inverter power calculation.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[2]
UPDATE_TOOL = "pvsyst_update_variant_parameters"
GET_TOOL = "pvsyst_get_variant_parameters"
FAKE_CLI = b"Not an executable: this contract must not run PVsyst"

# Three exclusive fixed subarrays, same PAN, three different inverter references
# that all start from the shared source declaration 7.50 and must be edited to
# each donor's per-MPPT value (25.0 / 3.75 / 225.0). Synthetic; no vendor file.
GROUPS = (  # (SubArrayId, GInverter, new PNomAC)
    (1, "InverterA.OND", 25.0),
    (2, "InverterB.OND", 3.75),
    (3, "InverterC.OND", 225.0),
)


def _vc():
    blocks = []
    for sa, ond, _new in GROUPS:
        blocks.append(
            "  PVObject_=pvSubArray\n"
            f"    SubArrayId={sa}\n"
            "    PVModule=SameModule.PAN\n"
            f"    GInverter={ond}\n"
            "    NModSerie=15\n"
            "    NStringCh=2\n"
            "    NInverter=1\n"
            "    NInvMPPT=1\n"
            "    PowerSharingInverterId=-1\n"
            "    PNomAC_InMPPT=7.50\n"
            "  End of PVObject pvSubArray\n")
    return ("PVObject_=pvVCalcul\n  Comment=PNomAC boundary\n  Version=8.1.6\n"
            + "".join(blocks) + "End of PVObject pvVCalcul\n")


def _payload(result):
    return json.loads("\n".join(item.text for item in result.content if hasattr(item, "text")))


def _subarrays(text):
    seen, current = {}, None
    for line in text.splitlines():
        s = line.strip()
        if s == "PVObject_=pvSubArray":
            current = {}
        elif current is not None and s == "End of PVObject pvSubArray":
            seen[current["id"]] = current
            current = None
        elif current is not None and "=" in s:
            key, value = s.split("=", 1)
            current["id" if key == "SubArrayId" else key] = int(value) if key == "SubArrayId" else value
    return seen


def _state_by_id(params):
    return {s["id"]: s["inverter_state"] for s in params["subarrays"]}


def _raw_vc(blocks):
    return ("PVObject_=pvVCalcul\n  Comment=PNomAC readback\n  Version=8.1.6\n"
            + "".join(blocks) + "End of PVObject pvVCalcul\n")


def _sa(sa, ond, module="Generic_Mono_300W.PAN", pnom=None):
    body = ("  PVObject_=pvSubArray\n"
            f"    SubArrayId={sa}\n"
            f"    PVModule={module}\n"
            f"    GInverter={ond}\n"
            "    NModSerie=15\n"
            "    NStringCh=2\n"
            "    NInverter=1\n"
            "    NInvMPPT=1\n"
            "    PowerSharingInverterId=-1\n")
    if pnom is not None:
        body += f"    PNomAC_InMPPT={pnom}\n"
    return body + "  End of PVObject pvSubArray\n"


class PNomACInMPPTContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pvsyst-pnomac-contract-")
        root = Path(self.tmp.name)
        self.projects = root / "Projects"
        self.projects.mkdir()
        (self.projects / "PNOM.PRJ").write_text(
            "PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        self.variant_path = self.projects / "PNOM.VC0"
        self.variant_path.write_bytes(b"\xef\xbb\xbf" + _vc().encode("utf-8"))
        (root / "PVsystCLI.exe").write_bytes(FAKE_CLI)
        environment = dict(os.environ, PVSYST_WORKSPACE=str(root),
                           PVSYST_CLI=str(root / "PVsystCLI.exe"))
        environment.pop("PVSYST_BUILTIN_COMPONENTS", None)
        self._params = StdioServerParameters(
            command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
            cwd=str(REPO), env=environment)

    def tearDown(self):
        self.tmp.cleanup()

    async def _sha(self, session, variant="VC0"):
        result = await session.call_tool(GET_TOOL, {"project": "PNOM.PRJ", "variant": variant})
        self.assertFalse(result.is_error, result.content)
        return _payload(result)["sha256"]

    async def test_positive_loop_writes_each_subarray_per_mppt_and_preserves_rest(self):
        async with stdio_client(self._params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for sa, ond, new in GROUPS:
                    result = await session.call_tool(UPDATE_TOOL, {
                        "project": "PNOM.PRJ", "variant": "VC0",
                        "expected_sha256": await self._sha(session), "subarray_id": sa,
                        "subarray_updates": {"PNomAC_InMPPT": new}})
                    self.assertFalse(result.is_error, result.content)
                    body = _payload(result)
                    self.assertTrue(body["changed"], f"SA{sa} should change from 7.50 to {new}")
                    self.assertIn("backup_name", body)

        seen = _subarrays(self.variant_path.read_bytes().decode("utf-8-sig"))
        for sa, ond, new in GROUPS:
            group = seen[sa]
            self.assertEqual(group["GInverter"], ond)              # reference untouched
            self.assertEqual(group["PVModule"], "SameModule.PAN")  # same PAN
            self.assertEqual(group["NInvMPPT"], "1")               # no MPPT resize
            self.assertEqual(group["PNomAC_InMPPT"], f"{new:.2f}")
        self.assertTrue(self.variant_path.read_bytes().startswith(b"\xef\xbb\xbf"))

    async def test_invalid_values_are_refused_without_writing(self):
        before = self.variant_path.read_bytes()
        async with stdio_client(self._params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for bad in (0.0, -1.0, True, "3.75"):
                    result = await session.call_tool(UPDATE_TOOL, {
                        "project": "PNOM.PRJ", "variant": "VC0",
                        "expected_sha256": await self._sha(session), "subarray_id": 1,
                        "subarray_updates": {"PNomAC_InMPPT": bad}})
                    self.assertTrue(result.is_error, f"{bad!r} must be rejected: {result.content}")
                    self.assertEqual(self.variant_path.read_bytes(), before)

    async def test_non_finite_values_are_refused_without_writing(self):
        before = self.variant_path.read_bytes()
        async with stdio_client(self._params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for bad in (float("nan"), float("inf"), float("-inf")):
                    try:
                        result = await session.call_tool(UPDATE_TOOL, {
                            "project": "PNOM.PRJ", "variant": "VC0",
                            "expected_sha256": await self._sha(session), "subarray_id": 1,
                            "subarray_updates": {"PNomAC_InMPPT": bad}})
                        refused = result.is_error
                    except Exception:
                        refused = True  # transport may reject non-finite JSON first
                    self.assertTrue(refused, f"{bad!r} must be rejected")
                    self.assertEqual(self.variant_path.read_bytes(), before)

    async def test_tiny_positive_that_serializes_to_zero_is_refused_without_writing(self):
        before = self.variant_path.read_bytes()
        async with stdio_client(self._params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # 0.004 is positive and finite but its 2-dp stored form is 0.00:
                # it must be refused before any byte is written, never saved as zero.
                result = await session.call_tool(UPDATE_TOOL, {
                    "project": "PNOM.PRJ", "variant": "VC0",
                    "expected_sha256": await self._sha(session), "subarray_id": 1,
                    "subarray_updates": {"PNomAC_InMPPT": 0.004}})
                self.assertTrue(result.is_error, f"0.004 must be rejected: {result.content}")
                self.assertEqual(self.variant_path.read_bytes(), before)
                # The smallest representable positive 2-dp value stays accepted (no over-reject).
                result = await session.call_tool(UPDATE_TOOL, {
                    "project": "PNOM.PRJ", "variant": "VC0",
                    "expected_sha256": await self._sha(session), "subarray_id": 1,
                    "subarray_updates": {"PNomAC_InMPPT": 0.01}})
                self.assertFalse(result.is_error, result.content)
        group = _subarrays(self.variant_path.read_bytes().decode("utf-8-sig"))[1]
        self.assertEqual(group["PNomAC_InMPPT"], "0.01")
        self.assertTrue(self.variant_path.read_bytes().startswith(b"\xef\xbb\xbf"))

    async def test_absent_declaration_is_refused(self):
        (self.projects / "PNOM.VC9").write_text(
            "PVObject_=pvVCalcul\n  Version=8.1.6\n  PVObject_=pvSubArray\n"
            "    SubArrayId=1\n  End of PVObject pvSubArray\nEnd of PVObject pvVCalcul\n",
            encoding="utf-8")
        async with stdio_client(self._params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(UPDATE_TOOL, {
                    "project": "PNOM.PRJ", "variant": "VC9",
                    "expected_sha256": await self._sha(session, "VC9"), "subarray_id": 1,
                    "subarray_updates": {"PNomAC_InMPPT": 3.75}})
                self.assertTrue(result.is_error, result.content)

    async def _get(self, session, variant="VC0", project="PNOM.PRJ"):
        result = await session.call_tool(GET_TOOL, {"project": project, "variant": variant})
        self.assertFalse(result.is_error, result.content)
        return _payload(result)

    async def test_readback_reports_stored_per_sa_inverter_state(self):
        """get_variant_parameters now returns a typed per-SA inverter_state (stored declarations)."""
        async with stdio_client(self._params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                body = await self._get(session)
                self.assertEqual([s["id"] for s in body["subarrays"]], [1, 2, 3])
                states = _state_by_id(body)
                for sa, ond, _ in GROUPS:
                    state = states[sa]
                    self.assertEqual(state["PVModule"], "SameModule.PAN")
                    self.assertEqual(state["GInverter"], ond)
                    self.assertEqual(state["NInverter"], 1)
                    self.assertEqual(state["NInvMPPT"], 1)
                    self.assertEqual(state["PowerSharingInverterId"], -1)
                    self.assertIsInstance(state["PNomAC_InMPPT"], float)
                    self.assertEqual(state["PNomAC_InMPPT"], 7.50)
                meta = body["source_metadata"]
                self.assertTrue(meta["stored_source_values"])
                self.assertIn("normalize", meta["note"])
                self.assertIn("does not prove", meta["note"])

    async def test_before_after_pnom_reads_actual_stored_float(self):
        """Read 3.75 -> edit to 2.50 -> read 2.50, as the actual stored float (never derived)."""
        (self.projects / "PNOM.VC8").write_text(_raw_vc(
            [_sa(1, "Generic_7.5kW_Unbalanced.OND", pnom="3.75")]), encoding="utf-8")
        async with stdio_client(self._params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                before = _state_by_id(await self._get(session, "VC8"))
                self.assertEqual(before[1]["GInverter"], "Generic_7.5kW_Unbalanced.OND")
                self.assertIsInstance(before[1]["PNomAC_InMPPT"], float)
                self.assertEqual(before[1]["PNomAC_InMPPT"], 3.75)
                sha = (await self._get(session, "VC8"))["sha256"]
                result = await session.call_tool(UPDATE_TOOL, {
                    "project": "PNOM.PRJ", "variant": "VC8", "expected_sha256": sha,
                    "subarray_id": 1, "subarray_updates": {"PNomAC_InMPPT": 2.50}})
                self.assertFalse(result.is_error, result.content)
                after = _state_by_id(await self._get(session, "VC8"))
                self.assertIsInstance(after[1]["PNomAC_InMPPT"], float)
                self.assertEqual(after[1]["PNomAC_InMPPT"], 2.50)
        self.assertIn("PNomAC_InMPPT=2.50", (self.projects / "PNOM.VC8").read_text(encoding="utf-8"))

    async def test_missing_and_invalid_pnom_are_omitted_not_zero(self):
        """Absent / non-finite / non-numeric PNomAC_InMPPT stays identifiably missing, never 0."""
        (self.projects / "PNOM.VC7").write_text(_raw_vc([
            _sa(1, "A.OND"), _sa(2, "B.OND", pnom="nan"), _sa(3, "C.OND", pnom="abc")]),
            encoding="utf-8")
        async with stdio_client(self._params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                states = _state_by_id(await self._get(session, "VC7"))
                for sa in (1, 2, 3):
                    self.assertNotIn("PNomAC_InMPPT", states[sa], states[sa])
                    self.assertNotEqual(states[sa].get("PNomAC_InMPPT"), 0.0)
                    self.assertEqual(states[sa]["NInvMPPT"], 1)  # other fields still read

    async def test_sparse_subarray_ids_read_sorted_by_id(self):
        """Sparse ids present numeric-ascending [2,7] publicly while the file stays 7,2 source order."""
        (self.projects / "PNOM.VC6").write_text(_raw_vc([
            _sa(7, "S7.OND", pnom="1.25"), _sa(2, "S2.OND", pnom="9.00")]), encoding="utf-8")
        async with stdio_client(self._params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                body = await self._get(session, "VC6")
                # Existing public presentation order stays numeric-ascending SubArrayId,
                # never the raw file appearance order.
                self.assertEqual([s["id"] for s in body["subarrays"]], [2, 7])
                states = _state_by_id(body)
                self.assertEqual(states[2]["PNomAC_InMPPT"], 9.00)
                self.assertEqual(states[7]["PNomAC_InMPPT"], 1.25)
                self.assertEqual(states[2]["GInverter"], "S2.OND")
                self.assertEqual(states[7]["GInverter"], "S7.OND")
        # The stored file keeps its original 7-then-2 source appearance order, untouched.
        raw = (self.projects / "PNOM.VC6").read_text(encoding="utf-8")
        self.assertLess(raw.index("SubArrayId=7"), raw.index("SubArrayId=2"))


if __name__ == "__main__":
    unittest.main()
