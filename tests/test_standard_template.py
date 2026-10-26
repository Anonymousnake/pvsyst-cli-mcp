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
    """Text-only component checks without hashing, for self-contained fixtures."""

    def _inspect(self, kind, data):
        format_name, text = self._text(kind, data)
        fields = self._fields(text or "")
        return {"type": kind, "format": format_name, "size": len(data),
                "manufacturer": fields.get("Manufacturer", [None])[0],
                "model": fields.get("Model", [None])[0], "fields": fields,
                "structural_errors": [], "warnings": [], "known_value_errors": []}


class FakeCLI:
    """Only the adapter surface this module uses; no process is ever started."""

    def __init__(self, install, workspace, version="PVsystCLI 8.1.6 (build 1234)"):
        self.cli = Path(install).resolve()
        self.workspace = Path(workspace).resolve()
        self._version = version

    def version(self):
        return self._version

    def capabilities(self):
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
        """Write the seed fixture and adopt it as the immutable baseline for later checks."""
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
        # Derived texts keep their own leading U+FEFF, so encode as-is.
        _, lines = self.store._text(text.encode("utf-8"))
        arrays, _ = self.store._sections(lines)
        return lines, arrays, grid_inventory(lines, arrays)

    def assert_seed_untouched(self):
        self.assertEqual((self.seed_prj.read_bytes(), self.seed_vc0.read_bytes()), self.seed_bytes)

    def assert_blocked(self, result, field):
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual([item["field"] for item in result["issues"]], [field], result)

    def test_dry_run_previews_without_writing_anything(self):
        result = self.prepare()
        self.assertEqual((result["status"], result["stage"]), ("ready", "derived"))
        self.assertEqual(result["definition"]["variant"], SEED_VARIANT)
        self.assertEqual(result["source"]["version"], "8.1.0")
        self.assertEqual(result["source"]["project_bom_preserved"], False)
        self.assertEqual(result["source"]["variant_bom_preserved"], True)
        self.assertNotIn("prj_text", result)
        self.assertNotIn("vc0_text", result)
        self.assertEqual(sorted(self.projects.iterdir()), [])
        self.assert_seed_untouched()
        self.assertEqual([item["field"] for item in result["changes"]].count("NInvMPPT"), 2)

    def test_pan_ond_identity_references_are_resolved_and_reported(self):
        result = self.prepare()
        references = [item for item in result["changes"] if item["field"] == "PVModule/GInverter"]
        self.assertEqual(references[0]["after"], f"{PAN_NAME}/{OND_NAME}")
        self.assertTrue(any("ParamsFileName" in warning for warning in result["warnings"]))

    def test_installed_cli_must_be_81_and_readable(self):
        for cli in (FakeCLI(self.install / "PVsystCLI.exe", self.root, version="PVsystCLI 8.0.9"),
                    FakeCLI(self.install / "PVsystCLI.exe", self.root, version="PVsystCLI")):
            store = VariantStore(self.root, FixtureComponents(self.root))
            result = prepare_standard_template(cli, store, ond_electrical=self.ond_electrical(),
                                               pan_name=PAN_NAME, ond_name=OND_NAME,
                                               new_project=NEW_PROJECT)
            self.assert_blocked(result, "seed")
            self.assertIn("8.1", result["issues"][0]["message"])

    def test_missing_dat_ro_seed_and_ambiguous_library_block(self):
        shutil.rmtree(self.install / "DataRO")
        self.assert_blocked(self.prepare(), "seed")
        self.seed_dir.mkdir(parents=True)
        self.write_seed(crlf(SEED_PRJ), crlf(SEED_VC0))
        (self.install / "DataRO" / "PVsyst8.1.6_Data" / "Projects").mkdir(parents=True)
        result = self.prepare()
        self.assert_blocked(result, "seed")
        self.assertIn("ambiguous", result["issues"][0]["message"])

    def test_old_release_seed_is_never_substituted(self):
        shutil.rmtree(self.install / "DataRO" / "PVsyst8.1_Data")
        (self.install / "DataRO" / "PVsyst8.0_Data" / "Projects").mkdir(parents=True)
        (self.install / "DataRO" / "PVsyst8.0_Data" / "Projects" / "_DEMO_Residential.PRJ").write_bytes(
            b"PVObject_=pvProject\nEnd of PVObject pvProject\n")
        result = self.prepare()
        self.assert_blocked(result, "seed")
        self.assertIn("8.0", result["issues"][0]["message"])

    def test_active_shading_blocks_the_derivation(self):
        self.write_seed(crlf(SEED_PRJ), crlf(SEED_VC0).replace(
            "  PVObject_Ombrage=pvShading", "  PVObject_ShdTable=pvShdTable\n  PVObject_Ombrage=pvShading"))
        result = self.prepare()
        self.assert_blocked(result, "seed")
        self.assertIn("shading", result["issues"][0]["message"])

    def test_mode_and_count_inconsistencies_block_the_derivation(self):
        self.write_seed(crlf(SEED_PRJ), crlf(SEED_VC0).replace("NInvMPPT=1", "NInvMPPT=2"))
        result = self.prepare()
        self.assert_blocked(result, "seed")
        self.assertIn("NInvMPPT=1", result["issues"][0]["message"])
        self.write_seed(crlf(SEED_PRJ), crlf(SEED_VC0).replace("Flags=$001E0002", "Flags=$001E0402"))
        result = self.prepare()
        self.assert_blocked(result, "seed")
        self.assertIn("ordinary NInvMPPT=1", result["issues"][0]["message"])
        self.write_seed(crlf(SEED_PRJ), crlf(SEED_VC0).replace("NStringCh=2", "NStringCh=3").replace(
            "NStrOrient1=2", "NStrOrient1=3"))
        result = self.prepare()
        self.assert_blocked(result, "seed")
        self.assertIn("counts", result["issues"][0]["message"])
        self.assert_seed_untouched()

    def test_derived_seed_is_not_a_pv_vcalcul_or_a_non_utf8_variant(self):
        self.write_seed(crlf(SEED_PRJ), crlf(SEED_VC0).replace(
            "PVObject_=pvVCalcul", "PVObject_=pvSubArray", 1))
        self.assert_blocked(self.prepare(), "seed")

    def test_mppt_1_2_4_conversion_declares_counts_and_is_strict(self):
        for inputs in (1, 2, 4):
            with self.subTest(mppt_inputs=inputs):
                derived = self.derived(mppt=inputs)
                text = derived["vc0_text"]
                self.assertIn("Flags=$001E0402\r\n", text)
                self.assertIn(f"NInvMPPT={inputs}\r\n", text)
                self.assertIn(f"NStringCh={inputs}\r\n", text)
                self.assertIn(f"NStrOrient1={inputs}\r\n", text)
                self.assertEqual(derived["source"]["mppt_inputs_before"], 1)
                self.assertEqual(derived["source"]["strings_before"], "2")
                self.assertEqual(derived["source"]["strings_after"], str(inputs))
                lines, arrays, checked = self.structure(text)
                self.assertTrue(checked["counts_valid"], checked)
                row = checked["subarrays"][0]
                self.assertEqual(row["declared"]["NInvMPPT"], str(inputs))
                self.assertEqual(row["explicit_mppt_inputs_match_declared"], True)
                self.assertEqual(row["circuit"]["explicit_mppt_inputs"], inputs)
                self.assertEqual(row["circuit"]["strings"], inputs)
                self.assertEqual(row["declared_module_count"], inputs * 15)
                groups = [node for root in circuit_nodes(lines) for node in walk(root)
                          if node.kind == "InverterNode"]
                self.assertEqual(len(groups), 1)
                # one() raises on a duplicated field, so every branch declares NElements exactly once
                self.assertEqual(groups[0].one("NElements"), "1")
                self.assertEqual(groups[0].children[0].one("NElements"), str(inputs))
                self.assertEqual([leaf.one("NElements") for leaf in groups[0].children[0].children], ["1"])
                self.assert_seed_untouched()

    def test_explicit_counts_are_inserted_without_duplicates(self):
        seeded = crlf(SEED_VC0).replace(
            "          InverterNode Start;\r\n            SubArrayId=1",
            "          InverterNode Start;\r\n            NElements=1\r\n            SubArrayId=1")
        self.write_seed(crlf(SEED_PRJ), seeded)
        derived = self.derived(mppt=4)
        self.assertEqual(sum(1 for item in derived["changes"] if item["field"] == "NElements"), 1)
        self.assertNotIn("NElements=1\r\n            NElements=1", derived["vc0_text"])
        lines, _, checked = self.structure(derived["vc0_text"])
        self.assertTrue(checked["counts_valid"], checked)
        group = next(node for root in circuit_nodes(lines) for node in walk(root)
                     if node.kind == "InverterNode")
        self.assertEqual(group.fields["NElements"], ["1"])
        self.assertEqual(group.one("NElements"), "1")

    def test_other_explicit_branch_counts_are_not_rewritten(self):
        self.write_seed(crlf(SEED_PRJ), crlf(SEED_VC0).replace(
            "          InverterNode Start;\r\n            SubArrayId=1",
            "          InverterNode Start;\r\n            NElements=3\r\n            SubArrayId=1"))
        result = self.prepare()
        self.assert_blocked(result, "seed")
        self.assertIn("implicit single counts are not rewritten", result["issues"][0]["message"])

    def test_missing_loose_component_blocks_before_writing(self):
        (self.root / "ComposPV" / "Inverters" / OND_NAME).unlink()
        result = self.prepare()
        self.assert_blocked(result, "seed")
        self.assertEqual(sorted(self.projects.iterdir()), [])

    def test_ond_input_count_must_be_declared_and_bounded(self):
        for value in (None, 0, 65, True):
            with self.subTest(value=value):
                electrical = self.ond_electrical()
                if value is None:
                    electrical["fields"].pop("NbMPPT")
                else:
                    electrical["fields"]["NbMPPT"] = {"value": value, "unit": "count",
                                                      "status": "declared", "path": "root",
                                                      "source_key": "NbMPPT"}
                result = prepare_standard_template(self.cli, self.store, ond_electrical=electrical,
                                                   pan_name=PAN_NAME, ond_name=OND_NAME,
                                                   new_project=NEW_PROJECT)
                self.assert_blocked(result, "seed")
                self.assertIn("NbMPPT", result["issues"][0]["message"])

    def test_existing_destination_is_blocked_and_never_overwritten(self):
        existing = self.projects / NEW_PROJECT
        existing.write_bytes(b"PVObject_=pvProject\nEnd of PVObject pvProject\n")
        before = existing.read_bytes()
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                result = self.prepare(dry_run=dry_run)
                self.assert_blocked(result, "new_project")
                self.assertIn("no file is overwritten", result["issues"][0]["message"])
                self.assertEqual(existing.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.projects.iterdir()), [NEW_PROJECT])
        self.assert_seed_untouched()

    def test_capacity_preview_uses_the_derived_text_without_writing(self):
        result = self.prepare(capacity=CAPACITY)
        self.assertEqual(result["status"], "ready", result)
        plan = result["capacity_plan"]
        self.assertEqual(plan["status"], "ready", plan)
        self.assertEqual(plan["mppt_per_inverter"], 4)
        self.assertEqual(plan["series_modules"], 7)
        self.assertEqual(plan["module_count"], 28)
        self.assertEqual(plan["inverter_count"], 1)
        self.assertEqual(sorted(self.projects.iterdir()), [])
        self.assert_seed_untouched()

    def test_unsatisfied_capacity_blocks_with_the_capacity_stage(self):
        result = self.prepare(capacity={**CAPACITY, "module_max_operating_current_a": 8})
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["stage"], "capacity")
        self.assertTrue(all(item["field"].startswith("capacity.") for item in result["issues"]))
        self.assertEqual(sorted(self.projects.iterdir()), [])

    def test_invalid_inputs_block_before_the_seed_is_read(self):
        result = prepare_standard_template(self.cli, self.store, ond_electrical=self.ond_electrical(),
                                           pan_name=PAN_NAME, ond_name=OND_NAME, new_project="Seed.VC0")
        self.assert_blocked(result, "new_project")
        result = prepare_standard_template(self.cli, self.store, ond_electrical=self.ond_electrical(),
                                           pan_name="", ond_name=OND_NAME, new_project=NEW_PROJECT)
        self.assert_blocked(result, "pan_name")
        result = prepare_standard_template(self.cli, self.store, ond_electrical=self.ond_electrical(),
                                           pan_name=PAN_NAME, ond_name=OND_NAME,
                                           new_project=NEW_PROJECT, dry_run="yes")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("dry_run", [item["field"] for item in result["issues"]])
        self.assertEqual(sorted(self.projects.iterdir()), [])
        self.assert_seed_untouched()

    def test_creation_writes_one_pair_and_reports_it(self):
        result = self.prepare(dry_run=False)
        self.assertEqual((result["status"], result["stage"]), ("created", "created"))
        self.assertEqual(result["template_project"], NEW_PROJECT)
        self.assertEqual(result["variant"], SEED_VARIANT)
        self.assertEqual(sorted(path.name for path in self.projects.iterdir()),
                         ["Seed_template.PRJ", "Seed_template.VC0"])
        self.assertEqual(sorted(result["created"]["files"]), ["Seed_template.PRJ", "Seed_template.VC0"])
        self.assertEqual(result["created"]["variants"], [SEED_VARIANT])
        self.assert_seed_untouched()
        lines, _, checked = self.structure((self.projects / "Seed_template.VC0").read_text(
            encoding="utf-8-sig"))
        self.assertTrue(checked["counts_valid"], checked)
        self.assertEqual(next(item for item in checked["subarrays"])["declared"]["NInvMPPT"], "4")
        self.assertEqual(len(circuit_nodes(lines)), 1)

    def test_bom_and_non_ascii_metadata_survive_actual_creation(self):
        # Escapes keep this source ASCII while the fixture carries real UTF-8 metadata.
        chinese_project = "\u4f4f\u5b85\u793a\u8303\u9879\u76ee"
        chinese_variant = "\u9996\u6b21\u6a21\u62df"
        prj = crlf(SEED_PRJ).replace("Comment=Residential DEMO",
                                     f"Comment={chinese_project} DEMO")
        vc0 = crlf(SEED_VC0).replace("Comment=First simulation",
                                     f"Comment={chinese_variant} run")
        self.write_seed(prj, vc0, prj_bom=True, vc0_bom=True)
        derived = self.derived()
        self.assertEqual(derived["source"]["project_bom_preserved"], True)
        self.assertEqual(derived["source"]["variant_bom_preserved"], True)
        self.assertTrue(derived["prj_text"].startswith("\ufeff"))
        self.assertTrue(derived["vc0_text"].startswith("\ufeff"))
        self.assertIn(chinese_project, derived["prj_text"])
        self.assertIn(chinese_variant, derived["vc0_text"])
        # No PRJ field is added or renamed: the returned project text is the seed text.
        self.assertEqual(derived["prj_text"], self.seed_prj.read_bytes().decode("utf-8"))
        result = self.prepare(dry_run=False)
        self.assertEqual(result["status"], "created", result)
        prj_bytes = (self.projects / NEW_PROJECT).read_bytes()
        vc0_bytes = (self.projects / "Seed_template.VC0").read_bytes()
        self.assertTrue(prj_bytes.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(vc0_bytes.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(prj_bytes, derived["prj_text"].encode("utf-8"))
        self.assertEqual(vc0_bytes, derived["vc0_text"].encode("utf-8"))
        self.assertIn(chinese_project, prj_bytes.decode("utf-8-sig"))
        self.assertIn(chinese_variant, vc0_bytes.decode("utf-8-sig"))
        self.assert_seed_untouched()

    def test_bom_markers_are_reported_per_seed_member(self):
        derived = self.derived()
        self.assertEqual(derived["source"]["project_bom_preserved"], False)
        self.assertEqual(derived["source"]["variant_bom_preserved"], True)
        self.assertFalse(derived["prj_text"].startswith("\ufeff"))
        self.assertTrue(derived["vc0_text"].startswith("\ufeff"))
        result = self.prepare(dry_run=False)
        self.assertEqual(result["status"], "created", result)
        prj_bytes = (self.projects / NEW_PROJECT).read_bytes()
        vc0_bytes = (self.projects / "Seed_template.VC0").read_bytes()
        self.assertFalse(prj_bytes.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(vc0_bytes.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(prj_bytes, derived["prj_text"].encode("utf-8"))
        self.assertEqual(vc0_bytes, derived["vc0_text"].encode("utf-8"))
        self.assert_seed_untouched()

    def test_absent_bom_marker_is_not_invented(self):
        self.write_seed(crlf(SEED_PRJ), crlf(SEED_VC0), prj_bom=False, vc0_bom=False)
        derived = self.derived()
        self.assertEqual(derived["source"]["project_bom_preserved"], False)
        self.assertEqual(derived["source"]["variant_bom_preserved"], False)
        self.assertFalse(derived["prj_text"].startswith("\ufeff"))
        self.assertFalse(derived["vc0_text"].startswith("\ufeff"))
        result = self.prepare(new_project="NoBom_template.PRJ", dry_run=False)
        self.assertEqual(result["status"], "created", result)
        prj_bytes = (self.projects / "NoBom_template.PRJ").read_bytes()
        vc0_bytes = (self.projects / "NoBom_template.VC0").read_bytes()
        self.assertFalse(prj_bytes.startswith(b"\xef\xbb\xbf"))
        self.assertFalse(vc0_bytes.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(prj_bytes, derived["prj_text"].encode("utf-8"))
        self.assertEqual(vc0_bytes, derived["vc0_text"].encode("utf-8"))
        self.assert_seed_untouched()

    def test_creation_error_retains_stage_and_the_untouched_seed(self):
        shutil.rmtree(self.projects)
        result = self.prepare(dry_run=False)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stage"], "create")
        self.assertTrue(result["error"])
        self.assert_seed_untouched()
        self.assertFalse(self.projects.exists())


if __name__ == "__main__":
    unittest.main()
