from __future__ import annotations

import datetime as dt
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import FactoryConfig
from .io_utils import download_file
from .locks import workspace_lock
from .logging_utils import logged_action


OOKLA_BASE_URL = "https://ookla-open-data.s3.amazonaws.com/parquet/performance"
OOKLA_NETWORK_TYPES = ("fixed", "mobile")
QUARTER_START_MONTHS = {1: 1, 2: 4, 3: 7, 4: 10}
REQUIRED_COLUMNS = {"quadkey", "tile", "avg_d_kbps"}


@dataclass(frozen=True)
class OoklaYearResult:
    year: int
    network_types: tuple[str, ...]
    outputs: tuple[Path, ...]
    downloaded: int
    reused: int
    rows: int


def _validate_network_type(network_type: str) -> str:
    normalized = str(network_type).strip().lower()
    if normalized not in OOKLA_NETWORK_TYPES:
        raise ValueError(
            "Ookla network type must be one of: " + ", ".join(OOKLA_NETWORK_TYPES)
        )
    return normalized


def ookla_quarter_filename(year: int, quarter: int, network_type: str) -> str:
    network_type = _validate_network_type(network_type)
    try:
        month = QUARTER_START_MONTHS[int(quarter)]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Ookla quarter must be one of: 1, 2, 3, 4") from error
    return f"{int(year):04d}-{month:02d}-01_performance_{network_type}_tiles.parquet"


def ookla_quarter_url(year: int, quarter: int, network_type: str) -> str:
    network_type = _validate_network_type(network_type)
    filename = ookla_quarter_filename(year, quarter, network_type)
    return (
        f"{OOKLA_BASE_URL}/type={network_type}/year={int(year):04d}/"
        f"quarter={int(quarter)}/{filename}"
    )


def _combined_path(config: FactoryConfig, year: int, network_type: str) -> Path:
    source = config.source("internet")
    template = str(
        source.get(
            f"{network_type}_filename_template",
            f"{{year}}_combined_{network_type}.parquet",
        )
    )
    return Path(str(source["dataset_root"])) / template.format(year=year)


def _raw_dir(config: FactoryConfig) -> Path:
    source = config.source("internet")
    if not source.get("dataset_root"):
        raise ValueError("sources.internet.dataset_root is required")
    configured = source.get("raw_dir")
    if configured:
        return Path(str(configured))
    return Path(str(source["dataset_root"])) / "raw"


def _parquet_schema(path: Path) -> Any:
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(path)
    missing = REQUIRED_COLUMNS - set(parquet.schema.names)
    if missing:
        raise ValueError(f"{path} is missing Ookla columns: {sorted(missing)}")
    return parquet.schema_arrow


def combine_ookla_quarters(
    quarterly_paths: Iterable[Path],
    destination: Path,
    *,
    batch_size: int = 131_072,
) -> int:
    import pyarrow.parquet as pq

    paths = tuple(quarterly_paths)
    if len(paths) != 4:
        raise ValueError(f"A yearly Ookla file requires exactly 4 quarters, got {len(paths)}")
    if batch_size < 1:
        raise ValueError("Ookla Parquet batch size must be at least 1")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    writer = None
    expected_schema = None
    row_count = 0
    try:
        for path in paths:
            parquet = pq.ParquetFile(path)
            schema = _parquet_schema(path)
            if expected_schema is None:
                expected_schema = schema
                writer = pq.ParquetWriter(temporary, expected_schema, compression="snappy")
            elif not schema.equals(expected_schema, check_metadata=False):
                raise ValueError(
                    f"Ookla quarterly Parquet schema mismatch: {path} does not match {paths[0]}"
                )
            for batch in parquet.iter_batches(batch_size=batch_size, use_threads=True):
                writer.write_batch(batch)
                row_count += batch.num_rows
        if writer is None or row_count == 0:
            raise ValueError("Ookla quarterly Parquet files contain no rows")
        writer.close()
        writer = None
        os.replace(temporary, destination)
    finally:
        if writer is not None:
            writer.close()
        temporary.unlink(missing_ok=True)
    return row_count


def _cleanup_download(path: Path) -> None:
    for candidate in (
        path,
        path.with_suffix(path.suffix + ".download.json"),
        path.with_suffix(path.suffix + ".part"),
        path.with_suffix(path.suffix + ".part.json"),
    ):
        candidate.unlink(missing_ok=True)


def _validate_year(year: int, *, allow_incomplete_year: bool, today: dt.date | None = None) -> int:
    year = int(year)
    if year < 2026:
        raise ValueError("Ookla yearly acquisition is intended for 2026 and later")
    current_year = (today or dt.date.today()).year
    if not allow_incomplete_year and year >= current_year:
        raise ValueError(
            f"Ookla year {year} is not complete. Wait until {year + 1}, or use "
            "--allow-incomplete-year to attempt all four quarter URLs explicitly."
        )
    return year


def build_ookla_year(
    config: FactoryConfig,
    year: int,
    logger: Any,
    *,
    network_types: Iterable[str] = OOKLA_NETWORK_TYPES,
    allow_incomplete_year: bool = False,
    force: bool = False,
    today: dt.date | None = None,
) -> OoklaYearResult:
    year = _validate_year(
        year,
        allow_incomplete_year=allow_incomplete_year,
        today=today,
    )
    selected = tuple(dict.fromkeys(_validate_network_type(item) for item in network_types))
    if not selected:
        raise ValueError("At least one Ookla network type is required")

    source = config.source("internet")
    raw_dir = _raw_dir(config)
    raw_dir.mkdir(parents=True, exist_ok=True)
    timeout = (15, int(source.get("request_timeout_seconds", 120)))
    retries = int(source.get("download_retries", 5))
    use_environment_proxy = bool(
        config.data.get("network", {}).get("use_environment_proxy", True)
    )
    batch_size = int(source.get("combine_batch_size", 131_072))
    downloaded = 0
    reused = 0
    rows = 0
    outputs: list[Path] = []
    raw_paths: list[Path] = []

    lock_path = Path(str(source["dataset_root"])) / ".ookla-year.lock"
    with workspace_lock(lock_path, label="Ookla yearly acquisition"):
        for network_type in selected:
            destination = _combined_path(config, year, network_type)
            expected_raw_paths = [
                raw_dir / ookla_quarter_filename(year, quarter, network_type)
                for quarter in QUARTER_START_MONTHS
            ]
            raw_paths.extend(expected_raw_paths)
            if destination.is_file() and not force:
                _parquet_schema(destination)
                logger.info(
                    "Ookla yearly file reused: %s",
                    destination,
                    extra={"action": "reuse_year", "path": str(destination)},
                )
                outputs.append(destination)
                continue

            quarterly_paths: list[Path] = []
            for quarter, path in zip(QUARTER_START_MONTHS, expected_raw_paths, strict=True):
                existed = False
                if path.is_file() and path.stat().st_size > 0:
                    try:
                        _parquet_schema(path)
                        existed = True
                    except (OSError, ValueError):
                        _cleanup_download(path)
                with logged_action(
                    logger,
                    "download_ookla_quarter",
                    domain="internet",
                    phase=f"{year}-{network_type}-q{quarter}",
                    path=str(path),
                ):
                    download_file(
                        ookla_quarter_url(year, quarter, network_type),
                        path,
                        timeout=timeout,
                        use_environment_proxy=use_environment_proxy,
                        logger=logger,
                        retries=retries,
                    )
                    _parquet_schema(path)
                reused += int(existed)
                downloaded += int(not existed)
                quarterly_paths.append(path)

            with logged_action(
                logger,
                "combine_ookla_year",
                domain="internet",
                phase=f"{year}-{network_type}",
                path=str(destination),
            ):
                rows += combine_ookla_quarters(
                    quarterly_paths,
                    destination,
                    batch_size=batch_size,
                )
            outputs.append(destination)

        for path in raw_paths:
            _cleanup_download(path)
        try:
            raw_dir.rmdir()
        except OSError:
            pass

    return OoklaYearResult(
        year=year,
        network_types=selected,
        outputs=tuple(outputs),
        downloaded=downloaded,
        reused=reused,
        rows=rows,
    )
