from ldt_factory.domain_runner import DOMAINS
from ldt_factory.prerequisites import PREREQUISITES
from ldt_factory.web import WEB_SOURCES


def test_requested_stage_contract():
    assert WEB_SOURCES == ("osm", "climate_trace", "population")
    assert PREREQUISITES == ("key_assets", "transport", "population")
    assert {
        "flood",
        "land_cover",
        "luminosity",
        "air_pollution",
        "emissions",
        "heatwaves",
        "internet",
        "tourism",
        "accessibility",
    }.issubset(DOMAINS)
