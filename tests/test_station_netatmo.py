import pytest

from taco.core.stations import make_station
from taco.core.stations.netatmo import parse


def body(modules, base_reachable=True):
    return {"body": {"devices": [{
        "type": "NAMain", "reachable": base_reachable,
        "dashboard_data": {"time_utc": 1000, "Temperature": 21.3, "Humidity": 44, "CO2": 612,
                           "Noise": 38, "Pressure": 1016.2},
        "modules": modules,
    }]}}


OUTDOOR = {"type": "NAModule1", "reachable": True,
           "dashboard_data": {"time_utc": 1100, "Temperature": 12.5, "Humidity": 71}}


def test_indoor_and_outdoor():
    r = parse(body([OUTDOOR]))
    assert r.indoor.temp_c == 21.3 and r.indoor.humidity == 44 and r.indoor.co2_ppm == 612
    assert r.outdoor.temp_c == 12.5 and r.outdoor.humidity == 71
    assert r.outdoor.wind_kmh is None              # no wind gauge
    assert r.measured_at == 1100                   # newest measurement


def test_unreachable_outdoor_module_skipped():
    r = parse(body([{**OUTDOOR, "reachable": False}]))
    assert r.outdoor.temp_c is None and r.indoor.temp_c == 21.3
    assert r.measured_at == 1000


def test_wind_and_rain_modules():
    r = parse(body([OUTDOOR,
                    {"type": "NAModule2", "dashboard_data": {"time_utc": 1050, "WindStrength": 14,
                                                              "GustStrength": 25, "WindAngle": 270}},
                    {"type": "NAModule3", "dashboard_data": {"time_utc": 1050, "sum_rain_1": 0.4,
                                                              "sum_rain_24": 3.1}}]))
    assert (r.outdoor.wind_kmh, r.outdoor.gust_kmh, r.outdoor.wind_deg) == (14, 25, 270)
    assert (r.outdoor.rain_1h_mm, r.outdoor.rain_24h_mm) == (0.4, 3.1)


def test_nothing_reachable_is_an_error():
    with pytest.raises(ValueError):
        parse(body([{**OUTDOOR, "reachable": False}], base_reachable=False))
    with pytest.raises(ValueError):
        parse({"body": {"devices": []}})


def test_make_station(tmp_path):
    assert make_station({"provider": ""}, tmp_path) is None
    assert make_station({"provider": "nope"}, tmp_path) is None
    assert make_station({"provider": "netatmo", "netatmo": {}}, tmp_path) is None   # no credentials
    st = make_station({"provider": "netatmo", "netatmo": {"client_id": "a", "client_secret": "b",
                                                          "refresh_token": "c"}}, tmp_path)
    assert st is not None and st.name == "Netatmo"
