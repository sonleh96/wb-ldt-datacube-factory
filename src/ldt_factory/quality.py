from __future__ import annotations

import html
import json
import logging
import math
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .checkpoint_utils import write_frame_csv_atomic, write_manifest
from .context import RunContext
from .logging_utils import logged_action


SCORE_INDICATORS: dict[str, tuple[str, int]] = {
    "Broadband Internet Score": ("Average Broadband Internet Download Speed (Mbps)", 1),
    "Mobile Internet Score": ("Average Mobile Internet Download Speed (Mbps)", 1),
    "Key Structure Internet Access Score": (
        "Key Structures without Access to Broadband Internet (%)",
        -1,
    ),
    "Accessibility to Hospitals Score": ("Accessibility to Hospitals (%)", 1),
    "Accessibility to Schools Score": ("Accessibility to Schools (%)", 1),
    "Railway Heatwave Score": ("Railway Heatwave Risk (%)", -1),
    "Road Heatwave Score": ("Road Heatwave Risk (%)", -1),
    "Road Flood Score": ("Road Flood Risk (%)", -1),
    "Railway Flood Score": ("Railway Flood Risk (%)", -1),
    "Emissions Normalized Score": ("C02 Emissions per Area (tonnes/km2)", -1),
    "Air Quality Score": ("Average PM25 Concentration (ug/m3)", -1),
    "Deforestation Score": ("Change in Forest Area (%)", -1),
    "Emissions Score": ("CO2-Equivalent Emissions (tonnes)", -1),
    "Luminosity per Capita Score": ("Luminosity per Capita", 1),
    "Luminosity per Area Score": ("Luminosity per Area", 1),
    "Built Area Development Score": ("Change in Build Area (%)", 1),
    "Tourism Score": ("Number of Tourism POIs", 1),
    "Agricultural Land Score": ("Total Land Area for Agricultural Use (km2)", 1),
}

COMPOSITE_COMPONENTS: dict[str, tuple[str, ...]] = {
    "Infrastructure Score": (
        "Broadband Internet Score",
        "Mobile Internet Score",
        "Key Structure Internet Access Score",
        "Accessibility to Hospitals Score",
        "Accessibility to Schools Score",
        "Railway Heatwave Score",
        "Road Heatwave Score",
        "Road Flood Score",
        "Railway Flood Score",
    ),
    "Livability Score": (
        "Emissions Score",
        "Air Quality Score",
        "Deforestation Score",
        "Emissions Normalized Score",
    ),
    "Prosperity Score": (
        "Luminosity per Capita Score",
        "Luminosity per Area Score",
        "Built Area Development Score",
        "Tourism Score",
        "Agricultural Land Score",
    ),
}

DEFAULT_INDICATOR_RANGES: dict[str, tuple[float | None, float | None]] = {
    "Population": (0, None),
    "Nighttime Luminosity": (0, None),
    "Average Broadband Internet Download Speed (Mbps)": (0, None),
    "Average Mobile Internet Download Speed (Mbps)": (0, None),
    "Key Structures without Access to Broadband Internet (%)": (0, 100),
    "Accessibility to Schools (%)": (0, 100),
    "Accessibility to Hospitals (%)": (0, 100),
    "Railway Flood Risk (km)": (0, None),
    "Road Flood Risk (km)": (0, None),
    "Railway Heatwave Risk (km)": (0, None),
    "Road Heatwave Risk (km)": (0, None),
    "Total Railway Length (km)": (0, None),
    "Total Road Length (km)": (0, None),
    "Average PM25 Concentration (ug/m3)": (0, None),
    "Total Land Area (km2)": (0, None),
    "Luminosity per Capita": (0, None),
    "Luminosity per Area": (0, None),
    "Railway Flood Risk (%)": (0, 100),
    "Road Flood Risk (%)": (0, 100),
    "Railway Heatwave Risk (%)": (0, 100),
    "Road Heatwave Risk (%)": (0, 100),
    "Number of Tourism POIs": (0, None),
    "Total Land Area for Agricultural Use (km2)": (0, None),
}

STATIC_INDICATORS = {
    "Number of Tourism POIs",
    "Accessibility to Schools (%)",
    "Accessibility to Hospitals (%)",
    "Railway Flood Risk (km)",
    "Road Flood Risk (km)",
    "Railway Heatwave Risk (km)",
    "Road Heatwave Risk (km)",
    "Total Railway Length (km)",
    "Total Road Length (km)",
    "Railway Flood Risk (%)",
    "Road Flood Risk (%)",
    "Railway Heatwave Risk (%)",
    "Road Heatwave Risk (%)",
}

FINDING_COLUMNS = (
    "severity",
    "blocking",
    "check",
    "dataset",
    "column",
    "count",
    "rate",
    "message",
    "recommendation",
)


@dataclass(frozen=True)
class Finding:
    severity: str
    blocking: bool
    check: str
    dataset: str
    column: str | None
    count: int | None
    rate: float | None
    message: str
    recommendation: str


@dataclass(frozen=True)
class QualityResult:
    status: str
    indicator_rows: int
    score_rows: int
    findings: tuple[Finding, ...]
    output_dir: Path

    @property
    def blocking_count(self) -> int:
        return sum(item.blocking for item in self.findings)


class QualityGateError(RuntimeError):
    """Raised after evidence is written when a publication fails a hard QA rule."""


def _add(
    findings: list[Finding],
    severity: str,
    check: str,
    dataset: str,
    message: str,
    recommendation: str,
    *,
    blocking: bool = False,
    column: str | None = None,
    count: int | None = None,
    rate: float | None = None,
) -> None:
    findings.append(
        Finding(
            severity=severity,
            blocking=blocking,
            check=check,
            dataset=dataset,
            column=column,
            count=count,
            rate=round(float(rate), 6) if rate is not None else None,
            message=message,
            recommendation=recommendation,
        )
    )


def _coerce_measures(frame, keys: list[str], dataset: str, findings: list[Finding]):
    import pandas as pd

    result = frame.copy()
    for column in (name for name in result.columns if name not in keys):
        original = result[column]
        converted = pd.to_numeric(original, errors="coerce")
        populated = original.notna() & original.astype(str).str.strip().ne("")
        invalid = populated & converted.isna()
        if invalid.any():
            _add(
                findings,
                "high",
                "numeric_type",
                dataset,
                f"{int(invalid.sum())} populated value(s) cannot be parsed as numeric.",
                "Correct the upstream type or remove non-numeric sentinel values.",
                blocking=True,
                column=column,
                count=int(invalid.sum()),
                rate=float(invalid.mean()),
            )
        result[column] = converted
    return result


def _check_keys(
    frame,
    dataset: str,
    keys: list[str],
    expected_years: list[int],
    findings: list[Finding],
) -> bool:
    import pandas as pd

    absent = [column for column in keys if column not in frame.columns]
    if absent:
        _add(
            findings,
            "critical",
            "required_columns",
            dataset,
            f"Missing required key columns: {absent}.",
            "Restore the configured administrative columns and Year before publication.",
            blocking=True,
            count=len(absent),
        )
        return False

    null_key = frame[keys].isna().any(axis=1)
    blank_key = pd.Series(False, index=frame.index)
    for column in keys[:-1]:
        blank_key |= frame[column].astype("string").str.strip().eq("").fillna(False)
    invalid_key = null_key | blank_key
    if invalid_key.any():
        _add(
            findings,
            "critical",
            "key_completeness",
            dataset,
            f"{int(invalid_key.sum())} row(s) have null or blank key values.",
            "Repair the administrative join and do not publish rows without a complete key.",
            blocking=True,
            count=int(invalid_key.sum()),
            rate=float(invalid_key.mean()),
        )

    duplicates = frame.duplicated(keys, keep=False)
    if duplicates.any():
        _add(
            findings,
            "critical",
            "key_uniqueness",
            dataset,
            f"{int(duplicates.sum())} row(s) participate in duplicate publication keys.",
            "Identify the join expansion or aggregation error and restore one row per admin unit and year.",
            blocking=True,
            count=int(duplicates.sum()),
            rate=float(duplicates.mean()),
        )

    normalized = frame[keys].copy()
    for column in keys[:-1]:
        normalized[column] = normalized[column].astype("string").str.strip().str.casefold()
    normalized_duplicates = normalized.duplicated(keys, keep=False) & ~duplicates
    if normalized_duplicates.any():
        _add(
            findings,
            "high",
            "normalized_key_uniqueness",
            dataset,
            f"{int(normalized_duplicates.sum())} row(s) collide after trimming and case normalization.",
            "Standardize administrative names before merging.",
            blocking=True,
            count=int(normalized_duplicates.sum()),
            rate=float(normalized_duplicates.mean()),
        )

    years = pd.to_numeric(frame[keys[-1]], errors="coerce")
    invalid_years = frame[keys[-1]].notna() & years.isna()
    if invalid_years.any():
        _add(
            findings,
            "critical",
            "year_type",
            dataset,
            f"{int(invalid_years.sum())} Year value(s) are not numeric.",
            "Write publication years as four-digit integers.",
            blocking=True,
            count=int(invalid_years.sum()),
        )
    if expected_years:
        actual = set(years.dropna().astype(int))
        unexpected = sorted(actual - set(expected_years))
        missing = sorted(set(expected_years) - actual)
        if unexpected or missing:
            _add(
                findings,
                "critical",
                "year_coverage",
                dataset,
                f"Expected years {expected_years}; missing={missing}, unexpected={unexpected}.",
                "Reconcile the final table with years.indicators in the country YAML.",
                blocking=True,
            )

        admin_pairs = frame[keys[:-1]].dropna().drop_duplicates()
        expected_rows = len(admin_pairs) * len(expected_years)
        actual_rows = len(frame.drop_duplicates(keys))
        if actual_rows != expected_rows:
            _add(
                findings,
                "critical",
                "panel_completeness",
                dataset,
                f"The panel has {actual_rows} unique keys; {expected_rows} are expected from "
                f"{len(admin_pairs)} admin units x {len(expected_years)} years.",
                "Find missing or extra administrative-year rows before release.",
                blocking=True,
                count=abs(expected_rows - actual_rows),
                rate=abs(expected_rows - actual_rows) / expected_rows if expected_rows else 0,
            )
    return True


def _profile(frame, dataset: str, keys: list[str]):
    import numpy as np
    import pandas as pd

    records: list[dict[str, Any]] = []
    for column in (name for name in frame.columns if name not in keys):
        series = pd.to_numeric(frame[column], errors="coerce")
        valid = series.replace([np.inf, -np.inf], np.nan).dropna()
        record: dict[str, Any] = {
            "dataset": dataset,
            "column": column,
            "dtype": str(frame[column].dtype),
            "rows": len(frame),
            "count": int(valid.count()),
            "missing_count": int(series.isna().sum()),
            "missing_rate": float(series.isna().mean()),
            "zero_count": int((valid == 0).sum()),
            "zero_rate": float((valid == 0).mean()) if len(valid) else math.nan,
            "distinct_count": int(valid.nunique()),
        }
        for name, value in (
            ("mean", valid.mean()),
            ("std", valid.std()),
            ("min", valid.min()),
            ("p01", valid.quantile(0.01)),
            ("p25", valid.quantile(0.25)),
            ("median", valid.median()),
            ("p75", valid.quantile(0.75)),
            ("p99", valid.quantile(0.99)),
            ("max", valid.max()),
        ):
            record[name] = float(value) if pd.notna(value) else math.nan
        records.append(record)
    return pd.DataFrame.from_records(records)


def _by_year(frame, dataset: str, keys: list[str]):
    import numpy as np
    import pandas as pd

    if not all(column in frame for column in keys):
        return pd.DataFrame()
    records: list[dict[str, Any]] = []
    for column in (name for name in frame.columns if name not in keys):
        values = pd.to_numeric(frame[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
        working = pd.DataFrame({"Year": frame["Year"], "value": values})
        for year, group in working.groupby("Year", dropna=False):
            valid = group["value"].dropna()
            records.append(
                {
                    "dataset": dataset,
                    "column": column,
                    "Year": year,
                    "rows": len(group),
                    "count": len(valid),
                    "missing_rate": float(group["value"].isna().mean()),
                    "zero_rate": float((valid == 0).mean()) if len(valid) else math.nan,
                    "mean": float(valid.mean()) if len(valid) else math.nan,
                    "median": float(valid.median()) if len(valid) else math.nan,
                    "min": float(valid.min()) if len(valid) else math.nan,
                    "max": float(valid.max()) if len(valid) else math.nan,
                    "distinct_count": int(valid.nunique()),
                }
            )
    return pd.DataFrame.from_records(records)


def _range_specs(settings: dict[str, Any]) -> dict[str, tuple[float | None, float | None]]:
    result = dict(DEFAULT_INDICATOR_RANGES)
    configured = settings.get("indicator_ranges", {})
    if not isinstance(configured, dict):
        raise ValueError("quality.indicator_ranges must be a mapping")
    for column, limits in configured.items():
        if isinstance(limits, dict):
            result[str(column)] = (limits.get("min"), limits.get("max"))
        elif isinstance(limits, (list, tuple)) and len(limits) == 2:
            result[str(column)] = (limits[0], limits[1])
        else:
            raise ValueError(f"Invalid quality range for {column!r}; use [min, max]")
    return result


def _check_measures(
    frame,
    dataset: str,
    keys: list[str],
    findings: list[Finding],
    *,
    settings: dict[str, Any],
    static_year: int | None,
) -> None:
    import numpy as np
    import pandas as pd

    warn_missing = float(settings.get("missingness_warning_rate", 0.20))
    high_missing = float(settings.get("high_missingness_rate", 0.50))
    for column in (name for name in frame.columns if name not in keys):
        series = pd.to_numeric(frame[column], errors="coerce")
        finite = series.dropna()
        nonfinite = finite[~np.isfinite(finite)]
        if len(nonfinite):
            _add(
                findings,
                "critical" if dataset == "scores" else "high",
                "finite_values",
                dataset,
                f"{len(nonfinite)} value(s) are positive or negative infinity.",
                "Fix zero-denominator calculations before publication.",
                blocking=True,
                column=column,
                count=len(nonfinite),
                rate=len(nonfinite) / len(frame) if len(frame) else 0,
            )

        missing_rate = float(series.isna().mean()) if len(series) else 0
        if missing_rate >= warn_missing:
            expected_static = column in STATIC_INDICATORS and static_year is not None
            _add(
                findings,
                "low" if expected_static else ("high" if missing_rate >= high_missing else "medium"),
                "missingness",
                dataset,
                f"{missing_rate:.1%} of values are missing"
                + ("; this is consistent with a static snapshot column." if expected_static else "."),
                "Confirm that null means not observed; do not replace it with zero unless zero is measured.",
                column=column,
                count=int(series.isna().sum()),
                rate=missing_rate,
            )

        valid = series.replace([np.inf, -np.inf], np.nan).dropna()
        if len(valid) and valid.nunique() == 1:
            _add(
                findings,
                "low",
                "zero_variance",
                dataset,
                f"All populated values are {valid.iloc[0]:g}; the column cannot distinguish locations or years.",
                "Verify whether a constant field is expected or reflects a failed extraction/default fill.",
                column=column,
                count=len(valid),
                rate=1.0,
            )
        elif len(valid) and float((valid == 0).mean()) >= 0.95:
            _add(
                findings,
                "medium",
                "zero_dominance",
                dataset,
                f"{float((valid == 0).mean()):.1%} of populated values are zero.",
                "Check whether zeros are measurements or placeholders for missing source coverage.",
                column=column,
                count=int((valid == 0).sum()),
                rate=float((valid == 0).mean()),
            )

        if static_year is not None and column in STATIC_INDICATORS and "Year" in frame:
            outside = series[pd.to_numeric(frame["Year"], errors="coerce") != static_year].dropna()
            inside = series[pd.to_numeric(frame["Year"], errors="coerce") == static_year].dropna()
            if len(outside) and (outside == 0).all() and len(inside) and (inside != 0).any():
                _add(
                    findings,
                    "high",
                    "possible_zero_imputation",
                    dataset,
                    f"All {len(outside)} values outside static year {static_year} are zero while the static year has non-zero values.",
                    "Preserve unavailable static observations as null rather than backfilling zero across years.",
                    column=column,
                    count=len(outside),
                    rate=len(outside) / len(frame) if len(frame) else 0,
                )


def _check_indicator_ranges(frame, findings: list[Finding], specs) -> None:
    import numpy as np
    import pandas as pd

    for column, (minimum, maximum) in specs.items():
        if column not in frame:
            continue
        series = pd.to_numeric(frame[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
        invalid = pd.Series(False, index=series.index)
        if minimum is not None:
            invalid |= series < float(minimum)
        if maximum is not None:
            invalid |= series > float(maximum)
        if invalid.any():
            _add(
                findings,
                "high",
                "indicator_range",
                "indicators",
                f"{int(invalid.sum())} value(s) fall outside [{minimum}, {maximum}].",
                "Inspect units, aggregation, and denominator logic for the affected records.",
                blocking=True,
                column=column,
                count=int(invalid.sum()),
                rate=float(invalid.mean()),
            )


def _check_derived_indicators(frame, findings: list[Finding], tolerance: float) -> None:
    import numpy as np
    import pandas as pd

    specifications = {
        "Luminosity per Capita": ("Nighttime Luminosity", "Population", 1.0),
        "Luminosity per Area": ("Nighttime Luminosity", "Total Land Area (km2)", 1.0),
        "Railway Flood Risk (%)": ("Railway Flood Risk (km)", "Total Railway Length (km)", 100.0),
        "Road Flood Risk (%)": ("Road Flood Risk (km)", "Total Road Length (km)", 100.0),
        "Railway Heatwave Risk (%)": ("Railway Heatwave Risk (km)", "Total Railway Length (km)", 100.0),
        "Road Heatwave Risk (%)": ("Road Heatwave Risk (km)", "Total Road Length (km)", 100.0),
        "C02 Emissions per Area (tonnes/km2)": (
            "CO2-Equivalent Emissions (tonnes)",
            "Total Land Area (km2)",
            1.0,
        ),
    }
    for output, (numerator, denominator, multiplier) in specifications.items():
        if not all(column in frame for column in (output, numerator, denominator)):
            continue
        actual = pd.to_numeric(frame[output], errors="coerce")
        top = pd.to_numeric(frame[numerator], errors="coerce")
        bottom = pd.to_numeric(frame[denominator], errors="coerce")
        valid = actual.notna() & top.notna() & bottom.gt(0)
        expected = multiplier * top / bottom
        allowed = tolerance + 1e-6 * expected.abs()
        mismatch = valid & (actual - expected).abs().gt(allowed)
        if mismatch.any():
            _add(
                findings,
                "high",
                "derived_value_consistency",
                "indicators",
                f"{int(mismatch.sum())} value(s) do not match {multiplier:g} * {numerator} / {denominator}.",
                "Recompute the derived column from the published numerator and denominator.",
                blocking=True,
                column=output,
                count=int(mismatch.sum()),
                rate=float(mismatch.mean()),
            )
        impossible = top.gt(0) & bottom.le(0)
        if impossible.any():
            _add(
                findings,
                "high",
                "denominator_consistency",
                "indicators",
                f"{int(impossible.sum())} row(s) have a positive numerator and a non-positive denominator.",
                "Repair total-area or total-network-length coverage before calculating ratios.",
                blocking=True,
                column=output,
                count=int(impossible.sum()),
                rate=float(impossible.mean()),
            )

    if all(column in frame for column in ("Total Land Area for Agricultural Use (km2)", "Total Land Area (km2)")):
        agricultural = pd.to_numeric(
            frame["Total Land Area for Agricultural Use (km2)"], errors="coerce"
        )
        total = pd.to_numeric(frame["Total Land Area (km2)"], errors="coerce")
        invalid = agricultural.notna() & total.notna() & (agricultural > 1.01 * total)
        if invalid.any():
            _add(
                findings,
                "high",
                "area_consistency",
                "indicators",
                f"{int(invalid.sum())} agricultural-area value(s) exceed total land area by more than 1%.",
                "Check raster pixel-area conversion, boundary clipping, and units.",
                blocking=True,
                column="Total Land Area for Agricultural Use (km2)",
                count=int(invalid.sum()),
                rate=float(invalid.mean()),
            )


def _check_score_ranges(frame, keys: list[str], findings: list[Finding]) -> None:
    import numpy as np
    import pandas as pd

    score_columns = [column for column in frame.columns if column not in keys]
    if not score_columns:
        _add(
            findings,
            "critical",
            "score_schema",
            "scores",
            "The score table has no score columns.",
            "Run score construction before publication.",
            blocking=True,
        )
        return
    for column in score_columns:
        series = pd.to_numeric(frame[column], errors="coerce")
        invalid = series.notna() & (~np.isfinite(series) | (series < 0) | (series > 100))
        if invalid.any():
            _add(
                findings,
                "critical",
                "score_range",
                "scores",
                f"{int(invalid.sum())} score(s) are outside the required 0-100 range or non-finite.",
                "Correct score normalization and block this table from release.",
                blocking=True,
                column=column,
                count=int(invalid.sum()),
                rate=float(invalid.mean()),
            )


def _check_key_alignment(indicators, scores, keys: list[str], findings: list[Finding]) -> None:
    if not all(column in indicators and column in scores for column in keys):
        return
    left = indicators[keys].drop_duplicates()
    right = scores[keys].drop_duplicates()
    comparison = left.merge(right, on=keys, how="outer", indicator=True)
    unmatched = comparison["_merge"] != "both"
    if unmatched.any():
        indicator_only = int((comparison["_merge"] == "left_only").sum())
        score_only = int((comparison["_merge"] == "right_only").sum())
        _add(
            findings,
            "critical",
            "table_key_alignment",
            "publication",
            f"Indicator-only keys={indicator_only}; score-only keys={score_only}.",
            "Generate both tables from the same base panel and reconcile dropped/extra rows.",
            blocking=True,
            count=int(unmatched.sum()),
            rate=float(unmatched.mean()),
        )


def _check_composites(scores, findings: list[Finding], tolerance: float):
    import pandas as pd

    records: list[dict[str, Any]] = []
    for composite, candidates in COMPOSITE_COMPONENTS.items():
        if composite not in scores:
            continue
        components = [column for column in candidates if column in scores]
        if not components:
            continue
        expected = scores[components].mean(axis=1, skipna=True)
        actual = pd.to_numeric(scores[composite], errors="coerce")
        difference = (actual - expected).abs()
        invalid = difference > tolerance
        records.append(
            {
                "composite": composite,
                "components": " | ".join(components),
                "rows_compared": int((actual.notna() & expected.notna()).sum()),
                "mismatch_count": int(invalid.sum()),
                "max_absolute_difference": float(difference.max()) if difference.notna().any() else math.nan,
            }
        )
        if invalid.any():
            _add(
                findings,
                "high",
                "composite_consistency",
                "scores",
                f"{int(invalid.sum())} row(s) differ from the mean of available components by more than {tolerance}.",
                "Recompute the composite from its documented component scores.",
                blocking=True,
                column=composite,
                count=int(invalid.sum()),
                rate=float(invalid.mean()),
            )
    return records


def _check_score_alignment(indicators, scores, keys: list[str], findings: list[Finding], minimum: float):
    import pandas as pd

    if not all(column in indicators and column in scores for column in keys):
        return []
    merged = indicators.merge(scores, on=keys, how="inner", suffixes=("", "__score"))
    records: list[dict[str, Any]] = []
    for score, (indicator, direction) in SCORE_INDICATORS.items():
        if score not in merged or indicator not in merged:
            continue
        pair = pd.DataFrame(
            {
                "indicator": pd.to_numeric(merged[indicator], errors="coerce"),
                "score": pd.to_numeric(merged[score], errors="coerce"),
            }
        ).dropna()
        correlation = math.nan
        status = "insufficient_data"
        if len(pair) >= 3 and pair["indicator"].nunique() > 1 and pair["score"].nunique() > 1:
            correlation = float(
                pair["indicator"].rank(method="average").corr(
                    pair["score"].rank(method="average")
                )
            )
            aligned = correlation * direction >= minimum
            status = "pass" if aligned else "fail"
            if not aligned:
                _add(
                    findings,
                    "high",
                    "score_indicator_alignment",
                    "publication",
                    f"Spearman correlation is {correlation:.3f}; expected direction "
                    f"{'positive' if direction > 0 else 'negative'} with magnitude >= {minimum}.",
                    "Verify rank direction, joins, missing-value handling, and whether scoring is panel-wide or within-year.",
                    blocking=True,
                    column=score,
                    count=len(pair),
                )
        records.append(
            {
                "score": score,
                "indicator": indicator,
                "expected_direction": "positive" if direction > 0 else "negative",
                "pairs": len(pair),
                "spearman": correlation,
                "status": status,
            }
        )
    return records


def _find_outliers(frame, keys: list[str], findings: list[Finding], multiplier: float, cap: int):
    import numpy as np
    import pandas as pd

    outputs: list[pd.DataFrame] = []
    for column in (name for name in frame.columns if name not in keys):
        series = pd.to_numeric(frame[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
        valid = series.dropna()
        if len(valid) < 8 or valid.nunique() < 4:
            continue
        q1, q3 = valid.quantile([0.25, 0.75])
        iqr = q3 - q1
        if iqr <= 0:
            continue
        lower = q1 - multiplier * iqr
        upper = q3 + multiplier * iqr
        mask = (series < lower) | (series > upper)
        if not mask.any():
            continue
        _add(
            findings,
            "low",
            "robust_outliers",
            "indicators",
            f"{int(mask.sum())} extreme value(s) exceed the {multiplier:g} x IQR fences [{lower:.4g}, {upper:.4g}].",
            "Review source records and units; keep legitimate geographic extremes with documentation.",
            column=column,
            count=int(mask.sum()),
            rate=float(mask.mean()),
        )
        sample = frame.loc[mask, keys].copy()
        sample["column"] = column
        sample["value"] = series[mask]
        sample["lower_fence"] = lower
        sample["upper_fence"] = upper
        outputs.append(sample.head(cap))
    if not outputs:
        return pd.DataFrame(columns=[*keys, "column", "value", "lower_fence", "upper_fence"])
    return pd.concat(outputs, ignore_index=True)


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_charts(output_dir: Path, indicator_profile, score_frame, keys: list[str]) -> list[Path]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []

    paths: list[Path] = []
    if not indicator_profile.empty:
        view = indicator_profile.sort_values("missing_rate").tail(20)
        fig, ax = plt.subplots(figsize=(10, max(4, 0.32 * len(view))))
        ax.barh(view["column"], 100 * view["missing_rate"], color="#4C78A8")
        ax.set_xlabel("Missing values (%)")
        ax.set_title("Indicator completeness (20 highest missing rates)")
        ax.set_xlim(0, 100)
        fig.tight_layout()
        path = output_dir / "indicator_missingness.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        paths.append(path)

    score_columns = [column for column in score_frame.columns if column not in keys]
    if score_columns:
        data = [score_frame[column].dropna().to_numpy() for column in score_columns]
        selected = [(column, values) for column, values in zip(score_columns, data) if len(values)]
        if selected:
            fig, ax = plt.subplots(figsize=(11, max(5, 0.36 * len(selected))))
            ax.boxplot(
                [values for _, values in selected],
                labels=[column for column, _ in selected],
                vert=False,
                showfliers=False,
            )
            ax.set_xlim(0, 100)
            ax.set_xlabel("Score")
            ax.set_title("Score distributions")
            fig.tight_layout()
            path = output_dir / "score_distributions.png"
            fig.savefig(path, dpi=150)
            plt.close(fig)
            paths.append(path)
    return paths


def _render_report(
    *,
    status: str,
    iso3: str,
    indicator_rows: int,
    score_rows: int,
    keys: list[str],
    expected_years: list[int],
    findings_frame,
    indicator_profile,
    alignment_frame,
    charts: Iterable[Path],
) -> str:
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    findings_view = findings_frame.copy()
    if not findings_view.empty:
        findings_view["_order"] = findings_view["severity"].map(severity_order)
        findings_view = findings_view.sort_values(["blocking", "_order"], ascending=[False, True]).drop(columns="_order")
    finding_table = (
        findings_view.to_html(index=False, escape=True, classes="data-table")
        if not findings_view.empty
        else "<p>No anomalies were detected by the configured checks.</p>"
    )
    profile_columns = ["column", "count", "missing_rate", "zero_rate", "distinct_count", "min", "median", "max"]
    profile_table = indicator_profile[profile_columns].to_html(
        index=False, escape=True, classes="data-table", float_format=lambda value: f"{value:.4g}"
    ) if not indicator_profile.empty else "<p>No indicator measures were available.</p>"
    alignment_table = alignment_frame.to_html(
        index=False, escape=True, classes="data-table", float_format=lambda value: f"{value:.3f}"
    ) if not alignment_frame.empty else "<p>No score-indicator pairs were available.</p>"
    images = "".join(
        f'<figure><img src="{html.escape(path.name)}" alt="{html.escape(path.stem)}"><figcaption>{html.escape(path.stem.replace("_", " ").title())}</figcaption></figure>'
        for path in charts
    )
    blocking = int(findings_frame["blocking"].sum()) if not findings_frame.empty else 0
    warning_count = len(findings_frame) - blocking
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(iso3)} LDT publication quality report</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;color:#1f2937;margin:0;background:#f3f4f6}}main{{max-width:1280px;margin:auto;padding:28px}}
h1,h2{{color:#17365d}}.cards{{display:flex;gap:12px;flex-wrap:wrap}}.card{{background:white;border-radius:8px;padding:14px 18px;box-shadow:0 1px 4px #0002;min-width:150px}}
.status{{font-weight:700;color:{'#a61b1b' if status == 'fail' else '#8a5b00' if status == 'warn' else '#166534'}}}section{{background:white;margin-top:18px;padding:18px;border-radius:8px;overflow:auto}}
.data-table{{border-collapse:collapse;width:100%;font-size:13px}}.data-table th,.data-table td{{border:1px solid #d1d5db;padding:6px;text-align:left;vertical-align:top}}.data-table th{{background:#e8eef7;position:sticky;top:0}}
figure{{margin:20px 0}}img{{max-width:100%;height:auto}}code{{background:#eef2f7;padding:2px 4px}}</style></head>
<body><main><h1>{html.escape(iso3)} LDT publication quality report</h1>
<div class="cards"><div class="card">Status<br><span class="status">{status.upper()}</span></div><div class="card">Indicator rows<br><b>{indicator_rows:,}</b></div><div class="card">Score rows<br><b>{score_rows:,}</b></div><div class="card">Blocking findings<br><b>{blocking}</b></div><div class="card">Review findings<br><b>{warning_count}</b></div></div>
<section><h2>Scope and assumptions</h2><p>Expected grain: one row per <code>{html.escape(' + '.join(keys))}</code>. Expected years: {html.escape(str(expected_years))}. Scores must be finite and within 0-100. Indicator outliers use robust IQR fences and are review flags, not automatic errors. Zero is treated as an observed value; suspicious static-year zero fills are reported separately.</p></section>
<section><h2>Findings</h2>{finding_table}</section>
<section><h2>Indicator profile</h2>{profile_table}</section>
<section><h2>Score-to-indicator alignment</h2>{alignment_table}</section>
<section><h2>Visual checks</h2>{images or '<p>Charts were not generated because no plottable data or plotting backend was available.</p>'}</section>
</main></body></html>"""


def analyze_publication(
    indicators_path: Path,
    scores_path: Path,
    *,
    admin_columns: tuple[str, str],
    expected_years: list[int],
    static_year: int | None,
    output_dir: Path,
    iso3: str,
    settings: dict[str, Any] | None = None,
    create_charts: bool = True,
) -> QualityResult:
    import numpy as np
    import pandas as pd

    settings = dict(settings or {})
    keys = [admin_columns[0], admin_columns[1], "Year"]
    indicators = pd.read_csv(indicators_path)
    scores = pd.read_csv(scores_path)
    findings: list[Finding] = []

    indicator_keys_ok = _check_keys(indicators, "indicators", keys, expected_years, findings)
    score_keys_ok = _check_keys(scores, "scores", keys, expected_years, findings)
    if indicator_keys_ok:
        indicators = _coerce_measures(indicators, keys, "indicators", findings)
    if score_keys_ok:
        scores = _coerce_measures(scores, keys, "scores", findings)

    _check_key_alignment(indicators, scores, keys, findings)
    if indicator_keys_ok:
        _check_measures(
            indicators,
            "indicators",
            keys,
            findings,
            settings=settings,
            static_year=static_year,
        )
        _check_indicator_ranges(indicators, findings, _range_specs(settings))
        _check_derived_indicators(
            indicators,
            findings,
            float(settings.get("derived_value_tolerance", 0.02)),
        )
    if score_keys_ok:
        _check_measures(
            scores,
            "scores",
            keys,
            findings,
            settings=settings,
            static_year=static_year,
        )
        _check_score_ranges(scores, keys, findings)

    indicator_profile = _profile(indicators, "indicators", keys) if indicator_keys_ok else pd.DataFrame()
    score_profile = _profile(scores, "scores", keys) if score_keys_ok else pd.DataFrame()
    indicator_by_year = _by_year(indicators, "indicators", keys) if indicator_keys_ok else pd.DataFrame()
    score_by_year = _by_year(scores, "scores", keys) if score_keys_ok else pd.DataFrame()
    outliers = (
        _find_outliers(
            indicators,
            keys,
            findings,
            float(settings.get("outlier_iqr_multiplier", 3.0)),
            int(settings.get("max_outlier_rows_per_column", 100)),
        )
        if indicator_keys_ok
        else pd.DataFrame()
    )
    composite_records = (
        _check_composites(scores, findings, float(settings.get("composite_tolerance", 0.02)))
        if score_keys_ok
        else []
    )
    alignment_records = (
        _check_score_alignment(
            indicators,
            scores,
            keys,
            findings,
            float(settings.get("minimum_rank_correlation", 0.95)),
        )
        if indicator_keys_ok and score_keys_ok
        else []
    )

    findings_frame = pd.DataFrame([asdict(item) for item in findings], columns=FINDING_COLUMNS)
    composite_frame = pd.DataFrame.from_records(composite_records)
    alignment_frame = pd.DataFrame.from_records(alignment_records)
    blocking_count = sum(item.blocking for item in findings)
    status = "fail" if blocking_count else ("warn" if findings else "pass")

    output_dir.mkdir(parents=True, exist_ok=True)
    write_frame_csv_atomic(findings_frame, output_dir / "findings.csv")
    write_frame_csv_atomic(indicator_profile, output_dir / "indicator_profile.csv")
    write_frame_csv_atomic(score_profile, output_dir / "score_profile.csv")
    write_frame_csv_atomic(indicator_by_year, output_dir / "indicator_by_year.csv")
    write_frame_csv_atomic(score_by_year, output_dir / "score_by_year.csv")
    write_frame_csv_atomic(outliers, output_dir / "indicator_outliers.csv")
    write_frame_csv_atomic(composite_frame, output_dir / "composite_checks.csv")
    write_frame_csv_atomic(alignment_frame, output_dir / "score_indicator_alignment.csv")
    chart_targets = (
        output_dir / "indicator_missingness.png",
        output_dir / "score_distributions.png",
    )
    for chart_target in chart_targets:
        chart_target.unlink(missing_ok=True)
    charts = _write_charts(output_dir, indicator_profile, scores, keys) if create_charts else []

    summary = {
        "schema_version": 1,
        "country": iso3,
        "status": status,
        "grain": keys,
        "expected_years": expected_years,
        "static_year": static_year,
        "indicator_rows": len(indicators),
        "indicator_columns": len(indicators.columns),
        "score_rows": len(scores),
        "score_columns": len(scores.columns),
        "finding_count": len(findings),
        "blocking_finding_count": blocking_count,
        "severity_counts": {
            severity: sum(item.severity == severity for item in findings)
            for severity in ("critical", "high", "medium", "low", "info")
        },
        "inputs": {"indicators": str(indicators_path), "scores": str(scores_path)},
        "artifacts": sorted(path.name for path in output_dir.iterdir() if path.is_file()),
    }
    write_manifest(output_dir / "summary.json", summary)
    report = _render_report(
        status=status,
        iso3=iso3,
        indicator_rows=len(indicators),
        score_rows=len(scores),
        keys=keys,
        expected_years=expected_years,
        findings_frame=findings_frame,
        indicator_profile=indicator_profile,
        alignment_frame=alignment_frame,
        charts=charts,
    )
    _write_text_atomic(output_dir / "report.html", report)

    # Re-write the summary after every artifact exists so the inventory is complete.
    summary["artifacts"] = sorted(path.name for path in output_dir.iterdir() if path.is_file())
    write_manifest(output_dir / "summary.json", summary)
    return QualityResult(status, len(indicators), len(scores), tuple(findings), output_dir)


def run(ctx: RunContext, logger: logging.Logger) -> QualityResult:
    cfg = ctx.config
    indicators_path = cfg.dataset_dir / f"GPBP_LDT_{cfg.iso3}_admin_2.csv"
    scores_path = cfg.dataset_dir / f"GPBP_LDT_{cfg.iso3}_scores_admin_2.csv"
    output_dir = cfg.workspace / "quality"
    with logged_action(logger, "quality_read", domain="publication", path=str(cfg.dataset_dir)):
        if not indicators_path.is_file() or not scores_path.is_file():
            raise FileNotFoundError(
                f"Publication QA requires {indicators_path.name} and {scores_path.name}"
            )
    with logged_action(logger, "quality_analyze", domain="publication"):
        result = analyze_publication(
            indicators_path,
            scores_path,
            admin_columns=(cfg.admin1, cfg.admin2),
            expected_years=cfg.years("indicators"),
            static_year=int(cfg.data["years"]["static_merge"]),
            output_dir=output_dir,
            iso3=cfg.iso3,
            settings=dict(cfg.data.get("quality", {})),
        )
    logger.info(
        "quality report status=%s findings=%d blocking=%d",
        result.status,
        len(result.findings),
        result.blocking_count,
        extra={
            "action": "quality_summary",
            "domain": "publication",
            "path": str(output_dir / "report.html"),
            "status": result.status,
        },
    )
    if result.blocking_count:
        raise QualityGateError(
            f"Publication quality gate failed with {result.blocking_count} blocking finding(s); "
            f"review {output_dir / 'report.html'}"
        )
    return result
