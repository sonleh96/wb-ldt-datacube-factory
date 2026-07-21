from __future__ import annotations

import base64
import csv
import datetime as dt
import hashlib
import json
import logging
from pathlib import Path

import google_crc32c
import pytest
import yaml

from ldt_factory.plans.acquisition import (
    AcquisitionError,
    DownloadedDocument,
    PublishedDocument,
    acquire_approved,
    detect_document_type,
    validate_selected_url,
)
from ldt_factory.plans.config import load_plan_config
from ldt_factory.plans.registry import load_admin_registry


def _logger() -> logging.Logger:
    logger = logging.getLogger("test.plan-acquisition")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def _config(tmp_path: Path):
    registry_path = tmp_path / "admin2.csv"
    with registry_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["code", "name", "parent"])
        writer.writeheader()
        writer.writerow({"code": "TST001", "name": "Central", "parent": "North"})
    payload = {
        "schema_version": 1,
        "country": {
            "iso3": "TST",
            "name": "Testland",
            "languages": ["en"],
            "admin2_types": ["district"],
        },
        "workspace": "workflow",
        "admin_registry": {
            "path": registry_path.name,
            "id_field": "code",
            "name_field": "name",
            "parent_field": "parent",
            "alias_fields": [],
        },
        "search": {
            "api_key_env": "EXA_API_KEY",
            "minimum_passes": 2,
            "max_passes": 3,
            "num_results": 10,
            "request_timeout_seconds": 30,
            "document_terms": ["integrated development plan"],
            "final_terms": ["approved"],
            "draft_terms": ["draft"],
            "official_domain_suffixes": ["gov.test"],
        },
        "review": {
            "auto_accept_threshold": 0.9,
            "minimum_margin": 0.1,
            "require_formal_adoption_for_auto_accept": True,
            "auto_accept_enabled": False,
        },
        "acquisition": {
            "request_timeout_seconds": 30,
            "download_retries": 2,
            "min_file_bytes": 10,
            "max_file_bytes": 1000000,
        },
        "storage": {"bucket": "bucket", "prefix": "plans/districts"},
    }
    path = tmp_path / "plans.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return load_plan_config(path, data_root=tmp_path)


def _content_response(*, status: str = "approved"):
    return {
        "requestId": "contents-1",
        "statuses": [{"id": "https://central.gov.test/plan.pdf", "status": "success"}],
        "results": [
            {
                "title": "Central District Integrated Development Plan 2023-2033",
                "url": "https://central.gov.test/plan.pdf",
                "highlights": ["Approved Central District Integrated Development Plan 2023-2033"],
                "summary": {
                    "jurisdiction": "Central District",
                    "jurisdiction_match": True,
                    "is_development_plan": True,
                    "is_full_document": True,
                    "status": status,
                    "start_year": "2023",
                    "end_year": "2033",
                    "adoption_date": "2023-06-01",
                    "issuing_authority": "Central District Council",
                    "direct_document_url": "https://central.gov.test/plan.pdf",
                },
                "extras": {"links": []},
            }
        ],
    }


class FakeContentClient:
    def __init__(self, *, status: str = "approved"):
        self.status = status
        self.calls = 0

    def contents(self, urls, *, query, fresh=True):
        self.calls += 1
        assert fresh is True
        return _content_response(status=self.status)


class FakeObjectStore:
    def __init__(self):
        self.objects: dict[str, tuple[str, int]] = {}
        self.manifests = {}
        self.latest = {}
        self.publish_calls = 0

    def verify(self, object_name, *, crc32c, size):
        return self.objects.get(object_name) == (crc32c, size)

    def publish(
        self,
        local_path,
        *,
        object_name,
        manifest_name,
        latest_name,
        manifest,
        latest,
        crc32c,
        size,
    ):
        assert local_path.is_file()
        self.publish_calls += 1
        self.objects[object_name] = (crc32c, size)
        self.manifests[manifest_name] = manifest
        self.latest[latest_name] = latest
        return PublishedDocument(object_name, manifest_name, latest_name, 1, crc32c, size)


def _write_approval(config, run_id: str):
    config.prepare_run(run_id)
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "country": "TST",
        "decisions": [
            {
                "admin2_id": "TST001",
                "admin2_name": "Central",
                "parent": "North",
                "storage_slug": "central",
                "decision": "APPROVE",
                "selected_url": "https://central.gov.test/plan.pdf",
                "replacement_url": "",
                "reviewer_notes": "approved",
                "pinned": True,
                "reviewed_at": "2026-01-01T00:00:00+00:00",
            }
        ],
    }
    path = config.run_dir(run_id) / "decisions" / "reviewed.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_acquisition_publishes_verified_artifacts_then_deletes_local_copy(tmp_path: Path, monkeypatch):
    config = _config(tmp_path)
    areas = load_admin_registry(config)
    _write_approval(config, "run-1")
    staged_paths = []

    def fake_download(url, destination, **_kwargs):
        content = b"%PDF-1.7\n" + b"plan" * 20
        destination.write_bytes(content)
        staged_paths.append(destination)
        crc = google_crc32c.Checksum(content).digest()
        return DownloadedDocument(
            destination,
            url,
            "application/pdf",
            ".pdf",
            len(content),
            hashlib.sha256(content).hexdigest(),
            base64.b64encode(crc).decode("ascii"),
        )

    monkeypatch.setattr("ldt_factory.plans.acquisition.download_document", fake_download)
    content_client = FakeContentClient()
    store = FakeObjectStore()
    completed = acquire_approved(
        config,
        areas,
        run_id="run-1",
        content_client=content_client,
        object_store=store,
        logger=_logger(),
        today=dt.date(2026, 7, 21),
    )

    assert len(completed) == 1
    assert completed[0]["status"] == "UPLOADED_VERIFIED"
    assert completed[0]["object_name"].startswith("plans/districts/central/development_plan_2023-2033_")
    assert not staged_paths[0].exists()
    assert store.publish_calls == 1
    assert store.latest["plans/districts/central/latest.json"]["object_name"] == completed[0]["object_name"]

    acquire_approved(
        config,
        areas,
        run_id="run-1",
        content_client=content_client,
        object_store=store,
        logger=_logger(),
    )
    assert content_client.calls == 1
    assert store.publish_calls == 1


def test_fresh_validation_rejects_draft_even_after_review_approval(tmp_path: Path):
    config = _config(tmp_path)
    area = load_admin_registry(config)[0]
    with pytest.raises(AcquisitionError, match="status_draft"):
        validate_selected_url(
            config,
            area,
            "https://central.gov.test/plan.pdf",
            client=FakeContentClient(status="draft"),
            current_year=2026,
        )


def test_document_type_detection_rejects_html_disguised_as_pdf(tmp_path: Path):
    path = tmp_path / "fake.pdf"
    path.write_text("<html>Not a PDF</html>", encoding="utf-8")
    with pytest.raises(AcquisitionError, match="not a supported"):
        detect_document_type(path)
