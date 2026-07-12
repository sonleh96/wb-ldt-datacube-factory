from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import requests


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write a JSON cache record without exposing a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


class RatePacer:
    """Reserve globally paced request slots across all worker threads."""

    def __init__(self, requests_per_minute: int) -> None:
        self.interval = 60.0 / max(1, requests_per_minute)
        self._next_request = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._next_request)
            self._next_request = scheduled + self.interval
        delay = scheduled - now
        if delay > 0:
            time.sleep(delay)


class ThreadLocalSessions:
    """Provide one persistent requests session per API worker thread."""

    def __init__(self, *, trust_env: bool) -> None:
        self._trust_env = trust_env
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._lock = threading.Lock()

    def get(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.trust_env = self._trust_env
            session.headers.update({"User-Agent": "wb-ldt-datacube-factory/0.1"})
            self._local.session = session
            with self._lock:
                self._sessions.append(session)
        return session

    def close(self) -> None:
        for session in self._sessions:
            session.close()


def request_json_with_retry(
    session: requests.Session,
    url: str,
    *,
    params: Mapping[str, Any],
    pacer: RatePacer,
    timeout: tuple[int, int] | int,
    max_retries: int,
    backoff_seconds: float,
    logger: logging.Logger,
    domain: str,
) -> dict[str, Any]:
    """Issue a paced request and retry only transient transport/API failures."""
    retry_statuses = {429, 500, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        pacer.wait()
        retry_delay: float | None = None
        try:
            with session.get(url, params=params, timeout=timeout) as response:
                if response.status_code in retry_statuses:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            retry_delay = max(0.0, float(retry_after))
                        except ValueError:
                            retry_delay = None
                    response.raise_for_status()
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("API response was not a JSON object")
                return payload
        except requests.HTTPError as exc:
            last_error = exc
            status = exc.response.status_code if exc.response is not None else None
            if status not in retry_statuses or attempt >= max_retries:
                raise
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt >= max_retries:
                raise

        delay = retry_delay if retry_delay is not None else backoff_seconds * (2**attempt)
        logger.warning(
            "transient API failure; retrying attempt=%d/%d delay_seconds=%.1f error=%s",
            attempt + 1,
            max_retries,
            delay,
            last_error,
            extra={"action": "api_retry", "domain": domain},
        )
        if delay > 0:
            time.sleep(delay)

    assert last_error is not None
    raise last_error


def resolve_assets_path(shape_dir: Path) -> Path:
    parquet = shape_dir / "assets.parquet"
    return parquet if parquet.is_file() else shape_dir / "assets.geojson"


def build_accessibility_entries(
    assets: Any,
    *,
    distance_meters: int,
    profile: str,
) -> list[dict[str, Any]]:
    """Create unique, deterministic API requests without changing union semantics."""
    unique: dict[str, dict[str, Any]] = {}
    for row in assets.itertuples(index=False):
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        category = "hospital" if getattr(row, "amenity", None) == "hospital" else "school"
        point = geometry.centroid
        # Twelve decimal places make GeoJSON/GeoParquet round trips stable while
        # retaining materially the same request origin as the source geometry.
        lon = round(float(point.x), 12)
        lat = round(float(point.y), 12)
        identity = f"{profile}|{distance_meters}|{category}|{lon:.12f}|{lat:.12f}"
        cache_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        unique.setdefault(
            cache_key,
            {
                "cache_key": cache_key,
                "category": category,
                "lon": lon,
                "lat": lat,
            },
        )
    return [unique[key] for key in sorted(unique)]


def accessibility_signature(
    entries: Sequence[Mapping[str, Any]],
    *,
    distance_meters: int,
    profile: str,
) -> str:
    canonical = {
        "profile": profile,
        "distance_meters": distance_meters,
        "entries": list(entries),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
