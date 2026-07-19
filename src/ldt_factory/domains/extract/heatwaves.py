from __future__ import annotations

import glob
import logging
from pathlib import Path

from ...context import RunContext
from ...drive_sources import load_verified_drive_inventory
from ...geo import load_admin0
from ...logging_utils import logged_action
from ...raster_utils import atomic_output_path


def _spatial_dimensions(raster) -> tuple[str, str]:
    x_name = next((name for name in ("x", "lon", "longitude") if name in raster.dims), None)
    y_name = next((name for name in ("y", "lat", "latitude") if name in raster.dims), None)
    if not x_name or not y_name:
        raise ValueError(f"Cannot identify spatial dimensions: {raster.dims}")
    return x_name, y_name


def _coordinate_slice(values, lower: float, upper: float) -> slice:
    return slice(lower, upper) if float(values[0]) <= float(values[-1]) else slice(upper, lower)


def _validate_clipped(path: Path, variable: str) -> dict[str, int]:
    import xarray as xr

    with xr.open_dataset(path) as dataset:
        if variable not in dataset:
            raise ValueError(f"{path} does not contain configured variable {variable!r}")
        raster = dataset[variable]
        x_name, y_name = _spatial_dimensions(raster)
        if "time" not in raster.dims or not raster.sizes.get("time"):
            raise ValueError(f"Clipped heatwave file has no time values: {path}")
        if not raster.sizes.get(x_name) or not raster.sizes.get(y_name):
            raise ValueError(f"Clipped heatwave file has an empty spatial grid: {path}")
        return {name: int(size) for name, size in raster.sizes.items()}


def run(ctx: RunContext, logger: logging.Logger) -> None:
    # Import the NetCDF backend before GDAL-backed libraries. Some Windows
    # conda combinations otherwise emit a harmless NumPy C-ABI warning on the
    # first delayed backend import even though read/write operations succeed.
    import netCDF4  # noqa: F401
    import rioxarray  # noqa: F401 - registers the xarray .rio accessor
    import xarray as xr

    source = ctx.config.source("heatwaves")
    if source.get("provider") == "google_drive":
        paths = load_verified_drive_inventory(ctx.config, "heatwaves")
    else:
        paths = [Path(item) for item in sorted(glob.glob(str(source["source_glob"])))]
    if not paths:
        raise FileNotFoundError(
            f"No heatwave netCDFs available from {source.get('source_glob', 'Drive inventory')}"
        )
    admin0 = load_admin0(ctx.config)
    boundary = admin0.to_crs("EPSG:4326")
    minx, miny, maxx, maxy = boundary.total_bounds
    output_dir = ctx.raw("heatwaves")
    variable = str(source.get("variable", "tasmax"))
    spatial_chunk = int(source.get("spatial_chunk_cells", 128))
    time_chunk = int(source.get("extraction_time_chunk_days", 365))
    for path in paths:
        output = output_dir / f"{path.stem}_{ctx.config.iso3}.nc"
        if output.is_file():
            try:
                sizes = _validate_clipped(output, variable)
                logger.info(
                    "reusing clipped heatwave file source=%s sizes=%s",
                    path.name,
                    sizes,
                    extra={"action": "reuse", "domain": "heatwaves", "phase": path.name, "path": str(output)},
                )
                continue
            except ValueError as exc:
                logger.warning(
                    "existing heatwave clip is invalid and will be replaced source=%s error=%s",
                    path.name,
                    exc,
                    extra={"action": "validate", "domain": "heatwaves", "phase": path.name, "path": str(output)},
                )
        with logged_action(logger, "clip", domain="heatwaves", phase=path.name, path=str(output)):
            with xr.open_dataset(path, chunks="auto") as dataset:
                if variable not in dataset:
                    raise ValueError(f"{path} does not contain configured variable {variable!r}")
                raster = dataset[variable]
                x_name, y_name = _spatial_dimensions(raster)
                raster = raster.sel(
                    {
                        x_name: _coordinate_slice(raster[x_name].values, minx, maxx),
                        y_name: _coordinate_slice(raster[y_name].values, miny, maxy),
                    }
                )
                if not raster.sizes.get(x_name) or not raster.sizes.get(y_name):
                    raise ValueError(f"Romania bounding box does not overlap {path}")
                raster = raster.rio.set_spatial_dims(x_dim=x_name, y_dim=y_name).rio.write_crs("EPSG:4326")
                clipped = raster.rio.clip(boundary.geometry, boundary.crs, drop=True)
                chunksizes = []
                for dimension in clipped.dims:
                    requested = time_chunk if dimension == "time" else spatial_chunk
                    chunksizes.append(min(int(clipped.sizes[dimension]), requested))
                encoding = {
                    variable: {
                        "zlib": True,
                        "complevel": 4,
                        "shuffle": True,
                        "chunksizes": tuple(chunksizes),
                    }
                }
                with atomic_output_path(output) as temporary:
                    clipped.to_dataset(name=variable).to_netcdf(temporary, encoding=encoding)
                    sizes = _validate_clipped(temporary, variable)
            logger.info(
                "heatwave clip validated source=%s sizes=%s",
                path.name,
                sizes,
                extra={"action": "validate", "domain": "heatwaves", "phase": path.name, "path": str(output)},
            )
