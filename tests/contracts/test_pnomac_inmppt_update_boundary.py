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
- The value must be strictly positive and finite. Zero, negative, a JSON boolean,
  a numeric string, NaN and infinity are refused before any byte is written; a
  subarray without the declaration is refused too (it is an edit, not a create).
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


if __name__ == "__main__":
    unittest.main()
