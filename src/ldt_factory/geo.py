from __future__ import annotations

from pathlib import Path

from .config import FactoryConfig


def load_admin0(config: FactoryConfig):
    """Load only the country boundary when lower administrative levels are unnecessary."""
    import geopandas as gpd

    admin0 = gpd.read_file(config.boundary_path("admin0"))
    if admin0.crs is None:
        raise ValueError("The configured admin-0 boundary file must declare a CRS")
    return admin0


def load_admin2(config: FactoryConfig):
    """Load and normalize only the admin-2 boundary fields used by processing jobs."""
    import geopandas as gpd

    fields = config.data["boundaries"]
    source1 = fields["admin1_source_field"]
    source2 = fields["admin2_source_field"]
    admin2 = gpd.read_file(
        config.boundary_path("admin2"),
        columns=[source1, source2],
        use_arrow=True,
    )
    if admin2.crs is None:
        raise ValueError("The configured admin-2 boundary file must declare a CRS")
    missing = [name for name in (source1, source2) if name not in admin2.columns]
    if missing:
        raise ValueError(f"Admin-2 boundary is missing configured fields: {missing}")
    admin2 = admin2[[source1, source2, "geometry"]].rename(
        columns={source1: config.admin1, source2: config.admin2}
    )
    if admin2.duplicated([config.admin1, config.admin2]).any():
        raise ValueError("Admin-2 composite names are not unique")
    return admin2


def load_boundaries(config: FactoryConfig):
    import geopandas as gpd

    admin0 = load_admin0(config)
    admin1 = gpd.read_file(config.boundary_path("admin1"))
    admin2 = load_admin2(config)
    if admin1.crs is None:
        raise ValueError("Every configured boundary file must declare a CRS")
    return admin0, admin1, admin2


def write_normalized_boundaries(config: FactoryConfig) -> Path:
    admin2 = load_admin2(config)
    destination = config.dataset_dir / f"GPBP_LDT_{config.iso3}_admin_2_regions.geojson"
    destination.parent.mkdir(parents=True, exist_ok=True)
    admin2.to_crs("EPSG:4326").to_file(destination, driver="GeoJSON", index=False)
    return destination


def osm_layer(config: FactoryConfig, filename: str) -> Path:
    osm = config.source("osm")
    return config.raw_dir / str(osm["extracted_dir"]) / filename


def read_osm_filtered(
    path: Path,
    *,
    category_column: str,
    categories: set[str],
    columns: list[str] | None = None,
    case_insensitive: bool = True,
):
    """Read only matching OSM features using OGR attribute-filter pushdown."""
    import geopandas as gpd
    import pyogrio

    info = pyogrio.read_info(path)
    available = set(info["fields"])
    if category_column not in available:
        raise ValueError(f"{path.name} lacks {category_column!r}")
    selected_columns = [
        name for name in (columns or [category_column]) if name in available
    ]
    if category_column not in selected_columns:
        selected_columns.append(category_column)
    escaped_values = [value.replace("'", "''") for value in sorted(categories)]
    if case_insensitive:
        predicates = [
            f'"{category_column}" ILIKE \'{value}\'' for value in escaped_values
        ]
        where = " OR ".join(predicates) if predicates else "1 = 0"
    else:
        values = ", ".join(f"'{value}'" for value in escaped_values)
        where = f'"{category_column}" IN ({values})' if values else "1 = 0"
    return gpd.read_file(
        path,
        columns=selected_columns,
        where=where,
        use_arrow=True,
    )
