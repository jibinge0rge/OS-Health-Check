"""Shared conservative version → release train matching."""

from __future__ import annotations


def version_parts(value: str) -> list[str]:
    return [part for part in str(value or "").split(".") if part != ""]


def numeric_version_parts(value: str) -> list[int] | None:
    """Split a dotted version into integer segments, or None if non-numeric."""
    parts = version_parts(value)
    if not parts:
        return None
    nums: list[int] = []
    for part in parts:
        if not part.isdigit():
            return None
        nums.append(int(part))
    return nums


def score_release_against_hint(release_name: str, hint: str) -> int:
    """Score how well an API release name matches a version hint.

    Rules:
    - Exact match → 100
    - Numeric train prefix match → 90 (API ``17.9`` matches hint ``17.09.08``)
    - Bare major hint (``11``) must not match a finer release (``11.4``)
    - Shared major only when both sides are multi-part but trains differ → 55
    - Non-all-numeric versions fall back to string segment comparison
    """
    if not release_name or not hint:
        return 0
    if release_name == hint:
        return 100

    rel_nums = numeric_version_parts(release_name)
    hint_nums = numeric_version_parts(hint)
    if rel_nums is not None and hint_nums is not None:
        return _score_numeric_parts(rel_nums, hint_nums)

    rel_parts = version_parts(release_name)
    hint_parts = version_parts(hint)
    if not rel_parts or not hint_parts:
        return 0
    return _score_string_parts(rel_parts, hint_parts)


def _score_numeric_parts(rel_nums: list[int], hint_nums: list[int]) -> int:
    # API release train is a numeric prefix of the OS hint (coarser API).
    if len(rel_nums) <= len(hint_nums) and rel_nums == hint_nums[: len(rel_nums)]:
        if len(hint_nums) == 1 and len(rel_nums) > 1:
            return 0
        return 90

    # OS hint is a numeric prefix of the API release (coarser hint).
    if len(hint_nums) <= len(rel_nums) and hint_nums == rel_nums[: len(hint_nums)]:
        if len(hint_nums) == 1 and len(rel_nums) > 1:
            return 0
        return 90

    if len(hint_nums) > 1 and len(rel_nums) > 1 and hint_nums[0] == rel_nums[0]:
        return 55
    return 0


def _score_string_parts(rel_parts: list[str], hint_parts: list[str]) -> int:
    shorter = min(len(rel_parts), len(hint_parts))
    if rel_parts[:shorter] == hint_parts[:shorter]:
        if len(hint_parts) == 1 and len(rel_parts) > 1:
            return 0
        return 90
    if len(hint_parts) > 1 and len(rel_parts) > 1 and rel_parts[0] == hint_parts[0]:
        return 55
    return 0
