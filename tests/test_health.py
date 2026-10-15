from taco.core.health import Limits, card, evaluate, parse_cpu, parse_memory, parse_throttled

STAT = """cpu  100 0 50 800 50 0 0 0 0 0
cpu0 25 0 12 200 12 0 0 0 0 0
"""
MEMINFO = """MemTotal:        8000000 kB
MemFree:          500000 kB
MemAvailable:    2000000 kB
Buffers:          100000 kB
"""
L = Limits()


def test_parse_cpu_counts_iowait_as_idle():
    assert parse_cpu(STAT) == (150, 1000)


def test_parse_memory_uses_available():
    assert parse_memory(MEMINFO) == 75.0


def test_parse_throttled():
    assert parse_throttled("throttled=0xe0000\n") == 0xE0000


def test_thresholds_start_and_hysteresis():
    r = {"cpu_percent": 80, "memory_percent": 50, "temp_c": 76}
    assert evaluate(r, set(), L) == ({"cpu", "temperature"}, set())
    # Just under the limit isn't enough to clear...
    assert evaluate({"cpu_percent": 72, "temp_c": 71}, {"cpu", "temperature"}, L) == (set(), set())
    # ...5 points under is.
    assert evaluate({"cpu_percent": 69, "temp_c": 69.9}, {"cpu", "temperature"}, L) == (set(), {"cpu", "temperature"})


def test_active_condition_not_raised_twice():
    assert evaluate({"memory_percent": 90}, {"memory"}, L) == (set(), set())


def test_missing_readings_change_nothing():
    r = {"cpu_percent": None, "memory_percent": None, "temp_c": None, "throttled": None}
    assert evaluate(r, {"cpu"}, L) == (set(), set())


def test_only_current_throttle_bits_count():
    # 0xe0000: throttled/capped/soft-limit at some point since boot, not now.
    assert evaluate({"throttled": 0xE0000}, set(), L) == (set(), set())
    assert evaluate({"throttled": 0x1}, set(), L) == ({"power"}, set())
    assert evaluate({"throttled": 0x8}, set(), L) == ({"throttle"}, set())
    assert evaluate({"throttled": 0x50005}, set(), L) == ({"power", "throttle"}, set())
    assert evaluate({"throttled": 0xE0000}, {"power", "throttle"}, L) == (set(), {"power", "throttle"})


def test_custom_limits():
    assert evaluate({"temp_c": 61}, set(), Limits(temp_c=60)) == ({"temperature"}, set())


def test_card_text():
    assert card("temperature", {"temp_c": 77.4})[0] == "Taco is running hot: 77 °C"
    assert card("cpu", {"cpu_percent": 81.6})[0] == "Taco's CPU is busy: 82%"
