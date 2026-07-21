from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import mimetypes
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import google_crc32c
import requests

from ..domains._api_cache import atomic_write_json
from ..logging_utils import logged_action
from .config import DevelopmentPlanConfig
from .discovery import candidates_from_response, load_area_selection
from .models import PlanCandidate
from .registry import AdminArea
from .scoring import FORMAL_STATUSES


class AcquisitionError(RuntimeError):
    """Raised when an approved document cannot be safely acquired or published."""


@dataclass(frozen=True)
class DownloadedDocument:
    path: Path
    final_url: str
    content_type: str
    extension: str
    size: int
    sha256: str
    crc32c: str


@dataclass(frozen=True)
class PublishedDocument:
    object_name: str
    manifest_name: str
    latest_name: str
    generation: int
    crc32c: str
    size: int


class ContentClient(Protocol):
    def contents(self, urls: list[str], *, query: str, fresh: bool = True) -> dict[str, Any]: ...


class ObjectStore(Protocol):
    def publish(
        self,
        local_path: Path,
        *,
        object_name: str,
        manifest_name: str,
        latest_name: str,
        manifest: dict[str, Any],
        latest: dict[str, Any],
        crc32c: str,
        size: int,
    ) -> PublishedDocument: ...

    def verify(self, object_name: str, *, crc32c: str, size: int) -> bool: ...


def _crc32c(path: Path) -> str:
    checksum = google_crc32c.Checksum()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            checksum.update(chunk)
    return base64.b64encode(checksum.digest()).decode("ascii")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def detect_document_type(path: Path) -> tuple[str, str]:
    with path.open("rb") as handle:
        header = handle.read(16)
    if header.startswith(b"%PDF-"):
        return ".pdf", "application/pdf"
    if header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return ".doc", "application/msword"
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as package:
            names = set(package.namelist())
            if "[Content_Types].xml" in names and "word/document.xml" in names:
                return ".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    raise AcquisitionError(f"Downloaded content is not a supported PDF or Word document: {path}")


def download_document(
    url: str,
    destination: Path,
    *,
    timeout_seconds: int,
    retries: int,
    max_file_bytes: int,
    min_file_bytes: int,
    use_environment_proxy: bool,
    logger: logging.Logger,
) -> DownloadedDocument:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    last_error: Exception | None = None
    final_url = url
    content_type = "application/octet-stream"
    with requests.Session() as session:
        session.trust_env = use_environment_proxy
        session.headers.update({"User-Agent": "wb-ldt-datacube-factory/0.1"})
        for attempt in range(1, retries + 1):
            try:
                with session.get(url, stream=True, timeout=(15, timeout_seconds)) as response:
                    response.raise_for_status()
                    final_url = response.url
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
                    declared_size = int(response.headers.get("Content-Length", 0) or 0)
                    if declared_size > max_file_bytes:
                        raise AcquisitionError(
                            f"Document exceeds configured maximum of {max_file_bytes} bytes: {declared_size}"
                        )
                    written = 0
                    with partial.open("wb") as handle:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            written += len(chunk)
                            if written > max_file_bytes:
                                raise AcquisitionError(
                                    f"Document exceeded configured maximum of {max_file_bytes} bytes while downloading"
                                )
                            handle.write(chunk)
                    if written < min_file_bytes:
                        raise AcquisitionError(
                            f"Document is smaller than configured minimum of {min_file_bytes} bytes: {written}"
                        )
                    partial.replace(destination)
                    extension, detected_type = detect_document_type(destination)
                    if content_type in {"", "application/octet-stream"}:
                        content_type = detected_type
                    return DownloadedDocument(
                        destination,
                        final_url,
                        content_type,
                        extension,
                        destination.stat().st_size,
                        _sha256(destination),
                        _crc32c(destination),
                    )
            except (requests.RequestException, OSError, AcquisitionError) as error:
                last_error = error
                logger.warning(
                    "approved document download failed attempt=%d/%d error=%s",
                    attempt,
                    retries,
                    error,
                    extra={"action": "development_plan_download_retry", "path": str(destination)},
                )
                if attempt == retries:
                    break
                time.sleep(min(2 ** (attempt - 1), 15))
    raise AcquisitionError(f"Failed to download approved document {url}: {last_error}")


def validate_selected_url(
    config: DevelopmentPlanConfig,
    area: AdminArea,
    url: str,
    *,
    client: ContentClient,
    current_year: int,
) -> PlanCandidate:
    query = (
        f"Fresh validation of the selected full development plan for {area.name}, "
        f"{area.parent}, {config.country_name}. Verify exact jurisdiction, full document type, "
        "formal status, plan period, authority, and direct document URL."
    )
    response = client.contents([url], query=query, fresh=True)
    statuses = response.get("statuses", [])
    if isinstance(statuses, list):
        failures = [
            str(item.get("error", {}).get("tag") or "unknown")
            for item in statuses
            if isinstance(item, dict) and item.get("status") == "error"
        ]
        if failures:
            raise AcquisitionError(f"Fresh URL validation failed for {area.admin2_id}: {', '.join(failures)}")
    candidates = candidates_from_response(
        config,
        area,
        pass_number=3,
        request={"query": query},
        response=response,
        current_year=current_year,
    )
    if not candidates:
        raise AcquisitionError(f"Fresh URL validation returned no document for {area.admin2_id}: {url}")
    candidate = candidates[0]
    hard_failures = []
    if not candidate.jurisdiction_match:
        hard_failures.append("jurisdiction_unverified")
    if candidate.document_type != "development_plan":
        hard_failures.append("wrong_or_unverified_document_type")
    if not candidate.is_full_document:
        hard_failures.append("not_verified_full_document")
    if (
        config.review.get("require_formal_adoption_for_auto_accept", True)
        and candidate.document_status not in FORMAL_STATUSES
    ):
        hard_failures.append(f"status_{candidate.document_status}")
    if hard_failures:
        raise AcquisitionError(
            f"Fresh validation rejected {area.admin2_id}: {', '.join(hard_failures)}"
        )
    return candidate


class GCSObjectStore:
    def __init__(self, bucket_name: str, *, client: Any | None = None):
        if client is None:
            from google.cloud import storage

            client = storage.Client()
        self.bucket = client.bucket(bucket_name)

    def verify(self, object_name: str, *, crc32c: str, size: int) -> bool:
        blob = self.bucket.blob(object_name)
        try:
            blob.reload()
        except Exception as error:
            if type(error).__name__ == "NotFound":
                return False
            raise
        return blob.crc32c == crc32c and int(blob.size or 0) == size

    def _upload_json(self, name: str, payload: dict[str, Any], *, replace: bool) -> int:
        blob = self.bucket.blob(name)
        generation = 0
        if replace:
            try:
                blob.reload()
                generation = int(blob.generation)
            except Exception as error:
                if type(error).__name__ != "NotFound":
                    raise
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        blob.upload_from_string(
            encoded,
            content_type="application/json",
            if_generation_match=generation,
        )
        blob.reload()
        return int(blob.generation)

    def publish(
        self,
        local_path: Path,
        *,
        object_name: str,
        manifest_name: str,
        latest_name: str,
        manifest: dict[str, Any],
        latest: dict[str, Any],
        crc32c: str,
        size: int,
    ) -> PublishedDocument:
        blob = self.bucket.blob(object_name)
        if not self.verify(object_name, crc32c=crc32c, size=size):
            try:
                blob.upload_from_filename(
                    str(local_path),
                    if_generation_match=0,
                    checksum="crc32c",
                    content_type=str(manifest.get("content_type") or "application/octet-stream"),
                )
            except Exception as error:
                if type(error).__name__ != "PreconditionFailed" or not self.verify(
                    object_name,
                    crc32c=crc32c,
                    size=size,
                ):
                    raise
        blob.reload()
        if blob.crc32c != crc32c or int(blob.size or 0) != size:
            raise AcquisitionError(f"GCS checksum verification failed for gs://{self.bucket.name}/{object_name}")

        manifest_blob = self.bucket.blob(manifest_name)
        try:
            self._upload_json(manifest_name, manifest, replace=False)
        except Exception as error:
            if type(error).__name__ != "PreconditionFailed":
                raise
            manifest_blob.reload()
        self._upload_json(latest_name, latest, replace=True)
        return PublishedDocument(
            object_name=object_name,
            manifest_name=manifest_name,
            latest_name=latest_name,
            generation=int(blob.generation),
            crc32c=str(blob.crc32c),
            size=int(blob.size),
        )


def load_approved_decisions(
    config: DevelopmentPlanConfig,
    run_id: str,
    areas: list[AdminArea],
) -> list[dict[str, Any]]:
    path = config.run_dir(run_id) / "decisions" / "reviewed.json"
    decisions: dict[str, dict[str, Any]] = {}
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("decisions"), list):
            raise AcquisitionError(f"Invalid reviewed decisions file: {path}")
        decisions = {
            str(item["admin2_id"]): dict(item)
            for item in payload["decisions"]
            if isinstance(item, dict) and item.get("decision") == "APPROVE"
        }
    if bool(config.review.get("auto_accept_enabled", False)):
        for area in areas:
            if area.admin2_id in decisions:
                continue
            try:
                selection, _ = load_area_selection(config, run_id, area)
            except FileNotFoundError:
                continue
            if selection.proposed_decision == "AUTO_ACCEPT" and selection.selected:
                decisions[area.admin2_id] = {
                    "admin2_id": area.admin2_id,
                    "admin2_name": area.name,
                    "parent": area.parent,
                    "storage_slug": area.storage_slug,
                    "decision": "APPROVE",
                    "selected_url": selection.selected.identity_url,
                    "replacement_url": "",
                    "reviewer_notes": "Automatic acceptance enabled by configuration",
                    "pinned": False,
                    "reviewed_at": "",
                }
    return [decisions[key] for key in sorted(decisions)]


def _safe_period(candidate: PlanCandidate) -> str:
    start = str(candidate.start_year) if candidate.start_year else "unknown"
    end = str(candidate.end_year) if candidate.end_year else "unknown"
    return f"{start}-{end}"


def acquire_approved(
    config: DevelopmentPlanConfig,
    areas: list[AdminArea],
    *,
    run_id: str,
    content_client: ContentClient,
    object_store: ObjectStore,
    logger: logging.Logger,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    today = today or dt.date.today()
    config.prepare_run(run_id)
    decisions = load_approved_decisions(config, run_id, areas)
    area_by_id = {area.admin2_id: area for area in areas}
    acquisition = config.acquisition
    report_path = config.run_dir(run_id) / "reports" / "acquisition.json"
    completed: list[dict[str, Any]] = []
    if report_path.is_file():
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        completed = list(payload.get("documents", [])) if isinstance(payload, dict) else []
    completed_by_id = {str(item.get("admin2_id")): item for item in completed}

    for decision in decisions:
        admin2_id = str(decision["admin2_id"])
        area = area_by_id.get(admin2_id)
        if area is None:
            raise AcquisitionError(f"Reviewed decision references unknown admin2_id {admin2_id}")
        previous = completed_by_id.get(admin2_id)
        if previous and object_store.verify(
            str(previous["object_name"]),
            crc32c=str(previous["crc32c"]),
            size=int(previous["size"]),
        ):
            previous_local = Path(str(previous.get("local_path") or ""))
            if previous_local.is_file():
                previous_local.unlink()
            continue

        selected_url = str(decision["selected_url"])
        with logged_action(
            logger,
            "development_plan_fresh_validation",
            domain="development_plans",
            phase="acquire",
            task_id=admin2_id,
        ):
            candidate = validate_selected_url(
                config,
                area,
                selected_url,
                client=content_client,
                current_year=today.year,
            )
        download_url = candidate.document_url or candidate.landing_url
        stage_path = config.run_dir(run_id) / "downloads" / f"{admin2_id}.download"
        with logged_action(
            logger,
            "development_plan_download",
            domain="development_plans",
            phase="acquire",
            task_id=admin2_id,
            path=str(stage_path),
        ):
            document = download_document(
                download_url,
                stage_path,
                timeout_seconds=int(acquisition["request_timeout_seconds"]),
                retries=int(acquisition["download_retries"]),
                max_file_bytes=int(acquisition["max_file_bytes"]),
                min_file_bytes=int(acquisition["min_file_bytes"]),
                use_environment_proxy=bool(config.search.get("use_environment_proxy", False)),
                logger=logger,
            )
        filename = (
            f"development_plan_{_safe_period(candidate)}_{document.sha256[:8]}{document.extension}"
        )
        base = f"{config.storage_prefix}/{area.storage_slug}"
        object_name = f"{base}/{filename}"
        manifest_name = f"{base}/{filename}.json"
        latest_name = f"{base}/latest.json"
        retrieved_at = dt.datetime.now(dt.timezone.utc).isoformat()
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "country": config.iso3,
            "admin2": asdict(area),
            "review_decision": decision,
            "selected_url": selected_url,
            "download_url": download_url,
            "final_url": document.final_url,
            "candidate": candidate.to_dict(),
            "retrieved_at": retrieved_at,
            "sha256": document.sha256,
            "crc32c": document.crc32c,
            "size": document.size,
            "content_type": document.content_type or mimetypes.guess_type(filename)[0],
            "object_name": object_name,
        }
        latest = {
            "schema_version": 1,
            "object_name": object_name,
            "manifest_name": manifest_name,
            "sha256": document.sha256,
            "crc32c": document.crc32c,
            "size": document.size,
            "start_year": candidate.start_year,
            "end_year": candidate.end_year,
            "updated_at": retrieved_at,
            "run_id": run_id,
        }
        with logged_action(
            logger,
            "development_plan_gcs_publish",
            domain="development_plans",
            phase="acquire",
            task_id=admin2_id,
            path=f"gs://{config.bucket}/{object_name}",
        ):
            published = object_store.publish(
                document.path,
                object_name=object_name,
                manifest_name=manifest_name,
                latest_name=latest_name,
                manifest=manifest,
                latest=latest,
                crc32c=document.crc32c,
                size=document.size,
            )
        record = {
            "admin2_id": admin2_id,
            "admin2_name": area.name,
            "status": "UPLOADED_VERIFIED",
            **asdict(published),
            "sha256": document.sha256,
            "selected_url": selected_url,
            "local_path": str(document.path),
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        completed_by_id[admin2_id] = record
        completed = [completed_by_id[key] for key in sorted(completed_by_id)]
        atomic_write_json(
            report_path,
            {
                "schema_version": 1,
                "run_id": run_id,
                "country": config.iso3,
                "documents": completed,
            },
        )
        document.path.unlink()
        partial = document.path.with_suffix(document.path.suffix + ".part")
        partial.unlink(missing_ok=True)
        logger.info(
            "verified upload complete; local document deleted",
            extra={
                "action": "development_plan_local_cleanup",
                "domain": "development_plans",
                "phase": "acquire",
                "task_id": admin2_id,
                "path": str(document.path),
            },
        )
    return [completed_by_id[key] for key in sorted(completed_by_id)]
