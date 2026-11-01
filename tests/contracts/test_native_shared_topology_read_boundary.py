"""Public stdio-MCP contract for the opt-in native shared-topology read.

External, persisted behaviour only. A synthetic self-contained workspace and a fake
``PVsystCLI.exe`` are used, so ZERO PVsyst CLI/Native/GUI runs: the tool is reached
exactly as a caller reaches it, through the MCP server.

Pinned behaviour of ``pvsyst_get_variant_parameters(include_topology=...)``:

- The default (``include_topology=False``) keeps the prior response shape untouched:
  no ``topology`` key is added, so the existing payload size/behaviour is unchanged.
  A getter call never writes the variant file.
- ``include_topology=True`` adds a ``topology`` block read from the variant's own
  stored native ``PVObject_SystemCircuit=pvCircuit`` tree: node kind, source line and
  the full raw scalar field map (unknown fields kept). ``subarray_id``/``nelements``
  carry an explicit ``present`` flag, so an OMITTED owner (present=False, value=None)
  stays distinct from an explicit ``0`` (present=True, value="0").
- Member nodes keep their source appearance/order (never sorted by SubArrayId), and
  each StringNode block's own NElements is reported as stored rather than aggregated
  into a single string total.
- A variant without a circuit reports ``present=False``; a structurally malformed
  circuit reports an issue instead of an empty/verified tree.
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
GET_TOOL = "pvsyst_get_variant_parameters"
FAKE_CLI = b"Not an executable: this contract must not run PVsyst"
NAMES = {"1": "PV Array", "2": "Sub-array #2"}


def _shared_circuit(member_order=("1", "2"), root_subarray_id=None,
                    string_counts=None, unknown_field=False, close=True):
    """A shared-parent pvCircuit: one owner InverterNode over explicit MPPT members."""
    if string_counts is None:
        string_counts = {"1": "29", "2": "28"}
    lines = ["  PVObject_SystemCircuit=pvCircuit\n", "    Version=8.1.0\n", "    Flags=$00\n",
             "    BaseNode=Start\n", "      InjectionPointNode Start;\n", "        Children=Start\n",
             "          InverterNode Start;\n", "            SubArrayName=PV Array; Sub-array #2\n"]
    if root_subarray_id is not None:
        lines.append(f"            SubArrayId={root_subarray_id}\n")
    if unknown_field:
        lines.append("            FutureMarker=PRESERVE_ME\n")
    lines.append("            Children=Start\n")
    for sid in member_order:
        lines += [f"              MPPTNode Start;\n", "                NElements=2\n",
                  f"                SubArrayId={sid}\n", f"                SubArrayName={NAMES[sid]}\n",
                  "                Children=Start\n", "                  StringNode Start;\n",
                  f"                    NElements={string_counts[sid]}\n",
                  "                    CableDefinition=Start\n",
                  "                        Resistance=10.355 mOhm\n",
                  "                    CableDefinition=End\n",
                  f"                    SubArrayId={sid}\n",
                  f"                    SubArrayName={NAMES[sid]}\n",
                  "                  StringNode End;\n", "                Children=End\n",
                  "              MPPTNode End;\n"]
    lines += ["            Children=End\n", "          InverterNode End;\n",
              "        Children=End\n", "      InjectionPointNode End;\n", "    BaseNode=End\n"]
    if close:
        lines.append("  End of PVObject pvCircuit\n")
    return "".join(lines)


def _subarrays():
    def sa(sid, nch, pnom):
        return ["  PVObject_=pvSubArray\n", f"    Comment={NAMES[str(sid)]}\n", "    Version=8.1.0\n",
                "    Flags=$019E0502\n", f"    SubArrayId={sid}\n", "    PVModule=Mono.PAN\n",
                "    GInverter=Inv.OND\n", "    NModSerie=15\n", f"    NStringCh={nch}\n",
                "    NInverter=2\n", "    NInvMPPT=2\n", "    NoOrientation=1\n",
                "    PowerSharingInverterId=0\n", f"    PNomAC_InMPPT={pnom}\n",
                "  End of PVObject pvSubArray\n"]
    return sa(1, 58, "228.95") + sa(2, 56, "221.05")


def _vc(circuit=None, subarrays=True):
    parts = ["PVObject_=pvVCalcul\n", "  Comment=Topology read\n", "  Version=8.1.0\n",
             "  Flags=$00100000\n"]
    if circuit is not None:
        parts.append(circuit)
    if subarrays:
        parts += _subarrays()
    parts += ["  PVObject_System=pvSystem\n", "    SystemType=Grid\n",
              "  End of PVObject pvSystem\n", "End of PVObject pvVCalcul\n"]
    return "".join(parts)


def _payload(result):
    return json.loads("\n".join(item.text for item in result.content if hasattr(item, "text")))


class SharedTopologyReadContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pvsyst-topology-contract-")
        root = Path(self.tmp.name)
        self.projects = root / "Projects"
        self.projects.mkdir()
        (self.projects / "T.PRJ").write_text(
            "PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        self.vc = self._write("T.VC0", _vc(circuit=_shared_circuit()))
        (root / "PVsystCLI.exe").write_bytes(FAKE_CLI)
        environment = dict(os.environ, PVSYST_WORKSPACE=str(root),
                           PVSYST_CLI=str(root / "PVsystCLI.exe"))
        environment.pop("PVSYST_BUILTIN_COMPONENTS", None)
        self._params = StdioServerParameters(
            command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
            cwd=str(REPO), env=environment)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, text):
        path = self.projects / name
        path.write_bytes(text.encode("utf-8"))
        return path

    async def _get(self, session, variant="VC0", **flags):
        payload = {"project": "T.PRJ", "variant": variant}
        payload.update(flags)
        result = await session.call_tool(GET_TOOL, payload)
        self.assertFalse(result.is_error, result.content)
        return _payload(result)

    async def test_default_payload_has_no_topology_and_never_writes(self):
        before = self.vc.read_bytes()
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                body = await self._get(session)
                self.assertNotIn("topology", body)
                self.assertEqual([row["id"] for row in body["subarrays"]], [1, 2])
        self.assertEqual(self.vc.read_bytes(), before)  # a getter read is byte-pure

    async def test_include_topology_reads_shared_tree_exactly_as_source(self):
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                body = await self._get(session, include_topology=True)
                topo = body["topology"]
                self.assertTrue(topo["present"])
                self.assertEqual(topo["issues"], [])
                root = topo["roots"][0]
                self.assertEqual(root["kind"], "InjectionPointNode")
                owner = root["children"][0]
                self.assertEqual(owner["kind"], "InverterNode")
                # Root owner SubArrayId is OMITTED: presence False, value None (not 0).
                self.assertEqual(owner["subarray_id"], {"present": False, "value": None})
                self.assertEqual(owner["fields"].get("SubArrayName"),
                                 ["PV Array; Sub-array #2"])
                members = owner["children"]
                self.assertEqual([m["kind"] for m in members], ["MPPTNode", "MPPTNode"])
                self.assertEqual([m["subarray_id"]["value"] for m in members], ["1", "2"])
                # Two distinct StringNode blocks keep their own NElements (29 and 28),
                # never aggregated into one 114/57 string total.
                self.assertEqual([m["children"][0]["nelements"]["value"] for m in members],
                                 ["29", "28"])
                # Per-subarray stored inverter declaration is unchanged by the new block.
                self.assertEqual(body["subarrays"][0]["inverter_state"]["PNomAC_InMPPT"], 228.95)
                self.assertEqual(body["subarrays"][1]["inverter_state"]["PNomAC_InMPPT"], 221.05)
                self.assertEqual(body["subarrays"][0]["inverter_state"]["PowerSharingInverterId"], 0)

    async def test_member_order_keeps_source_appearance_not_numeric_sorted(self):
        self._write("T.VC1", _vc(circuit=_shared_circuit(member_order=("2", "1"))))
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                body = await self._get(session, variant="VC1", include_topology=True)
                members = body["topology"]["roots"][0]["children"][0]["children"]
                self.assertEqual([m["subarray_id"]["value"] for m in members], ["2", "1"])
                # The existing subarray presentation stays numeric-ascending, distinct.
                self.assertEqual([row["id"] for row in body["subarrays"]], [1, 2])

    async def test_explicit_zero_owner_stays_present_zero(self):
        self._write("T.VC2", _vc(circuit=_shared_circuit(root_subarray_id=0)))
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                body = await self._get(session, variant="VC2", include_topology=True)
                owner = body["topology"]["roots"][0]["children"][0]
                self.assertEqual(owner["subarray_id"], {"present": True, "value": "0"})

    async def test_unknown_field_preserved_and_missing_block_reports_absent(self):
        self._write("T.VC3", _vc(circuit=_shared_circuit(unknown_field=True)))
        (self.projects / "T.VC4").write_bytes(_vc(circuit=None).encode("utf-8"))
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                with_unknown = await self._get(session, variant="VC3", include_topology=True)
                owner = with_unknown["topology"]["roots"][0]["children"][0]
                self.assertEqual(owner["fields"].get("FutureMarker"), ["PRESERVE_ME"])
                absent = await self._get(session, variant="VC4", include_topology=True)
                self.assertEqual(absent["topology"],
                                 {"present": False, "issues": [], "roots": [],
                                  "scope": absent["topology"]["scope"]})

    async def test_malformed_circuit_reports_issue_not_empty_verified_tree(self):
        self._write("T.VC5", _vc(circuit=_shared_circuit(close=False)))
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                body = await self._get(session, variant="VC5", include_topology=True)
                self.assertTrue(body["topology"]["present"])
                self.assertTrue(body["topology"]["issues"])
                self.assertEqual(body["topology"]["roots"], [])


if __name__ == "__main__":
    unittest.main()
