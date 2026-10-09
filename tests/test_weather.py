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
