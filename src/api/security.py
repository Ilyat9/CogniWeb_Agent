"""Маскирование секретов для GET /config.

Вынесено из src/api/app.py без изменения логики - чистое перемещение
кода (структурный рефакторинг); app.py реэкспортирует mask_settings,
так что существующие импорты продолжают работать.
"""

from pathlib import Path
from typing import Any

# /config masks any field whose name looks secret-ish. Deliberately
# over-broad (key/token/secret/password/credential): false positives cost a
# masked value, false negatives leak a credential to the UI.
_SECRET_NAME_HINTS = ("key", "token", "secret", "password", "credential")


def _mask_value(value: Any) -> Any:
    if isinstance(value, str) and value:
        return f"***masked ({len(value)} chars)***"
    return "***masked***"


def mask_settings(settings_dict: dict[str, Any]) -> dict[str, Any]:
    """Return a UI-safe copy of a settings dump: every secret-looking field
    masked, everything else (bools, ints, paths, lists) passed through."""
    masked = {}
    for name, value in settings_dict.items():
        if any(hint in name.lower() for hint in _SECRET_NAME_HINTS):
            masked[name] = _mask_value(value)
        elif isinstance(value, Path):
            masked[name] = str(value)
        else:
            masked[name] = value
    return masked
