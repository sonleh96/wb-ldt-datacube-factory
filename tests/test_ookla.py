from __future__ import annotations

import datetime as dt
import logging
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from ldt_factory.config import load_config
from ldt_factory.ookla import (
    build_ookla_year,
    ookla_quarter_filename,
    ookla_quarter_url,
)


def _logger() -> logging.Logger:
    logger = logging.getLogger("test.ookla")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def _config(tmp_path: Path):
    boundaries = {}
    for level in ("admin0", "admin1", "admin2"):
        path = tmp_path / f"{level}.geojson"
        path.write_text("{}", encoding="utf-8")
        boundaries[level] = str(path)
    payload = {
        "network": {"use_environment_proxy": False},
        "country": {"iso3": "TST", "name": "Testland"},
        "workspace": str(tmp_path / "workspace"),
        "boundaries": {
            **boundaries,
            "admin1_source_field": "NAME_1",
            "admin2_source_field": "NAME_2",
            "admin1_output_name": "Admin1",
            "admin2_output_name": "Admin2",
        },
        "sources": {
            "internet": {
                "provider": "local",
                "dataset_root": str(tmp_path / "ookla"),
                "raw_dir": str(tmp_path / "ookla" / "raw"),
                "fixed_filename_template": "{year}_combined_fixed.parquet",
                "mobile_filename_template": "{year}_combined_mobile.parquet",
                "download_retries": 1,
                "combine_batch_size": 2,
            }
        },
    }
    config_path = tmp_path / "country.yaml"
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return load_config(config_path)


def _quarter_files(tmp_path: Path, *, mismatched_quarter: int | None = None) -> dict[str, Path]:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    files = {}
    for network_type in ("fixed", "mobile"):
        for quarter in range(1, 5):
            values = {
                "quadkey": [f"{network_type}-{quarter}"],
                "tile": ["POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"],
                "avg_d_kbps": [quarter * 1000],
                "quarter": [quarter],
            }
            if quarter == mismatched_quarter and network_type == "fixed":
                values["unexpected"] = [True]
            path = source_dir / ookla_quarter_filename(2026, quarter, network_type)
            pq.write_table(pa.table(values), path)
            files[path.name] = path
    return files


def test_ookla_url_maps_quarters_to_valid_start_months():
    assert ookla_quarter_url(2026, 1, "mobile").endswith(
        "/type=mobile/year=2026/quarter=1/2026-01-01_performance_mobile_tiles.parquet"
    )
    assert ookla_quarter_url(2026, 4, "fixed").endswith(
        "/type=fixed/year=2026/quarter=4/2026-10-01_performance_fixed_tiles.parquet"
    )
    with pytest.raises(ValueError, match="quarter must be one of"):
        ookla_quarter_url(2026, 5, "fixed")
    with pytest.raises(ValueError, match="network type must be one of"):
        ookla_quarter_url(2026, 1, "satellite")


def test_build_ookla_year_streams_both_types_and_removes_raw_files(tmp_path, monkeypatch):
    config = _config(tmp_path)
    sources = _quarter_files(tmp_path)

    def fake_download(url, destination, **_kwargs):
        shutil.copyfile(sources[url.rsplit("/", 1)[-1]], destination)
        destination.with_suffix(destination.suffix + ".download.json").write_text(
            "{}", encoding="utf-8"
        )
        return destination

    monkeypatch.setattr("ldt_factory.ookla.download_file", fake_download)
    result = build_ookla_year(
        config,
        2026,
        _logger(),
        today=dt.date(2027, 1, 1),
    )

    assert result.downloaded == 8
    assert result.reused == 0
    assert result.rows == 8
    assert [path.name for path in result.outputs] == [
        "2026_combined_fixed.parquet",
        "2026_combined_mobile.parquet",
    ]
    for output in result.outputs:
        combined = pq.read_table(output)
        assert combined.column("quarter").to_pylist() == [1, 2, 3, 4]
        assert combined.num_rows == 4
    assert not Path(config.source("internet")["raw_dir"]).exists()


def test_build_ookla_year_retains_raw_files_and_no_output_on_schema_mismatch(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    sources = _quarter_files(tmp_path, mismatched_quarter=4)

    def fake_download(url, destination, **_kwargs):
        shutil.copyfile(sources[url.rsplit("/", 1)[-1]], destination)
        return destination

    monkeypatch.setattr("ldt_factory.ookla.download_file", fake_download)
    with pytest.raises(ValueError, match="schema mismatch"):
        build_ookla_year(
            config,
            2026,
            _logger(),
            network_types=("fixed",),
            today=dt.date(2027, 1, 1),
        )

    root = Path(config.source("internet")["dataset_root"])
    assert not (root / "2026_combined_fixed.parquet").exists()
    assert len(list((root / "raw").glob("*.parquet"))) == 4


def test_build_ookla_year_requires_a_completed_year_by_default(tmp_path):
    with pytest.raises(ValueError, match="not complete"):
        build_ookla_year(
            _config(tmp_path),
            2026,
            _logger(),
            today=dt.date(2026, 12, 31),
        )
