from __future__ import annotations

import logging
from pathlib import Path

from ...context import RunContext
from ...io_utils import require_files
from ...logging_utils import logged_action


def run(ctx: RunContext, logger: logging.Logger) -> None:
    import pyarrow.parquet as pq

    source = ctx.config.source("internet")
    root = Path(str(source["dataset_root"]))
    paths = []
    for year in ctx.config.years("indicators"):
        paths.extend(
            [
                root / str(source["fixed_filename_template"]).format(year=year),
                root / str(source["mobile_filename_template"]).format(year=year),
            ]
        )
    # Extraction is intentionally a validation-only phase. The files are the
    # already-downloaded global Ookla dataset and are shared by country runs.
    with logged_action(logger, "use_existing_global_dataset", domain="internet", path=str(root)):
        require_files(paths, "Ookla parquet inputs")
        required = {"quadkey", "tile", "avg_d_kbps"}
        for path in paths:
            columns = set(pq.ParquetFile(path).schema.names)
            missing = required - columns
            if missing:
                raise ValueError(f"{path} is missing Ookla columns: {sorted(missing)}")
