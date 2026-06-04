"""Settings loaded from environment variables and an optional .env file.

Replaces pydantic-settings with the standard library. Precedence (highest
first): explicit keyword overrides, process environment, .env file, default.
"""

from __future__ import annotations

import os
from pathlib import Path

_ALIASES = {
    "access_token": "M365_ACCESS_TOKEN",
    "time_zone": "M365_TIME_ZONE",
    "model_alias": "M365_MODEL_ALIAS",
}
_DEFAULTS = {
    "access_token": "",
    "time_zone": "Asia/Tokyo",
    "model_alias": "m365-copilot",
}


def _load_env_file(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return values
    for line in p.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


class Settings:
    access_token: str
    time_zone: str
    model_alias: str

    def __init__(self, env_file: str | Path = ".env", **overrides):
        file_values = _load_env_file(env_file)
        for field, alias in _ALIASES.items():
            if alias in overrides:
                value = overrides[alias]
            elif field in overrides:
                value = overrides[field]
            elif alias in os.environ:
                value = os.environ[alias]
            elif alias in file_values:
                value = file_values[alias]
            else:
                value = _DEFAULTS[field]
            setattr(self, field, value)

    def __repr__(self) -> str:
        shown = "<set>" if self.access_token else "<empty>"
        return (
            f"Settings(access_token={shown}, time_zone={self.time_zone!r}, "
            f"model_alias={self.model_alias!r})"
        )
