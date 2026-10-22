"""Optional local weather stations (see README.md here)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from .base import Indoor, Outdoor, Reading, Station
from .netatmo import NetatmoStation

log = logging.getLogger(__name__)

# provider name in [weather_station] -> factory(its config table, token store dir)
PROVIDERS: dict[str, Callable[[dict[str, Any], Path], Station]] = {
    "netatmo": NetatmoStation,
}


def make_station(cfg: dict[str, Any], store_dir: Path) -> Station | None:
    """The configured station, or None when none is set up (or it's misconfigured)."""
    provider = (cfg.get("provider") or "").lower()
    if not provider:
        return None
    if provider not in PROVIDERS:
        log.warning("weather station: unknown provider %r (known: %s)", provider, ", ".join(PROVIDERS))
        return None
    try:
        return PROVIDERS[provider](cfg.get(provider) or {}, store_dir)
    except ValueError as e:
        log.warning("weather station: %s", e)
        return None


__all__ = ["Indoor", "Outdoor", "Reading", "Station", "PROVIDERS", "make_station"]
