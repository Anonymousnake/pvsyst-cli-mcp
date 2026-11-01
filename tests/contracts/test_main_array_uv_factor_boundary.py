"""Public stdio-MCP contract for the present-only UVFactor (native wind-loss Uv) edit.

External, persisted behaviour only. A synthetic self-contained workspace and a fake
``PVsystCLI.exe`` are used, so ZERO PVsyst CLI/Native/GUI runs: the tools are reached
exactly as a caller reaches them, through the MCP server.

Pinned behaviour of ``pvsyst_update_variant_parameters(main_array_updates=...)``:

- ``UVFactor`` is a new present-only supported key on the existing owning
  ``pvMainArray`` declaration (sibling of ``UFactor``). It is a stored scalar of the
  native thermal model; this adapter never creates the field, never guesses a default
  and never fits/implements the Uc+Uv*wind physics.
- The UV branch accepts the genuine observed 8.1.0/8.1.6 Grid tracker flags
  ``$01900400`` (or ``$00100400``) with ``$082A`` and deliberately does NOT require an
  unshaded/geometry-free variant: the real fixture is an 80-object shaded tracker with a
  nonzero-wind MET. It edits only the UV literal and invalidates this variant's own
  derived result cache; every other field (tracking/shading/geometry marker, UFactor,
  PAN/OND, input controls) is preserved byte-for-byte.
- The value must be a finite JSON number; bool/str/NaN/inf are refused before any byte is
  written, a missing or duplicate UVFactor is refused (edit, not create), and UVFactor
  cannot be combined with UFactor in one transaction.
- Because an input change must not retain a stale result claim, a changed UV literal
  drops the root ``VarList`` result block and histograms and clears ONLY the root
  simulation-done bit (bit 0), preserving every other flag bit. A no-op value leaves the
  stored file (and its result cache) untouched.
- Existing preview (dry_run, no write), SHA conflict guard, backup and public restore are
  reused unchanged.
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
RESTORE_TOOL = "pvsyst_restore_variant"
FAKE_CLI = b"Not an executable: this contract must not run PVsyst"


def _vc(uv="    UVFactor=1.2\n", flags="$01900400", geometry=True, varlist=True):
    """A synthetic pvVCalcul with a shaded-tracker-style grid main array and a warm cache."""
    body = [
        "PVObject_=pvVCalcul\n",
        "  Comment=UV boundary\n",
        "  Version=8.1.0\n",
        "  Flags=$050A0001\n",
        "  PVObject_PVMainArray=pvMainArray\n",
        f"    Flags={flags}\n",
        "  Flags2=$082A\n",
        "  UFactor=25.0\n",
        uv,
        "  TrackerHardwareNote=UNCHANGED\n",
        "  End of PVObject pvMainArray\n",
        "  PVObject_=pvSubArray\n",
        "    SubArrayId=1\n",
        "    Version=8.1.0\n",
        "    PVModule=Mono.PAN\n",
        "    GInverter=Inv.OND\n",
        "    NModSerie=15\n",
        "    NStringCh=2\n",
        "    NInverter=1\n",
        "    NInvMPPT=1\n",
        "    NoOrientation=1\n",
        "  End of PVObject pvSubArray\n",
        "  PVObject_System=pvSystem\n",
        "    SystemType=Grid\n",
        "  End of PVObject pvSystem\n",
    ]
    if geometry:
        # A geometry marker that makes _require_no_geometry() raise; the UV branch must not
        # consult it, and the marker must survive the edit unchanged.
        body.append("  ListeObjets, list of=80 PVObject_TShdObject\n")
    if varlist:
        body += [
            "  VarList, list of=1 TVarSimul\n",
            "    VarSimul_1=Result\n",
            "      VarType=Eco\n",
            "      ValMonth=0,0,0,0,0,0,0,0,0,0,0,0,\n",
            "    End of VarSimul=Result\n",
            "  End of the List VarList\n",
            "  Histogram=THistogramme\n",
            "  End of THistogramme\n",
        ]
    body.append("End of PVObject pvVCalcul\n")
    return "".join(body)


def _payload(result):
    return json.loads("\n".join(item.text for item in result.content if hasattr(item, "text")))


class UVFactorContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pvsyst-uv-contract-")
        root = Path(self.tmp.name)
        self.projects = root / "Projects"
        self.projects.mkdir()
        (self.projects / "UV.PRJ").write_text(
            "PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        self.vc = self.projects / "UV.VC0"
        self.vc.write_bytes(_vc().encode("utf-8"))
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
        result = await session.call_tool(GET_TOOL, {"project": "UV.PRJ", "variant": variant})
        self.assertFalse(result.is_error, result.content)
        return _payload(result)["sha256"]

    async def _get(self, session, variant="VC0"):
        result = await session.call_tool(GET_TOOL, {"project": "UV.PRJ", "variant": variant})
        self.assertFalse(result.is_error, result.content)
        return _payload(result)

    async def _update(self, session, variant="VC0", **kwargs):
        payload = {"project": "UV.PRJ", "variant": variant,
                   "expected_sha256": await self._sha(session, variant)}
        payload.update(kwargs)
        return await session.call_tool(UPDATE_TOOL, payload)

    def _write(self, name, text):
        path = self.projects / name
        path.write_bytes(text.encode("utf-8"))
        return path

    async def test_readback_reports_present_uv_factor(self):
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                body = await self._get(session)
                self.assertEqual(body["main_array"]["values"]["UVFactor"], "1.2")
                self.assertEqual(body["main_array"]["values"]["UFactor"], "25.0")
                self.assertEqual(body["main_array"]["values"]["Flags"], "$01900400")

    async def test_edit_changes_uv_invalidates_cache_preserves_scene(self):
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await self._update(session, main_array_updates={"UVFactor": 2.0})
                self.assertFalse(result.is_error, result.content)
                body = _payload(result)
                self.assertTrue(body["changed"])
                self.assertIn("backup_name", body)
                self.assertEqual(body["derived_cache"],
                                 {"var_list_removed": True, "histograms_removed": 1,
                                  "root_simulation_bit_cleared": True})
                after = await self._get(session)
                self.assertEqual(after["main_array"]["values"]["UVFactor"], "2.000")
        text = self.vc.read_bytes().decode("utf-8")
        # Only the UV literal changed among the main-array declarations; the 80-object
        # geometry marker, UFactor and the tracker hardware note are byte-preserved.
        self.assertIn("UVFactor=2.000", text)
        self.assertIn("ListeObjets, list of=80 PVObject_TShdObject", text)
        self.assertIn("TrackerHardwareNote=UNCHANGED", text)
        self.assertIn("UFactor=25.0", text)
        # Derived result cache gone; root simulation bit (bit 0) cleared, others preserved.
        self.assertNotIn("VarList, list of=", text)
        self.assertNotIn("Histogram=THistogramme", text)
        self.assertIn("Flags=$050A0000", text)  # $050A0001 bit0 cleared, high bits kept

    async def test_uv_branch_accepts_tracker_flags_while_ufactor_would_reject_geometry(self):
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                # $00100400 + active-geometry marker: the UFactor branch raises (needs no geometry)
                fixed = self._write("UV.VC1", _vc(uv="    UVFactor=1.2\n", flags="$00100400"))
                r1 = await self._update(session, variant="VC1", main_array_updates={"UFactor": 26.0})
                self.assertTrue(r1.is_error, r1.content)
                self.assertFalse(fixed.read_bytes().decode("utf-8").split("UFactor=")[1].startswith("26"))
                # The UV branch on the same geometry-bearing file succeeds.
                r2 = await self._update(session, variant="VC1", main_array_updates={"UVFactor": 2.0})
                self.assertFalse(r2.is_error, r2.content)

    async def test_dry_run_previews_without_writing(self):
        before = self.vc.read_bytes()
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await self._update(session, main_array_updates={"UVFactor": 2.0},
                                            dry_run=True)
                self.assertFalse(result.is_error, result.content)
                body = _payload(result)
                self.assertTrue(body["changed"])
                self.assertTrue(body["dry_run"])
                self.assertNotIn("backup_name", body)
        self.assertEqual(self.vc.read_bytes(), before)  # preview never writes

    async def test_noop_same_stored_value_writes_nothing_and_keeps_cache(self):
        before = self.vc.read_bytes()
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                # 1.2 renders to "1.200"; use a file already stored as 2.000 for a true no-op.
                self._write("UV.VC2", _vc(uv="    UVFactor=2.000\n"))
                before = (self.projects / "UV.VC2").read_bytes()
                result = await self._update(session, variant="VC2", main_array_updates={"UVFactor": 2.0})
                self.assertFalse(result.is_error, result.content)
                body = _payload(result)
                self.assertFalse(body["changed"])
                self.assertNotIn("backup_name", body)
                self.assertNotIn("derived_cache", body)
        after = (self.projects / "UV.VC2").read_bytes()
        self.assertEqual(after, before)              # no write
        self.assertIn(b"VarList, list of=", after)   # cache untouched on a no-op

    async def test_missing_and_duplicate_uv_are_refused_without_writing(self):
        before = self.vc.read_bytes()
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                self._write("UV.VC3", _vc(uv=""))  # no UVFactor at all
                r1 = await self._update(session, variant="VC3", main_array_updates={"UVFactor": 2.0})
                self.assertTrue(r1.is_error, r1.content)
                dup = _vc(uv="    UVFactor=1.2\n    UVFactor=3.0\n")
                self._write("UV.VC4", dup)
                r2 = await self._update(session, variant="VC4", main_array_updates={"UVFactor": 2.0})
                self.assertTrue(r2.is_error, r2.content)
        self.assertEqual(self.vc.read_bytes(), before)

    async def test_invalid_values_are_refused_without_writing(self):
        before = self.vc.read_bytes()
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                for bad in (True, "2.0", float("nan"), float("inf"), float("-inf")):
                    try:
                        result = await self._update(session, main_array_updates={"UVFactor": bad})
                        refused = result.is_error
                    except Exception:
                        refused = True  # transport may reject non-finite JSON first
                    self.assertTrue(refused, f"{bad!r} must be rejected")
                    self.assertEqual(self.vc.read_bytes(), before)

    async def test_sha_conflict_is_refused(self):
        before = self.vc.read_bytes()
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(UPDATE_TOOL, {
                    "project": "UV.PRJ", "variant": "VC0",
                    "expected_sha256": "0" * 64, "main_array_updates": {"UVFactor": 2.0}})
                self.assertTrue(result.is_error, result.content)
        self.assertEqual(self.vc.read_bytes(), before)

    async def test_uv_cannot_combine_with_ufactor(self):
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await self._update(
                    session, main_array_updates={"UVFactor": 2.0, "UFactor": 26.0})
                self.assertTrue(result.is_error, result.content)

    async def test_backup_and_public_restore_recover_original_bytes(self):
        original = self.vc.read_bytes()
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await self._update(session, main_array_updates={"UVFactor": 2.0})
                self.assertFalse(result.is_error, result.content)
                backup = _payload(result)["backup_name"]
                self.assertNotEqual(self.vc.read_bytes(), original)
                sha = await self._sha(session)
                restored = await session.call_tool(RESTORE_TOOL, {
                    "project": "UV.PRJ", "variant": "VC0", "backup_name": backup,
                    "expected_sha256": sha, "confirm": True})
                self.assertFalse(restored.is_error, restored.content)
        self.assertEqual(self.vc.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
