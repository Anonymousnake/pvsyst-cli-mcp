"""Public stdio-MCP contract for the opt-in native economic-INPUT read.

External, persisted behaviour only. A synthetic self-contained workspace and a fake
``PVsystCLI.exe`` are used, so ZERO PVsyst CLI/Native/GUI runs.

Pinned behaviour of ``pvsyst_get_variant_parameters(include_economics=...)``:

- The default (``include_economics=False``) keeps the prior response shape untouched: no
  ``economics`` key is added, so the existing payload size/behaviour is unchanged.
- ``include_economics=True`` adds a ``economics`` block read from the variant's own stored
  ``EconomicEval=TEcoEvaluation`` native input root field: the verbatim raw block text, the
  unambiguous direct native scalars (a repeated ``DiscountRate`` is kept as a two-item list,
  never flattened) and the direct list declarations (``InvestmentCosts``=10,
  ``OperatingCosts``=7, ``Loans``=3 counts with item types). Unknown/extra fields survive in
  the raw text; nothing is unit-converted or modelled and ``native_consumption_verified`` is
  false (no finance/IRR/LCOE arithmetic).
- The block boundary is the explicit ``End of TEcoEvaluation`` tag when present; a valid cold
  source that carries no ``VarList`` result cache still yields the complete block (the reader
  never depends on a following VarList and never truncates at the first ``EndTags``).
- A variant with no economic block reports ``present=False``.
- A structurally unbalanced block (a ``list of=`` with a missing ``End of List``) is reported
  as an error rather than returning partial data.
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


def _econ_block(truncate_endlist=False):
    lines = [
        "  EconomicEval=TEcoEvaluation\n",
        "    ProjectLife=20\n",
        "    PriceKind=Global\n",
        "    Currency=USD\n",
        "    InvestmentCosts, list of=10 InvestmentCost\n",
        "      InvestmentCost=1\n",
        "          CostIdent=invPVModule\n",
        "          Quantity=1152.00\n",
        "          Subcosts, list of=2 InvestmentCost\n",
        "            InvestmentCost=1\n",
        "                Name=Mono\n",
        "                UnitPrice=140.00000\n",
        "            EndTags\n",
        "            InvestmentCost=2\n",
        "                Name=Supports\n",
        "                UnitPrice=45.00000\n",
        "            EndTags\n",
        "          End of List Subcosts\n",
        "      EndTags\n",
    ]
    if not truncate_endlist:
        lines.append("    End of List InvestmentCosts\n")
    lines += [
        "    OperatingCosts, list of=7 OperatingCost\n",
        "      OperatingCost=1\n",
        "          Name=O&M\n",
        "          Price=1027.91\n",
        "      EndTags\n",
        "    End of List OperatingCosts\n",
        "    Loans, list of=3 Loan\n",
        "      Loan=1 LoanType=ltFixedAnnuity Amount=200000.00 Duration=15 Rate=0.02000\n",
        "    End of List Loans\n",
        "    OwnFunds=200000.00\n",
        "    InflationRate=0.0150\n",
        "    DiscountRate=0.0100\n",
        "    TariffType=taFixed\n",
        "    HourlyCSVTariff=TVarSimul\n",
        "      VarSimul_1=Tariff\n",
        "      End of VarSimul=Tariff\n",
        "    DiscountRate=0.0100\n",
        "    UnknownFutureField=PRESERVE_ME\n",
        "  End of TEcoEvaluation\n",
    ]
    return "".join(lines)


def _vc(econ=True, varlist=True, truncate_endlist=False):
    parts = ["PVObject_=pvVCalcul\n", "  Comment=Econ read\n", "  Version=8.1.0\n",
             "  Flags=$00100000\n"]
    if econ:
        parts.append(_econ_block(truncate_endlist))
    parts += [
        "  PVObject_=pvSubArray\n", "    SubArrayId=1\n", "    Version=8.1.0\n",
        "    PVModule=Mono.PAN\n", "    GInverter=Inv.OND\n", "    NModSerie=15\n",
        "    NStringCh=2\n", "    NInverter=1\n", "    NInvMPPT=1\n",
        "    NoOrientation=1\n", "  End of PVObject pvSubArray\n",
        "  PVObject_System=pvSystem\n", "    SystemType=Grid\n", "  End of PVObject pvSystem\n",
    ]
    if varlist:
        parts += [
            "  VarList, list of=1 TVarSimul\n", "    VarSimul_1=Result\n",
            "      VarType=Eco\n", "      ValMonth=0,0,0,0,0,0,0,0,0,0,0,0,\n",
            "    End of VarSimul=Result\n", "  End of the List VarList\n",
        ]
    parts.append("End of PVObject pvVCalcul\n")
    return "".join(parts)


def _payload(result):
    return json.loads("\n".join(item.text for item in result.content if hasattr(item, "text")))


class EconomicReadContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pvsyst-econ-contract-")
        root = Path(self.tmp.name)
        self.projects = root / "Projects"
        self.projects.mkdir()
        (self.projects / "ECON.PRJ").write_text(
            "PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        (self.projects / "ECON.VC0").write_bytes(_vc().encode("utf-8"))
        (root / "PVsystCLI.exe").write_bytes(FAKE_CLI)
        environment = dict(os.environ, PVSYST_WORKSPACE=str(root),
                           PVSYST_CLI=str(root / "PVsystCLI.exe"))
        environment.pop("PVSYST_BUILTIN_COMPONENTS", None)
        self._params = StdioServerParameters(
            command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
            cwd=str(REPO), env=environment)

    def tearDown(self):
        self.tmp.cleanup()

    async def _get(self, session, variant="VC0", **extra):
        result = await session.call_tool(GET_TOOL, {"project": "ECON.PRJ", "variant": variant, **extra})
        self.assertFalse(result.is_error, result.content)
        return _payload(result)

    def _write(self, name, text):
        (self.projects / name).write_bytes(text.encode("utf-8"))

    async def test_default_response_shape_unchanged(self):
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                body = await self._get(session)
                self.assertNotIn("economics", body)
                self.assertIn("main_array", body)  # existing shape intact

    async def test_optin_returns_verbatim_block_scalars_and_list_declarations(self):
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                econ = (await self._get(session, include_economics=True))["economics"]
        self.assertTrue(econ["present"])
        self.assertEqual(econ["owner"], "TEcoEvaluation")
        self.assertEqual(econ["terminator"], "End of TEcoEvaluation")
        self.assertEqual(econ["native_type"], "TEcoEvaluation")
        self.assertEqual(econ["fields_scope"], "stored_declaration")
        self.assertFalse(econ["native_consumption_verified"])
        # Verbatim raw block: nested lists, subcosts, unknown field all preserved.
        self.assertIn("InvestmentCosts, list of=10 InvestmentCost", econ["raw"])
        self.assertIn("Subcosts, list of=2 InvestmentCost", econ["raw"])
        self.assertIn("UnknownFutureField=PRESERVE_ME", econ["raw"])
        # raw is the inner block only (opener/terminator excluded).
        self.assertNotIn("EconomicEval=TEcoEvaluation", econ["raw"])
        # Scalars: unambiguous direct children, duplicate DiscountRate preserved as a list.
        self.assertEqual(econ["scalars"]["Currency"], ["USD"])
        self.assertEqual(econ["scalars"]["ProjectLife"], ["20"])
        self.assertEqual(econ["scalars"]["DiscountRate"], ["0.0100", "0.0100"])
        self.assertEqual(econ["scalars"]["UnknownFutureField"], ["PRESERVE_ME"])
        # List declarations (declared counts/types from the native list headers).
        self.assertEqual(econ["lists"]["InvestmentCosts"], {"type": "InvestmentCost", "declared_count": 10})
        self.assertEqual(econ["lists"]["OperatingCosts"], {"type": "OperatingCost", "declared_count": 7})
        self.assertEqual(econ["lists"]["Loans"], {"type": "Loan", "declared_count": 3})

    async def test_cold_source_without_varlist_still_reads_full_block(self):
        self._write("ECON.VC1", _vc(varlist=False))
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                econ = (await self._get(session, "VC1", include_economics=True))["economics"]
        self.assertTrue(econ["present"])
        self.assertIn("InvestmentCosts, list of=10 InvestmentCost", econ["raw"])
        self.assertIn("Loans, list of=3 Loan", econ["raw"])
        self.assertNotIn("VarList", econ["raw"])  # no result cache present, block still complete

    async def test_no_econ_block_present_false(self):
        self._write("ECON.VC2", _vc(econ=False))
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                econ = (await self._get(session, "VC2", include_economics=True))["economics"]
        self.assertEqual(econ, {"present": False})

    async def test_unbalanced_block_reports_error_not_partial_data(self):
        self._write("ECON.VC3", _vc(truncate_endlist=True))
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(
                    GET_TOOL, {"project": "ECON.PRJ", "variant": "VC3", "include_economics": True})
                self.assertTrue(result.is_error, result.content)


if __name__ == "__main__":
    unittest.main()
