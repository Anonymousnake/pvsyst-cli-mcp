"""Offline seed-construction tests: no installed PVsyst, CLI, quota or hashing involved."""
import shutil
import tempfile
import unittest
from pathlib import Path

from pvsyst_auto.components.store import ComponentStore, electrical_inventory
from pvsyst_auto.project.circuit import circuit_nodes, inventory as grid_inventory
from pvsyst_auto.project.template import SEED_VARIANT, prepare_standard_template, standard_seed_texts
from pvsyst_auto.project.variants import VariantStore

PAN_NAME = "Generic_Mono_300W.PAN"
OND_NAME = "Generic_7.5kW.OND"
NEW_PROJECT = "Seed_template.PRJ"

SEED_PRJ = """PVObject_=pvProject
  Comment=Residential DEMO
  Version=8.1.0
  Flags=$00080010
MeteoFileName=Geneva_MN90_SYN.MET
PVObject_SitePrj=pvSite
  Comment=Geneva/Cointrin;Switzerland;Europe;
  Flags=$0006083F
  NomF=Geneva/Cointrin
  Latitude=46.2383
  Longitude=6.1094
  Altitude=418
  TimeZone=1.0
End of PVObject pvSite
End of PVObject pvProject
"""

SEED_VC0 = """PVObject_=pvVCalcul
  Comment=First simulation
  Version=8.1.0
  Flags=$00100001
  PRCorrTemp=False

  PVObject_SiteSimul=pvSite
    Flags=$0006083F
    NomF=Geneva/Cointrin
    Latitude=46.2383
    Longitude=6.1094
  End of PVObject pvSite

  Orientations, list of=1 TOrientGroup

    PVObject_=pvOrient
      Comment=Fixed, Tilt 25.0 deg
      Version=8.1.0
      Flags=$00
    NoOrient=1
    FieldType=FixedPlane
    FieldTilt=25.0
    FieldAzim=20.0
    UsesTiltAver=False
    UsesAzimAver=False
    UsesSlopeAver=False
    End of TOrientGroup
  End of Orientations

  PVObject_PVMainArray=pvMainArray
    Flags=$00100400
  UFactor=20.0
  MVNbTransfos=1
  PVObject_SystemCircuit=pvCircuit
    Version=8.1.0
    Flags=$00
    BaseNode=Start
      InjectionPointNode Start;
        Children=Start
          InverterNode Start;
            SubArrayId=1
            SubArrayName=PV Array
            Children=Start
              MPPTNode Start;
                NElements=2
                SubArrayId=1
                SubArrayName=PV Array
                Children=Start
                  StringNode Start;
                    SubArrayId=1
                    SubArrayName=PV Array
                  StringNode End;
                Children=End
              MPPTNode End;
            Children=End
          InverterNode End;
        Children=End
      InjectionPointNode End;
    BaseNode=End
  End of PVObject pvCircuit
  SubArrays=Start
    PVObject_=pvSubArray
      Comment=PV Array
      Version=8.1.0
      Flags=$001E0002
      Flags2=$0020
      SubArrayId=1
      PVModule=Generic_Mono_300W.PAN
      GInverter=Generic_7.5kW.OND
      NModSerie=15
      NStringCh=2
      NStrOrient1=2
      NoOrientation=1
      NInverter=1
      NInvMPPT=1
      SEIsHeterogeneousString=False,
      PowerSharingInverterId=-1
      PNomAC_InMPPT=7.50
      PQualityModU=3.00
      PQualityModI=3.00
      PQualityModP=3.00
      PMismMPP=2.00
      PMismUFixe=2.50
      LIDLoss=0.00
    End of PVObject pvSubArray
  EndTags
  End of PVObject pvMainArray

  PVObject_System=pvSystem
    Flags=$00
    SystemType=Grid
  End of PVObject pvSystem

  PVObject_Ombrage=pvShading
    Comment=No shading scene defined
    Flags=$00080000
    Precision=1
  End of PVObject pvShading

  BatchSimul=TBatchSimul
    Version=8.1.0
    ParamsFileName=_DEMO_RESIDENTIAL_BatchParams_0.CSV
    SaveMode=3
  EndTags
End of PVObject pvVCalcul
"""

PAN_TEXT = """PVObject_=pvModule
  Version=8.1.0
  Flags=$00
  Manufacturer=Generic
  Model=Mono 300W
  Technol=mtSiMono
  NCelS=60
  NCelP=1
  NDiode=3
  PNom=300
  GRef=1000
  TRef=25
  Absorb=0.6
  ISC=9.6
  Voc=46
  Imp=9.0
  Vmp=38
  MuISC=0.05
  muVocSpec=-0.3
  VMaxIEC=1000
  VMaxUL=600
  PVObject_Commercial=pvCommercial
    Manufacturer=Generic
    Model=Mono 300W
    Width=1.00
    Height=1.70
  End of PVObject pvCommercial
End of PVObject pvModule
"""

OND_TEMPLATE = """PVObject_=pvGInverter
  Version=8.1.0
  Flags=$00
  Manufacturer=Generic
  Model=7.5kW
  NbInputs={mppt}
  NbMPPT={mppt}
  Converter=TConverter
    ModeOper=MPPT
    CompPMax=Lim
    CompVMax=Lim
    PNomConv=7.50
    PMaxOUT=8.25
    VMppMin=200
    VMPPMax=800
    VAbsMax=1000
    IDCMax=20
  End of TConverter
  ProfilPIO=TCubicProfile
  End of TCubicProfile
End of PVObject pvGInverter
"""

CAPACITY = {"target_dc_kw": 8.4, "max_dc_ac_ratio": 1.2, "cold_voc_v": 50, "hot_vmp_v": 30,
            "cold_vmp_v": 42, "module_max_operating_current_a": 10,
            "module_max_short_circuit_current_a": 12, "mppt_max_operating_current_a": 10,
            "mppt_max_short_circuit_current_a": 12, "design_min_temperature_c": -10,
            "design_min_operating_temperature_c": 0, "design_max_operating_temperature_c": 60,
            "voltage_standard": "IEC", "design_basis": "Fixture declared design envelope"}


def ond_text(mppt=4):
    return OND_TEMPLATE.format(mppt=mppt)


def crlf(text):
    return text.replace("\n", "\r\n")


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


class FixtureComponents(ComponentStore):
    def _inspect(self, kind, data):
        format_name, text = self._text(kind, data)
        fields = self._fields(text or "")
        return {"type": kind, "format": format_name, "size": len(data),
                "manufacturer": fields.get("Manufacturer", [None])[0],
                "model": fields.get("Model", [None])[0], "fields": fields,
                "structural_errors": [], "warnings": [], "known_value_errors": []}


class FakeCLI:
    def __init__(self, install, workspace, version="PVsystCLI 8.1.6 (build 1234)"):
        self.cli = Path(install).resolve()
        self.workspace = Path(workspace).resolve()
        self._version = version

    def version(self, *, allow_window_creation=None):
        return self._version

    def capabilities(self, *, allow_window_creation=None):
        return {"run-simulation": ["start-date"], "create-site": []}

    def _redact(self, text):
        return text


class StandardTemplateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.projects = self.root / "Projects"
        self.projects.mkdir()
        (self.root / "ComposPV" / "PVmodules").mkdir(parents=True)
        (self.root / "ComposPV" / "Inverters").mkdir(parents=True)
        self.install = self.root / "install"
        self.install.mkdir()
        (self.install / "PVsystCLI.exe").write_bytes(b"stub")
        self.seed_dir = self.install / "DataRO" / "PVsyst8.1_Data" / "Projects"
        self.seed_dir.mkdir(parents=True)
        self.seed_prj = self.seed_dir / "_DEMO_Residential.PRJ"
        self.seed_vc0 = self.seed_dir / "_DEMO_Residential.VC0"
        self.write_seed(crlf(SEED_PRJ), crlf(SEED_VC0))
        (self.root / "ComposPV" / "PVmodules" / PAN_NAME).write_bytes(crlf(PAN_TEXT).encode("utf-8"))
        (self.root / "ComposPV" / "Inverters" / OND_NAME).write_bytes(crlf(ond_text(4)).encode("utf-8"))
        self.cli = FakeCLI(self.install / "PVsystCLI.exe", self.root)
        self.store = VariantStore(self.root, FixtureComponents(self.root))

    def write_seed(self, prj, vc0, prj_bom=False, vc0_bom=True):
        self.seed_prj.write_bytes((b"\xef\xbb\xbf" if prj_bom else b"") + prj.encode("utf-8"))
        self.seed_vc0.write_bytes((b"\xef\xbb\xbf" if vc0_bom else b"") + vc0.encode("utf-8"))
        self.seed_bytes = (self.seed_prj.read_bytes(), self.seed_vc0.read_bytes())

    def ond_electrical(self, mppt=4):
        return electrical_inventory("OND", crlf(ond_text(mppt)))

    def derived(self, mppt=4, **kwargs):
        return standard_seed_texts(self.cli, self.store, ond_electrical=self.ond_electrical(mppt),
                                   pan_name=PAN_NAME, ond_name=OND_NAME, **kwargs)

    def prepare(self, mppt=4, new_project=NEW_PROJECT, **kwargs):
        return prepare_standard_template(self.cli, self.store, ond_electrical=self.ond_electrical(mppt),
                                         pan_name=PAN_NAME, ond_name=OND_NAME,
                                         new_project=new_project, **kwargs)

    def structure(self, text):
        _, lines = self.store._text(text.encode("utf-8"))
        arrays, _ = self.store._sections(lines)
        return lines, arrays, grid_inventory(lines, arrays)

    def assert_seed_untouched(self):
        self.assertEqual((self.seed_prj.read_bytes(), self.seed_vc0.read_bytes()), self.seed_bytes)

    def assert_blocked(self, result, field):
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual([item["field"] for item in result["issues"]], [field], result)

    def test_dry_run_preview_references_and_capacity_check(self):
        result = self.prepare()
        self.assertEqual((result["status"], result["stage"], result["definition"]["variant"]), ("ready", "derived", SEED_VARIANT))
        self.assertEqual((result["source"]["version"], result["source"]["project_bom_preserved"], result["source"]["variant_bom_preserved"]),
                         ("8.1.0", False, True))
        self.assertNotIn("prj_text", result)
        self.assertEqual(sorted(self.projects.iterdir()), [])
        references = [item for item in result["changes"] if item["field"] == "PVModule/GInverter"]
        self.assertEqual(references[0]["after"], f"{PAN_NAME}/{OND_NAME}")
        self.assertTrue(any("ParamsFileName" in warning for warning in result["warnings"]))
        self.assert_seed_untouched()

        cap_ok = self.prepare(capacity=CAPACITY)
        self.assertEqual((cap_ok["status"], cap_ok["capacity_plan"]["mppt_per_inverter"],
                          cap_ok["capacity_plan"]["series_modules"]), ("ready", 4, 7))
        cap_bad = self.prepare(capacity={**CAPACITY, "module_max_operating_current_a": 8})
        self.assertEqual((cap_bad["status"], cap_bad["stage"]), ("blocked", "capacity"))
        self.assertEqual(sorted(self.projects.iterdir()), [])

    def test_installed_cli_and_seed_library_validation(self):
        for version in ("PVsystCLI 8.0.9", "PVsystCLI"):
            res = prepare_standard_template(
                FakeCLI(self.install / "PVsystCLI.exe", self.root, version=version),
                VariantStore(self.root, FixtureComponents(self.root)),
                ond_electrical=self.ond_electrical(), pan_name=PAN_NAME, ond_name=OND_NAME, new_project=NEW_PROJECT,
            )
            self.assert_blocked(res, "seed")
            self.assertIn("8.1", res["issues"][0]["message"])

        shutil.rmtree(self.install / "DataRO")
        self.assert_blocked(self.prepare(), "seed")
        (self.install / "DataRO" / "PVsyst8.0_Data" / "Projects").mkdir(parents=True)
        (self.install / "DataRO" / "PVsyst8.0_Data" / "Projects" / "_DEMO_Residential.PRJ").write_bytes(
            b"PVObject_=pvProject\nEnd of PVObject pvProject\n")
        old_res = self.prepare()
        self.assert_blocked(old_res, "seed")
        self.assertIn("8.0", old_res["issues"][0]["message"])

        shutil.rmtree(self.install / "DataRO" / "PVsyst8.0_Data")
        self.seed_dir.mkdir(parents=True)
        self.write_seed(crlf(SEED_PRJ), crlf(SEED_VC0))
        (self.install / "DataRO" / "PVsyst8.1.6_Data" / "Projects").mkdir(parents=True)
        ambig = self.prepare()
        self.assert_blocked(ambig, "seed")
        self.assertIn("ambiguous", ambig["issues"][0]["message"])

    def test_seed_structural_and_count_inconsistencies_block(self):
        for vc0, fragment in (
            (crlf(SEED_VC0).replace("  PVObject_Ombrage=pvShading", "  PVObject_ShdTable=pvShdTable\n  PVObject_Ombrage=pvShading"), "shading"),
            (crlf(SEED_VC0).replace("NInvMPPT=1", "NInvMPPT=2"), "NInvMPPT=1"),
            (crlf(SEED_VC0).replace("Flags=$001E0002", "Flags=$001E0402"), "ordinary NInvMPPT=1"),
            (crlf(SEED_VC0).replace("NStringCh=2", "NStringCh=3").replace("NStrOrient1=2", "NStrOrient1=3"), "counts"),
            (crlf(SEED_VC0).replace("PVObject_=pvVCalcul", "PVObject_=pvSubArray", 1), "pvVCalcul"),
            (crlf(SEED_VC0).replace("          InverterNode Start;\r\n            SubArrayId=1",
                                    "          InverterNode Start;\r\n            NElements=3\r\n            SubArrayId=1"),
             "implicit single counts are not rewritten"),
        ):
            self.write_seed(crlf(SEED_PRJ), vc0)
            res = self.prepare()
            self.assert_blocked(res, "seed")
            self.assertIn(fragment, res["issues"][0]["message"])

    def test_mppt_1_2_4_conversion_and_explicit_count_dedup(self):
        for inputs in (1, 2, 4):
            with self.subTest(mppt_inputs=inputs):
                derived = self.derived(mppt=inputs)
                text = derived["vc0_text"]
                self.assertIn("Flags=$001E0402\r\n", text)
                self.assertIn(f"NInvMPPT={inputs}\r\n", text)
                self.assertIn(f"NStringCh={inputs}\r\n", text)
                lines, _, checked = self.structure(text)
                self.assertTrue(checked["counts_valid"], checked)
                row = checked["subarrays"][0]
                self.assertEqual((row["declared"]["NInvMPPT"], row["explicit_mppt_inputs_match_declared"],
                                  row["circuit"]["explicit_mppt_inputs"], row["circuit"]["strings"],
                                  row["declared_module_count"]), (str(inputs), True, inputs, inputs, inputs * 15))
                groups = [node for root in circuit_nodes(lines) for node in walk(root) if node.kind == "InverterNode"]
                self.assertEqual((groups[0].one("NElements"), groups[0].children[0].one("NElements"),
                                  [leaf.one("NElements") for leaf in groups[0].children[0].children]),
                                 ("1", str(inputs), ["1"]))

        seeded = crlf(SEED_VC0).replace(
            "          InverterNode Start;\r\n            SubArrayId=1",
            "          InverterNode Start;\r\n            NElements=1\r\n            SubArrayId=1",
        )
        self.write_seed(crlf(SEED_PRJ), seeded)
        derived = self.derived(mppt=4)
        self.assertEqual(sum(1 for item in derived["changes"] if item["field"] == "NElements"), 1)
        self.assertNotIn("NElements=1\r\n            NElements=1", derived["vc0_text"])

    def test_synthesized_template_writes_power_per_mppt_input(self):
        result = self.prepare(mppt=4, dry_run=False, allow_synthesize=True)
        self.assertEqual(result["status"], "created", result)
        variant = self.projects / "Seed_template.VC0"
        _, _, checked = self.structure(variant.read_text(encoding="utf-8-sig"))
        declared = checked["subarrays"][0]["declared"]
        self.assertEqual((declared["NInvMPPT"], declared["PNomAC_InMPPT"]), ("4", "1.88"))

    def test_input_validation_missing_component_and_existing_destination(self):
        for kwargs, field in (({"new_project": "Seed.VC0"}, "new_project"),
                              ({"pan_name": ""}, "pan_name"),
                              ({"dry_run": "yes"}, "dry_run")):
            args = dict(ond_electrical=self.ond_electrical(), pan_name=PAN_NAME, ond_name=OND_NAME, new_project=NEW_PROJECT)
            args.update(kwargs)
            res = prepare_standard_template(self.cli, self.store, **args)
            self.assertEqual(res["status"], "blocked")
            self.assertIn(field, [item["field"] for item in res["issues"]])

        for val in (None, 0, 65, True):
            electrical = self.ond_electrical()
            if val is None:
                electrical["fields"].pop("NbMPPT")
            else:
                electrical["fields"]["NbMPPT"] = {"value": val, "unit": "count", "status": "declared",
                                                  "path": "root", "source_key": "NbMPPT"}
            res = prepare_standard_template(self.cli, self.store, ond_electrical=electrical,
                                            pan_name=PAN_NAME, ond_name=OND_NAME, new_project=NEW_PROJECT)
            self.assert_blocked(res, "seed")
            self.assertIn("NbMPPT", res["issues"][0]["message"])

        existing = self.projects / NEW_PROJECT
        existing.write_bytes(b"PVObject_=pvProject\nEnd of PVObject pvProject\n")
        for dry_run in (True, False):
            res = self.prepare(dry_run=dry_run)
            self.assert_blocked(res, "new_project")
            self.assertIn("no file is overwritten", res["issues"][0]["message"])
        existing.unlink()

        (self.root / "ComposPV" / "Inverters" / OND_NAME).unlink()
        self.assert_blocked(self.prepare(), "seed")
        self.assertEqual(sorted(self.projects.iterdir()), [])

    def test_creation_bom_preservation_and_creation_error(self):
        result = self.prepare(dry_run=False)
        self.assertEqual((result["status"], result["stage"], sorted(result["created"]["files"])),
                         ("created", "created", ["Seed_template.PRJ", "Seed_template.VC0"]))
        lines, _, checked = self.structure((self.projects / "Seed_template.VC0").read_text(encoding="utf-8-sig"))
        self.assertEqual((checked["counts_valid"], checked["subarrays"][0]["declared"]["NInvMPPT"], len(circuit_nodes(lines))),
                         (True, "4", 1))
        for p in self.projects.iterdir():
            p.unlink()

        zh_prj, zh_vc0 = "\u4f4f\u5b85\u793a\u8303\u9879\u76ee", "\u9996\u6b21\u6a21\u62df"
        for prj_bom, vc0_bom, name, non_ascii in (
            (True, True, "BothBom.PRJ", True),
            (False, True, "Vc0Bom.PRJ", False),
            (False, False, "NoBom.PRJ", False),
        ):
            prj = crlf(SEED_PRJ).replace("Comment=Residential DEMO",
                                         f"Comment={zh_prj} DEMO" if non_ascii else "Comment=Residential DEMO")
            vc0 = crlf(SEED_VC0).replace("Comment=First simulation",
                                         f"Comment={zh_vc0} run" if non_ascii else "Comment=First simulation")
            self.write_seed(prj, vc0, prj_bom=prj_bom, vc0_bom=vc0_bom)
            derived = self.derived()
            self.assertEqual((derived["source"]["project_bom_preserved"], derived["source"]["variant_bom_preserved"]),
                             (prj_bom, vc0_bom))
            res = self.prepare(new_project=name, dry_run=False)
            self.assertEqual(res["status"], "created")
            stem = Path(name).stem
            self.assertEqual((self.projects / name).read_bytes().startswith(b"\xef\xbb\xbf"), prj_bom)
            self.assertEqual((self.projects / f"{stem}.VC0").read_bytes().startswith(b"\xef\xbb\xbf"), vc0_bom)
            if non_ascii:
                self.assertIn(zh_prj, (self.projects / name).read_text(encoding="utf-8-sig"))
                self.assertIn(zh_vc0, (self.projects / f"{stem}.VC0").read_text(encoding="utf-8-sig"))

        shutil.rmtree(self.projects)
        failed = self.prepare(dry_run=False)
        self.assertEqual((failed["status"], failed["stage"]), ("failed", "create"))
        self.assertTrue(failed["error"])
        self.assert_seed_untouched()


if __name__ == "__main__":
    unittest.main()
