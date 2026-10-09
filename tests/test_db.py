import pytest

from taco.core.db import Database


def test_settings_defaults_and_update(tmp_path):
    db = Database(tmp_path / "t.db")
    assert db.settings()["screensaver_minutes"] == 5
    db.update_settings({"screensaver_minutes": 2, "location": {"name": "X", "lat": 1, "lon": 2}})
    s = Database(tmp_path / "t.db").settings()
    assert s["screensaver_minutes"] == 2 and s["location"]["name"] == "X"


def test_unknown_setting_rejected(tmp_path):
    with pytest.raises(KeyError):
        Database(tmp_path / "t.db").update_settings({"nope": 1})
