import pandas as pd
import pytest

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
            }
        )

    output = derive_indicators(
        pd.DataFrame(rows),
        "County",
        "Municipality",
        baseline_year=2017,
        output_years=[2021],
        pixel_size_m=10,
    )

    assert output["year"].tolist() == [2021]
    assert output.loc[0, "built_pct_change"] == pytest.approx(100.0)
    assert output.loc[0, "tree_pct_change"] == pytest.approx(-25.0)
    assert output.loc[0, "agri_land"] == pytest.approx(0.0025)
