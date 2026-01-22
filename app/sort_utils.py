"""Helpers for validating and normalizing sort parameters."""

from typing import Iterable, Tuple


def normalize_sort_params(
    sort_key: str | None,
    sort_dir: str | None,
    allowed_keys: Iterable[str],
    default_key: str,
    default_dir: str = "asc",
) -> Tuple[str, str]:
    allowed = set(allowed_keys)
    key = sort_key or ""
    if key not in allowed:
        key = default_key

    direction = (sort_dir or default_dir).lower()
    if direction not in {"asc", "desc"}:
        direction = default_dir

    return key, direction
