from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest
import yaml

from ldt_factory.config import ConfigError, load_config
from ldt_factory.drive_sources import (
    DriveFile,
    GoogleDriveClient,
    expected_drive_file_names,
    load_verified_drive_inventory,
    sync_drive_source,
)
from ldt_factory.inspection import build_plan


HEATWAVE_INTERVALS = (
    (2015, 2020),
    (2021, 2030),
    (2031, 2040),
    (2041, 2050),
    (2051, 2060),
    (2061, 2070),
    (2071, 2080),
    (2081, 2090),
    (2091, 2100),
)


def _logger() -> logging.Logger:
    logger = logging.getLogger("test.drive-sources")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def _config(tmp_path: Path, *, domains: list[str] | None = None):
    boundaries = {}
    for level in ("admin0", "admin1", "admin2"):
        path = tmp_path / f"{level}.geojson"
        path.write_text("{}", encoding="utf-8")
        boundaries[level] = str(path)
    payload = {
        "country": {"iso3": "TST", "name": "Testland"},
        "workspace": str(tmp_path / "workspace"),
        "boundaries": {
            **boundaries,
            "admin1_source_field": "NAME_1",
            "admin2_source_field": "NAME_2",
            "admin1_output_name": "Admin1",
            "admin2_output_name": "Admin2",
        },
        "years": {"indicators": [2021, 2022, 2023, 2024, 2025]},
        "sources": {
            "heatwaves": {
                "provider": "google_drive",
                "drive_folder_id": "heatwave-folder",
                "cache_dir": str(tmp_path / "shared" / "heatwaves"),
            },
            "internet": {
                "provider": "google_drive",
                "drive_folder_id": "ookla-folder",
                "dataset_root": str(tmp_path / "shared" / "ookla"),
                "fixed_filename_template": "{year}_combined_fixed.parquet",
                "mobile_filename_template": "{year}_combined_mobile.parquet",
            },
        },
        "pipeline": {"main_domains": domains or ["heatwaves", "internet"]},
    }
    path = tmp_path / "country.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return load_config(path)


class FakeDriveClient:
    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        self.downloads: list[str] = []

    def list_folder(self, folder_id: str) -> list[DriveFile]:
        return [
            DriveFile(
                id=f"id-{index}",
                name=name,
                size=len(content),
                md5_checksum=hashlib.md5(content).hexdigest(),  # noqa: S324 - Drive supplies MD5 metadata
                mime_type="application/octet-stream",
                modified_time="2026-01-01T00:00:00Z",
            )
            for index, (name, content) in enumerate(sorted(self.files.items()))
        ]

    def download(self, remote: DriveFile, destination: Path, *, logger: logging.Logger) -> Path:
        self.downloads.append(remote.name)
        partial = destination.with_suffix(destination.suffix + ".part")
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(self.files[remote.name])
        partial.replace(destination)
        return destination


def _heatwave_files() -> dict[str, bytes]:
    return {
        f"gfdl-esm4_r1i1p1f1_w5e5_ssp585_tasmax_global_daily_{start}_{end}.nc": (
            f"netcdf-{start}-{end}".encode()
        )
        for start, end in HEATWAVE_INTERVALS
    }


def _internet_files() -> dict[str, bytes]:
    return {
        f"{year}_combined_{network}.parquet": f"{network}-{year}".encode()
        for year in range(2021, 2026)
        for network in ("fixed", "mobile")
    }


def test_expected_file_contracts_cover_all_heatwave_intervals_and_ookla_years(tmp_path):
    config = _config(tmp_path)

    heatwaves = expected_drive_file_names(config, "heatwaves")
    internet = expected_drive_file_names(config, "internet")

    assert heatwaves == tuple(_heatwave_files())
    assert len(heatwaves) == 9
    assert internet == tuple(_internet_files())
    assert len(internet) == 10


def test_drive_source_configuration_requires_folder_and_shared_cache(tmp_path):
    config = _config(tmp_path)
    payload = yaml.safe_load(config.path.read_text(encoding="utf-8"))
    del payload["sources"]["heatwaves"]["drive_folder_id"]
    del payload["sources"]["heatwaves"]["cache_dir"]
    config.path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="drive_folder_id.*cache_dir"):
        load_config(config.path)


def test_sync_writes_verified_inventory_reuses_cache_and_repairs_corruption(tmp_path):
    config = _config(tmp_path)
    client = FakeDriveClient(_internet_files())

    result = sync_drive_source(config, "internet", _logger(), client=client)
    assert result.downloaded == 10
    assert result.reused == 0
    assert len(client.downloads) == 10

    inventory = json.loads(result.inventory_path.read_text(encoding="utf-8"))
    assert inventory["source"] == "internet"
    assert inventory["folder_id"] == "ookla-folder"
    assert len(inventory["files"]) == 10
    assert all(Path(item["local_path"]).is_file() for item in inventory["files"])

    client.downloads.clear()
    second = sync_drive_source(config, "internet", _logger(), client=client)
    assert second.downloaded == 0
    assert second.reused == 10
    assert client.downloads == []

    corrupt = Path(inventory["files"][0]["local_path"])
    corrupt.write_bytes(b"corrupt")
    third = sync_drive_source(config, "internet", _logger(), client=client)
    assert third.downloaded == 1
    assert third.reused == 9
    assert client.downloads == [corrupt.name]

    verified = load_verified_drive_inventory(config, "internet")
    assert len(verified) == 10
    assert corrupt in verified


def test_sync_redownloads_remote_replacement_with_same_name(tmp_path):
    config = _config(tmp_path)
    files = _internet_files()
    client = FakeDriveClient(files)
    sync_drive_source(config, "internet", _logger(), client=client)

    changed_name = "2023_combined_fixed.parquet"
    client.files[changed_name] = b"replacement-content"
    client.downloads.clear()
    result = sync_drive_source(config, "internet", _logger(), client=client)

    assert result.downloaded == 1
    assert client.downloads == [changed_name]
    assert (Path(config.source("internet")["dataset_root"]) / changed_name).read_bytes() == b"replacement-content"


def test_sync_rejects_missing_or_duplicate_expected_files(tmp_path):
    config = _config(tmp_path)
    files = _heatwave_files()
    files.pop(next(iter(files)))
    with pytest.raises(ValueError, match="missing expected file"):
        sync_drive_source(config, "heatwaves", _logger(), client=FakeDriveClient(files))

    class DuplicateClient(FakeDriveClient):
        def list_folder(self, folder_id: str) -> list[DriveFile]:
            listed = super().list_folder(folder_id)
            return [*listed, listed[0]]

    with pytest.raises(ValueError, match="duplicate expected filename"):
        sync_drive_source(config, "heatwaves", _logger(), client=DuplicateClient(_heatwave_files()))


def test_google_drive_listing_follows_pagination_without_exposing_credentials():
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if len(self.calls) == 1:
                return Response(
                    {
                        "nextPageToken": "page-2",
                        "files": [
                            {
                                "id": "one",
                                "name": "one.bin",
                                "size": "3",
                                "md5Checksum": "abc",
                                "mimeType": "application/octet-stream",
                                "modifiedTime": "2026-01-01T00:00:00Z",
                            }
                        ],
                    }
                )
            return Response({"files": []})

    session = Session()
    files = GoogleDriveClient(session=session).list_folder("public-folder")

    assert [item.name for item in files] == ["one.bin"]
    assert session.calls[0][1]["params"]["q"] == "'public-folder' in parents and trashed = false"
    assert session.calls[1][1]["params"]["pageToken"] == "page-2"


def test_plan_places_drive_sources_before_domain_extraction(tmp_path):
    config = _config(tmp_path)
    payload = build_plan(config)
    stages = {stage["name"]: stage["tasks"] for stage in payload["stages"]}

    assert [task["task_id"] for task in stages["sources"]] == [
        "source.heatwaves.sync",
        "source.internet.sync",
    ]
    domain_tasks = {task["task_id"]: task for task in stages["domains"]}
    assert "source.heatwaves.sync" in domain_tasks["domain.heatwaves.extract"]["depends_on"]
    assert "source.internet.sync" in domain_tasks["domain.internet.extract"]["depends_on"]
