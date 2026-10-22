from taco.core.weather import _shape, describe


def test_describe_night_icons():
    assert describe(0, is_day=False) == {"condition": "Clear", "icon": "clear-night"}
    assert describe(63, is_day=False)["icon"] == "rain"
    assert describe(1234)["condition"] == "Unknown"


def test_shape_starts_hourly_at_current_hour():
    hours = [f"2026-10-09T{h:02d}:00" for h in range(24)]
    raw = {
        "current": {"time": "2026-10-09T11:45", "temperature_2m": 61.4, "apparent_temperature": 59.6,
                    "relative_humidity_2m": 50, "weather_code": 2, "is_day": 1, "wind_speed_10m": 7.2},
        "hourly": {"time": hours, "temperature_2m": list(range(24)), "weather_code": [0] * 24,
                   "precipitation_probability": [0] * 24, "is_day": [1] * 24},
        "daily": {"time": ["2026-10-09"], "weather_code": [3], "temperature_2m_max": [68.2],
                  "temperature_2m_min": [45.9], "precipitation_probability_max": [10],
                  "sunrise": ["2026-10-09T06:55"], "sunset": ["2026-10-09T18:15"]},
    }
    w = _shape(raw, {"name": "Northborough, Massachusetts"}, imperial=True)
    assert w["hourly"][0]["time"] == "2026-10-09T11:00"
    assert len(w["hourly"]) == 12
    assert w["current"]["temp"] == 61 and w["current"]["condition"] == "Partly cloudy"
    assert w["today"]["high"] == 68 and w["units"]["temp"] == "°F"


from taco.core.stations import Indoor, Outdoor, Reading
from taco.core.weather import apply_station

FORECAST = {"location": "Home", "units": {"temp": "°F", "wind": "mph"},
            "current": {"temp": 58, "feels_like": 55, "humidity": 60, "wind": 9, "condition": "Clear"},
            "today": {}, "hourly": [], "daily": []}


def test_station_replaces_what_it_measures():
    r = Reading(measured_at=1000, outdoor=Outdoor(temp_c=15.0, humidity=71.4),
                indoor=Indoor(temp_c=21.3, humidity=44, co2_ppm=612))
    w = apply_station(FORECAST, r, "fahrenheit", stale_after_s=1800, now=1100)
    cur = w["current"]
    assert cur["temp"] == 59 and cur["humidity"] == 71
    assert cur["wind"] == 9 and cur["feels_like"] == 55     # not measured: forecast kept
    assert cur["indoor"] == {"temp": 70, "humidity": 44, "co2": 612}
    assert cur["local"] == ["temp", "humidity", "indoor"]
    assert FORECAST["current"]["temp"] == 58                 # forecast not modified


def test_station_wind_converted_and_celsius():
    r = Reading(measured_at=1000, outdoor=Outdoor(wind_kmh=16.1))
    assert apply_station(FORECAST, r, "fahrenheit", 1800, now=1000)["current"]["wind"] == 10
    r = Reading(measured_at=1000, outdoor=Outdoor(temp_c=14.6))
    assert apply_station(FORECAST, r, "celsius", 1800, now=1000)["current"]["temp"] == 15


def test_stale_empty_or_missing_reading_keeps_forecast():
    fresh_empty = Reading(measured_at=1000)
    stale = Reading(measured_at=1000, outdoor=Outdoor(temp_c=15.0))
    assert apply_station(FORECAST, fresh_empty, "fahrenheit", 1800, now=1000) is FORECAST
    assert apply_station(FORECAST, stale, "fahrenheit", 1800, now=1000 + 1801) is FORECAST
    assert apply_station(FORECAST, None, "fahrenheit", 1800) is FORECAST
    assert apply_station(None, stale, "fahrenheit", 1800) is None


def test_outdoor_only_has_no_indoor():
    r = Reading(measured_at=1000, outdoor=Outdoor(temp_c=15.0))
    assert "indoor" not in apply_station(FORECAST, r, "fahrenheit", 1800, now=1000)["current"]
