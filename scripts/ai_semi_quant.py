#!/usr/bin/env python3
"""AI-SemiQuant producer for the portfolio dashboard.

This module turns the user's AI semiconductor research framework into a
repeatable scoring artifact:

1. curated supply-chain universe and structural factor priors;
2. optional live market overlay from Yahoo Finance;
3. optional portfolio exposure overlay from the generated dashboard;
4. renderable JSON plus a human-readable Markdown reference report.

The structural score is intentionally separate from the tactical market overlay.
It answers "who controls the bottleneck?" before asking whether price action is
already stretched or broken.
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    from scripts.artifact_io import atomic_write_json, atomic_write_text
    from scripts.dashboard_payload import read_dashboard_payload as _read_dashboard_payload
except ModuleNotFoundError:  # direct ``python scripts/ai_semi_quant.py``
    from artifact_io import atomic_write_json, atomic_write_text
    from dashboard_payload import read_dashboard_payload as _read_dashboard_payload


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "output" / "ai_semi_quant.json"
OUT_MD = ROOT / "output" / "ai_semi_quant_report.md"
DASHBOARD = ROOT / "output" / "portfolio_dashboard.html"

STRUCTURAL_FACTOR_WEIGHTS = {
    "pricingPower": 0.30,
    "profitElasticity": 0.24,
    "capexConversion": 0.24,
    "valuationGrowth": 0.22,
}

FACTOR_WEIGHTS = dict(STRUCTURAL_FACTOR_WEIGHTS)
TORQUE_OVERLAY = {
    "maxBonus": 6,
    "maxFragilityPenalty": 8,
    "meaning": "Size/growth torque is a payoff-convexity overlay, not part of structural moat quality.",
}

FACTOR_LABELS = {
    "pricingPower": "Pricing Power / 定价权",
    "profitElasticity": "Profit Elasticity / 利润弹性",
    "capexConversion": "Capex Conversion / 资本开支转化",
    "valuationGrowth": "Valuation & Growth / 估值成长",
    "sizeGrowthTorque": "Size/Growth Torque / 市值成长弹性",
}

FX_SPECS = {
    "USD": (None, "identity"),
    "EUR": ("EURUSD=X", "multiply"),
    "GBP": ("GBPUSD=X", "multiply"),
    "GBX": ("GBPUSD=X", "multiply"),
    "KRW": ("KRW=X", "divide"),
    "TWD": ("TWD=X", "divide"),
    "JPY": ("JPY=X", "divide"),
    "HKD": ("HKD=X", "divide"),
    "CNY": ("CNY=X", "divide"),
}

CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "GBX": "GBp ",
    "KRW": "₩",
    "TWD": "NT$",
    "JPY": "¥",
    "HKD": "HK$",
    "CNY": "¥",
}

GATE_ORDER = ("ALLOW_PLAN", "ALLOW_DD", "WATCH", "WATCH_RESET", "PORTFOLIO_BLOCK", "BLOCK", "DATA_REVIEW")
MODEL_VERSION = "0.4.0"
RISK_FLOOR = 35
MAX_SINGLE_POSITION_WEIGHT = 20.0

TICKER_CURRENCY_SUFFIXES = (
    (".KS", "KRW"),
    (".KQ", "KRW"),
    (".TW", "TWD"),
    (".TWO", "TWD"),
    (".T", "JPY"),
    (".DE", "EUR"),
    (".PA", "EUR"),
    (".AS", "EUR"),
    (".BR", "EUR"),
    (".MI", "EUR"),
    (".L", "GBX"),
    (".HK", "HKD"),
    (".SS", "CNY"),
    (".SZ", "CNY"),
)

CURRENCY_UNIT_SCALES = {"GBX": 0.01}


@dataclass(frozen=True)
class Company:
    companyId: str
    name: str
    ticker: str
    aliases: Tuple[str, ...]
    market: str
    region: str
    node: str
    role: str
    thesis: str
    factors: Dict[str, int]
    riskFlags: Tuple[str, ...] = ()


UNIVERSE: Tuple[Company, ...] = (
    Company(
        "tsmc",
        "TSMC",
        "TSM",
        ("2330.TW",),
        "US ADR / Taiwan",
        "Taiwan",
        "Advanced foundry / CoWoS",
        "Advanced logic, CoWoS, SoIC",
        "Controls the most important physical bottleneck: advanced logic plus advanced packaging.",
        {"pricingPower": 96, "profitElasticity": 88, "capexConversion": 92, "valuationGrowth": 86},
        ("Taiwan geopolitics", "customer concentration"),
    ),
    Company(
        "broadcom",
        "Broadcom",
        "AVGO",
        (),
        "US",
        "United States",
        "Custom ASIC / networking",
        "Hyperscaler custom AI silicon and networking",
        "The ASIC bridge between hyperscaler capex and TSMC capacity allocation.",
        {"pricingPower": 90, "profitElasticity": 86, "capexConversion": 78, "valuationGrowth": 93},
        ("large-customer timing", "multiple expansion"),
    ),
    Company(
        "sk_hynix",
        "SK hynix",
        "000660.KS",
        (),
        "Korea",
        "South Korea",
        "HBM / DRAM",
        "HBM leader, DRAM upcycle beneficiary",
        "Directly monetizes the memory wall; HBM supply contracts give strong order visibility.",
        {"pricingPower": 94, "profitElasticity": 92, "capexConversion": 86, "valuationGrowth": 88},
        ("memory-cycle reversal", "Nvidia qualification concentration"),
    ),
    Company(
        "asml",
        "ASML",
        "ASML",
        ("ASML.AS",),
        "US ADR / Netherlands",
        "Netherlands",
        "Lithography equipment",
        "EUV / DUV bottleneck equipment",
        "The highest-quality equipment bottleneck, tied to advanced-node and memory capex.",
        {"pricingPower": 92, "profitElasticity": 80, "capexConversion": 94, "valuationGrowth": 82},
        ("export controls", "capex digestion"),
    ),
    Company(
        "micron",
        "Micron",
        "MU",
        (),
        "US",
        "United States",
        "HBM / DRAM / NAND",
        "HBM challenger and memory-cycle torque",
        "The most important HBM share-gain and memory-cycle catch-up story in the US market.",
        {"pricingPower": 82, "profitElasticity": 92, "capexConversion": 82, "valuationGrowth": 88},
        ("commodity memory volatility", "capex intensity"),
    ),
    Company(
        "amkor",
        "Amkor",
        "AMKR",
        (),
        "US",
        "United States / Korea / SE Asia",
        "OSAT / advanced packaging",
        "Advanced packaging and test, US reshoring beneficiary",
        "High operating leverage to advanced-packaging overflow, with cleaner OSAT purity than mixed EMS peers.",
        {"pricingPower": 72, "profitElasticity": 90, "capexConversion": 82, "valuationGrowth": 86},
        ("customer qualification", "fixed-cost utilization"),
    ),
    Company(
        "besi",
        "Besi",
        "BESI.AS",
        (),
        "Netherlands",
        "Netherlands",
        "Hybrid bonding equipment",
        "Hybrid bonding and die attach equipment",
        "High-leverage equipment supplier for hybrid bonding, HBM4, and 3D packaging transitions.",
        {"pricingPower": 80, "profitElasticity": 84, "capexConversion": 92, "valuationGrowth": 84},
        ("order lumpiness", "small-cap volatility"),
    ),
    Company(
        "ase",
        "ASE Technology",
        "ASX",
        ("3711.TW",),
        "US ADR / Taiwan",
        "Taiwan",
        "OSAT / advanced packaging",
        "OSAT leader and CoWoS overflow partner",
        "Largest OSAT platform, positioned to absorb outsourced advanced-packaging and test workloads.",
        {"pricingPower": 76, "profitElasticity": 86, "capexConversion": 78, "valuationGrowth": 78},
        ("EMS mix dilution", "Taiwan cycle exposure"),
    ),
    Company(
        "samsung",
        "Samsung Electronics",
        "005930.KS",
        ("SMSN.L",),
        "Korea / London GDR",
        "South Korea",
        "Memory / foundry / packaging",
        "DRAM leader, HBM repair, second-source foundry",
        "One of the only integrated memory-foundry-packaging challengers, but execution still matters.",
        {"pricingPower": 78, "profitElasticity": 84, "capexConversion": 82, "valuationGrowth": 76},
        ("HBM qualification", "foundry losses"),
    ),
    Company(
        "nvidia",
        "NVIDIA",
        "NVDA",
        (),
        "US",
        "United States",
        "AI accelerators / networking",
        "AI GPU platform, networking, systems",
        "Demand originator for the AI capex chain; strongest platform power, but not the physical-capacity bottleneck.",
        {"pricingPower": 96, "profitElasticity": 92, "capexConversion": 72, "valuationGrowth": 86},
        ("valuation", "supply-chain dependency"),
    ),
    Company(
        "amd",
        "AMD",
        "AMD",
        (),
        "US",
        "United States",
        "AI accelerator / CPU",
        "MI accelerator second source and EPYC CPU",
        "Second-source AI accelerator optionality with strong CPU base, but less pricing power than NVIDIA.",
        {"pricingPower": 72, "profitElasticity": 82, "capexConversion": 70, "valuationGrowth": 86},
        ("GPU share uncertainty", "margin mix"),
    ),
    Company(
        "intel",
        "Intel",
        "INTC",
        (),
        "US",
        "United States",
        "CPU / foundry / packaging",
        "18A/14A foundry option plus advanced packaging",
        "Large asymmetric turnaround: meaningful if external 18A/14A customers scale, weak if losses persist.",
        {"pricingPower": 54, "profitElasticity": 78, "capexConversion": 78, "valuationGrowth": 66},
        ("foundry losses", "execution risk"),
    ),
    Company(
        "applied_materials",
        "Applied Materials",
        "AMAT",
        (),
        "US",
        "United States",
        "Semiconductor equipment",
        "Materials engineering, deposition, packaging tools",
        "Broad equipment beta to foundry, logic, memory, and packaging capex.",
        {"pricingPower": 78, "profitElasticity": 76, "capexConversion": 88, "valuationGrowth": 76},
        ("China controls", "capex cycle"),
    ),
    Company(
        "lam_research",
        "Lam Research",
        "LRCX",
        (),
        "US",
        "United States",
        "Semiconductor equipment",
        "Etch/deposition, memory and advanced process tools",
        "Memory and HBM capex conversion with high sensitivity to wafer-fab equipment cycles.",
        {"pricingPower": 78, "profitElasticity": 78, "capexConversion": 88, "valuationGrowth": 78},
        ("memory capex cycle", "China controls"),
    ),
    Company(
        "kla",
        "KLA",
        "KLAC",
        (),
        "US",
        "United States",
        "Process control equipment",
        "Inspection, metrology, yield control",
        "Yield control becomes more valuable as 2nm/3D/HBM process complexity rises.",
        {"pricingPower": 86, "profitElasticity": 78, "capexConversion": 86, "valuationGrowth": 76},
        ("equipment cycle", "valuation"),
    ),
    Company(
        "asm_international",
        "ASM International",
        "ASM.AS",
        (),
        "Netherlands",
        "Netherlands",
        "Front-end equipment",
        "ALD, epitaxy and advanced-node deposition",
        "Advanced-node deposition supplier with cleaner structural exposure than broad WFE indexes.",
        {"pricingPower": 78, "profitElasticity": 78, "capexConversion": 86, "valuationGrowth": 80},
        ("order cyclicality", "European listing liquidity"),
    ),
    Company(
        "hanmi_semiconductor",
        "Hanmi Semiconductor",
        "042700.KS",
        (),
        "Korea",
        "South Korea",
        "HBM equipment",
        "Thermo-compression bonders and HBM stack equipment",
        "Small, high-beta pick-and-shovel exposure to HBM stack buildout.",
        {"pricingPower": 74, "profitElasticity": 88, "capexConversion": 90, "valuationGrowth": 82},
        ("customer concentration", "small-cap volatility"),
    ),
    Company(
        "hon_hai",
        "Hon Hai / Foxconn",
        "2317.TW",
        (),
        "Taiwan",
        "Taiwan",
        "AI server ODM",
        "AI server and rack assembly",
        "Downstream server/rack capture of AI infrastructure spending, with lower margin but large volume.",
        {"pricingPower": 58, "profitElasticity": 76, "capexConversion": 68, "valuationGrowth": 76},
        ("low-margin assembly", "customer concentration"),
    ),
    Company(
        "quanta",
        "Quanta Computer",
        "2382.TW",
        (),
        "Taiwan",
        "Taiwan",
        "AI server ODM",
        "Cloud server and rack ODM",
        "High-volume AI server ODM exposure; watch margin capture versus component suppliers.",
        {"pricingPower": 62, "profitElasticity": 78, "capexConversion": 70, "valuationGrowth": 78},
        ("margin compression", "server cycle"),
    ),
    Company(
        "wiwynn",
        "Wiwynn",
        "6669.TW",
        (),
        "Taiwan",
        "Taiwan",
        "AI server ODM",
        "Cloud infrastructure and AI server systems",
        "Pureer cloud server exposure than broader EMS peers, but still downstream of silicon bottlenecks.",
        {"pricingPower": 66, "profitElasticity": 80, "capexConversion": 72, "valuationGrowth": 82},
        ("customer concentration", "ODM margin"),
    ),
)

CAPITAL_WATERFALL = (
    {
        "stage": "Hyperscaler capex",
        "description": "Cloud capex starts the chain: AI clusters, custom ASICs, GPUs, networking, memory, servers.",
        "beneficiaries": ("NVDA", "AVGO", "AMD", "TSM"),
        "leadingKpis": ("cloud capex guide", "AI accelerator backlog", "gigawatt deployment plans"),
    },
    {
        "stage": "Design / platform capture",
        "description": "Platform and ASIC vendors translate cloud budgets into silicon demand.",
        "beneficiaries": ("NVDA", "AVGO", "AMD"),
        "leadingKpis": ("AI revenue growth", "custom silicon customers", "networking attach rate"),
    },
    {
        "stage": "Advanced manufacturing",
        "description": "Advanced-node wafer starts and CoWoS allocation cap final AI chip supply.",
        "beneficiaries": ("TSM", "005930.KS", "INTC"),
        "leadingKpis": ("2nm/3nm utilization", "CoWoS monthly capacity", "customer prepayments"),
    },
    {
        "stage": "HBM / memory",
        "description": "HBM and advanced DRAM determine accelerator bandwidth and increasingly crowd out standard DRAM.",
        "beneficiaries": ("000660.KS", "MU", "005930.KS"),
        "leadingKpis": ("HBM share", "HBM bit output", "DRAM contract prices"),
    },
    {
        "stage": "Equipment / materials",
        "description": "Capex converts into lithography, deposition, etch, inspection and bonding equipment orders.",
        "beneficiaries": ("ASML", "AMAT", "LRCX", "KLAC", "ASM.AS", "BESI.AS", "042700.KS"),
        "leadingKpis": ("WFE spending", "book-to-bill", "backlog"),
    },
    {
        "stage": "Advanced packaging overflow",
        "description": "If integrated CoWoS capacity is tight, OSATs and packaging equipment pick up overflow.",
        "beneficiaries": ("AMKR", "ASX", "3711.TW", "BESI.AS"),
        "leadingKpis": ("CoWoS supply gap", "OSAT utilization", "advanced packaging revenue"),
    },
    {
        "stage": "AI server integration",
        "description": "Server ODMs monetize racks and systems after silicon is allocated.",
        "beneficiaries": ("2317.TW", "2382.TW", "6669.TW"),
        "leadingKpis": ("AI server revenue", "rack-scale orders", "gross margin trend"),
    },
)

CAPITAL_FLOW_EDGES = (
    {"source": "hyperscaler_capex", "target": "nvidia", "edgeType": "direct_platform_spend", "weight": 94, "confidence": 0.85},
    {"source": "hyperscaler_capex", "target": "broadcom", "edgeType": "custom_asic_spend", "weight": 88, "confidence": 0.80},
    {"source": "hyperscaler_capex", "target": "amd", "edgeType": "second_source_accelerator", "weight": 72, "confidence": 0.68},
    {"source": "nvidia", "target": "tsmc", "edgeType": "advanced_logic_cowos", "weight": 95, "confidence": 0.90},
    {"source": "broadcom", "target": "tsmc", "edgeType": "custom_asic_foundry", "weight": 88, "confidence": 0.82},
    {"source": "nvidia", "target": "sk_hynix", "edgeType": "hbm_supply", "weight": 90, "confidence": 0.82},
    {"source": "nvidia", "target": "micron", "edgeType": "hbm_supply", "weight": 76, "confidence": 0.68},
    {"source": "tsmc", "target": "asml", "edgeType": "advanced_node_capex", "weight": 86, "confidence": 0.85},
    {"source": "tsmc", "target": "applied_materials", "edgeType": "wfe_capex", "weight": 74, "confidence": 0.70},
    {"source": "tsmc", "target": "kla", "edgeType": "yield_control_capex", "weight": 78, "confidence": 0.74},
    {"source": "hbm_makers", "target": "hanmi_semiconductor", "edgeType": "hbm_bonder_capex", "weight": 82, "confidence": 0.70},
    {"source": "advanced_packaging_shortage", "target": "amkor", "edgeType": "cowos_overflow", "weight": 80, "confidence": 0.68},
    {"source": "advanced_packaging_shortage", "target": "ase", "edgeType": "cowos_overflow", "weight": 78, "confidence": 0.70},
    {"source": "ai_rack_demand", "target": "wiwynn", "edgeType": "server_odm", "weight": 70, "confidence": 0.62},
    {"source": "ai_rack_demand", "target": "quanta", "edgeType": "server_odm", "weight": 66, "confidence": 0.62},
    {"source": "ai_rack_demand", "target": "hon_hai", "edgeType": "server_odm", "weight": 62, "confidence": 0.58},
)

SOURCE_LINKS = (
    {
        "name": "TSMC annual reports and fab capacity",
        "url": "https://investor.tsmc.com/english/annual-reports",
        "use": "official capacity, advanced-node revenue, annual report anchor",
    },
    {
        "name": "TSMC fab capacity page",
        "url": "https://www.tsmc.com/english/dedicatedFoundry/manufacturing/fab_capacity",
        "use": "manufacturing capacity reference",
    },
    {
        "name": "TrendForce CoWoS capacity estimate",
        "url": "https://www.trendforce.com/news/2026/06/15/news-tsmc-cowos-supply-demand-gap-reportedly-seen-narrowing-from-20-to-10-by-end-2026-as-capacity-expands/",
        "use": "CoWoS and OSAT advanced-packaging capacity checks",
    },
    {
        "name": "SEMI 300mm fab equipment outlook",
        "url": "https://www.semi.org/en/semi-press-release/semi-projects-double-digit-growth-in-global-300mm-fab-equipment-spending-for-2026-and-2027",
        "use": "equipment spending and capex conversion context",
    },
    {
        "name": "Counterpoint DRAM and HBM share tracker",
        "url": "https://counterpointresearch.com/en/insights/global-dram-and-hbm-market-share",
        "use": "DRAM/HBM market-share baseline",
    },
)


def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    if value is None or not math.isfinite(value):
        value = 50.0
    return max(lo, min(hi, value))


def rn(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    try:
        if not math.isfinite(float(value)):
            return None
        return round(float(value), digits)
    except Exception:
        return None


def finite(value) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except Exception:
        return False


def validated_factor(value, label: str) -> float:
    """Return a finite 0..100 factor or fail instead of silently neutralizing it."""
    if isinstance(value, bool) or not finite(value):
        raise ValueError(f"{label} must be a finite number in [0, 100]")
    score = float(value)
    if not 0.0 <= score <= 100.0:
        raise ValueError(f"{label}={score:g} is outside [0, 100]")
    return score


def infer_ticker_currency(ticker: Optional[str]) -> str:
    symbol = str(ticker or "").upper()
    for suffix, currency in TICKER_CURRENCY_SUFFIXES:
        if symbol.endswith(suffix):
            return currency
    return "USD"


def normalize_currency(value: Optional[str]) -> str:
    raw = str(value or "").strip()
    if raw in ("GBp", "GBX", "GBx"):
        return "GBX"
    return raw.upper()


def normalize_market_cap_value(value, quote_currency: Optional[str], source: Optional[str]) -> Tuple[Optional[float], float]:
    """Normalize provider market cap into the major currency unit."""
    currency = normalize_currency(quote_currency)
    scale = 0.01 if currency == "GBX" and source == "yahoo_fast_info" else 1.0
    if not finite(value) or float(value) <= 0:
        return None, scale
    return float(value) * scale, scale


def fmt_local_money(value: Optional[float], currency: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    currency = normalize_currency(currency) or "USD"
    symbol = CURRENCY_SYMBOLS.get(currency, f"{currency} ")
    try:
        v = float(value)
    except Exception:
        return None
    if currency in ("KRW", "TWD", "JPY"):
        return f"{symbol}{v:,.0f}"
    return f"{symbol}{v:,.2f}"


def pct_rank(value: Optional[float], values: Iterable[Optional[float]]) -> float:
    vals = sorted(float(v) for v in values if finite(v))
    if not vals or not finite(value):
        return 50.0
    v = float(value)
    below = sum(1 for x in vals if x < v)
    equal = sum(1 for x in vals if x == v)
    return ((below + 0.5 * equal) / len(vals)) * 100.0


def percentile_int(value: Optional[float], values: Iterable[Optional[float]]) -> Optional[int]:
    if not finite(value):
        return None
    return int(round(clamp(pct_rank(value, values))))


def peer_percentile_display(percentile: Optional[int], peer_count: int) -> str:
    if peer_count < 3:
        return f"N/A · n={peer_count}"
    if percentile is None:
        return f"— · n={peer_count}"
    return f"P{percentile} · n={peer_count}"


def pct_change(values, days: int) -> Optional[float]:
    if len(values) <= days:
        return None
    prev = float(values.iloc[-1 - days])
    if not prev:
        return None
    return (float(values.iloc[-1]) / prev - 1.0) * 100.0


def rsi(series, n: int = 14):
    delta = series.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down)


def structural_score(factors: Dict[str, int]) -> int:
    missing = [key for key in STRUCTURAL_FACTOR_WEIGHTS if key not in factors]
    if missing:
        raise ValueError(f"missing structural factors: {', '.join(missing)}")
    total = sum(validated_factor(factors[k], k) * w for k, w in STRUCTURAL_FACTOR_WEIGHTS.items())
    return int(round(clamp(total)))


def peer_group(company: Company) -> str:
    node = company.node.lower()
    role = company.role.lower()
    if "hbm" in node or "dram" in node or "memory" in node:
        return "HBM / Memory"
    if "equipment" in node or "lithography" in node or "bonding" in node or "process control" in node:
        return "Equipment"
    if "osat" in node or "packaging" in node:
        return "Advanced Packaging / OSAT"
    if "server odm" in node:
        return "AI Server ODM"
    if "foundry" in node:
        return "Foundry / Manufacturing"
    if "accelerator" in node or "asic" in node or "networking" in role:
        return "AI Accelerator / ASIC"
    return "Other"


def market_cap_size_score(market_cap_usd: Optional[float]) -> int:
    """Nonlinear size elasticity score.

    Smaller market cap can produce larger upside if growth is real, but very
    small/liquidation-risk buckets are not blindly rewarded.
    """
    if not market_cap_usd or market_cap_usd <= 0:
        return 55
    if market_cap_usd < 2e9:
        return 78
    if market_cap_usd < 10e9:
        return 95
    if market_cap_usd < 25e9:
        return 92
    if market_cap_usd < 75e9:
        return 86
    if market_cap_usd < 150e9:
        return 78
    if market_cap_usd < 300e9:
        return 70
    if market_cap_usd < 750e9:
        return 60
    if market_cap_usd < 1.5e12:
        return 50
    return 40


def market_cap_bucket(market_cap_usd: Optional[float]) -> str:
    if not market_cap_usd or market_cap_usd <= 0:
        return "unknown"
    if market_cap_usd < 2e9:
        return "micro/small"
    if market_cap_usd < 10e9:
        return "small"
    if market_cap_usd < 25e9:
        return "small-mid"
    if market_cap_usd < 75e9:
        return "mid"
    if market_cap_usd < 150e9:
        return "large-mid"
    if market_cap_usd < 300e9:
        return "large"
    if market_cap_usd < 750e9:
        return "mega"
    if market_cap_usd < 1.5e12:
        return "mega+"
    return "hyper-scale"


def size_growth_torque(company: Company, market_cap_usd: Optional[float]) -> int:
    """Blend size elasticity with growth quality and structural quality."""
    growth = company.factors.get("valuationGrowth", 50)
    size = market_cap_size_score(market_cap_usd)
    quality = (
        company.factors.get("pricingPower", 50)
        + company.factors.get("profitElasticity", 50)
        + company.factors.get("capexConversion", 50)
    ) / 3.0
    score = growth * 0.50 + size * 0.35 + quality * 0.15
    if market_cap_usd and market_cap_usd < 5e9 and quality < 70:
        score -= 8
    if market_cap_usd and market_cap_usd > 1.5e12 and growth < 88:
        score -= 4
    return int(round(clamp(score)))


def torque_overlay(company: Company, factors: Dict[str, int], metrics: Dict[str, Optional[float]]) -> Tuple[int, int, int]:
    """Return torque-adjusted structural score, bonus, and fragility penalty.

    Torque captures payoff convexity. It can lift a strong smaller company, but
    it should not make a fragile small company look like it has a stronger moat.
    """
    base = structural_score(factors)
    torque = factors.get("sizeGrowthTorque", 50)
    cap_usd = metrics.get("scoringMarketCapUsd")
    quality = (
        factors.get("pricingPower", 50)
        + factors.get("profitElasticity", 50)
        + factors.get("capexConversion", 50)
    ) / 3.0
    bonus = int(round(clamp((torque - 60) * 0.18, 0, TORQUE_OVERLAY["maxBonus"])))
    fragility = 0.0
    risk_text = " ".join(company.riskFlags).lower()
    if cap_usd and cap_usd < 25e9 and any(x in risk_text for x in ("customer", "small-cap", "lumpiness", "liquidity")):
        fragility += 2
    if cap_usd and cap_usd < 10e9 and quality < 72:
        fragility += 3
    if (metrics.get("vol1y") or 0) > 90:
        fragility += 2
    fragility += min(overextension_penalty(metrics) * 0.12, 3)
    penalty = int(round(clamp(fragility, 0, TORQUE_OVERLAY["maxFragilityPenalty"])))
    adjusted = int(round(clamp(base + bonus - penalty)))
    return adjusted, bonus, penalty


def market_profile_fields(profile: Optional[dict]) -> Dict[str, Optional[float]]:
    profile = profile or {}
    cap_usd = profile.get("marketCapUsd")
    price_currency = normalize_currency(profile.get("currency")) or None
    cap_currency = normalize_currency(profile.get("marketCapCurrency")) or ("GBP" if price_currency == "GBX" else price_currency)
    point_in_time = profile.get("marketCapPointInTimeCompatible")
    scoring_cap_usd = cap_usd if point_in_time is not False else None
    return {
        "marketCap": rn(profile.get("marketCap"), 0),
        "marketCapRaw": rn(profile.get("marketCapRaw"), 0),
        "marketCapUnitScale": rn(profile.get("marketCapUnitScale"), 4),
        "marketCapSource": profile.get("marketCapSource"),
        "marketCapCurrency": cap_currency,
        "priceCurrency": price_currency,
        "marketCapUsd": rn(cap_usd, 0),
        "scoringMarketCapUsd": rn(scoring_cap_usd, 0),
        "marketCapBucket": market_cap_bucket(cap_usd),
        "marketCapFxRate": rn(profile.get("fxRate"), 6),
        "marketCapFxSource": profile.get("fxSource"),
        "marketCapFxAsOf": profile.get("fxAsOf"),
        "marketCapAsOf": profile.get("marketCapAsOf"),
        "marketCapAsOfSource": profile.get("marketCapAsOfSource"),
        "marketCapQuoteAsOf": profile.get("marketCapQuoteAsOf"),
        "marketCapDecisionAsOf": profile.get("marketCapDecisionAsOf"),
        "marketCapAlignmentReason": profile.get("marketCapAlignmentReason"),
        "marketCapFetchedAt": profile.get("marketCapFetchedAt"),
        "marketCapPointInTimeCompatible": point_in_time,
        "shareCountPointInTimeVerified": profile.get("shareCountPointInTimeVerified"),
        "fxMode": profile.get("fxMode"),
    }


def convert_amount_to_usd(value: Optional[float], currency: Optional[str], fx_rate: Optional[float], fx_mode: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    currency = normalize_currency(currency) or "USD"
    value = float(value) * CURRENCY_UNIT_SCALES.get(currency, 1.0)
    if currency == "USD":
        return value
    if not fx_rate:
        return None
    if fx_mode == "multiply":
        return value * float(fx_rate)
    if fx_mode == "divide":
        return value / float(fx_rate)
    return None


def _date_value(value) -> Optional[dt.date]:
    try:
        if hasattr(value, "date"):
            return value.date()
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def usd_adjusted_series(series, currency: str, fx_series=None, fx_mode: Optional[str] = None):
    """Convert an adjusted-close history to USD with backward-looking FX only."""
    s = series.dropna().sort_index()
    if currency == "USD":
        return s, None, None
    if fx_series is None:
        return None, None, None
    fx = fx_series.dropna().sort_index()
    if len(fx) == 0:
        return None, None, None
    try:
        aligned_fx = fx.reindex(s.index, method="ffill")
        if fx_mode == "multiply":
            converted = s * aligned_fx
        elif fx_mode == "divide":
            converted = s / aligned_fx
        else:
            return None, None, None
        converted = converted.replace([math.inf, -math.inf], float("nan")).dropna()
        if len(converted) == 0:
            return None, None, None
        last_rate = aligned_fx.loc[converted.index[-1]]
        source_dates = [index for index in fx.index if index <= converted.index[-1]]
        fx_date = _date_value(source_dates[-1]) if source_dates else None
        return converted, float(last_rate), fx_date
    except Exception:
        return None, None, None


def market_metrics(
    series,
    benchmark=None,
    profile: Optional[dict] = None,
    fx_series=None,
    reference_date: Optional[str] = None,
    reference_index=None,
) -> Dict[str, Optional[float]]:
    """Return USD-based technical/risk metrics plus local-price display fields."""
    profile_fields = market_profile_fields(profile)
    s = series.dropna()
    if len(s) < 30:
        return {"available": False, **profile_fields}
    last_local = float(s.iloc[-1])
    currency = normalize_currency(profile_fields.get("priceCurrency")) or "USD"
    fx_mode = profile_fields.get("fxMode") or (FX_SPECS.get(currency) or (None, None))[1]
    scaled_series = s * CURRENCY_UNIT_SCALES.get(currency, 1.0)
    analysis, price_fx_rate, price_fx_date = usd_adjusted_series(scaled_series, currency, fx_series, fx_mode)
    analysis_available = analysis is not None and len(analysis) >= 30
    local_date = _date_value(s.index[-1])
    ref_date = _date_value(reference_date)
    lag_days = (ref_date - local_date).days if ref_date and local_date else None
    fx_lag_days = (local_date - price_fx_date).days if local_date and price_fx_date else None
    stale_bars = None
    if reference_index is not None and local_date:
        reference_dates = {_date_value(index) for index in reference_index}
        stale_bars = sum(1 for date in reference_dates if date and date > local_date and (not ref_date or date <= ref_date))
    base = {
        "available": True,
        "analysisAvailable": analysis_available,
        "date": local_date.isoformat() if local_date else str(s.index[-1])[:10],
        "referenceDate": ref_date.isoformat() if ref_date else None,
        "priceDateLagDays": lag_days,
        "staleTradingBars": stale_bars,
        "futurePriceDate": bool(lag_days is not None and lag_days < 0),
        "historyBars": len(analysis) if analysis is not None else len(s),
        "rangeLookbackBars": min(len(analysis), 252) if analysis is not None else None,
        "medianLookbackBars": min(len(analysis), 252) if analysis is not None else None,
        "price": rn(last_local, 2),
        "priceLocal": rn(last_local, 2),
        "currency": currency,
        "priceUsd": rn(float(analysis.iloc[-1]), 2) if analysis_available else None,
        "displayPrice": fmt_local_money(last_local, currency),
        "analysisCurrency": "USD" if analysis_available else None,
        "returnBasis": ("USD adjusted close" if currency == "USD" else "USD adjusted close including FX") if analysis_available else None,
        "fxAdjustedReturns": bool(analysis_available and currency != "USD"),
        "priceFxRate": rn(price_fx_rate, 6),
        "priceFxSource": (FX_SPECS.get(currency) or (None, None))[0],
        "priceFxAsOf": price_fx_date.isoformat() if price_fx_date else None,
        "priceFxLagDays": fx_lag_days,
        "ret1d": None,
        "ret5d": None,
        "ret1m": None,
        "ret3m": None,
        "ret6m": None,
        "ret1y": None,
        "sma50": None,
        "sma200": None,
        "vs50": None,
        "vs200": None,
        "high52": None,
        "low52": None,
        "median252": None,
        "priceTo252dMedian": None,
        "below52High": None,
        "drawdownMax": None,
        "vol1y": None,
        "betaToSox": None,
        "rsi14": None,
        **profile_fields,
    }
    if not analysis_available:
        return base
    a = analysis
    last = float(a.iloc[-1])
    sma50 = float(a.rolling(50).mean().iloc[-1]) if len(a) >= 50 else None
    sma200 = float(a.rolling(200).mean().iloc[-1]) if len(a) >= 200 else None
    hi52 = float(a.tail(252).max()) if len(a) >= 20 else float(a.max())
    low52 = float(a.tail(252).min()) if len(a) >= 20 else float(a.min())
    median252 = float(a.tail(252).median()) if len(a) >= 30 else None
    drawdown = float((a / a.cummax() - 1.0).min() * 100.0)
    ret = a.pct_change(fill_method=None).dropna()
    vol = float(ret.tail(252).std() * math.sqrt(252) * 100.0) if len(ret) > 20 else None
    beta = None
    if benchmark is not None:
        b = benchmark.dropna()
        aligned = (
            a.pct_change(fill_method=None)
            .rename("asset")
            .to_frame()
            .join(b.pct_change(fill_method=None).rename("bench"), how="inner")
            .dropna()
            .tail(252)
        )
        if len(aligned) > 30:
            var = float(aligned["bench"].var())
            beta = float(aligned["asset"].cov(aligned["bench"]) / var) if var else None
    rsi14 = float(rsi(a).iloc[-1]) if len(a) >= 15 else None
    return {
        **base,
        "ret1d": rn(pct_change(a, 1), 2),
        "ret5d": rn(pct_change(a, 5), 2),
        "ret1m": rn(pct_change(a, 21), 2),
        "ret3m": rn(pct_change(a, 63), 2),
        "ret6m": rn(pct_change(a, 126), 2),
        "ret1y": rn(pct_change(a, 252), 2),
        "sma50": rn(sma50, 2),
        "sma200": rn(sma200, 2),
        "vs50": rn((last / sma50 - 1) * 100.0, 2) if sma50 else None,
        "vs200": rn((last / sma200 - 1) * 100.0, 2) if sma200 else None,
        "high52": rn(hi52, 2),
        "low52": rn(low52, 2),
        "median252": rn(median252, 2),
        "priceTo252dMedian": rn(last / median252, 2) if median252 else None,
        "below52High": rn((last / hi52 - 1) * 100.0, 2) if hi52 else None,
        "drawdownMax": rn(drawdown, 2),
        "vol1y": rn(vol, 2),
        "betaToSox": rn(beta, 2),
        "betaMethod": "OLS covariance of aligned USD daily adjusted-close returns over up to 252 observations",
        "betaDecisionGrade": False,
        "crossMarketCloseTimingCaveat": currency != "USD",
        "rsi14": rn(rsi14, 1),
    }


def overextension_penalty(metrics: Dict[str, Optional[float]]) -> int:
    vs50 = metrics.get("vs50") or 0.0
    vs200 = metrics.get("vs200") or 0.0
    ret3 = metrics.get("ret3m") or 0.0
    rsi14 = metrics.get("rsi14") or 50.0
    penalty = 0.0
    penalty += max(vs50 - 35, 0) * 0.45
    penalty += max(vs200 - 80, 0) * 0.18
    penalty += max(ret3 - 100, 0) * 0.12
    penalty += max(rsi14 - 80, 0) * 0.9
    return int(round(clamp(penalty, 0, 30)))


def relative_momentum_score(metrics: Dict[str, Optional[float]], peer_metrics: List[dict]) -> int:
    if not metrics.get("available") or metrics.get("analysisAvailable") is False:
        return 35
    ret3_rank = pct_rank(metrics.get("ret3m"), [m.get("ret3m") for m in peer_metrics])
    ret6_rank = pct_rank(metrics.get("ret6m"), [m.get("ret6m") for m in peer_metrics])
    ret1_rank = pct_rank(metrics.get("ret1y"), [m.get("ret1y") for m in peer_metrics])
    vs50 = metrics.get("vs50")
    vs200 = metrics.get("vs200")
    dd = abs(metrics.get("drawdownMax") or 35.0)
    if vs50 is None and vs200 is None:
        regime = 45
    elif vs200 is None:
        regime = 62 if float(vs50) >= 0 else 35
    elif vs50 is None:
        regime = 58 if float(vs200) >= 0 else 35
    elif vs50 >= 0 and vs200 >= 0:
        regime = 82
    elif vs50 >= 0:
        regime = 62
    elif vs200 >= 0:
        regime = 48
    else:
        regime = 30
    resilience = clamp(100 - min(dd, 70) * 1.1)
    raw = ret3_rank * 0.35 + ret6_rank * 0.25 + ret1_rank * 0.20 + regime * 0.10 + resilience * 0.10
    return int(round(clamp(raw - overextension_penalty(metrics))))


def market_overlay(metrics: Dict[str, Optional[float]], peer_metrics: Optional[List[dict]] = None) -> Tuple[int, int, int, int]:
    """Return trend_score, risk_score, tactical_score, risk_penalty."""
    if not metrics.get("available") or metrics.get("analysisAvailable") is False:
        return 35, 45, 40, 8
    vs50 = metrics.get("vs50") or 0.0
    vol = metrics.get("vol1y") or 45.0
    drawdown = abs(metrics.get("drawdownMax") or 25.0)
    below_hi = abs(metrics.get("below52High") or 0.0)

    trend_i = relative_momentum_score(metrics, peer_metrics or [metrics])

    risk = 96
    risk -= min(vol, 110) * 0.42
    risk -= min(drawdown, 70) * 0.28
    risk -= max(vs50 - 25, 0) * 0.7
    risk -= max(8 - below_hi, 0) * 0.35  # near high is fine, but not "cheap"

    risk_i = int(round(clamp(risk)))
    tactical = int(round(clamp(trend_i * 0.62 + risk_i * 0.38)))
    penalty = int(round(clamp((100 - risk_i) * 0.08 + overextension_penalty(metrics) * 0.45, 0, 18)))
    return trend_i, risk_i, tactical, penalty


def risk_level(score: int) -> str:
    if score >= 75:
        return "low"
    if score >= 55:
        return "medium"
    if score >= 35:
        return "medium-high"
    return "high"


def risk_breakdown(company: Company, metrics: Dict[str, Optional[float]], portfolio: dict) -> Dict[str, str]:
    technical = "high" if overextension_penalty(metrics) >= 18 else ("medium-high" if overextension_penalty(metrics) >= 10 else "medium")
    if not metrics.get("available"):
        technical = "unknown"
    cycle = "high" if any(x in company.node for x in ("HBM", "DRAM", "Memory")) or "memory" in " ".join(company.riskFlags).lower() else "medium"
    valuation = "high" if (metrics.get("scoringMarketCapUsd") or 0) > 1.5e12 else ("medium-high" if (metrics.get("vs50") or 0) > 25 else "medium")
    geo = "high" if company.region == "Taiwan" else ("medium-high" if company.region == "South Korea" else "medium")
    customer = "medium-high" if any("customer" in x.lower() or "concentration" in x.lower() for x in company.riskFlags) else "medium"
    weight = portfolio.get("weightPct") or 0.0
    portfolio_risk = "high" if weight >= MAX_SINGLE_POSITION_WEIGHT else ("medium-high" if weight >= 12 else ("medium" if weight >= 5 else "low"))
    return {
        "technicalOverextension": technical,
        "cycle": cycle,
        "valuation": valuation,
        "geopolitical": geo,
        "customerConcentration": customer,
        "portfolioConcentration": portfolio_risk,
    }


def data_quality(metrics: Dict[str, Optional[float]]) -> Tuple[int, List[dict]]:
    score = 100
    reasons = []
    if not metrics.get("available"):
        return 35, [{"rule": "missing_price_history", "detail": "Price history is unavailable or shorter than required."}]
    if metrics.get("futurePriceDate"):
        return 0, [{"rule": "future_price_date", "detail": "Price date is later than the portfolio decision date."}]
    stale_bars = metrics.get("staleTradingBars")
    if finite(stale_bars) and float(stale_bars) >= 3:
        score -= 55
        reasons.append({"rule": "stale_price_history", "detail": f"Price history trails the reference market by {int(float(stale_bars))} trading bars."})
    elif finite(stale_bars) and float(stale_bars) >= 2:
        score -= 20
        reasons.append({"rule": "lagged_price_history", "detail": f"Price history trails the reference market by {int(float(stale_bars))} trading bars."})
    if metrics.get("marketCapPointInTimeCompatible") is False:
        score -= 25
        alignment = metrics.get("marketCapAlignmentReason") or "point-in-time provenance is incomplete"
        reasons.append({
            "rule": "market_cap_future_as_of",
            "detail": f"Market cap is excluded from scoring because it is not point-in-time compatible ({alignment}).",
        })
    history_bars = metrics.get("historyBars")
    if finite(history_bars) and float(history_bars) < 50:
        score -= 20
        reasons.append({"rule": "limited_price_history", "detail": f"Only {int(float(history_bars))} adjusted-close bars are available; 50/200DMA entry checks are incomplete."})
    fx_lag_days = metrics.get("priceFxLagDays")
    if (metrics.get("currency") or "USD") != "USD" and finite(fx_lag_days) and float(fx_lag_days) > 4:
        score -= 55
        reasons.append({"rule": "stale_fx_history", "detail": f"FX conversion trails the local price by {int(float(fx_lag_days))} calendar days."})
    elif (metrics.get("currency") or "USD") != "USD" and finite(fx_lag_days) and float(fx_lag_days) > 2:
        score -= 20
        reasons.append({"rule": "lagged_fx_history", "detail": f"FX conversion trails the local price by {int(float(fx_lag_days))} calendar days."})
    if metrics.get("marketCapUsd") is None:
        score -= 20
        reasons.append({"rule": "market_cap_missing", "detail": "Market cap could not be normalized to USD."})
    if (metrics.get("currency") or "USD") != "USD" and metrics.get("priceUsd") is None:
        score -= 25
        reasons.append({"rule": "fx_conversion_missing", "detail": f"{metrics.get('currency')} price could not be converted to USD."})
    if abs(metrics.get("ret1d") or 0.0) > 25:
        score -= 25
        reasons.append({"rule": "daily_return_outlier", "detail": f"1D return {metrics.get('ret1d')}% may need split/news review."})
    if abs(metrics.get("ret5d") or 0.0) > 60:
        score -= 15
        reasons.append({"rule": "five_day_return_outlier", "detail": f"5D return {metrics.get('ret5d')}% exceeds normal audit band."})
    if abs(metrics.get("ret3m") or 0.0) > 250:
        score -= 35
        reasons.append({"rule": "three_month_return_outlier", "detail": f"3M return {metrics.get('ret3m')}% requires data or split-adjustment review."})
    price_ratio = metrics.get("priceTo252dMedian")
    if finite(price_ratio) and (price_ratio > 3.0 or price_ratio < 0.35):
        score -= 18
        reasons.append({"rule": "price_band_outlier", "detail": f"Price is {float(price_ratio):.2f}x the trailing 252D median; verify split/corporate action."})
    return int(round(clamp(score))), reasons


def portfolio_penalty(portfolio: dict) -> Tuple[int, List[dict]]:
    weight = portfolio.get("weightPct") or 0.0
    if weight >= MAX_SINGLE_POSITION_WEIGHT:
        return 12, [{"rule": "portfolio_concentration", "detail": f"Existing weight {weight:.2f}% is above the {MAX_SINGLE_POSITION_WEIGHT:.0f}% single-name cap."}]
    if weight >= 12:
        return 6, [{"rule": "portfolio_concentration", "detail": f"Existing weight {weight:.2f}% is already large; new capital should be constrained."}]
    if weight >= 8:
        return 3, [{"rule": "portfolio_concentration", "detail": f"Existing weight {weight:.2f}% is meaningful; size any add cautiously."}]
    return 0, []


def gate_family(gate: str) -> str:
    if gate in ("ALLOW_PLAN", "ALLOW_DD"):
        return "ALLOW"
    if gate in ("WATCH", "WATCH_RESET"):
        return "WATCH"
    if gate == "DATA_REVIEW":
        return "DATA_REVIEW"
    return "BLOCK"


def research_gate(
    final_score: int,
    standalone_score: int,
    base_score: int,
    trend_score: int,
    risk_score: int,
    metrics: Dict[str, Optional[float]],
    factors: Dict[str, int],
    dq_score: int,
    dq_reasons: List[dict],
    portfolio_penalty: int,
    penalty_reasons: List[dict],
) -> Tuple[str, str, str, List[dict]]:
    """Return explainable gate. Gate is due-diligence permission, not a trade."""
    reasons = []
    vs200 = metrics.get("vs200")
    vs50 = metrics.get("vs50") or 0.0
    rsi14 = metrics.get("rsi14") or 50.0
    overext = overextension_penalty(metrics)
    if dq_score < 50:
        return "DATA_REVIEW", "caution", "Data quality issue: review before ranking.", dq_reasons[:3]
    if dq_reasons:
        reasons.extend(dq_reasons[:2])
    if portfolio_penalty >= 12:
        reasons.extend(penalty_reasons)
        return "PORTFOLIO_BLOCK", "avoid", "Company may remain high quality, but portfolio concentration blocks new adds.", reasons[:3]
    if risk_score < RISK_FLOOR:
        reasons.append({"rule": "risk_floor", "detail": f"Risk score {risk_score} is below required floor {RISK_FLOOR}."})
        if base_score >= 80:
            return "WATCH_RESET", "caution", "Strategic quality is high, but risk must reset before adding.", reasons[:3]
        return "BLOCK", "avoid", "Risk score is below floor.", reasons[:3]
    if vs200 is not None and vs200 < -3:
        reasons.append({"rule": "trend_break", "detail": f"Price is {vs200:.2f}% below 200DMA."})
        if base_score >= 76:
            return "WATCH_RESET", "caution", "High-quality name, but trend repair is required.", reasons[:3]
        return "BLOCK", "avoid", "Below 200DMA: keep on research list, wait for trend repair.", reasons[:3]
    if overext >= 18:
        reasons.append({"rule": "overextension", "detail": f"Overextension penalty {overext} from 3M return / moving-average distance / RSI."})
    if penalty_reasons:
        reasons.extend(penalty_reasons)
    if overext >= 18 and base_score >= 76:
        return "WATCH_RESET", "caution", "Structure is attractive, but the move is too stretched; wait for reset.", reasons[:3]
    if final_score >= 82 and trend_score >= 60 and factors.get("valuationGrowth", 0) >= 45 and finite(metrics.get("vs50")) and finite(metrics.get("vs200")) and vs50 < 28 and rsi14 < 76 and not penalty_reasons:
        reasons.append({"rule": "plan_pass", "detail": f"Adjusted score {final_score}, strategic score {base_score}, trend {trend_score}, risk {risk_score}."})
        return "ALLOW_PLAN", "good", "Strong structure and current setup can enter a staged plan review.", reasons[:3]
    if base_score >= 82 and final_score >= 68:
        if not reasons:
            reasons.append({"rule": "dd_candidate", "detail": f"Strategic score {base_score}; adjusted score {final_score} needs trigger/risk review."})
        return "ALLOW_DD", "good", "Strong strategic candidate for deeper due diligence, not an automatic add.", reasons[:3]
    if final_score >= 70:
        if not reasons:
            reasons.append({"rule": "watch_threshold", "detail": f"Adjusted score {final_score}; wait for cleaner trigger or risk reset."})
        return "WATCH", "caution", "Good structure, but entry should wait for a cleaner pullback or trigger.", reasons[:3]
    if base_score >= 80:
        if not reasons:
            reasons.append({"rule": "reset_watch", "detail": f"Strategic score {base_score}, but adjusted score {final_score} and risk/trend setup are not ready."})
        return "WATCH_RESET", "caution", "Strategic core or high-torque name, but wait for risk reset.", reasons[:3]
    reasons.append({"rule": "score_below_threshold", "detail": f"Adjusted score {final_score} is below WATCH threshold 70."})
    return "BLOCK", "avoid", "Score is not strong enough for new capital under this framework.", reasons[:3]


def read_dashboard_payload(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return _read_dashboard_payload(path)
    except Exception:
        return None


def portfolio_exposure(payload: Optional[dict]) -> Dict[str, dict]:
    if not payload:
        return {}
    total = ((payload.get("summary") or {}).get("marketValue") or 0.0) or 0.0
    out = {}
    for stock in payload.get("stocks") or []:
        sym = stock.get("sym")
        if not sym:
            continue
        value = float(stock.get("value") or 0.0)
        out[sym.upper()] = {
            "held": bool(stock.get("held")),
            "value": rn(value, 2),
            "weightPct": rn(value / total * 100.0, 2) if total else None,
            "unrealPct": rn(stock.get("unrealPct"), 2),
        }
    return out


def load_market_data(tickers: Iterable[str], period: str, no_fetch: bool = False):
    if no_fetch:
        return None
    try:
        import warnings

        warnings.filterwarnings("ignore")
        import yfinance as yf
    except Exception as exc:
        print(f"!! yfinance unavailable: {exc}", file=sys.stderr)
        return None
    tickers = sorted(set(tickers))
    print(f"· fetching {len(tickers)} AI/SemiQuant symbols from Yahoo Finance ({period}) ...", file=sys.stderr)
    try:
        raw = yf.download(
            tickers,
            period=period,
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        print(f"!! yfinance download failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    if raw is None or raw.empty:
        return None
    try:
        if hasattr(raw, "columns") and getattr(raw.columns, "nlevels", 1) > 1:
            if "Close" in raw.columns.get_level_values(0):
                return raw["Close"]
            return raw.xs("Close", axis=1, level=1)
        if "Close" in raw:
            return raw[["Close"]].rename(columns={"Close": tickers[0]})
    except Exception:
        return None
    return None


def dashboard_market_as_of(payload: Optional[dict]) -> Optional[str]:
    summary = (payload or {}).get("summary") or {}
    value = summary.get("priceAsOf") or ((summary.get("dateRange") or [None, None])[-1])
    try:
        return dt.date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def truncate_market_data(raw, as_of: Optional[str]):
    """Exclude closes later than the dashboard's US-market decision date.

    Global Yahoo downloads can include a forming next-calendar-day Asian bar
    after the US close.  Mixing that bar into a July-09 portfolio snapshot both
    creates cross-market look-ahead and makes artifact freshness incomparable.
    """
    if raw is None or getattr(raw, "empty", True) or not as_of:
        return raw
    try:
        cutoff = dt.date.fromisoformat(as_of)
        mask = [getattr(index, "date", lambda: dt.date.fromisoformat(str(index)[:10]))() <= cutoff
                for index in raw.index]
        return raw.loc[mask]
    except (TypeError, ValueError, AttributeError):
        return raw


def _last_close(raw, symbol: str) -> Optional[float]:
    try:
        if raw is None or raw.empty:
            return None
        if hasattr(raw, "columns") and getattr(raw.columns, "nlevels", 1) > 1:
            close = raw["Close"][symbol].dropna()
        elif "Close" in raw:
            close = raw["Close"].dropna()
        else:
            close = raw.dropna()
        return float(close.iloc[-1]) if len(close) else None
    except Exception:
        return None


def _last_close_record(raw, symbol: str) -> Tuple[Optional[float], Optional[str]]:
    try:
        if raw is None or raw.empty:
            return None, None
        if hasattr(raw, "columns") and getattr(raw.columns, "nlevels", 1) > 1:
            close = raw["Close"][symbol].dropna()
        elif "Close" in raw:
            close = raw["Close"].dropna()
        elif symbol in getattr(raw, "columns", []):
            close = raw[symbol].dropna()
        else:
            close = raw.dropna()
        if len(close) == 0:
            return None, None
        as_of = _date_value(close.index[-1])
        return float(close.iloc[-1]), as_of.isoformat() if as_of else None
    except Exception:
        return None, None


def load_fx_rates(currencies: Iterable[str], yf) -> Dict[str, dict]:
    out = {"USD": {"rate": 1.0, "source": "identity"}}
    pairs = {FX_SPECS[c][0] for c in currencies if c in FX_SPECS and FX_SPECS[c][0]}
    if not pairs:
        return out
    try:
        raw = yf.download(sorted(pairs), period="10d", interval="1d", auto_adjust=False, progress=False)
    except Exception:
        raw = None
    for currency in currencies:
        spec = FX_SPECS.get(currency)
        if not spec:
            continue
        pair, mode = spec
        if mode == "identity":
            out[currency] = {"rate": 1.0, "source": "identity"}
            continue
        px, fx_as_of = _last_close_record(raw, pair) if pair else (None, None)
        if not px:
            continue
        out[currency] = {
            "rate": px,
            "source": pair,
            "mode": mode,
            "asOf": fx_as_of,
        }
    return out


def convert_market_cap_to_usd(market_cap: Optional[float], currency: Optional[str], fx: Dict[str, dict]) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    if not market_cap:
        return None, None, None
    currency = normalize_currency(currency) or "USD"
    if currency == "GBX":
        currency = "GBP"  # Yahoo marketCap is already pounds even when the quote is pence.
    scaled_market_cap = float(market_cap)
    rec = fx.get(currency)
    if not rec:
        return None, None, None
    rate = rec.get("rate")
    mode = rec.get("mode", "identity")
    if not rate:
        return None, None, rec.get("source")
    if mode == "multiply":
        return scaled_market_cap * rate, rate, rec.get("source")
    if mode == "divide":
        return scaled_market_cap / rate, rate, rec.get("source")
    return scaled_market_cap, rate, rec.get("source")


def load_market_profiles(
    tickers: Iterable[str],
    no_fetch: bool = False,
    market_data_as_of: Optional[str] = None,
) -> Dict[str, dict]:
    if no_fetch:
        return {}
    try:
        import warnings

        warnings.filterwarnings("ignore")
        import yfinance as yf
    except Exception as exc:
        print(f"!! yfinance unavailable for market caps: {exc}", file=sys.stderr)
        return {}
    raw_profiles = {}
    currencies = set()
    fetched_at = dt.datetime.now().astimezone()
    fetched_date = fetched_at.date()
    try:
        decision_date = dt.date.fromisoformat(str(market_data_as_of)[:10]) if market_data_as_of else None
    except ValueError:
        decision_date = None
    for ticker in sorted(set(tickers)):
        try:
            yft = yf.Ticker(ticker)
            fast = dict(yft.fast_info or {})
            market_cap = fast.get("market_cap") or fast.get("marketCap")
            market_cap_source = "yahoo_fast_info" if market_cap else None
            currency = normalize_currency(fast.get("currency") or fast.get("last_price_currency"))
            if (not market_cap or not currency) and hasattr(yft, "get_info"):
                info = yft.get_info() or {}
                if not market_cap and info.get("marketCap"):
                    market_cap = info.get("marketCap")
                    market_cap_source = "yahoo_quote_summary"
                currency = currency or normalize_currency(info.get("currency"))
            currency = currency or infer_ticker_currency(ticker)
            normalized_market_cap, market_cap_unit_scale = normalize_market_cap_value(
                market_cap, currency, market_cap_source
            )
            raw_profiles[ticker] = {
                "ticker": ticker,
                "marketCap": normalized_market_cap,
                "marketCapRaw": float(market_cap) if market_cap and finite(market_cap) else None,
                "marketCapUnitScale": market_cap_unit_scale,
                "marketCapSource": market_cap_source,
                "currency": currency,
                "marketCapCurrency": "GBP" if currency == "GBX" else currency,
            }
            currencies.update((currency, "GBP" if currency == "GBX" else currency))
        except Exception:
            currency = infer_ticker_currency(ticker)
            raw_profiles[ticker] = {
                "ticker": ticker,
                "marketCap": None,
                "marketCapRaw": None,
                "marketCapUnitScale": None,
                "marketCapSource": "missing",
                "currency": currency,
                "marketCapCurrency": "GBP" if currency == "GBX" else currency,
            }
            currencies.update((currency, "GBP" if currency == "GBX" else currency))
    fx = load_fx_rates(currencies, yf)
    out = {}
    for ticker, rec in raw_profiles.items():
        cap_currency = rec.get("marketCapCurrency") or rec.get("currency")
        usd, rate, source = convert_market_cap_to_usd(rec.get("marketCap"), cap_currency, fx)
        out[ticker] = {
            **rec,
            "marketCapUsd": usd,
            "fxRate": rate,
            "fxSource": source,
            "fxMode": (fx.get(rec.get("currency")) or {}).get("mode", "identity"),
            "fxAsOf": (fx.get(cap_currency) or {}).get("asOf"),
            "marketCapAsOf": fetched_date.isoformat(),
            "marketCapAsOfSource": "fetch_date_fallback",
            "marketCapFetchedAt": fetched_at.isoformat(timespec="seconds"),
            "marketCapPointInTimeCompatible": decision_date is None or fetched_date <= decision_date,
            "shareCountPointInTimeVerified": False,
        }
    print(f"· market caps: {len(out)}/{len(set(tickers))} profiles", file=sys.stderr)
    return out


def required_fx_tickers(profiles: Dict[str, dict], tickers: Iterable[str]) -> List[str]:
    pairs = set()
    for ticker in tickers:
        currency = ((profiles.get(ticker) or {}).get("currency") or infer_ticker_currency(ticker)).upper()
        pair = (FX_SPECS.get(currency) or (None, None))[0]
        if pair:
            pairs.add(pair)
    return sorted(pairs)


def align_market_profile_as_of(profiles: Dict[str, dict], prices, decision_as_of: Optional[str]) -> Dict[str, dict]:
    """Audit current market-cap provenance against a decision date.

    A downloaded close can date the quote observation, but it cannot date the
    provider's current share count.  In particular, never relabel a market cap
    fetched today as a historical market cap merely because historical prices
    are available.  A backdated profile is usable only when its own cap date is
    not in the future and a post-decision fetch explicitly verifies historical
    shares.
    """
    decision_date = _date_value(decision_as_of)
    for ticker, profile in profiles.items():
        series = _series_column(prices, ticker)
        quote_date = None
        if series is not None:
            clean = series.dropna()
            quote_date = _date_value(clean.index[-1]) if len(clean) else None
        cap_date = _date_value(profile.get("marketCapAsOf"))
        fetched_date = _date_value(profile.get("marketCapFetchedAt")) or cap_date
        fx_date = _date_value(profile.get("fxAsOf"))
        currency = normalize_currency(profile.get("currency")) or "USD"
        if quote_date:
            profile["marketCapQuoteAsOf"] = quote_date.isoformat()
        profile["marketCapDecisionAsOf"] = decision_date.isoformat() if decision_date else None

        if decision_date is None:
            compatible = cap_date is not None and quote_date is not None
            if currency != "USD":
                compatible = compatible and fx_date is not None
            reason = "current_profile_dates_observed" if compatible else "profile_provenance_incomplete"
        else:
            cap_compatible = cap_date is not None and cap_date <= decision_date
            quote_compatible = quote_date is not None and quote_date <= decision_date
            fx_compatible = currency == "USD" or (fx_date is not None and fx_date <= decision_date)
            post_decision_fetch = fetched_date is not None and fetched_date > decision_date
            historical_shares_verified = profile.get("shareCountPointInTimeVerified") is True
            shares_compatible = not post_decision_fetch or historical_shares_verified
            compatible = bool(cap_compatible and quote_compatible and fx_compatible and shares_compatible)

            reasons = []
            if not cap_compatible:
                reasons.append("market_cap_as_of_after_decision_or_missing")
            if not quote_compatible:
                reasons.append("quote_as_of_after_decision_or_missing")
            if not fx_compatible:
                reasons.append("fx_as_of_after_decision_or_missing")
            if not shares_compatible:
                reasons.append("historical_share_count_unverified")
            reason = "point_in_time_compatible" if compatible else ";".join(reasons)

        profile["marketCapAlignmentReason"] = reason
        profile["marketCapPointInTimeCompatible"] = bool(compatible)
    return profiles


def _series_column(prices, symbol: Optional[str]):
    try:
        if symbol and prices is not None and symbol in getattr(prices, "columns", []):
            return prices[symbol]
    except Exception:
        pass
    return None


def _reference_date_from_prices(prices, benchmark_symbol: str) -> Optional[str]:
    series = _series_column(prices, benchmark_symbol)
    if series is None:
        try:
            if prices is None or prices.empty:
                return None
            valid_dates = [date for date in (_date_value(index) for index in prices.index) if date]
            return max(valid_dates).isoformat() if valid_dates else None
        except Exception:
            return None
    clean = series.dropna()
    if len(clean) == 0:
        return None
    value = _date_value(clean.index[-1])
    return value.isoformat() if value else None


def annotate_percentiles(rows: List[dict]) -> None:
    ranked_rows = [
        r for r in rows
        if r.get("gate") != "DATA_REVIEW" and r.get("dataQualitySeverity") != "hard_review"
        and (r.get("market") or {}).get("analysisAvailable") is not False
    ]
    final_scores = [r.get("finalScore") for r in ranked_rows]
    strategic_scores = [r.get("strategicRankScore") for r in rows]
    tactical_scores = [r.get("tacticalScore") for r in ranked_rows]
    by_peer: Dict[str, List[dict]] = {}
    for row in rows:
        by_peer.setdefault(row.get("peerGroup") or "Other", []).append(row)
    for row in rows:
        peers = by_peer.get(row.get("peerGroup") or "Other", [])
        ranked_peers = [p for p in peers if p in ranked_rows]
        peer_count = len(ranked_peers)
        is_ranked = row in ranked_rows
        peer_pct = percentile_int(row.get("finalScore"), [p.get("finalScore") for p in ranked_peers]) if is_ranked else None
        peer_strategic_pct = percentile_int(row.get("strategicRankScore"), [p.get("strategicRankScore") for p in peers])
        row["universePercentile"] = percentile_int(row.get("finalScore"), final_scores) if is_ranked else None
        row["strategicPercentile"] = percentile_int(row.get("strategicRankScore"), strategic_scores)
        row["tacticalPercentile"] = percentile_int(row.get("tacticalScore"), tactical_scores) if is_ranked else None
        row["peerGroupSize"] = peer_count
        row["peerPercentile"] = peer_pct
        row["peerStrategicPercentile"] = peer_strategic_pct
        row["peerPercentileDisplay"] = "DATA_REVIEW · not ranked" if not is_ranked else peer_percentile_display(peer_pct, peer_count)
        row["peerStrategicPercentileDisplay"] = peer_percentile_display(peer_strategic_pct, len(peers))
        if row.get("gate") == "ALLOW_DD":
            row["gateReasons"] = allow_dd_gate_reasons(row)


def allow_dd_gate_reasons(row: dict) -> List[dict]:
    strategic = row.get("strategicRankScore")
    final = row.get("finalScore")
    peer_count = row.get("peerGroupSize") or 0
    peer_pct = row.get("peerPercentile")
    if peer_count < 3:
        peer_clause = f"; peer sample n={peer_count} is too small for percentile confirmation"
        row["allowDdBasis"] = "strategic_small_peer_sample"
    elif peer_pct is not None and peer_pct >= 70:
        peer_clause = f" and peer percentile P{peer_pct} >= 70"
        row["allowDdBasis"] = "strategic_peer_confirmed"
    else:
        peer_label = f"P{peer_pct}" if peer_pct is not None else "unavailable"
        peer_clause = f"; peer percentile {peer_label} is not a strong peer confirmation"
        row["allowDdBasis"] = "strategic_only"
    if final is not None and final < 70:
        score_clause = f"despite adjusted score {final} below WATCH threshold."
    elif final is not None and final < 82:
        score_clause = f"adjusted score {final} remains below ALLOW_PLAN threshold and still needs trigger/risk review."
    else:
        score_clause = "ALLOW_PLAN still needs trend, valuation, or risk confirmation."
    reason = {
        "rule": "dd_candidate",
        "detail": f"ALLOW_DD because strategic score {strategic} >= 82{peer_clause}; {score_clause}",
    }
    existing = [r for r in (row.get("gateReasons") or []) if r.get("rule") != "dd_candidate"]
    return [reason, *existing[:2]]


def build_scores(
    prices,
    exposures: Dict[str, dict],
    profiles: Optional[Dict[str, dict]] = None,
    reference_date: Optional[str] = None,
) -> Tuple[List[dict], Optional[str]]:
    profiles = profiles or {}
    bench = None
    if prices is not None and "SOXX" in getattr(prices, "columns", []):
        bench = prices["SOXX"]
    reference_date = reference_date or _reference_date_from_prices(prices, "SOXX")
    reference_index = getattr(bench, "index", None) if bench is not None else getattr(prices, "index", None)
    latest_date = None
    metrics_by_company = {}
    for company in UNIVERSE:
        profile = {
            "ticker": company.ticker,
            "currency": infer_ticker_currency(company.ticker),
            **(profiles.get(company.ticker) or {}),
        }
        metrics = {"available": False, **market_profile_fields(profile)}
        if prices is not None and company.ticker in getattr(prices, "columns", []):
            currency = (profile.get("currency") or infer_ticker_currency(company.ticker)).upper()
            fx_ticker = (FX_SPECS.get(currency) or (None, None))[0]
            metrics = market_metrics(
                prices[company.ticker],
                benchmark=bench,
                profile=profile,
                fx_series=_series_column(prices, fx_ticker),
                reference_date=reference_date,
                reference_index=reference_index,
            )
            latest_date = max(latest_date or metrics.get("date"), metrics.get("date") or latest_date)
        metrics_by_company[company.companyId] = metrics

    peer_metrics = list(metrics_by_company.values())
    rows = []
    for company in UNIVERSE:
        metrics = metrics_by_company[company.companyId]
        factors = dict(company.factors)
        factors["sizeGrowthTorque"] = size_growth_torque(company, metrics.get("scoringMarketCapUsd"))
        structural_base = structural_score(factors)
        torque_adjusted, torque_bonus, fragility_penalty = torque_overlay(company, factors, metrics)
        exp = {}
        for sym in (company.ticker, *company.aliases):
            if sym.upper() in exposures:
                exp = exposures[sym.upper()]
                break
        exp = exp or {"held": False, "value": 0.0, "weightPct": 0.0, "unrealPct": None}
        trend_score, risk_score, tactical_score, risk_penalty = market_overlay(metrics, peer_metrics)
        standalone = int(round(clamp(torque_adjusted * 0.76 + tactical_score * 0.24 - risk_penalty)))
        p_penalty, p_reasons = portfolio_penalty(exp)
        final = int(round(clamp(standalone - p_penalty)))
        dq_score, dq_reasons = data_quality(metrics)
        gate, tone, gate_note, gate_reasons = research_gate(
            final,
            standalone,
            structural_base,
            trend_score,
            risk_score,
            metrics,
            factors,
            dq_score,
            dq_reasons,
            p_penalty,
            p_reasons,
        )
        risks = risk_breakdown(company, metrics, exp)
        row = {
            **asdict(company),
            "factors": factors,
            "peerGroup": peer_group(company),
            "sizeScore": market_cap_size_score(metrics.get("scoringMarketCapUsd")),
            "structuralBaseScore": structural_base,
            "torqueAdjustedScore": torque_adjusted,
            "torqueBonus": torque_bonus,
            "fragilityPenalty": fragility_penalty,
            "strategicRankScore": structural_base,
            "structuralScore": structural_base,
            "trendScore": trend_score,
            "riskScore": risk_score,
            "tacticalScore": tactical_score,
            "riskPenalty": risk_penalty,
            "portfolioPenalty": p_penalty,
            "standaloneScore": standalone,
            "portfolioAdjustedScore": final,
            "finalScore": final,
            "gate": gate,
            "gateFamily": gate_family(gate),
            "tone": tone,
            "gateNote": gate_note,
            "gateReasons": gate_reasons,
            "dataQualityScore": dq_score,
            "dataQualitySeverity": "hard_review" if dq_score < 50 else ("soft_review" if dq_reasons else "clean"),
            "dataQualityReasons": dq_reasons,
            "riskBreakdown": risks,
            "market": metrics,
            "portfolio": exp,
        }
        rows.append(row)
    annotate_percentiles(rows)
    rows.sort(key=lambda x: (-x["finalScore"], -x["structuralScore"], x["ticker"]))
    return rows, latest_date


def summarize(rows: List[dict]) -> dict:
    top = rows[:5]
    strategic = sorted(rows, key=lambda r: (-r["strategicRankScore"], -r["finalScore"], r["ticker"]))[:6]
    tactical_raw = sorted(rows, key=lambda r: (-r["tacticalScore"], -r["riskScore"], r["ticker"]))[:6]
    tactical_investable = [
        r
        for r in sorted(rows, key=lambda r: (-r["tacticalScore"], -r["riskScore"], r["ticker"]))
        if r.get("gate") not in ("BLOCK", "DATA_REVIEW", "PORTFOLIO_BLOCK")
    ][:6]
    reset_watch = [
        r
        for r in sorted(rows, key=lambda r: (-r["strategicRankScore"], -r["finalScore"], r["ticker"]))
        if r.get("gate") in ("WATCH_RESET", "PORTFOLIO_BLOCK")
    ][:6]
    by_node = {}
    for row in rows:
        bucket = by_node.setdefault(row["node"], {"node": row["node"], "count": 0, "avgScore": 0.0, "leaders": []})
        bucket["count"] += 1
        bucket["avgScore"] += row["finalScore"]
        if len(bucket["leaders"]) < 3:
            bucket["leaders"].append(row["ticker"])
    for bucket in by_node.values():
        bucket["avgScore"] = rn(bucket["avgScore"] / bucket["count"], 1)
    return {
        "leaders": [{"ticker": r["ticker"], "name": r["name"], "score": r["finalScore"], "percentile": r.get("universePercentile"), "gate": r["gate"]} for r in top],
        "strategicLeaders": [{"ticker": r["ticker"], "name": r["name"], "score": r["strategicRankScore"], "percentile": r.get("strategicPercentile"), "gate": r["gate"]} for r in strategic],
        "tacticalLeadersRaw": [{"ticker": r["ticker"], "name": r["name"], "score": r["tacticalScore"], "percentile": r.get("tacticalPercentile"), "gate": r["gate"]} for r in tactical_raw],
        "tacticalLeadersInvestable": [{"ticker": r["ticker"], "name": r["name"], "score": r["tacticalScore"], "percentile": r.get("tacticalPercentile"), "gate": r["gate"]} for r in tactical_investable],
        "tacticalLeaders": [{"ticker": r["ticker"], "name": r["name"], "score": r["tacticalScore"], "percentile": r.get("tacticalPercentile"), "gate": r["gate"]} for r in tactical_investable],
        "resetWatchlist": [{"ticker": r["ticker"], "name": r["name"], "score": r["strategicRankScore"], "gate": r["gate"]} for r in reset_watch],
        "byNode": sorted(by_node.values(), key=lambda x: -x["avgScore"]),
        "gateCounts": {g: sum(1 for r in rows if r["gate"] == g) for g in GATE_ORDER},
        "gateFamilyCounts": {g: sum(1 for r in rows if r.get("gateFamily") == g) for g in ("ALLOW", "WATCH", "BLOCK", "DATA_REVIEW")},
    }


def model_card(rows: List[dict], latest_date: Optional[str]) -> dict:
    missing_price = sum(1 for r in rows if not (r.get("market") or {}).get("available"))
    missing_market_cap = sum(1 for r in rows if (r.get("market") or {}).get("marketCapUsd") is None)
    data_review = sum(1 for r in rows if r.get("gate") == "DATA_REVIEW")
    soft_data_review = sum(1 for r in rows if r.get("dataQualitySeverity") == "soft_review")
    saturated_momentum = sum(1 for r in rows if r.get("trendScore") == 100)
    anomalies = []
    for r in rows:
        for reason in r.get("dataQualityReasons") or []:
            anomalies.append({"ticker": r["ticker"], "name": r.get("name"), "severity": r.get("dataQualitySeverity"), **reason})
    soft_flags = [a for a in anomalies if a.get("severity") == "soft_review"]
    hard_flags = [a for a in anomalies if a.get("severity") == "hard_review"]
    return {
        "modelVersion": MODEL_VERSION,
        "decisionGrade": False,
        "structuralPriorStatus": "curated priors without row-level source lineage or point-in-time fundamental history",
        "structuralPriorAsOf": None,
        "structuralPriorRequiresManualReview": True,
        "dataDate": latest_date,
        "universeSize": len(rows),
        "missingPriceCount": missing_price,
        "missingMarketCapCount": missing_market_cap,
        "dataReviewCount": data_review,
        "softDataReviewCount": soft_data_review,
        "momentumScoreAt100Count": saturated_momentum,
        "usdFxAdjustedReturnCount": sum(1 for r in rows if (r.get("market") or {}).get("fxAdjustedReturns")),
        "missingUsdReturnConversionCount": sum(
            1 for r in rows
            if (r.get("market") or {}).get("available")
            and (r.get("market") or {}).get("analysisAvailable") is False
        ),
        "stalePriceCount": sum(1 for r in rows if ((r.get("market") or {}).get("staleTradingBars") or 0) >= 2),
        "pointInTimeIncompatibleMarketCapCount": sum(
            1 for r in rows if (r.get("market") or {}).get("marketCapPointInTimeCompatible") is False
        ),
        "pointInTimeVerifiedShareCountCount": sum(
            1 for r in rows if (r.get("market") or {}).get("shareCountPointInTimeVerified") is True
        ),
        "factorWeights": FACTOR_WEIGHTS,
        "torqueOverlay": TORQUE_OVERLAY,
        "thresholds": {
            "riskFloor": RISK_FLOOR,
            "maxSinglePositionWeight": MAX_SINGLE_POSITION_WEIGHT,
            "watchScore": 70,
            "allowPlanScore": 82,
            "allowDdStrategicScore": 82,
        },
        "largestDataAnomalies": anomalies[:8],
        "softDataFlags": soft_flags[:8],
        "hardDataFlags": hard_flags[:8],
    }


def render_report(doc: dict) -> str:
    rows = doc["scores"]
    top_rows = rows[:12]
    lines = [
        "# AI-SemiQuant Reference Report",
        "",
        f"Generated: {doc['generatedAt']}",
        f"Market data as of: {doc.get('marketDataAsOf') or 'unavailable'}",
        "",
        "This report is a decision-support framework, not investment advice. `ALLOW_PLAN` means the setup can enter staged plan review; `ALLOW_DD` means deeper diligence, not an automatic buy order.",
        "",
        "## Framework",
        "",
        "Structural Alpha Score = 30% pricing power + 24% profit elasticity + 24% capex conversion + 22% valuation/growth.",
        "Size/growth torque is a separate overlay: it can add payoff convexity, but fragility penalties prevent small market cap from masquerading as moat quality.",
        "Final score = torque-adjusted structural score with a smaller market overlay for trend/risk and risk penalties for volatility, overextension, and portfolio concentration.",
        "",
        "| Factor | Weight | Meaning |",
        "| --- | ---: | --- |",
    ]
    for key, weight in FACTOR_WEIGHTS.items():
        lines.append(f"| {FACTOR_LABELS[key]} | {weight:.0%} | {factor_meaning(key)} |")

    lines.extend(
        [
            "",
            "## Capital Waterfall",
            "",
            "| Stage | Main Beneficiaries | Leading KPIs |",
            "| --- | --- | --- |",
        ]
    )
    for stage in doc["capitalWaterfall"]:
        lines.append(
            f"| {stage['stage']} | {', '.join(stage['beneficiaries'])} | {', '.join(stage['leadingKpis'])} |"
        )

    lines.extend(
        [
            "",
            "## Top Ranked Names",
            "",
            "| Rank | Ticker | Company | Node | Gate | Final | %ile | Peer rank | Standalone | Structural | Torque Adj. | Size/Growth | Mkt Cap | Price | Tactical | Risk | Main Gate Reason |",
            "| ---: | --- | --- | --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- |",
        ]
    )
    for i, row in enumerate(top_rows, 1):
        m = row.get("market") or {}
        reason = (row.get("gateReasons") or [{}])[0]
        lines.append(
            f"| {i} | {row['ticker']} | {row['name']} | {row['node']} | {row['gate']} | "
            f"{row['finalScore']} | {row.get('universePercentile', '-')} | {row.get('peerPercentileDisplay', '-')} | {row['standaloneScore']} | {row['structuralScore']} | {row.get('torqueAdjustedScore', '-')} | {row['factors'].get('sizeGrowthTorque', '-')} | "
            f"{fmt_cap(m.get('marketCapUsd'))} | {m.get('displayPrice') or '-'} | {row['tacticalScore']} | {row['riskScore']} | "
            f"{reason.get('detail', '-')} |"
        )

    lines.extend(
        [
            "",
            "## Strategic vs Tactical Rank",
            "",
            "Strategic rank is long-term bottleneck quality. Raw tactical rank can include blocked names; investable tactical rank excludes hard BLOCK, PORTFOLIO_BLOCK, and DATA_REVIEW.",
            "",
            "| Strategic | Score | Gate | Raw Tactical | Score | Gate | Investable Tactical | Score | Gate |",
            "| --- | ---: | --- | --- | ---: | --- | --- | ---: | --- |",
        ]
    )
    strategic = doc["summary"].get("strategicLeaders") or []
    tactical_raw = doc["summary"].get("tacticalLeadersRaw") or []
    tactical_inv = doc["summary"].get("tacticalLeadersInvestable") or []
    for i in range(max(len(strategic), len(tactical_raw), len(tactical_inv))):
        s = strategic[i] if i < len(strategic) else {}
        t = tactical_raw[i] if i < len(tactical_raw) else {}
        inv = tactical_inv[i] if i < len(tactical_inv) else {}
        lines.append(
            f"| {s.get('ticker', '-')} | {s.get('score', '-')} | {s.get('gate', '-')} | "
            f"{t.get('ticker', '-')} | {t.get('score', '-')} | {t.get('gate', '-')} | "
            f"{inv.get('ticker', '-')} | {inv.get('score', '-')} | {inv.get('gate', '-')} |"
        )

    card = doc.get("modelCard") or {}
    lines.extend(
        [
            "",
            "## Model Card / Score Audit",
            "",
            f"- Model version: {card.get('modelVersion', '-')}",
            f"- Universe size: {card.get('universeSize', '-')}",
            f"- Missing price count: {card.get('missingPriceCount', '-')}",
            f"- Missing market-cap count: {card.get('missingMarketCapCount', '-')}",
            f"- DATA_REVIEW count: {card.get('dataReviewCount', '-')}",
            f"- Soft data-review flags: {card.get('softDataReviewCount', '-')}",
            f"- Momentum scores at 100: {card.get('momentumScoreAt100Count', '-')}",
            f"- Risk floor: {(card.get('thresholds') or {}).get('riskFloor', '-')}",
            f"- Max single-name portfolio weight: {(card.get('thresholds') or {}).get('maxSinglePositionWeight', '-')}%",
        ]
    )
    soft_flags = card.get("softDataFlags") or []
    hard_flags = card.get("hardDataFlags") or []
    if soft_flags or hard_flags:
        lines.append("")
        lines.append("Named data-quality flags:")
        for anomaly in hard_flags:
            lines.append(f"- HARD {anomaly.get('ticker')}: {anomaly.get('rule')} — {anomaly.get('detail')}")
        for anomaly in soft_flags:
            lines.append(f"- SOFT {anomaly.get('ticker')}: {anomaly.get('rule')} — {anomaly.get('detail')}")

    lines.extend(
        [
            "",
            "## Trigger Checklist",
            "",
            "- Upgrade OSAT/advanced-packaging watch when CoWoS supply gap persists while AMKR/ASX utilization rises.",
            "- Upgrade HBM names when HBM bit-output guidance and contract pricing rise together; downgrade when standard DRAM weakens while HBM orders slip.",
            "- Upgrade equipment when foundry/memory capex guidance and backlog rise together; downgrade if order growth becomes China-only and export controls tighten.",
            "- Treat below-200DMA names as research-only until trend repair, even when structural score is high.",
            "- Do not add high-beta AI/semi exposure when the portfolio's semiconductor risk contribution is already above its capital weight and the market gate is closed.",
            "",
            "## Sources",
            "",
        ]
    )
    for src in doc["sources"]:
        lines.append(f"- [{src['name']}]({src['url']}) — {src['use']}")
    lines.extend(
        [
            "",
            "## Method Notes",
            "",
            "- Structural factors are curated priors from the attached AI-SemiQuant research notes and should be reviewed after earnings, capex guides, or market-share changes.",
            "- The artifact has no row-level point-in-time fundamental history, so structural priors and curated supply-chain edges are not decision-grade on their own.",
            "- Market overlay uses Yahoo Finance adjusted daily closes; non-USD listings are converted through backward-looking daily FX before returns, trend, volatility, drawdown, and beta are calculated.",
            "- Current Yahoo profile market caps fetched after an older dashboard decision date remain display-only and are excluded from size/torque scoring.",
            "- Portfolio exposure is read from `output/portfolio_dashboard.html` when present.",
        ]
    )
    return "\n".join(lines) + "\n"


def factor_meaning(key: str) -> str:
    return {
        "pricingPower": "Market share, scarcity, margin and contract-price leverage.",
        "profitElasticity": "Operating leverage once utilization crosses fixed-cost break-even.",
        "capexConversion": "How directly customer/fab capex becomes backlog or revenue.",
        "valuationGrowth": "Growth durability relative to valuation and market expectations.",
        "sizeGrowthTorque": "Market-cap elasticity blended with growth quality and business-quality guardrails.",
    }[key]


def fmt_cap(value: Optional[float]) -> str:
    if value is None:
        return "-"
    try:
        v = float(value)
    except Exception:
        return "-"
    if v >= 1e12:
        return f"${v/1e12:.2f}T"
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.0f}M"
    return f"${v:,.0f}"


def fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):+.2f}%"
    except Exception:
        return "-"


def build_document(args) -> dict:
    company_tickers = [c.ticker for c in UNIVERSE]
    payload = read_dashboard_payload(Path(args.dashboard)) if args.dashboard else None
    target_as_of = dashboard_market_as_of(payload)
    profiles = load_market_profiles(
        company_tickers,
        no_fetch=args.no_fetch,
        market_data_as_of=target_as_of,
    )
    tickers = company_tickers + ["SOXX", *required_fx_tickers(profiles, company_tickers)]
    raw_prices = load_market_data(tickers, args.period, no_fetch=args.no_fetch)
    profiles = align_market_profile_as_of(profiles, raw_prices, target_as_of)
    raw_rows = len(raw_prices) if raw_prices is not None else 0
    prices = truncate_market_data(raw_prices, target_as_of)
    aligned_rows = len(prices) if prices is not None else 0
    exposures = portfolio_exposure(payload)
    scores, latest_date = build_scores(prices, exposures, profiles, reference_date=target_as_of)
    generated_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "schemaVersion": 3,
        "researchOnly": True,
        "decisionGrade": False,
        "generatedAt": generated_at,
        "marketDataAsOf": latest_date,
        "dataAlignment": {
            "portfolioPriceAsOf": target_as_of,
            "futureCalendarRowsExcluded": max(0, raw_rows - aligned_rows),
            "policy": "market overlays do not use closes after the dashboard price date; non-USD technical returns include backward-filled daily FX",
        },
        "title": "AI-SemiQuant",
        "subtitle": "AI 半导体全产业链量化评分与资金流分析系统",
        "method": "Curated structural AI semiconductor factors + Yahoo Finance market/market-cap overlay + portfolio exposure overlay.",
        "factorWeights": FACTOR_WEIGHTS,
        "factorLabels": FACTOR_LABELS,
        "torqueOverlay": TORQUE_OVERLAY,
        "capitalWaterfall": list(CAPITAL_WATERFALL),
        "capitalFlowEdges": list(CAPITAL_FLOW_EDGES),
        "summary": summarize(scores),
        "modelCard": model_card(scores, latest_date),
        "scores": scores,
        "sources": list(SOURCE_LINKS),
        "disclaimer": "Research framework only. Not investment advice, not a recommendation, and not a guarantee of future return.",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate AI-SemiQuant JSON and Markdown reference report.")
    ap.add_argument("--out-json", default=str(OUT_JSON))
    ap.add_argument("--out-md", default=str(OUT_MD))
    ap.add_argument("--dashboard", default=str(DASHBOARD), help="portfolio_dashboard.html for exposure overlay")
    ap.add_argument("--period", default="2y", help="Yahoo Finance history period")
    ap.add_argument("--no-fetch", action="store_true", help="skip Yahoo fetch and emit structural-only scores")
    args = ap.parse_args()

    doc = build_document(args)
    out_json = Path(args.out_json)
    out_md = Path(args.out_md)
    atomic_write_json(out_json, doc)
    atomic_write_text(out_md, render_report(doc))
    print(f"✓ wrote {out_json}")
    print(f"✓ wrote {out_md}")
    print(
        f"  {len(doc['scores'])} companies · market data as of {doc.get('marketDataAsOf') or 'unavailable'} · "
        f"gates {doc['summary']['gateCounts']}"
    )


if __name__ == "__main__":
    main()
