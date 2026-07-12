from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ...context import RunContext
from ...io_utils import require_files
from ...logging_utils import logged_action
from .._api_cache import (
    RatePacer,
    ThreadLocalSessions,
    accessibility_signature,
    atomic_write_json,
    build_accessibility_entries,
    read_json,
    request_json_with_retry,
    resolve_assets_path,
)


def _cache_path(root: Path, entry: dict[str, Any]) -> Path:
    return root / "isochrones" / f"{entry['cache_key']}.json"


def _valid_cache(path: Path, entry: dict[str, Any]) -> bool:
    try:
        payload = read_json(path)
    except (OSError, ValueError):
        return False
    geometry = payload.get("geometry")
    return (
        payload.get("schema_version") == 1
        and payload.get("status") == "complete"
        and payload.get("cache_key") == entry["cache_key"]
        and payload.get("category") == entry["category"]
        and (geometry is None or isinstance(geometry, dict))
    )


def _manifest_payload(
    *,
    entries: list[dict[str, Any]],
    completed: int,
    distance: int,
    profile: str,
    signature: str,
    assets_path: Path,
) -> dict[str, Any]:
    expected = len(entries)
    return {
        "schema_version": 1,
        "status": "complete" if completed == expected else "in_progress",
        "profile": profile,
        "distance_meters": distance,
        "assets_signature": signature,
        "assets_path": str(assets_path),
        "expected_requests": expected,
        "completed_requests": completed,
        "pending_requests": expected - completed,
        "entries": entries,
    }


def run(ctx: RunContext, logger: logging.Logger) -> None:
    import geopandas as gpd

    source = ctx.config.source("mapbox")
    api_key_env = source.get("access_token_env")
    if not api_key_env:
        raise ValueError("sources.mapbox.access_token_env is required")
    distance = int(source.get("walking_distance_meters", 10000))
    profile = str(source.get("profile", "walking"))
    rpm = max(1, int(source.get("requests_per_minute", 60)))
    max_workers = max(1, int(source.get("max_workers", 4)))
    max_retries = max(0, int(source.get("max_retries", 4)))
    backoff_seconds = max(0.0, float(source.get("retry_backoff_seconds", 2.0)))
    manifest_interval = max(1, int(source.get("manifest_update_interval", 25)))
    trust_env = bool(ctx.config.data.get("network", {}).get("use_environment_proxy", True))

    assets_path = resolve_assets_path(ctx.config.shape_dir)
    require_files([assets_path], "key-assets output")
    with logged_action(logger, "load_assets", domain="accessibility", path=str(assets_path)):
        assets = gpd.read_parquet(assets_path) if assets_path.suffix == ".parquet" else gpd.read_file(assets_path)
        assets = assets.to_crs("EPSG:4326")
        entries = build_accessibility_entries(
            assets,
            distance_meters=distance,
            profile=profile,
        )

    root = ctx.raw("accessibility")
    manifest_path = root / "manifest.json"
    signature = accessibility_signature(entries, distance_meters=distance, profile=profile)
    completed = sum(_valid_cache(_cache_path(root, entry), entry) for entry in entries)
    atomic_write_json(
        manifest_path,
        _manifest_payload(
            entries=entries,
            completed=completed,
            distance=distance,
            profile=profile,
            signature=signature,
            assets_path=assets_path,
        ),
    )
    pending = [entry for entry in entries if not _valid_cache(_cache_path(root, entry), entry)]
    logger.info(
        "accessibility cache status expected=%d completed=%d pending=%d unique_from_assets=%d",
        len(entries),
        completed,
        len(pending),
        len(assets),
        extra={"action": "cache_status", "domain": "accessibility", "path": str(manifest_path)},
    )
    if not pending:
        return

    token = ctx.require_env(str(api_key_env))
    pacer = RatePacer(rpm)
    sessions = ThreadLocalSessions(trust_env=trust_env)
    abort = threading.Event()

    def acquire(entry: dict[str, Any]) -> None:
        if abort.is_set():
            raise RuntimeError("Accessibility extraction was cancelled after another request failed")
        output = _cache_path(root, entry)
        with logged_action(
            logger,
            "request",
            domain="accessibility",
            phase=entry["cache_key"],
            path=str(output),
        ):
            payload = request_json_with_retry(
                sessions.get(),
                f"https://api.mapbox.com/isochrone/v1/mapbox/{profile}/{entry['lon']},{entry['lat']}",
                params={
                    "contours_meters": distance,
                    "polygons": "true",
                    "access_token": token,
                },
                pacer=pacer,
                timeout=(15, 120),
                max_retries=max_retries,
                backoff_seconds=backoff_seconds,
                logger=logger,
                domain="accessibility",
            )
            features = payload.get("features") or []
            geometry = features[0].get("geometry") if features else None
            if geometry is not None and not isinstance(geometry, dict):
                raise ValueError("Mapbox isochrone geometry was not a JSON object")
            atomic_write_json(
                output,
                {
                    "schema_version": 1,
                    "status": "complete",
                    **entry,
                    "profile": profile,
                    "distance_meters": distance,
                    "geometry": geometry,
                },
            )

    first_error: Exception | None = None
    try:
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mapbox") as executor:
            futures = {executor.submit(acquire, entry): entry for entry in pending}
            for future in as_completed(futures):
                entry = futures[future]
                try:
                    future.result()
                    completed += 1
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                        abort.set()
                        logger.error(
                            "accessibility extraction stopped after cache_key=%s failed: %s",
                            entry["cache_key"],
                            exc,
                            extra={"action": "request_failed", "domain": "accessibility"},
                        )
                        for other in futures:
                            if other is not future:
                                other.cancel()
                if completed % manifest_interval == 0 or first_error is not None:
                    atomic_write_json(
                        manifest_path,
                        _manifest_payload(
                            entries=entries,
                            completed=completed,
                            distance=distance,
                            profile=profile,
                            signature=signature,
                            assets_path=assets_path,
                        ),
                    )
                    logger.info(
                        "accessibility extraction progress completed=%d total=%d",
                        completed,
                        len(entries),
                        extra={"action": "progress", "domain": "accessibility"},
                    )
                if first_error is not None:
                    break
    finally:
        sessions.close()
        completed = sum(_valid_cache(_cache_path(root, entry), entry) for entry in entries)
        atomic_write_json(
            manifest_path,
            _manifest_payload(
                entries=entries,
                completed=completed,
                distance=distance,
                profile=profile,
                signature=signature,
                assets_path=assets_path,
            ),
        )

    if first_error is not None:
        raise RuntimeError(
            f"Accessibility extraction stopped with {len(entries) - completed} pending request(s); rerun to resume"
        ) from first_error
