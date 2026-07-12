from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Iterable


def path_signature(path: Path, *, shapefile_family: bool = False) -> dict[str, Any]:
    """Return a cheap, deterministic signature suitable for restart checkpoints."""
    resolved = path.expanduser().resolve()
    paths: Iterable[Path]
    if shapefile_family and resolved.suffix.lower() == ".shp":
        paths = sorted(
            candidate
            for candidate in resolved.parent.glob(f"{resolved.stem}.*")
            if candidate.is_file()
        )
    else:
        paths = [resolved]
    entries = []
    for candidate in paths:
        stat = candidate.stat()
        entries.append(
            {
                "path": str(candidate),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    if not entries:
        raise FileNotFoundError(resolved)
    return {"files": entries}


def fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.part-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def checkpoint_matches(output: Path, manifest: Path, expected: dict[str, Any]) -> bool:
    if not output.is_file() or output.stat().st_size == 0:
        return False
    recorded = read_manifest(manifest)
    return recorded.get("fingerprint") == fingerprint(expected)


def write_checkpoint_manifest(manifest: Path, expected: dict[str, Any], **details: Any) -> None:
    write_manifest(manifest, {"fingerprint": fingerprint(expected), "inputs": expected, **details})


def write_frame_parquet_atomic(
    frame,
    path: Path,
    *,
    index: bool = False,
    row_group_size: int | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.stem}.part-{uuid.uuid4().hex}{path.suffix}")
    try:
        options = {"row_group_size": row_group_size} if row_group_size else {}
        frame.to_parquet(temporary, index=index, **options)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_frame_csv_atomic(frame, path: Path, *, index: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.part-{uuid.uuid4().hex}")
    try:
        frame.to_csv(temporary, index=index)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
