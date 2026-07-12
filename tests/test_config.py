from pathlib import Path

import pytest
import yaml

from ldt_factory.config import ConfigError, load_config


def _base(tmp_path: Path):
    boundaries = {}
    for level in ("admin0", "admin1", "admin2"):
        path = tmp_path / f"{level}.geojson"
        path.write_text("{}", encoding="utf-8")
        boundaries[level] = str(path)
    return {
        "country": {"iso3": "ROU", "name": "Romania"},
        "workspace": str(tmp_path / "workspace"),
        "boundaries": {
            **boundaries,
            "admin1_source_field": "NAME_1",
            "admin2_source_field": "NAME_2",
            "admin1_output_name": "County",
            "admin2_output_name": "Municipality",
        },
    }


def _write(tmp_path: Path, data) -> Path:
    path = tmp_path / "country.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_load_config_resolves_contract(tmp_path):
    config = load_config(_write(tmp_path, _base(tmp_path)))
    assert config.iso3 == "ROU"
    assert config.admin1 == "County"
    assert config.admin2 == "Municipality"
    assert config.workspace == (tmp_path / "workspace").resolve()


def test_load_config_rejects_same_admin_names(tmp_path):
    data = _base(tmp_path)
    data["boundaries"]["admin2_output_name"] = "County"
    with pytest.raises(ConfigError, match="must differ"):
        load_config(_write(tmp_path, data))


def test_load_config_rejects_non_iso3(tmp_path):
    data = _base(tmp_path)
    data["country"]["iso3"] = "Romania"
    with pytest.raises(ConfigError, match="ISO-3"):
        load_config(_write(tmp_path, data))
