from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
import logging
import time
import uuid
from pathlib import Path
from typing import Iterable

import requests


def _read_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    with partial.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    partial.replace(path)


def download_file(
    url: str,
    destination: Path,
    *,
    timeout: tuple[int, int] = (15, 120),
    use_environment_proxy: bool = True,
    logger: logging.Logger | None = None,
    progress_interval_bytes: int = 64 * 1024 * 1024,
    retries: int = 4,
    overwrite: bool = False,
) -> Path:
    """Download a file atomically, resuming a retained ``.part`` with HTTP Range.

    A completed destination is reused unless ``overwrite`` is requested. Streaming
    failures retain the partial file and retry from its current byte offset when
    the origin supports range requests.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    retries = max(1, int(retries))
    if destination.exists() and not destination.is_file():
        raise IsADirectoryError(destination)
    final_metadata_path = destination.with_suffix(destination.suffix + ".download.json")
    completed_metadata = _read_json(final_metadata_path)
    if destination.is_file() and destination.stat().st_size > 0 and not overwrite:
        if not completed_metadata or completed_metadata.get("url") == url:
            if logger:
                logger.info(
                    "download reused existing file bytes=%d",
                    destination.stat().st_size,
                    extra={"action": "download_reuse", "path": str(destination)},
                )
            return destination
        if logger:
            logger.info(
                "download URL changed; existing destination will be replaced",
                extra={"action": "download_source_changed", "path": str(destination)},
            )
    if destination.exists() and destination.stat().st_size == 0:
        destination.unlink()

    partial = destination.with_suffix(destination.suffix + ".part")
    partial_metadata_path = partial.with_suffix(partial.suffix + ".json")
    partial_metadata = _read_json(partial_metadata_path)
    if partial_metadata and partial_metadata.get("url") != url:
        partial.unlink(missing_ok=True)
        partial_metadata_path.unlink(missing_ok=True)
        partial_metadata = {}

    with requests.Session() as session:
        session.trust_env = use_environment_proxy
        session.headers["Accept-Encoding"] = "identity"
        for attempt in range(1, retries + 1):
            existing = partial.stat().st_size if partial.is_file() else 0
            headers: dict[str, str] = {}
            if existing:
                headers["Range"] = f"bytes={existing}-"
                validator = partial_metadata.get("etag") or partial_metadata.get("last_modified")
                if validator:
                    headers["If-Range"] = str(validator)
            try:
                with session.get(url, headers=headers, stream=True, timeout=timeout) as response:
                    if response.status_code == 416 and existing:
                        match = re.search(r"\*/(\d+)", response.headers.get("Content-Range", ""))
                        if match and existing == int(match.group(1)):
                            partial.replace(destination)
                            _write_json_atomic(
                                final_metadata_path,
                                {**partial_metadata, "url": url, "size": existing},
                            )
                            partial_metadata_path.unlink(missing_ok=True)
                            return destination
                    response.raise_for_status()

                    append = existing > 0 and response.status_code == 206
                    if append:
                        content_range = response.headers.get("Content-Range", "")
                        match = re.match(r"bytes\s+(\d+)-\d+/(\d+|\*)", content_range)
                        if not match or int(match.group(1)) != existing:
                            raise IOError(
                                f"Server returned an invalid resume range for {destination}: {content_range!r}"
                            )
                    else:
                        existing = 0

                    content_length = int(response.headers.get("Content-Length", 0)) or None
                    total = existing + content_length if append and content_length else content_length
                    partial_metadata = {
                        "url": url,
                        "etag": response.headers.get("ETag"),
                        "last_modified": response.headers.get("Last-Modified"),
                        "total_bytes": total,
                    }
                    _write_json_atomic(partial_metadata_path, partial_metadata)
                    if logger:
                        logger.info(
                            "download response received status=%d resumed_bytes=%d total_bytes=%s attempt=%d",
                            response.status_code,
                            existing,
                            total,
                            attempt,
                            extra={"action": "download_response", "path": str(destination)},
                        )

                    downloaded = existing
                    next_progress = (
                        ((downloaded // progress_interval_bytes) + 1) * progress_interval_bytes
                    )
                    with partial.open("ab" if append else "wb") as output:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            output.write(chunk)
                            downloaded += len(chunk)
                            if logger and downloaded >= next_progress:
                                logger.info(
                                    "download progress bytes=%d total_bytes=%s",
                                    downloaded,
                                    total,
                                    extra={"action": "download_progress", "path": str(destination)},
                                )
                                next_progress += progress_interval_bytes
                    if total is not None and downloaded != total:
                        raise IOError(
                            f"Incomplete download for {destination}: received {downloaded} of {total} bytes"
                        )

                    partial.replace(destination)
                    _write_json_atomic(
                        final_metadata_path,
                        {**partial_metadata, "size": downloaded},
                    )
                    partial_metadata_path.unlink(missing_ok=True)
                    return destination
            except (requests.RequestException, OSError) as exc:
                if logger:
                    logger.warning(
                        "download attempt failed attempt=%d/%d retained_bytes=%d error=%s",
                        attempt,
                        retries,
                        partial.stat().st_size if partial.is_file() else 0,
                        exc,
                        extra={"action": "download_retry", "path": str(destination)},
                    )
                if attempt == retries:
                    raise
                time.sleep(min(2 ** (attempt - 1), 15))
    raise RuntimeError(f"Download retry loop ended unexpectedly for {destination}")


def extract_zip(archive: Path, destination: Path, *, overwrite: bool = False) -> Path:
    """Validate and atomically extract a ZIP, reusing a matching prior extraction."""
    archive_signature = {
        "archive": str(archive.resolve()),
        "size": archive.stat().st_size,
        "mtime_ns": archive.stat().st_mtime_ns,
    }
    marker = destination / ".ldt-extract.json"
    if destination.is_dir() and not overwrite and _read_json(marker) == archive_signature:
        return destination

    partial = destination.with_name(f"{destination.name}.part-{uuid.uuid4().hex}")
    partial.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive, "r") as package:
            corrupt = package.testzip()
            if corrupt:
                raise zipfile.BadZipFile(f"Corrupt member {corrupt!r} in {archive}")
            root = partial.resolve()
            for member in package.infolist():
                target = (partial / member.filename).resolve()
                if os.path.commonpath([str(root), str(target)]) != str(root):
                    raise zipfile.BadZipFile(f"Unsafe member path {member.filename!r} in {archive}")
            package.extractall(partial)
        _write_json_atomic(partial / ".ldt-extract.json", archive_signature)
        replace_directory(partial, destination)
    finally:
        if partial.exists():
            shutil.rmtree(partial)
    return destination


def require_files(paths: Iterable[Path], label: str) -> list[Path]:
    resolved = list(paths)
    missing = [str(path) for path in resolved if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing {label}: {', '.join(missing)}")
    return resolved


def replace_directory(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve():
        raise ValueError("Source and destination directories must differ")
    if destination.exists():
        shutil.rmtree(destination)
    source.replace(destination)
