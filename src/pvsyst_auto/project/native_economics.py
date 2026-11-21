"""Native PVsyst 8.1.7 TEcoEvaluation writer so official CLI PDF reports include financial pages."""
from __future__ import annotations

import math
from typing import Any


def _finite(val: Any, name: str, *, allow_zero: bool = True) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
        raise ValueError(f"{name} must be a finite number")
    out = float(val)
    if (out < 0.0) if allow_zero else (out <= 0.0):
        raise ValueError(f"{name} must be {'>= 0' if allow_zero else '> 0'}")
    return out


def format_native_economics_block(cfg: dict[str, Any], eol: str) -> list[str]:
    """Serialize a clean, valid PVsyst 8.1.7 EconomicEval=TEcoEvaluation block."""
    life = cfg.get("project_life_years", 25)
    if type(life) is not int or not 1 <= life <= 60:
        raise ValueError("project_life_years must be an integer in 1..60")
    currency = str(cfg.get("currency") or "USD").strip().upper()
    if not 2 <= len(currency) <= 6 or not currency.isalpha():
        raise ValueError("currency must be a 2..6-letter ISO currency code (e.g. USD, EUR, CNY)")

    capex_total = _finite(cfg.get("capex_total", 0.0), "capex_total", allow_zero=False)
    annual_opex = _finite(cfg.get("annual_opex", 0.0), "annual_opex", allow_zero=True)
    subsidies = _finite(cfg.get("subsidies", 0.0), "subsidies", allow_zero=True)
    discount_pct = _finite(cfg.get("discount_rate_pct", 5.0), "discount_rate_pct", allow_zero=True)
    inflation_pct = _finite(cfg.get("inflation_rate_pct", 2.0), "inflation_rate_pct", allow_zero=True)
    sale_tariff = _finite(cfg.get("sale_tariff", 0.08), "sale_tariff", allow_zero=True)
    cons_tariff = _finite(cfg.get("consumption_tariff", 0.15), "consumption_tariff", allow_zero=True)
    tariff_esc_pct = _finite(cfg.get("tariff_escalation_pct", 0.0), "tariff_escalation_pct", allow_zero=True)
    inv_life = _finite(cfg.get("inverter_lifetime_years", 12.0), "inverter_lifetime_years", allow_zero=False)
    batt_life = _finite(cfg.get("battery_lifetime_years", 10.0), "battery_lifetime_years", allow_zero=False)
    fuel_price = _finite(cfg.get("fuel_price_per_liter", 1.50), "fuel_price_per_liter", allow_zero=True)

    own_funds = max(0.0, capex_total - subsidies)

    lines = [
        f"  EconomicEval=TEcoEvaluation{eol}",
        f"    ProjectLife={life}{eol}",
        f"    PriceKind=Global{eol}",
        f"    Currency={currency}{eol}",
        f"    InvestmentCosts, list of=1 InvestmentCost{eol}",
        f"      InvestmentCost=1{eol}",
        f"          CostIdent=invOtherComponents{eol}",
        f"          DepreciationType=deLinear{eol}",
        f"          DeprecDuration={life}{eol}",
        f"          RedDeprecCoeff=1.00{eol}",
        f"          CostCredit=incCost{eol}",
        f"          ValueType=valAmount{eol}",
        f"          Name=Total PV/BESS Turnkey CAPEX{eol}",
        f"          IsComponent=False{eol}",
        f"          SubFieldNb=1{eol}",
        f"          Quantity=1.00{eol}",
        f"          UnitPrice={capex_total:.5f}{eol}",
        f"      EndTags{eol}",
        f"    End of List InvestmentCosts{eol}",
        f"    OperatingCosts, list of=1 OperatingCost{eol}",
        f"      OperatingCost=1{eol}",
        f"          CostIdent=opeMaintenance{eol}",
        f"          CostCredit=incCost{eol}",
        f"          Name=Annual O&M{eol}",
        f"          Price={annual_opex:.2f}{eol}",
        f"      EndTags{eol}",
        f"    End of List OperatingCosts{eol}",
        f"    OwnFunds={own_funds:.2f}{eol}",
    ]
    if subsidies > 0.0:
        lines.append(f"    Subsidies={subsidies:.2f}{eol}")
    lines.extend([
        f"    InflationRate={inflation_pct / 100.0:.4f}{eol}",
        f"    DiscountRate={discount_pct / 100.0:.4f}{eol}",
        f"    InvertersLifeTime={inv_life:.2f}{eol}",
        f"    BatteriesLifeTime={batt_life:.2f}{eol}",
        f"    FuelPricePerLiter={fuel_price:.2f}{eol}",
        f"    TariffType=taFixed{eol}",
        f"    TariffHourlyPeak=False{eol}",
        f"    TariffSeasonal=False{eol}",
        f"    TariffCsv=False{eol}",
        f"    SummerPeakSaleTariff={sale_tariff:.5f}{eol}",
        f"    SummerOffPeakSaleTariff={sale_tariff:.5f}{eol}",
        f"    SaleTariffEvol={tariff_esc_pct / 100.0:.4f}{eol}",
        f"    ConsumptionTariffType=taFixed{eol}",
        f"    SummerPeakConsumptionTariff={cons_tariff:.5f}{eol}",
        f"    SummerOffPeakConsumptionTariff={cons_tariff:.5f}{eol}",
        f"    ConsumptionTariffHourlyPeak=False{eol}",
        f"    ConsumptionTariffSeasonal=False{eol}",
        f"    ConsumptionTariffCsv=False{eol}",
        f"  End of TEcoEvaluation{eol}",
    ])
    return lines


def replace_native_economics(lines: list[str], updates: dict[str, Any]) -> dict[str, Any]:
    """Write or remove native EconomicEval=TEcoEvaluation inside a .VCx variant."""
    if not isinstance(updates, dict):
        raise ValueError("native_economics_updates must be an object")
    mode = updates.get("mode", "set")
    if mode not in ("set", "none"):
        raise ValueError("native_economics_updates.mode must be 'set' or 'none'")

    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "EconomicEval=TEcoEvaluation"), None)
    end = next((i for i, ln in enumerate(lines) if ln.strip() == "End of TEcoEvaluation"), None)
    had_block = start is not None and end is not None and start < end

    cand = list(lines)
    if had_block:
        del cand[start:end + 1]

    if mode == "set":
        eco_lines = format_native_economics_block(updates, eol)
        # Insert before SimulPlots=Start or before End of PVObject pvVCalcul
        anchor = next(
            (i for i, ln in enumerate(cand) if ln.strip() in ("SimulPlots=Start", "End of PVObject pvVCalcul")),
            len(cand) - 1,
        )
        cand[anchor:anchor] = [eol] + eco_lines + [eol]

    lines[:] = cand
    return {
        "mode": mode,
        "previously_configured": had_block,
        "configured": mode == "set",
        "currency": updates.get("currency", "USD") if mode == "set" else None,
        "capex_total": updates.get("capex_total") if mode == "set" else None,
        "project_life_years": updates.get("project_life_years", 25) if mode == "set" else None,
    }
