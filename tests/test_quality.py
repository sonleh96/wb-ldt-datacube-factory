from __future__ import annotations

from pathlib import Path

import pandas as pd

from ldt_factory.quality import analyze_publication


KEYS = ["Province", "Municipality", "Year"]


def _write_valid_publication(tmp_path: Path) -> tuple[Path, Path]:
    indicators = pd.DataFrame(
        {
            "Province": ["North", "South", "North", "South"],
            "Municipality": ["A", "B", "A", "B"],
            "Year": [2021, 2021, 2022, 2022],
            "Average Broadband Internet Download Speed (Mbps)": [10.0, 20.0, 30.0, 40.0],
            "Average PM25 Concentration (ug/m3)": [40.0, 30.0, 20.0, 10.0],
        }
    )
    scores = indicators[KEYS].copy()
    scores["Broadband Internet Score"] = [25.0, 50.0, 75.0, 100.0]
    scores["Air Quality Score"] = [25.0, 50.0, 75.0, 100.0]
    scores["Infrastructure Score"] = scores["Broadband Internet Score"]
    scores["Livability Score"] = scores["Air Quality Score"]
    indicator_path = tmp_path / "indicators.csv"
    score_path = tmp_path / "scores.csv"
    indicators.to_csv(indicator_path, index=False)
    scores.to_csv(score_path, index=False)
    return indicator_path, score_path


def test_quality_analysis_writes_inspectable_artifacts(tmp_path: Path):
    indicators, scores = _write_valid_publication(tmp_path)
    output = tmp_path / "quality"

    result = analyze_publication(
        indicators,
        scores,
        admin_columns=("Province", "Municipality"),
        expected_years=[2021, 2022],
        static_year=2022,
        output_dir=output,
        iso3="TST",
        create_charts=False,
    )

    assert result.status == "pass"
    assert result.blocking_count == 0
    assert (output / "summary.json").is_file()
    assert (output / "report.html").is_file()
    assert (output / "indicator_profile.csv").is_file()
    alignment = pd.read_csv(output / "score_indicator_alignment.csv")
    assert set(alignment["status"]) == {"pass"}


def test_quality_analysis_blocks_invalid_scores_and_duplicate_grain(tmp_path: Path):
    indicators, scores = _write_valid_publication(tmp_path)
    score_frame = pd.read_csv(scores)
    score_frame.loc[0, "Broadband Internet Score"] = 101
    score_frame = pd.concat([score_frame, score_frame.iloc[[0]]], ignore_index=True)
    score_frame.to_csv(scores, index=False)

    result = analyze_publication(
        indicators,
        scores,
        admin_columns=("Province", "Municipality"),
        expected_years=[2021, 2022],
        static_year=2022,
        output_dir=tmp_path / "quality",
        iso3="TST",
        create_charts=False,
    )

    assert result.status == "fail"
    checks = {finding.check for finding in result.findings if finding.blocking}
    assert "key_uniqueness" in checks
    assert "score_range" in checks
    assert (tmp_path / "quality" / "report.html").is_file()


def test_static_zero_imputation_is_reported_without_being_silently_accepted(tmp_path: Path):
    indicators, scores = _write_valid_publication(tmp_path)
    indicator_frame = pd.read_csv(indicators)
    indicator_frame["Road Flood Risk (km)"] = [0.0, 0.0, 1.0, 2.0]
    indicator_frame.to_csv(indicators, index=False)

    result = analyze_publication(
        indicators,
        scores,
        admin_columns=("Province", "Municipality"),
        expected_years=[2021, 2022],
        static_year=2022,
        output_dir=tmp_path / "quality",
        iso3="TST",
        create_charts=False,
    )

    assert any(finding.check == "possible_zero_imputation" for finding in result.findings)
    assert result.blocking_count == 0


def test_inconsistent_derived_indicator_blocks_release(tmp_path: Path):
    indicators, scores = _write_valid_publication(tmp_path)
    indicator_frame = pd.read_csv(indicators)
    indicator_frame["Nighttime Luminosity"] = [100.0, 200.0, 300.0, 400.0]
    indicator_frame["Population"] = [10.0, 20.0, 30.0, 40.0]
    indicator_frame["Luminosity per Capita"] = [10.0, 10.0, 999.0, 10.0]
    indicator_frame.to_csv(indicators, index=False)

    result = analyze_publication(
        indicators,
        scores,
        admin_columns=("Province", "Municipality"),
        expected_years=[2021, 2022],
        static_year=2022,
        output_dir=tmp_path / "quality",
        iso3="TST",
        create_charts=False,
    )

    assert result.status == "fail"
    assert any(
        finding.check == "derived_value_consistency" and finding.blocking
        for finding in result.findings
    )
