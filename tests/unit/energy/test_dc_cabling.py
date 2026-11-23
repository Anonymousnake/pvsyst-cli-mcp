"""Unit tests for headless DC cabling solver and VCx variant creation."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.energy.dc import create_dc_cabling_variant, dc_cabling_state
from pvsyst_auto.project.variants import VariantStore

JKM600_PAN = """PVObject_=pvModule
  Version=7.2
  Flags=$00900043
  PVObject_Commercial=pvCommercial
    Flags=$0041
    Manufacturer=Jinkosolar
    Model=JKM600N-78HL4-BDV
    Width=1.134
    Height=2.465
    Depth=0.030
  End of PVObject pvCommercial
  Technol=mtSiMono
  NCelS=78
  NCelP=2
  NDiode=3
  SubModuleLayout=slTwinHalfCells
  GRef=1000
  TRef=25.0
  PNom=600
  Isc=13.87
  Voc=55.03
  Imp=13.26
  Vmp=45.25
  RShunt=1100
  RSerie=0.255
  PVObject_IAM=pvIAM
    Flags=$00
  End of PVObject pvIAM
End of PVObject pvModule
"""

OND = """PVObject_=pvOnduleur
  Version=8.1.6
  Flags=$00
  PVObject_Commercial=pvCommercial
    Flags=$0041
    Manufacturer=Solis
    Model=Solis-50K
  End of PVObject pvCommercial
  PNomConv=50.0
End of PVObject pvOnduleur
"""

VC_COLD = """PVObject_=pvVCalcul
  Comment=Base
  Version=8.1.6
  Flags=$00
  Orientations, list of=1 TOrientGroup
    PVObject_=pvOrient
      Version=8.1.6
      Flags=$00
      NoOrient=1
      FieldType=FixedPlane
      FieldTilt=25.0
      FieldAzim=20.0
      BaseSlope=0.0
      UsesTiltAver=False
      UsesAzimAver=False
      UsesSlopeAver=False
    End of TOrientGroup
  End of Orientations
  PVObject_PVMainArray=pvMainArray
    Flags=$00100400
    Flags2=$082A
    MVNbTransfos=0
    PVObject_SystemCircuit=pvCircuit
      Version=8.1.6
      Flags=$00
      InjectionPointNode Start;
        Children=Start
          InverterNode Start;
            NElements=1
            SubArrayId=1
            SubArrayName=PV Array
            Children=Start
              MPPTNode Start;
                NElements=4
                SubArrayId=1
                SubArrayName=PV Array
                Children=Start
                  StringNode Start;
                    NElements=1
                    SubArrayId=1
                    SubArrayName=PV Array
                  StringNode End;
                Children=End
              MPPTNode End;
            Children=End
          InverterNode End;
        Children=End
      InjectionPointNode End;
    End of PVObject pvCircuit
    SubArrays=Start
      PVObject_=pvSubArray
        Comment=PV Array
        Version=8.1.6
        Flags=$001E0402
        Flags2=$20
        SubArrayId=1
        PVModule={pan}
        GInverter=Solis50.OND
        NoOrientation=1
        NModSerie=15
        NStringCh=4
        NStrOrient1=4
        NInverter=1
        NInvMPPT=4
        RSCablageCh=378.105
        RSCablagePC=1.50
        ACWireLength=0.0
      End of PVObject pvSubArray
    EndTags
  End of PVObject pvMainArray
  PVObject_System=pvSystem
    Flags=$00
    SystemType=Grid
  End of PVObject pvSystem
End of PVObject pvVCalcul
"""


class HeadlessDCCablingTests(unittest.TestCase):
    def _setup_ws(self, *, pan_name="JKM600.PAN", pan_bytes=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = Path(tmp.name) / "ws"
        (ws / "Projects").mkdir(parents=True)
        (ws / "ComposPV" / "PVmodules").mkdir(parents=True)
        (ws / "ComposPV" / "Inverters").mkdir(parents=True)
        (ws / "Projects" / "Demo.PRJ").write_text("PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        (ws / "Projects" / "Demo.VC0").write_bytes(
            VC_COLD.format(pan=pan_name).replace("\n", "\r\n").encode("utf-8")
        )
        if pan_bytes is not None:
            (ws / "ComposPV" / "PVmodules" / pan_name).write_bytes(pan_bytes)
        (ws / "ComposPV" / "Inverters" / "Solis50.OND").write_text(OND.replace("\n", "\r\n"), encoding="utf-8")
        comps = ComponentStore(ws)
        return ws, VariantStore(ws, comps)

    def test_percent_mode_computes_one_diode_resistance_and_inserts_cable_definition(self):
        ws, store = self._setup_ws(pan_bytes=JKM600_PAN.encode("utf-8"))
        preview = create_dc_cabling_variant(
            store, project="Demo.PRJ", source_variant="VC0", new_variant="VC1",
            dc={"mode": "percent", "value": 1.5}, dry_run=True, variant_name="DC 1.50pct",
        )
        self.assertEqual(preview["status"], "ready")
        self.assertFalse((ws / "Projects" / "Demo.VC1").exists())

        res = create_dc_cabling_variant(
            store, project="Demo.PRJ", source_variant="VC0", new_variant="VC1",
            dc={"mode": "percent", "value": 1.5}, dry_run=False, variant_name="DC 1.50pct",
        )
        self.assertEqual(res["status"], "created")
        self.assertEqual(res["dc"]["mode"], "percent")
        self.assertEqual(res["dc"]["loss_percent"], "1.50")
        self.assertTrue(res["dc"]["cable_definition_present"])
        # 1-diode R_array(1.5%) for JKM600 (Ns=15, Nstr=4) is ~193.7 mOhm (within 0.6% of PVsyst 192.688 mOhm)
        r_val = float(res["dc"]["resistance_mohm"])
        self.assertAlmostEqual(r_val, 193.699, delta=1.5)
        self.assertEqual(res["pan_stc"]["source"], "one_diode_stc")

        # Update VC1 -> VC2 switching to resistance=500 mOhm
        res2 = create_dc_cabling_variant(
            store, project="Demo.PRJ", source_variant="VC1", new_variant="VC2",
            dc={"mode": "resistance", "value": 500}, dry_run=False, variant_name="DC 500mOhm",
        )
        self.assertEqual(res2["status"], "created")
        self.assertEqual(res2["dc"]["mode"], "resistance")
        self.assertEqual(res2["dc"]["resistance_mohm"], "500.000")
        self.assertAlmostEqual(float(res2["dc"]["loss_percent"]), 3.87, delta=0.05)

        txt2 = (ws / "Projects" / "Demo.VC2").read_text(encoding="utf-8-sig")
        self.assertIn("Flags=$01900400", txt2)
        self.assertIn("Flags=$019E0402", txt2)
        self.assertIn("Resistance=500 m\u03a9", txt2)
        self.assertEqual(dc_cabling_state(txt2.splitlines(True))["mode"], "resistance")

    def test_builtin_unreadable_pan_allows_resistance_and_rejects_percent(self):
        # Legacy binary PAN or missing built-in DB PAN
        for pan_bytes in (b"pvModule;binary-unreadable", None):
            ws, store = self._setup_ws(pan_name="BuiltinOnly.PAN", pan_bytes=pan_bytes)
            bad = create_dc_cabling_variant(
                store, project="Demo.PRJ", source_variant="VC0", new_variant="VC1",
                dc={"mode": "percent", "value": 1.5}, dry_run=True,
            )
            self.assertEqual(bad["status"], "blocked")
            self.assertIn("mode='resistance'", bad["issues"][0])

            good = create_dc_cabling_variant(
                store, project="Demo.PRJ", source_variant="VC0", new_variant="VC1",
                dc={"mode": "resistance", "value": 500}, dry_run=False, variant_name="Builtin Res",
            )
            self.assertEqual(good["status"], "created")
            self.assertEqual(good["dc"]["resistance_mohm"], "500.000")
            self.assertEqual(good["dc"]["loss_percent"], "0.00")
            self.assertIsNone(good["pan_stc"])
            self.assertTrue(any("0.00" in w for w in good["warnings"]))

    def test_rejects_excessive_resistance_when_loss_hits_99_percent(self):
        _, store = self._setup_ws(pan_bytes=JKM600_PAN.encode("utf-8"))
        res = create_dc_cabling_variant(
            store, project="Demo.PRJ", source_variant="VC0", new_variant="VC1",
            dc={"mode": "resistance", "value": 15000}, dry_run=False,
        )
        self.assertEqual(res["status"], "blocked")
        self.assertIn("99%", res["issues"][0])

        for pct in (99.0, 99.5, 100.0):
            bad_pct = create_dc_cabling_variant(
                store, project="Demo.PRJ", source_variant="VC0", new_variant="VC1",
                dc={"mode": "percent", "value": pct}, dry_run=True,
            )
            self.assertEqual(bad_pct["status"], "blocked")
            self.assertIn("99", bad_pct["issues"][0])

    def test_resistance_rejects_loss_that_rounds_up_to_99_percent(self):
        ws, store = self._setup_ws(pan_bytes=JKM600_PAN.encode("utf-8"))
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                result = create_dc_cabling_variant(
                    store, project="Demo.PRJ", source_variant="VC0", new_variant="VC1",
                    dc={"mode": "resistance", "value": 12783.754}, dry_run=dry_run,
                )
                self.assertEqual(result["status"], "blocked", result)
                self.assertIn("99.00%", result["issues"][0])
                self.assertFalse((ws / "Projects" / "Demo.VC1").exists())

        result = create_dc_cabling_variant(
            store, project="Demo.PRJ", source_variant="VC0", new_variant="VC2",
            dc={"mode": "resistance", "value": 12783.300}, dry_run=False,
        )
        self.assertEqual(result["status"], "created", result)
        self.assertEqual(result["dc"]["loss_percent"], "98.99")

    def test_resistance_disagreement_is_readable_but_not_editable(self):
        ws, store = self._setup_ws()
        source = ws / "Projects/Demo.VC0"
        text = source.read_text(encoding="utf-8").replace(
            "                    SubArrayId=1\n",
            "                    CableDefinition=Start\n"
            "                      Resistance=206.577633418868 m\u03a9\n"
            "                    CableDefinition=End\n"
            "                    SubArrayId=1\n",
        )
        rounded = dc_cabling_state(text.replace("378.105", "206.578").splitlines(True))
        self.assertNotIn("issues", rounded)
        original = text.replace("378.105", "13.621").encode("utf-8")
        source.write_bytes(original)
        state = store.inspect_parameters("Demo.PRJ", "VC0")["dc_cabling"]
        self.assertEqual((state["mode"], state["resistance_mohm"], state["circuit_resistance_mohm"],
                          state["resistance_difference_mohm"]),
                         ("resistance", "13.621", "206.577633418868", "192.956633418868"))
        blocked = create_dc_cabling_variant(
            store, project="Demo.PRJ", source_variant="VC0", new_variant="VC1",
            dc={"mode": "resistance", "value": 500}, dry_run=False,
        )
        self.assertEqual(blocked["status"], "blocked", blocked)
        self.assertEqual(blocked["issues"], state["issues"])
        self.assertFalse((source.parent / "Demo.VC1").exists())
        self.assertEqual(source.read_bytes(), original)

    def test_cold_and_legacy_flag_readback_supported(self):
        ws, _ = self._setup_ws(pan_bytes=JKM600_PAN.encode("utf-8"))
        lines = (ws / "Projects" / "Demo.VC0").read_text(encoding="utf-8-sig").splitlines(True)
        cold = dc_cabling_state(lines)
        self.assertEqual(cold["mode"], "resistance")
        self.assertFalse(cold["cable_definition_present"])
        self.assertIsNone(cold["circuit_resistance_mohm"])
        self.assertEqual(Decimal(cold["resistance_mohm"]), Decimal("378.105"))
