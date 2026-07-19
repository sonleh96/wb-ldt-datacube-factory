from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path


IMPORT_DISTRIBUTIONS = {
    "ee": "earthengine-api",
    "geemap": "geemap",
    "geopandas": "geopandas",
    "google": "google-auth",
    "matplotlib": "matplotlib",
    "netCDF4": "netcdf4",
    "numba": "numba",
    "numpy": "numpy",
    "pandas": "pandas",
    "pyarrow": "pyarrow",
    "pyogrio": "pyogrio",
    "pyquadkey2": "pyquadkey2",
    "rasterio": "rasterio",
    "rasterstats": "rasterstats",
    "requests": "requests",
    "rioxarray": "rioxarray",
    "shapely": "shapely",
    "urllib3": "urllib3",
    "xarray": "xarray",
    "yaml": "pyyaml",
}


def _normalized_distribution(specification: str) -> str:
    match = re.match(r"[A-Za-z0-9_.-]+", specification)
    assert match, f"Cannot parse dependency specification: {specification!r}"
    return match.group(0).lower().replace("_", "-")


def test_every_direct_import_has_a_declared_dependency():
    project_root = Path(__file__).parents[1]
    project = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    specifications = list(project.get("dependencies", []))
    for values in project.get("optional-dependencies", {}).values():
        specifications.extend(values)
    declared = {_normalized_distribution(value) for value in specifications}

    imported: set[str] = set()
    for path in (project_root / "src" / "ldt_factory").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])

    third_party = imported - set(sys.stdlib_module_names) - {"ldt_factory"}
    unmapped = third_party - set(IMPORT_DISTRIBUTIONS)
    assert not unmapped, f"Direct imports need distribution mappings: {sorted(unmapped)}"

    missing = {
        module: distribution
        for module, distribution in IMPORT_DISTRIBUTIONS.items()
        if module in third_party and distribution not in declared
    }
    assert not missing, f"Direct imports need declared dependencies: {missing}"
