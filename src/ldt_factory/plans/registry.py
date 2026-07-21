from __future__ import annotations

import csv
import hashlib
import re
import unicodedata
from dataclasses import dataclass

from .config import DevelopmentPlanConfig, PlanConfigError


@dataclass(frozen=True)
class AdminArea:
    admin2_id: str
    name: str
    parent: str
    aliases: tuple[str, ...]
    storage_slug: str


def _slugify(value: str, *, mode: str) -> str:
    normalized = value.strip().lower()
    if mode == "transliterate":
        normalized = unicodedata.normalize("NFKD", normalized).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    if not slug:
        raise PlanConfigError(f"cannot derive a storage slug from {value!r}")
    return slug


def _derived_id(iso3: str, parent: str, name: str) -> str:
    identity = f"{iso3}|{parent.casefold().strip()}|{name.casefold().strip()}"
    return f"{iso3}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]}"


def _split_aliases(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[|;]", value) if part.strip()]


def load_admin_registry(config: DevelopmentPlanConfig) -> list[AdminArea]:
    registry = config.registry
    name_field = str(registry["name_field"])
    parent_field = str(registry.get("parent_field") or "")
    id_field = str(registry.get("id_field") or "")
    slug_field = str(registry.get("storage_slug_field") or "")
    alias_fields = tuple(str(value) for value in registry.get("alias_fields", ()))
    mode = str(registry.get("slug_mode", "ascii_replace"))

    with config.registry_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or ())
        required = {name_field, *(value for value in (parent_field, id_field, slug_field) if value)}
        required.update(alias_fields)
        missing = sorted(required - headers)
        if missing:
            raise PlanConfigError(
                f"admin registry {config.registry_path} is missing columns: {', '.join(missing)}"
            )
        rows = list(reader)

    areas_by_id: dict[str, AdminArea] = {}
    slugs: dict[str, str] = {}
    for row_number, row in enumerate(rows, start=2):
        name = str(row.get(name_field) or "").strip()
        if not name:
            raise PlanConfigError(f"admin registry row {row_number} has no {name_field!r}")
        parent = str(row.get(parent_field) or "").strip() if parent_field else ""
        admin2_id = str(row.get(id_field) or "").strip() if id_field else ""
        admin2_id = admin2_id or _derived_id(config.iso3, parent, name)
        raw_slug = str(row.get(slug_field) or "").strip() if slug_field else name
        storage_slug = _slugify(raw_slug, mode=mode)

        aliases: list[str] = []
        for field in alias_fields:
            aliases.extend(_split_aliases(str(row.get(field) or "")))
        aliases = [alias for alias in dict.fromkeys(aliases) if alias.casefold() != name.casefold()]
        area = AdminArea(admin2_id, name, parent, tuple(aliases), storage_slug)

        current = areas_by_id.get(admin2_id)
        if current and current != area:
            raise PlanConfigError(
                f"admin registry ID {admin2_id!r} has inconsistent rows: {current!r} and {area!r}"
            )
        slug_owner = slugs.get(storage_slug)
        if slug_owner and slug_owner != admin2_id:
            raise PlanConfigError(
                f"storage slug {storage_slug!r} is shared by {slug_owner!r} and {admin2_id!r}"
            )
        areas_by_id[admin2_id] = area
        slugs[storage_slug] = admin2_id

    if not areas_by_id:
        raise PlanConfigError(f"admin registry is empty: {config.registry_path}")
    return sorted(areas_by_id.values(), key=lambda area: (area.parent.casefold(), area.name.casefold()))
