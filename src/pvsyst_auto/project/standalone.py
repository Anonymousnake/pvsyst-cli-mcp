"""Zero-template Stand-Alone off-grid PV + BESS + Controller + optional Backup Genset synthesis and workflow."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pvsyst_auto.native.cli import VAR_GROUPS, build_sfi, summarize_results
from pvsyst_auto.native.load import native_load_block
from pvsyst_auto.project.paths import project_name as normalize_project_name, safe_filename, workspace_file
from pvsyst_auto.project.synthesize import (
    SYNTH_VERSION,
    _DEFAULT_IAM_LINES,
    _DEFAULT_SHADING_LINES,
    _DEFAULT_WIRING_LINES,
    _clean_text,
    _is_pvmeteo_file,
    _normalize_orientation,
    load_meteo_header,
    load_site_block,
    synthesize_orientation_block,
    synthesize_project_text,
)
from pvsyst_auto.validation import bounded_number as _finite

STANDALONE_SCOPE = "stand_alone"
DEFAULT_MPPT_CONTROLLER = "Universal_Controller_LA_MPPT.RLT"
DEFAULT_DIRECT_CONTROLLER = "Universal_Controller_LA_Direct.RLT"
VALID_GENSET_TYPES = ("Diesel", "Gas", "Essence", "Biogas", "Other")


def _pos_int(val: Any, name: str) -> int:
    if type(val) is not int or val < 1:
        raise ValueError(f"{name} must be a positive integer")
    return val


def generate_genset_text(
    *,
    model: str,
    nominal_power_kw: float,
    fuel_consumption_l_per_h: float,
    manufacturer: str = "Generic",
    fuel_type: str = "Diesel",
    comment: str | None = None,
    version: str = SYNTH_VERSION,
) -> str:
    """Generate a complete PVsyst 8.1.7 UTF-8 CRLF .GEN backup generator component file."""
    clean_model = _clean_text(model, "genset.model")
    clean_mfg = _clean_text(manufacturer, "genset.manufacturer")
    if fuel_type not in VALID_GENSET_TYPES:
        raise ValueError(f"genset.fuel_type must be one of {VALID_GENSET_TYPES}")
    p_nom = _finite(nominal_power_kw, "genset.nominal_power_kw", lo=0.1, hi=100000.0)
    c_fuel = _finite(fuel_consumption_l_per_h, "genset.fuel_consumption_l_per_h", lo=0.01, hi=50000.0)
    note = _clean_text(comment or f"Backup {fuel_type} {p_nom:.1f} kW", "genset.comment")
    lines = [
        "PVObject_=pvGenerator",
        f"  Comment={note}",
        f"  Version={version}",
        "  Flags=$00",
        "  PVObject_Commercial=pvCommercial",
        "    Flags=$0041",
        f"    Manufacturer={clean_mfg}",
        f"    Model={clean_model}",
        "    DataSource=User",
        "    YearBeg=2025",
        "    Width=0.600",
        "    Height=0.800",
        "    Depth=0.500",
        "    Weight=85.00",
        "    NPieces=1",
        "  End of PVObject pvCommercial",
        f"  TypeGen={fuel_type}",
        f"  PNomGen={p_nom:.3f}",
        f"  CFuelHor={c_fuel:.3f}",
        "End of PVObject pvGenerator",
        "",
    ]
    return "\ufeff" + "\r\n".join(lines)


def synthesize_standalone_project(
    store=None,
    *,
    project_comment: str = "Stand-Alone Off-Grid PV System",
    variant_comment: str = "Stand-Alone PV+BESS",
    site_name: str,
    met_name: str,
    pan_name: str,
    battery_file: str,
    n_mod_serie: int = 2,
    n_string_ch: int = 4,
    n_batt_serie: int = 4,
    n_batt_parall: int = 2,
    tilt_deg: float = 20.0,
    azimuth_deg: float = 0.0,
    controller_type: str = "mppt",
    controller_file: str | None = None,
    mppt_effic_max_pct: float = 96.0,
    mppt_effic_euro_pct: float = 94.0,
    v_ch_decl: float = 0.90,
    v_ch_encl: float = 0.75,
    v_dech_decl: float = 0.20,
    v_dech_encl: float = 0.45,
    v_bkup_encl: float = 0.25,
    v_bkup_decl: float = 0.55,
    batt_voltage_v: float = 48.0,
    autonomy_days: float = 3.0,
    target_lol_pct: float = 5.0,
    batt_temp_c: float | list[float] = 20.0,
    genset_file: str | None = None,
    backup_operating_power_kw: float | None = None,
    load: dict[str, Any] | None = None,
    daily_load_kwh: float | None = None,
    u_factor: float = 20.0,
    dc_wiring_loss_pct: float = 1.5,
    lid_loss: float = 0.0,
    pmism_mpp: float = 1.0,
    module_quality_loss: float = 0.8,
    version: str = SYNTH_VERSION,
    with_bom: bool = True,
) -> dict[str, Any]:
    """Synthesize a complete zero-template PVsyst 8.1.7 Stand-Alone (.PRJ, .VC0) pair."""
    safe_filename(site_name, ".SIT")
    safe_filename(met_name, ".MET")
    safe_filename(pan_name, ".PAN")
    safe_filename(battery_file, ".BTR")
    if genset_file is not None:
        safe_filename(genset_file, ".GEN")

    nms = _pos_int(n_mod_serie, "n_mod_serie")
    nsc = _pos_int(n_string_ch, "n_string_ch")
    nbs = _pos_int(n_batt_serie, "n_batt_serie")
    nbp = _pos_int(n_batt_parall, "n_batt_parall")

    if controller_type not in ("mppt", "direct"):
        raise ValueError("controller_type must be 'mppt' or 'direct'")
    resolved_ctrl = controller_file or (
        DEFAULT_MPPT_CONTROLLER if controller_type == "mppt" else DEFAULT_DIRECT_CONTROLLER
    )
    safe_filename(resolved_ctrl, ".RLT")

    eff_max = _finite(mppt_effic_max_pct, "mppt_effic_max_pct", lo=50.0, hi=99.9)
    eff_euro = _finite(mppt_effic_euro_pct, "mppt_effic_euro_pct", lo=50.0, hi=eff_max)
    vc_d = _finite(v_ch_decl, "v_ch_decl", lo=0.5, hi=1.0)
    vc_e = _finite(v_ch_encl, "v_ch_encl", lo=0.2, hi=vc_d)
    vd_d = _finite(v_dech_decl, "v_dech_decl", lo=0.05, hi=0.8)
    vd_e = _finite(v_dech_encl, "v_dech_encl", lo=vd_d, hi=0.95)
    vb_e = _finite(v_bkup_encl, "v_bkup_encl", lo=0.05, hi=0.9)
    vb_d = _finite(v_bkup_decl, "v_bkup_decl", lo=vb_e + 0.01, hi=0.98)

    u_batt = _finite(batt_voltage_v, "batt_voltage_v", lo=12.0, hi=1500.0)
    auton = _finite(autonomy_days, "autonomy_days", lo=0.2, hi=30.0)
    lol_pct = _finite(target_lol_pct, "target_lol_pct", lo=0.0, hi=50.0)
    uf = _finite(u_factor, "u_factor", lo=10.0, hi=50.0)

    if isinstance(batt_temp_c, list):
        if len(batt_temp_c) != 12:
            raise ValueError("batt_temp_c list must contain 12 monthly temperatures")
        temps = [_finite(t, f"batt_temp_c[{i}]", lo=-30.0, hi=60.0) for i, t in enumerate(batt_temp_c)]
        t_ref = sum(temps) / 12.0
        temp_type = 2
    else:
        t_ref = _finite(batt_temp_c, "batt_temp_c", lo=-30.0, hi=60.0)
        temps = [t_ref] * 12
        temp_type = 1
    temps_csv = ",".join(f"{t:.0f}" for t in temps) + ","

    # Resolve load block (required for Stand-Alone systems)
    if load is None:
        kwh_day = _finite(daily_load_kwh if daily_load_kwh is not None else 10.0, "daily_load_kwh", lo=0.05, hi=1e6)
        load_spec: dict[str, Any] = {"mode": "constant", "constant_kw": max(0.001, round(kwh_day / 24.0, 3))}
    else:
        load_spec = dict(load)
        kwh_day = float(daily_load_kwh) if daily_load_kwh is not None else 10.0
    load_block_text = native_load_block(load_spec, "\r\n")
    load_lines = load_block_text.splitlines()

    # Resolve site & meteo header blocks
    if store is not None:
        site_path = workspace_file(store.workspace, "Sites", site_name, ".SIT")
        if not site_path.is_file():
            raise FileNotFoundError(f"Workspace SIT file is missing: {site_name}")
        resolved_site, site_identity, site_body = load_site_block(site_path)
        met_path = workspace_file(store.workspace, "Meteo", met_name, ".MET")
        if not met_path.is_file():
            raise FileNotFoundError(f"Workspace MET file is missing: {met_name}")
        if _is_pvmeteo_file(met_path):
            resolved_met, meteo_lines = load_meteo_header(met_path, site_identity, site_body)
        else:
            resolved_met = met_name
            meteo_lines = [
                "  PVObject_Meteo=pvMeteo",
                f"    Comment={site_identity} synthetic meteo",
                f"    Version={version}",
                "    Flags=$010084E1",
                f"    NomF={resolved_met}",
                f"    Site={site_identity}",
                "  End of PVObject pvMeteo",
            ]
    else:
        resolved_site = site_name
        resolved_met = met_name
        site_identity = Path(site_name).stem
        site_body = [
            f"Comment={site_identity}",
            f"Version={version}",
            "Flags=$0006283F",
            f"NomF={resolved_site}",
            f"Site={site_identity}",
        ]
        meteo_lines = [
            "  PVObject_Meteo=pvMeteo",
            f"    Comment={site_identity} synthetic meteo",
            f"    Version={version}",
            "    Flags=$010084E1",
            f"    NomF={resolved_met}",
            f"    Site={site_identity}",
            "  End of PVObject pvMeteo",
        ]

    # Pre-sizing heuristic estimates & sanity checks
    warnings: list[str] = []
    est_cap_ah = round((kwh_day * 1000.0 * auton) / max(u_batt * 0.8, 1.0), 1)
    est_pnom_pv_w = round(kwh_day * 1000.0 / 3.5, 2)
    if genset_file is not None:
        if backup_operating_power_kw is None:
            raise ValueError("backup_operating_power_kw is required when genset_file is set")
        p_bkup = _finite(backup_operating_power_kw, "backup_operating_power_kw", lo=0.05, hi=100000.0)
        sys_flags = "$0060"
    else:
        p_bkup = None
        sys_flags = "$0040"

    # Build orientation block
    orient = _normalize_orientation({"id": 1, "field_type": "FixedPlane", "tilt_deg": tilt_deg, "azimuth_deg": azimuth_deg})
    orient_lines = synthesize_orientation_block(orient, version=version)

    # SubArray flags: MPPT ($00010D22) vs Direct ($00010C22)
    sub_flags2 = "$00010D22" if controller_type == "mppt" else "$00010C22"

    vc_lines: list[str] = [
        "PVObject_=pvVCalcul",
        f"  Comment={_clean_text(variant_comment, 'variant_comment')}",
        f"  Version={version}",
        "  Flags=$00100001",
        "  SystemKindCustom=False",
        "  SystemKind=Standalone system with batteries",
        "",
        "  PVObject_Site=pvSite",
        *[f"    {line}" for line in site_body],
        "  End of PVObject pvSite",
        "",
        *meteo_lines,
        "",
        "  Orientations, list of=1 TOrientGroup",
        "",
        *orient_lines,
        "  End of Orientations",
        "",
        "  PerezUsedInSimul=True",
        "  TranspositionAlgorithm=CircSolSeparate",
        "  ACLossesRef=STC",
        "",
        "  PVObject_PVMainArray=pvMainArray",
        "    Flags=$02110040",
        "  Flags2=$00",
        f"  UFactor={uf:.1f}",
        "  Alpha=0.9",
        "  UFAverEffic=18.4",
        "  MVNbTransfos=1",
        "  UseSpectralCorr=False",
        "  PVObject_SystemCircuit=pvCircuit",
        "    Flags=$00",
        "    ShowLegend=True",
        "    BaseNode=Start",
        "      InjectionPointNode Start;",
        "      InjectionPointNode End;",
        "    BaseNode=End",
        "  End of PVObject pvCircuit",
        "  SubArrays=Start",
        "    PVObject_=pvSubArray",
        "      Comment=Stand-Alone PV Array",
        f"      Version={version}",
        "      Flags=$001C0802",
        f"      Flags2={sub_flags2}",
        "      SubArrayId=1",
        f"      PVModule={pan_name}",
        f"      Controller={resolved_ctrl}",
        f"      NModSerie={nms}",
        f"      NStringCh={nsc}",
        "      NoOrientation=1",
        "      NInverter=1",
        "      NInvMPPT=1",
        "      SEIsHeterogeneousString=False,",
        "      PowerSharingInverterId=-1",
        "      PNomAC_InMPPT=0.00",
        f"      PQualityModU={module_quality_loss:.2f}",
        f"      PQualityModI={module_quality_loss:.2f}",
        f"      PQualityModP={module_quality_loss:.2f}",
        f"      PMismMPP={pmism_mpp:.2f}",
        "      PMismUFixe=2.50",
        f"      LIDLoss={lid_loss:.2f}",
        "      LightSoaking=0.00",
        f"      RSCablagePC={dc_wiring_loss_pct:.2f}",
        "      VDiodeSerie=0.70",
        *_DEFAULT_IAM_LINES,
        *_DEFAULT_WIRING_LINES,
        "      ACWireNoSect=0",
        "      ACWireLength=0.0",
        f"      MPPTEfficMax={eff_max:.2f}",
        f"      MPPTEfficEuro={eff_euro:.2f}",
        f"      VChDecl_syst={vc_d:.3f}",
        f"      VChEncl_syst={vc_e:.3f}",
        f"      VDechDecl_syst={vd_d:.3f}",
        f"      VDechEncl_syst={vd_e:.3f}",
        f"      VBkUpEncl_syst={vb_e:.3f}",
        f"      VBkUpDecl_syst={vb_d:.3f}",
        "    End of PVObject pvSubArray",
        "  EndTags",
        "  End of PVObject pvMainArray",
        "",
        "  PVObject_System=pvSystem",
        f"    Flags={sys_flags}",
        "    SystemType=Battery",
        f"    BatteryFile={battery_file}",
    ]
    if genset_file is not None and p_bkup is not None:
        vc_lines.extend([
            f"    GensetFile={genset_file}",
            f"    PEffBackUp={p_bkup:.3f}",
        ])
    vc_lines.extend([
        "    CycleSOWBeg=1.000",
        "    StaticSOWBeg=1.000",
        f"    PreSizingLOL={lol_pct:.3f}",
        f"    PreSizingNJours={auton:.2f}",
        f"    PreSizingUBattery={u_batt:.1f}",
        f"    PreSizingCapacity={est_cap_ah:.1f}",
        f"    PreSizingPNomPV={est_pnom_pv_w:.2f}",
        "    Coefftemp_Syst=-5.00",
        f"    TypeTempBatt={temp_type}",
        f"    TempBattRef={t_ref:.1f}",
        f"    TempBattMois={temps_csv}",
        f"    NBattSerie={nbs}",
        f"    NBattParall={nbp}",
        "  End of PVObject pvSystem",
        "",
        *list(_DEFAULT_SHADING_LINES),
        "",
        *load_lines,
        "",
        "End of PVObject pvVCalcul",
        "",
    ])

    body_text = "\r\n".join(vc_lines)
    vc0_text = ("\ufeff" + body_text) if with_bom else body_text
    prj_text = synthesize_project_text(
        project_comment=project_comment,
        site_body=site_body,
        met_name=resolved_met,
        version=version,
        with_bom=with_bom,
    )
    return {
        "prj_text": prj_text,
        "vc0_text": vc0_text,
        "system_type": "Battery",
        "controller_file": resolved_ctrl,
        "controller_type": controller_type,
        "battery_file": battery_file,
        "genset_file": genset_file,
        "backup_operating_power_kw": p_bkup,
        "n_mod_serie": nms,
        "n_string_ch": nsc,
        "n_batt_serie": nbs,
        "n_batt_parall": nbp,
        "presizing_warnings": warnings,
    }


def run_standalone_design(
    cli,
    store,
    components,
    spec: dict[str, Any],
    *,
    dry_run: bool = True,
    allow_window_creation: bool | None = None,
) -> dict[str, Any]:
    """End-to-end Stand-Alone PV + BESS (+ optional Genset) design, simulation, diagnostics, and economics."""
    sa = spec.get("standalone")
    if not isinstance(sa, dict):
        raise ValueError("scope='stand_alone' requires a 'standalone' specification object")
    if spec.get("losses"):
        raise ValueError("losses overrides are not supported for scope='stand_alone'")

    raw_prj = spec.get("new_project")
    if not isinstance(raw_prj, str) or not raw_prj.strip():
        raise ValueError("Stand-Alone design requires a non-empty new_project filename")
    prj = normalize_project_name(raw_prj)
    stem = prj[:-4]
    raw_weather = spec.get("weather")
    if raw_weather is not None and not isinstance(raw_weather, dict):
        raise ValueError("Stand-Alone design requires weather as an object")
    weather = raw_weather or {}
    if weather.get("mode", "existing") != "existing":
        raise ValueError("Stand-Alone design requires weather.mode='existing'")
    site_name = weather.get("sit_name")
    met_name = weather.get("met_name")
    if not site_name or not met_name:
        raise ValueError("Stand-Alone design requires weather.sit_name and weather.met_name (existing workspace weather)")

    module_spec = spec.get("module") or {}
    if not isinstance(module_spec, dict):
        raise ValueError("Stand-Alone design requires module as an object")
    pan_name = module_spec.get("file_name")
    if not pan_name:
        raise ValueError("Stand-Alone design requires module.file_name (.PAN)")

    battery_file = sa.get("battery_file")
    if not battery_file:
        raise ValueError("standalone.battery_file (.BTR) is required")

    genset_file = sa.get("genset_file")
    genset_spec = sa.get("genset_spec")
    backup_kw = sa.get("backup_operating_power_kw")
    generated_gen_path: Path | None = None
    gen_text: str | None = None
    if genset_spec is not None:
        if not isinstance(genset_spec, dict):
            raise ValueError("standalone.genset_spec must be an object")
        gen_fname = genset_spec.get("file_name") or f"{stem}_BackupGen.GEN"
        safe_filename(gen_fname, ".GEN")
        genset_file = gen_fname
        nom_kw = genset_spec.get("nominal_power_kw")
        fuel_l_h = genset_spec.get("fuel_consumption_l_per_h")
        if nom_kw is None or fuel_l_h is None:
            raise ValueError("standalone.genset_spec requires nominal_power_kw and fuel_consumption_l_per_h")
        gen_text = generate_genset_text(
            model=genset_spec.get("model") or Path(genset_file).stem,
            nominal_power_kw=nom_kw,
            fuel_consumption_l_per_h=fuel_l_h,
            manufacturer=genset_spec.get("manufacturer", "Generic"),
            fuel_type=genset_spec.get("fuel_type", "Diesel"),
        )
        if backup_kw is None:
            backup_kw = float(nom_kw) * 0.8
        gen_dir = store.workspace / "ComposPV" / "Gensets"
        if gen_dir.is_dir() and any(p.name.casefold() == genset_file.casefold() for p in gen_dir.iterdir()):
            raise FileExistsError(f"Generated GEN already exists: {genset_file}")

    # Preflight project collision and economics config BEFORE dry_run returns or any file is written
    proj_root = store._root()
    if proj_root.is_dir() and any(store._belongs_to_project(p, prj) for p in proj_root.iterdir()):
        raise FileExistsError(f"Working project already exists: {prj}; choose a new project filename")

    eco_spec = sa.get("economics")
    if eco_spec is not None:
        if not isinstance(eco_spec, dict):
            raise ValueError("standalone.economics must be an object")
        if eco_spec.get("capex_total") is not None:
            from pvsyst_auto.engineering.economics import evaluate_standalone_economics
            evaluate_standalone_economics(
                {"e_user_kwh": 0.0, "e_backup_kwh": 0.0, "backup_runtime_hours": 0.0, "backup_fuel_liters": 0.0},
                capex_total=eco_spec["capex_total"],
                annual_om_opex=eco_spec.get("annual_om_opex", 0.0),
                fuel_price_per_liter=eco_spec.get("fuel_price_per_liter", 1.50),
                genset_opex_per_hour=eco_spec.get("genset_opex_per_hour", 0.0),
                genset_fuel_rate_l_per_kwh=eco_spec.get("genset_fuel_rate_l_per_kwh"),
                avoided_tariff_per_kwh=eco_spec.get("avoided_tariff_per_kwh", 0.0),
                financial=eco_spec.get("financial"),
            )

    synth = synthesize_standalone_project(
        store,
        project_comment=sa.get("project_comment") or f"{stem} Stand-Alone PV+BESS",
        variant_comment=sa.get("variant_comment") or "Stand-Alone Off-Grid",
        site_name=site_name,
        met_name=met_name,
        pan_name=pan_name,
        battery_file=battery_file,
        n_mod_serie=sa.get("n_mod_serie", 2),
        n_string_ch=sa.get("n_string_ch", 4),
        n_batt_serie=sa.get("n_batt_serie", 4),
        n_batt_parall=sa.get("n_batt_parall", 2),
        tilt_deg=spec.get("tilt", 20.0),
        azimuth_deg=spec.get("azimuth", 0.0),
        controller_type=sa.get("controller_type", "mppt"),
        controller_file=sa.get("controller_file"),
        mppt_effic_max_pct=sa.get("mppt_effic_max_pct", 96.0),
        mppt_effic_euro_pct=sa.get("mppt_effic_euro_pct", 94.0),
        v_ch_decl=sa.get("v_ch_decl", 0.90),
        v_ch_encl=sa.get("v_ch_encl", 0.75),
        v_dech_decl=sa.get("v_dech_decl", 0.20),
        v_dech_encl=sa.get("v_dech_encl", 0.45),
        v_bkup_encl=sa.get("v_bkup_encl", 0.25),
        v_bkup_decl=sa.get("v_bkup_decl", 0.55),
        batt_voltage_v=sa.get("batt_voltage_v", 48.0),
        autonomy_days=sa.get("autonomy_days", 3.0),
        target_lol_pct=sa.get("target_lol_pct", 5.0),
        batt_temp_c=sa.get("batt_temp_c", 20.0),
        genset_file=genset_file,
        backup_operating_power_kw=backup_kw,
        load=sa.get("load"),
        daily_load_kwh=sa.get("daily_load_kwh"),
    )

    if dry_run:
        return {
            "status": "ready",
            "scope": STANDALONE_SCOPE,
            "dry_run": True,
            "project": prj,
            "variant": "VC0",
            "standalone_summary": {
                k: v for k, v in synth.items() if k not in ("prj_text", "vc0_text")
            },
        }

    # Write optional synthetic .GEN file before creating project
    if gen_text is not None and genset_file is not None:
        try:
            components.create("GEN", genset_file, gen_text)
        except FileExistsError as exc:
            raise FileExistsError(f"Generated GEN already exists: {genset_file}") from exc
        generated_gen_path = store.workspace / "ComposPV" / "Gensets" / genset_file

    store.create_project(prj, synth["prj_text"], {"VC0": synth["vc0_text"]})

    # Run live PVsystCLI simulation with stand_alone variable group
    sfi_vars = list(VAR_GROUPS["stand_alone"])
    sfi_path = store.workspace / "Models" / f"{stem}_standalone.SFI"
    sfi_path.parent.mkdir(parents=True, exist_ok=True)
    build_sfi(sfi_path, sfi_vars, time_step="hour")
    out_csv = store.workspace / "Results" / f"{stem}_VC0_standalone.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    report_pdf_path = None
    if spec.get("report_pages"):
        report_pdf_path = store.workspace / "Results" / f"{stem}_VC0_standalone.pdf"

    sim = cli.run_simulation(
        prj,
        "VC0",
        sfi=sfi_path,
        out_csv=out_csv,
        report_pdf=report_pdf_path,
        report_pages=spec.get("report_pages"),
        report_language=spec.get("report_language"),
        allow_window_creation=allow_window_creation,
    )

    # Parse available columns from CSV header (PVsyst omits viE_BkUp/viFuelBU when no genset is enabled)
    try:
        raw_lines = out_csv.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError:
        raw_lines = out_csv.read_text(encoding="latin-1", errors="replace").splitlines()
    header_line = next(
        (ln for ln in raw_lines if ln.lstrip().lower().startswith(("date;", "date,"))),
        "",
    )
    delim = "," if header_line.lstrip().lower().startswith("date,") else ";"
    available_cols = [c.strip() for c in header_line.split(delim)[1:] if c.strip()]
    results = summarize_results(out_csv, available_cols)
    parsed = results["summary"]
    first = datetime.fromisoformat(results["first"])
    last = datetime.fromisoformat(results["last"])
    if (results["step_minutes"] != 60 or first != datetime(first.year, 1, 1)
            or last != datetime(first.year, 12, 31, 23)):
        raise ValueError("Stand-Alone results require a complete hourly calendar year")
    if not parsed.get("E_User", {}).get("complete"):
        raise ValueError("Stand-Alone results require a complete E_User column")
    for req_complete_col in ("E_Solar", "E_Miss", "T_LOL", "E_BkUp", "FuelBU", "BkUp_ON", "EArray"):
        if req_complete_col in parsed and not parsed[req_complete_col].get("complete"):
            raise ValueError(f"Stand-Alone results require a complete {req_complete_col} column")

    from pvsyst_auto.results import energy_unit_scale

    dt_hours = float(results["step_minutes"]) / 60.0

    def _sum_energy_kwh(col: str) -> float:
        entry = parsed.get(col)
        if not entry:
            return 0.0
        unit = entry.get("unit", "")
        if unit:
            factor, needs_dt = energy_unit_scale(unit)
            if factor == 0.0:
                raise ValueError(f"Stand-Alone {col} requires a power or energy unit, got {unit!r}")
            return float(entry["sum"]) * factor * (dt_hours if needs_dt else 1.0)
        return float(entry["energy_kwh"] if entry.get("energy_kwh") is not None else entry["sum"])

    def _sum_raw(col: str, *, allowed_units: tuple[str, ...] = ()) -> float:
        entry = parsed.get(col)
        if not entry:
            return 0.0
        unit = (entry.get("unit") or "").strip()
        if allowed_units and unit and unit.casefold() not in allowed_units:
            raise ValueError(f"Stand-Alone {col} has unsupported unit {unit!r}")
        return float(entry["sum"])

    def _mean(col: str) -> float:
        entry = parsed.get(col)
        if not entry or entry.get("mean") is None:
            return 0.0
        return float(entry["mean"])

    e_user = _sum_energy_kwh("E_User")
    e_solar = _sum_energy_kwh("E_Solar")
    e_bkup = _sum_energy_kwh("E_BkUp")
    e_miss = _sum_energy_kwh("E_Miss")
    e_avail = _sum_energy_kwh("E_Avail")
    e_unused = _sum_energy_kwh("EUnused")
    e_array = _sum_energy_kwh("EArray")
    t_lol = _sum_raw("T_LOL", allowed_units=("h", "hr", "hour", "hours"))
    bkup_h = _sum_raw("BkUp_ON", allowed_units=("h", "hr", "hour", "hours"))
    fuel_l = _sum_raw("FuelBU")
    soc_mean = _mean("SOCmean")
    e_load_total = e_user + e_miss
    solar_frac = (e_solar / e_load_total) if e_load_total > 0 else 0.0
    lol_pct_actual = (t_lol / results["rows"]) * 100.0

    energy_balance = {
        "e_array_kwh": round(e_array, 2),
        "e_avail_kwh": round(e_avail, 2),
        "e_unused_kwh": round(e_unused, 2),
        "e_user_kwh": round(e_user, 2),
        "e_solar_kwh": round(e_solar, 2),
        "e_backup_kwh": round(e_bkup, 2),
        "e_missing_kwh": round(e_miss, 2),
        "solar_fraction": round(solar_frac, 4),
        "soc_mean": round(soc_mean, 4),
        "t_lol_hours": round(t_lol, 2),
        "lol_pct": round(lol_pct_actual, 3),
        "backup_runtime_hours": round(bkup_h, 2),
        "backup_fuel_liters": round(fuel_l, 2),
    }

    from pvsyst_auto.engineering.diagnostics import diagnose_standalone

    diagnostics = diagnose_standalone(
        energy_balance,
        target_lol_pct=float(sa.get("target_lol_pct", 5.0)),
        has_genset=genset_file is not None,
    )

    eco_summary = None
    eco_spec = sa.get("economics")
    if isinstance(eco_spec, dict) and eco_spec.get("capex_total") is not None:
        from pvsyst_auto.engineering.economics import evaluate_standalone_economics
        eco_summary = evaluate_standalone_economics(
            energy_balance,
            capex_total=eco_spec["capex_total"],
            annual_om_opex=eco_spec.get("annual_om_opex", 0.0),
            fuel_price_per_liter=eco_spec.get("fuel_price_per_liter", 1.50),
            genset_opex_per_hour=eco_spec.get("genset_opex_per_hour", 0.0),
            genset_fuel_rate_l_per_kwh=eco_spec.get("genset_fuel_rate_l_per_kwh"),
            avoided_tariff_per_kwh=eco_spec.get("avoided_tariff_per_kwh", 0.0),
            financial=eco_spec.get("financial"),
        )

    out_result: dict[str, Any] = {
        "status": "needs_review" if diagnostics["needs_review"] else "completed",
        "scope": STANDALONE_SCOPE,
        "dry_run": False,
        "project": prj,
        "variant": "VC0",
        "warnings": sim.get("warnings", []),
        "artifacts": {
            "csv": str(out_csv),
            "sfi": str(sfi_path),
            "report_pdf": str(report_pdf_path) if report_pdf_path else None,
            "generated_genset": str(generated_gen_path) if generated_gen_path else None,
        },
        "energy_balance": energy_balance,
        "diagnostics": diagnostics,
    }
    if eco_summary is not None:
        out_result["economics"] = eco_summary
    return out_result
