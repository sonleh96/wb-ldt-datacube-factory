import logging

import pandas as pd
import pytest

from ldt_factory.domains.land_cover_contract import (
    DYNAMIC_WORLD_CLASSES,
    normalize_class_counts,
    validate_admin_keys,
)
from ldt_factory.domains.extract import land_cover as land_cover_extract
from ldt_factory.domains.process.land_cover import derive_indicators


def test_land_cover_matches_notebook_intent():
    rows = []
    for year, built, tree, crops in ((2017, 10, 40, 20), (2021, 20, 30, 25)):
        rows.append(
            {
                "County": "A",
                "Municipality": "B",
                "year": year,
                "water": 10,
                "tree": tree,
                "grass": 10,
                "flood_vegetation": 0,
                "crops": crops,
                "shrub_and_scrub": 5,
                "built": built,
                "bare": 5 if year == 2017 else 0,
                "snow_and_ice": 0,
                "admin_area_km2": 0.01,
            }
        )

    output = derive_indicators(
        pd.DataFrame(rows),
        "County",
        "Municipality",
        baseline_year=2017,
        output_years=[2021],
    )

    assert output["year"].tolist() == [2021]
    assert output.loc[0, "built_pct_change"] == pytest.approx(100.0)
    assert output.loc[0, "tree_pct_change"] == pytest.approx(-25.0)
    assert output.loc[0, "agri_land"] == pytest.approx(0.0025)
    assert output.loc[0, "agri_land"] <= 0.01


def _count_row(year=2025, **overrides):
    row = {
        "County": "A",
        "Municipality": "B",
        "year": year,
        **{name: 1 for name in DYNAMIC_WORLD_CLASSES.values()},
    }
    row.update(overrides)
    return row


def test_normalize_class_counts_fills_null_counts_and_rejects_fractional_pixels():
    frame = normalize_class_counts(
        pd.DataFrame([_count_row(crops=None)]),
        admin1="County",
        admin2="Municipality",
        expected_year=2025,
    )
    assert frame.loc[0, "crops"] == 0

    with pytest.raises(ValueError, match="fractional pixels"):
        normalize_class_counts(
            pd.DataFrame([_count_row(crops=1.5)]),
            admin1="County",
            admin2="Municipality",
            expected_year=2025,
        )


def test_validate_admin_keys_requires_exact_local_boundary_contract():
    frame = normalize_class_counts(
        pd.DataFrame([_count_row()]),
        admin1="County",
        admin2="Municipality",
        expected_year=2025,
    )
    local = pd.DataFrame(
        [{"County": "A", "Municipality": "B"}, {"County": "A", "Municipality": "C"}]
    )

    with pytest.raises(ValueError, match="missing=1, extra=0"):
        validate_admin_keys(frame, local, admin1="County", admin2="Municipality")


class _FakeChain:
    def filterDate(self, *_args):
        return self

    def select(self, *_args):
        return self

    def mode(self):
        return self

    def eq(self, class_id):
        return _FakeBand(class_id)

    def mask(self):
        return "valid-mask"


class _FakeBand:
    def __init__(self, class_id):
        self.class_id = class_id
        self.name = None

    def rename(self, name):
        self.name = name
        return self

    def toUint8(self):
        return self


class _FakeComposite:
    def __init__(self, bands):
        self.bands = bands
        self.mask_value = None
        self.reduce_kwargs = None

    def updateMask(self, value):
        self.mask_value = value
        return self

    def reduceRegions(self, **kwargs):
        self.reduce_kwargs = kwargs
        kwargs["collection"].composite = self
        return kwargs["collection"]


class _FakeFeatureCollection:
    def __init__(self, asset_id):
        self.asset_id = asset_id
        self.selected = None
        self.composite = None

    def select(self, names):
        self.selected = names
        return self

    def map(self, callback):
        callback(_FakeFeature())
        return self


class _FakeFeature:
    def get(self, name):
        return f"value:{name}"


class _FakeReducer:
    def unweighted(self):
        return "sum-unweighted"


class _FakeEE:
    def __init__(self):
        self.collection = None
        self.composite = None
        self.created_feature = None

        class ImageNamespace:
            pass

        self.Image = ImageNamespace()
        self.Image.cat = self._cat

        class ReducerNamespace:
            pass

        self.Reducer = ReducerNamespace()
        self.Reducer.sum = lambda: _FakeReducer()

    def ImageCollection(self, _collection_id):
        return _FakeChain()

    def FeatureCollection(self, asset_id):
        self.collection = _FakeFeatureCollection(asset_id)
        return self.collection

    def Feature(self, geometry, values):
        self.created_feature = (geometry, values)
        return self.created_feature

    def _cat(self, bands):
        self.composite = _FakeComposite(bands)
        return self.composite


def test_build_reduced_collection_uses_all_classes_and_unweighted_reducer(tmp_path):
    from ldt_factory.config import FactoryConfig
    from ldt_factory.context import RunContext

    config = FactoryConfig(
        tmp_path / "country.yaml",
        {
            "country": {"iso3": "ROU", "name": "Romania"},
            "workspace": str(tmp_path),
            "boundaries": {
                "admin1_output_name": "County",
                "admin2_output_name": "Municipality",
            },
            "sources": {
                "earth_engine": {
                    "admin2_asset_id": "projects/test/assets/rou_admin2",
                },
                "land_cover": {
                    "pixel_size_m": 10,
                    "tile_scale": 4,
                    "max_pixels_per_region": 123,
                },
            },
        },
    )
    fake = _FakeEE()

    result = land_cover_extract._build_reduced_collection(
        RunContext(config, "test"), fake, 2025
    )

    assert result.asset_id == "projects/test/assets/rou_admin2"
    assert result.selected == ["County", "Municipality"]
    assert [band.name for band in fake.composite.bands] == list(DYNAMIC_WORLD_CLASSES.values())
    assert fake.composite.mask_value == "valid-mask"
    assert fake.composite.reduce_kwargs["reducer"] == "sum-unweighted"
    assert fake.composite.reduce_kwargs["scale"] == 10
    assert fake.composite.reduce_kwargs["crs"] == "EPSG:4326"
    assert fake.composite.reduce_kwargs["tileScale"] == 4
    assert fake.composite.reduce_kwargs["maxPixelsPerRegion"] == 123
    assert fake.created_feature[0] is None
    assert fake.created_feature[1]["year"] == 2025


def _gee_config(tmp_path, years):
    from ldt_factory.config import FactoryConfig

    admin2_path = tmp_path / "admin2.geojson"
    admin2_path.write_text("{}", encoding="utf-8")
    return FactoryConfig(
        tmp_path / "country.yaml",
        {
            "country": {"iso3": "ROU", "name": "Romania"},
            "workspace": str(tmp_path / "workspace"),
            "boundaries": {
                "admin0": str(admin2_path),
                "admin1": str(admin2_path),
                "admin2": str(admin2_path),
                "admin1_output_name": "County",
                "admin2_output_name": "Municipality",
            },
            "years": {"land_cover": years, "indicators": [year for year in years if year != years[0]]},
            "sources": {
                "earth_engine": {
                    "project_id": "test-project",
                    "admin2_asset_id": "projects/test-project/assets/rou_admin2",
                },
                "land_cover": {
                    "backend": "gee_reduce_regions",
                    "pixel_size_m": 10,
                    "poll_seconds": 0.001,
                },
            },
        },
    )


class _FakeExportTask:
    id = "task-1"

    def __init__(self, data, asset_id):
        self.data = data
        self.asset_id = asset_id

    def start(self):
        self.data.assets.add(self.asset_id)


class _FakeData:
    def __init__(self, frame):
        self.frame = frame
        self.assets = set()

    def getAsset(self, asset_id):
        if asset_id not in self.assets:
            raise RuntimeError("Asset not found (404)")
        return {"id": asset_id}

    def getTaskStatus(self, _task_id):
        return [{"state": "COMPLETED"}]

    def computeFeatures(self, _params):
        return self.frame

    def deleteAsset(self, asset_id):
        self.assets.remove(asset_id)


class _FakeBatch:
    def __init__(self, data):
        class Table:
            pass

        class Export:
            pass

        self.Export = Export()
        self.Export.table = Table()
        self.Export.table.toAsset = lambda **kwargs: _FakeExportTask(data, kwargs["assetId"])


class _FakeExportEE:
    def __init__(self, frame):
        self.data = _FakeData(frame)
        self.batch = _FakeBatch(self.data)

    def FeatureCollection(self, asset_id):
        return asset_id


def test_gee_extraction_persists_resumable_local_count_table(tmp_path, monkeypatch):
    import geopandas as gpd
    from shapely.geometry import box

    from ldt_factory.context import RunContext
    from ldt_factory.domains.land_cover_contract import (
        count_manifest_path,
        count_table_path,
        gee_task_path,
    )

    config = _gee_config(tmp_path, [2025])
    config.prepare_directories()
    boundaries = gpd.GeoDataFrame(
        {"County": ["A"], "Municipality": ["B"]},
        geometry=[box(0, 0, 1, 1)],
        crs="EPSG:4326",
    )
    frame = pd.DataFrame([_count_row()])
    fake = _FakeExportEE(frame)
    monkeypatch.setattr(land_cover_extract, "load_admin2", lambda _config: boundaries)
    monkeypatch.setattr(land_cover_extract, "_build_reduced_collection", lambda *_args: "reduced")

    land_cover_extract._run_gee_reduce_regions(
        RunContext(config, "test"),
        logging.getLogger("test.land-cover-extract"),
        fake,
    )

    output = pd.read_csv(count_table_path(config, 2025))
    assert output.columns.tolist() == [
        "County",
        "Municipality",
        "year",
        *DYNAMIC_WORLD_CLASSES.values(),
    ]
    assert len(output) == 1
    assert count_manifest_path(config, 2025).is_file()
    assert gee_task_path(config, 2025).is_file()


def test_gee_count_tables_feed_the_existing_indicator_processor(tmp_path, monkeypatch):
    import geopandas as gpd
    from shapely.geometry import box

    from ldt_factory.context import RunContext
    from ldt_factory.domains.land_cover_contract import count_table_path
    from ldt_factory.domains.process import land_cover as land_cover_process

    config = _gee_config(tmp_path, [2017, 2021])
    config.prepare_directories()
    boundaries = gpd.GeoDataFrame(
        {"County": ["A"], "Municipality": ["B"]},
        geometry=[box(0, 0, 1, 1)],
        crs="EPSG:4326",
    )
    monkeypatch.setattr(land_cover_process, "load_admin2", lambda _config: boundaries)
    count_table_path(config, 2017).parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([_count_row(year=2017, built=10, tree=40, crops=20)]).to_csv(
        count_table_path(config, 2017), index=False
    )
    pd.DataFrame([_count_row(year=2021, built=20, tree=30, crops=25)]).to_csv(
        count_table_path(config, 2021), index=False
    )

    land_cover_process.run(
        RunContext(config, "test"),
        logging.getLogger("test.land-cover-process"),
    )

    output = pd.read_csv(config.dataset_dir / "lulc.csv")
    assert output.columns.tolist() == [
        "County",
        "Municipality",
        "year",
        "built_pct_change",
        "tree_pct_change",
        "agri_land",
    ]
    assert output["year"].tolist() == [2021]
    assert output.loc[0, "built_pct_change"] > 0
