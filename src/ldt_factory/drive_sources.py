from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import requests
from urllib3.util.retry import Retry

from .config import FactoryConfig
from .io_utils import download_file
from .locks import workspace_lock


DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_SOURCE_NAMES = ("heatwaves", "internet")
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


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    size: int
    md5_checksum: str
    mime_type: str
    modified_time: str


@dataclass(frozen=True)
class DriveSyncResult:
    source: str
    inventory_path: Path
    downloaded: int
    reused: int
    total_bytes: int


class DriveClient(Protocol):
    def list_folder(self, folder_id: str) -> list[DriveFile]: ...

    def download(self, remote: DriveFile, destination: Path, *, logger: Any) -> Path: ...


class GoogleDriveClient:
    """Small Drive v3 client backed by Application Default Credentials."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: tuple[int, int] = (15, 120),
        retries: int = 5,
        use_environment_proxy: bool = True,
    ):
        if session is None:
            try:
                import google.auth
                from google.auth.transport.requests import AuthorizedSession, Request
            except ImportError as error:
                raise RuntimeError(
                    "Google Drive support requires google-auth; install the project dependencies"
                ) from error
            credentials, _ = google.auth.default(scopes=[DRIVE_READONLY_SCOPE])
            auth_session = requests.Session()
            auth_session.trust_env = use_environment_proxy
            session = AuthorizedSession(
                credentials,
                auth_request=Request(auth_session),
                refresh_timeout=timeout[1],
            )
            retry = Retry(
                total=max(1, int(retries)),
                connect=max(1, int(retries)),
                read=max(1, int(retries)),
                status=max(1, int(retries)),
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET"}),
                backoff_factor=1,
            )
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=retry))
            session.trust_env = use_environment_proxy
        self.session = session
        self.timeout = timeout
        self.retries = max(1, int(retries))

    def list_folder(self, folder_id: str) -> list[DriveFile]:
        params: dict[str, Any] = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "nextPageToken,files(id,name,mimeType,size,md5Checksum,modifiedTime)",
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        files: list[DriveFile] = []
        while True:
            response = self.session.get(DRIVE_FILES_URL, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
            for item in payload.get("files", []):
                mime_type = str(item.get("mimeType", ""))
                if mime_type == "application/vnd.google-apps.folder":
                    continue
                try:
                    size = int(item["size"])
                except (KeyError, TypeError, ValueError) as error:
                    raise ValueError(f"Drive file {item.get('name')!r} has no byte size") from error
                checksum = str(item.get("md5Checksum", "")).lower()
                if not checksum:
                    raise ValueError(f"Drive file {item.get('name')!r} has no MD5 checksum")
                files.append(
                    DriveFile(
                        id=str(item["id"]),
                        name=str(item["name"]),
                        size=size,
                        md5_checksum=checksum,
                        mime_type=mime_type,
                        modified_time=str(item.get("modifiedTime", "")),
                    )
                )
            token = payload.get("nextPageToken")
            if not token:
                return files
            params["pageToken"] = str(token)

    def download(self, remote: DriveFile, destination: Path, *, logger: Any) -> Path:
        url = f"{DRIVE_FILES_URL}/{remote.id}?alt=media&supportsAllDrives=true"
        return download_file(
            url,
            destination,
            timeout=self.timeout,
            logger=logger,
            retries=self.retries,
            overwrite=True,
            session=self.session,
            source_identity={
                "drive_file_id": remote.id,
                "drive_md5": remote.md5_checksum,
                "drive_size": remote.size,
            },
        )


def configured_drive_sources(
    config: FactoryConfig, domains: set[str] | None = None
) -> tuple[str, ...]:
    selected = domains if domains is not None else set(config.pipeline.get("main_domains", []))
    return tuple(
        name
        for name in DRIVE_SOURCE_NAMES
        if name in selected and config.source(name).get("provider") == "google_drive"
    )


def source_cache_dir(config: FactoryConfig, source_name: str) -> Path:
    source = config.source(source_name)
    raw = source.get("cache_dir") or source.get("dataset_root")
    if not raw:
        raise ValueError(
            f"sources.{source_name} requires cache_dir"
            + (" or dataset_root" if source_name == "internet" else "")
        )
    return Path(str(raw)).expanduser().resolve()


def drive_inventory_path(config: FactoryConfig, source_name: str) -> Path:
    return source_cache_dir(config, source_name) / "drive_inventory.json"


def expected_drive_file_names(config: FactoryConfig, source_name: str) -> tuple[str, ...]:
    source = config.source(source_name)
    explicit = source.get("expected_files")
    if explicit is not None:
        if not isinstance(explicit, list) or not all(isinstance(item, str) and item for item in explicit):
            raise ValueError(f"sources.{source_name}.expected_files must be a list of filenames")
        return tuple(explicit)
    if source_name == "heatwaves":
        template = str(
            source.get(
                "filename_template",
                "gfdl-esm4_r1i1p1f1_w5e5_ssp585_tasmax_global_daily_{start}_{end}.nc",
            )
        )
        return tuple(template.format(start=start, end=end) for start, end in HEATWAVE_INTERVALS)
    if source_name == "internet":
        years = config.years("indicators")
        fixed = str(source.get("fixed_filename_template", "{year}_combined_fixed.parquet"))
        mobile = str(source.get("mobile_filename_template", "{year}_combined_mobile.parquet"))
        return tuple(
            template.format(year=year)
            for year in years
            for template in (fixed, mobile)
        )
    raise ValueError(f"Unsupported Google Drive source: {source_name}")


def _md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - Google Drive exposes MD5 for integrity checks
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _matches(path: Path, remote: DriveFile) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == remote.size
        and _md5(path).lower() == remote.md5_checksum.lower()
    )


def _record_matches_path(path: Path, remote: DriveFile, record: dict[str, Any] | None) -> bool:
    if not record or not path.is_file():
        return False
    stat = path.stat()
    return (
        str(record.get("id", "")) == remote.id
        and int(record.get("size", -1)) == remote.size == stat.st_size
        and str(record.get("md5_checksum", "")).lower() == remote.md5_checksum.lower()
        and int(record.get("local_mtime_ns", -1)) == stat.st_mtime_ns
    )


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _select_expected_files(
    listed: list[DriveFile], expected_names: tuple[str, ...], source_name: str
) -> list[DriveFile]:
    expected = set(expected_names)
    by_name: dict[str, DriveFile] = {}
    duplicates: list[str] = []
    for remote in listed:
        if remote.name not in expected:
            continue
        if remote.name in by_name:
            duplicates.append(remote.name)
        by_name[remote.name] = remote
    if duplicates:
        raise ValueError(
            f"Google Drive source {source_name} has duplicate expected filename(s): "
            + ", ".join(sorted(set(duplicates)))
        )
    missing = [name for name in expected_names if name not in by_name]
    if missing:
        raise ValueError(
            f"Google Drive source {source_name} is missing expected file(s): " + ", ".join(missing)
        )
    return [by_name[name] for name in expected_names]


def sync_drive_source(
    config: FactoryConfig,
    source_name: str,
    logger: Any,
    *,
    client: DriveClient | None = None,
) -> DriveSyncResult:
    source = config.source(source_name)
    if source.get("provider") != "google_drive":
        raise ValueError(f"sources.{source_name}.provider is not google_drive")
    folder_id = str(source.get("drive_folder_id", "")).strip()
    if not folder_id:
        raise ValueError(f"sources.{source_name}.drive_folder_id is required")
    cache_dir = source_cache_dir(config, source_name)
    cache_dir.mkdir(parents=True, exist_ok=True)
    active_client = client or GoogleDriveClient(
        timeout=(15, int(source.get("request_timeout_seconds", 120))),
        retries=int(source.get("download_retries", 5)),
        use_environment_proxy=bool(
            config.data.get("network", {}).get("use_environment_proxy", True)
        ),
    )
    inventory_path = drive_inventory_path(config, source_name)
    downloaded = 0
    reused = 0
    with workspace_lock(cache_dir / ".drive-sync.lock", label=f"{source_name} Drive cache"):
        try:
            previous_payload = json.loads(inventory_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            previous_payload = {}
        previous_records = {
            str(item.get("name")): item
            for item in previous_payload.get("files", [])
            if isinstance(item, dict)
        }
        remote_files = _select_expected_files(
            active_client.list_folder(folder_id),
            expected_drive_file_names(config, source_name),
            source_name,
        )
        records: list[dict[str, Any]] = []
        for remote in remote_files:
            destination = cache_dir / remote.name
            if _record_matches_path(destination, remote, previous_records.get(remote.name)) or _matches(
                destination, remote
            ):
                reused += 1
                logger.info("Drive file reused: %s", remote.name)
            else:
                logger.info("Drive file download started: %s (%d bytes)", remote.name, remote.size)
                active_client.download(remote, destination, logger=logger)
                if not _matches(destination, remote):
                    raise IOError(
                        f"Drive download failed integrity verification for {remote.name}: "
                        f"expected size={remote.size} md5={remote.md5_checksum}"
                    )
                downloaded += 1
                logger.info("Drive file verified: %s", remote.name)
            records.append(
                {
                    **asdict(remote),
                    "local_path": str(destination),
                    "local_mtime_ns": destination.stat().st_mtime_ns,
                }
            )

        payload = {
            "schema_version": 1,
            "source": source_name,
            "folder_id": folder_id,
            "synced_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "files": records,
        }
        _write_json_atomic(inventory_path, payload)
    return DriveSyncResult(
        source=source_name,
        inventory_path=inventory_path,
        downloaded=downloaded,
        reused=reused,
        total_bytes=sum(item.size for item in remote_files),
    )


def inspect_drive_source(
    config: FactoryConfig,
    source_name: str,
    *,
    client: DriveClient | None = None,
) -> list[DriveFile]:
    source = config.source(source_name)
    folder_id = str(source.get("drive_folder_id", "")).strip()
    if not folder_id:
        raise ValueError(f"sources.{source_name}.drive_folder_id is required")
    active_client = client or GoogleDriveClient(
        timeout=(15, int(source.get("request_timeout_seconds", 120))),
        retries=int(source.get("download_retries", 5)),
        use_environment_proxy=bool(
            config.data.get("network", {}).get("use_environment_proxy", True)
        ),
    )
    return _select_expected_files(
        active_client.list_folder(folder_id),
        expected_drive_file_names(config, source_name),
        source_name,
    )


def load_verified_drive_inventory(config: FactoryConfig, source_name: str) -> list[Path]:
    path = drive_inventory_path(config, source_name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as error:
        raise FileNotFoundError(
            f"Verified Drive inventory is missing or unreadable for {source_name}: {path}. "
            f"Run source.{source_name}.sync first."
        ) from error
    if payload.get("source") != source_name:
        raise ValueError(f"Drive inventory source mismatch in {path}")
    records = payload.get("files")
    if not isinstance(records, list):
        raise ValueError(f"Drive inventory files must be a list: {path}")
    by_name = {str(item.get("name")): item for item in records if isinstance(item, dict)}
    paths: list[Path] = []
    for name in expected_drive_file_names(config, source_name):
        item = by_name.get(name)
        if item is None:
            raise ValueError(f"Drive inventory is missing expected file {name}: {path}")
        local = Path(str(item.get("local_path", ""))).expanduser().resolve()
        remote = DriveFile(
            id=str(item.get("id", "")),
            name=name,
            size=int(item.get("size", -1)),
            md5_checksum=str(item.get("md5_checksum", "")),
            mime_type=str(item.get("mime_type", "")),
            modified_time=str(item.get("modified_time", "")),
        )
        if not _record_matches_path(local, remote, item) and not _matches(local, remote):
            raise IOError(f"Cached Drive file failed integrity verification: {local}")
        paths.append(local)
    return paths
