"""OpenAI-backed normalization fallback constrained to existing lookup pairs."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from openai import OpenAI


AMBIGUOUS_OS = "ambiguous os"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_FUZZY_MATCH_THRESHOLD = 95


def _normalize_for_match(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", _clean(value).lower()).strip()
    return re.sub(r"\s+", " ", normalized)


def _tokenize(value: object) -> list[str]:
    normalized = _normalize_for_match(value)
    return normalized.split(" ") if normalized else []


def strict_match_percent(query: object, candidate: object) -> int:
    query_normalized = _normalize_for_match(query)
    candidate_normalized = _normalize_for_match(candidate)
    if not query_normalized or not candidate_normalized:
        return 0
    if query_normalized == candidate_normalized:
        return 100

    query_tokens = _tokenize(query)
    candidate_tokens = _tokenize(candidate)
    if not query_tokens or not candidate_tokens:
        return 0

    candidate_token_set = set(candidate_tokens)
    if any(token not in candidate_token_set for token in query_tokens):
        return 0

    if len(candidate_tokens) > len(query_tokens):
        return round(100 * len(query_tokens) / len(candidate_tokens))

    if len(candidate_tokens) < len(query_tokens):
        return round(100 * len(candidate_tokens) / len(query_tokens))

    return 100


def pair_match_percent(os_string: str, pair: dict[str, str]) -> int:
    return max(
        strict_match_percent(os_string, pair.get("normalized_os_detailed_name", "")),
        strict_match_percent(os_string, pair.get("normalized_os", "")),
    )


def _clean(value: object) -> str:
    return str(value or "").strip()


def _is_valid_pair(pair: dict[str, str]) -> bool:
    detailed = _clean(pair.get("normalized_os_detailed_name"))
    normalized = _clean(pair.get("normalized_os"))
    if not detailed or not normalized:
        return False
    return detailed.lower() != AMBIGUOUS_OS and normalized.lower() != AMBIGUOUS_OS


def unique_allowed_pairs(pairs: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, str]] = []

    for pair in pairs:
        if not _is_valid_pair(pair):
            continue

        detailed = _clean(pair["normalized_os_detailed_name"])
        normalized = _clean(pair["normalized_os"])
        key = (detailed.casefold(), normalized.casefold())
        if key in seen:
            continue

        seen.add(key)
        unique.append(
            {
                "normalized_os_detailed_name": detailed,
                "normalized_os": normalized,
            }
        )

    return unique


def _parse_index(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def _pair_from_index(index: int | None, allowed_pairs: list[dict[str, str]]) -> dict[str, str] | None:
    if index is None or index < 0 or index >= len(allowed_pairs):
        return None

    pair = allowed_pairs[index]
    return {
        "normalized_os_detailed_name": pair["normalized_os_detailed_name"],
        "normalized_os": pair["normalized_os"],
    }


def suggest_normalization_batch(
    os_strings: list[str],
    allowed_pairs: list[dict[str, str]],
    fuzzy_match_threshold: int = DEFAULT_FUZZY_MATCH_THRESHOLD,
) -> list[dict[str, str] | None]:
    threshold = max(50, min(100, int(fuzzy_match_threshold)))
    cleaned_strings = [_clean(value) for value in os_strings]
    if not any(cleaned_strings):
        return [None for _ in cleaned_strings]

    unique_pairs = unique_allowed_pairs(allowed_pairs)
    if not unique_pairs:
        return [None for _ in cleaned_strings]

    api_key = _clean(os.environ.get("OPENAI_API_KEY"))
    if not api_key:
        return [None for _ in cleaned_strings]

    indexed_items = [
        {"item_index": index, "os_string": value}
        for index, value in enumerate(cleaned_strings)
        if value
    ]
    if not indexed_items:
        return [None for _ in cleaned_strings]

    pair_catalog = [
        {
            "pair_index": index,
            "normalized_os_detailed_name": pair["normalized_os_detailed_name"],
            "normalized_os": pair["normalized_os"],
        }
        for index, pair in enumerate(unique_pairs)
    ]

    model = _clean(os.environ.get("OPENAI_MODEL")) or DEFAULT_MODEL
    client = OpenAI(api_key=api_key)

    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Match each operating system string to one existing normalization pair only when "
                        f"you are at least {threshold}% confident it is the same product/edition. "
                        "You must only choose pair_index values from the allowed_pairs list. "
                        "Never invent normalized values. "
                        "Edition, SKU, and qualifier words matter. "
                        "For example, 'Windows 11 Pro' must NOT match 'Windows 11 Pro Enterprise'. "
                        "If the OS string is missing qualifiers present in a candidate, reject that candidate. "
                        "If no pair is a very sure match, return pair_index as null for that item. "
                        'Respond with JSON: {"matches":[{"item_index":0,"pair_index":1}]}'
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "items": indexed_items,
                            "allowed_pairs": pair_catalog,
                        },
                        ensure_ascii=True,
                    ),
                },
            ],
        )
    except Exception:
        return [None for _ in cleaned_strings]

    content = _clean(response.choices[0].message.content)
    if not content:
        return [None for _ in cleaned_strings]

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return [None for _ in cleaned_strings]

    matches = payload.get("matches")
    if not isinstance(matches, list):
        return [None for _ in cleaned_strings]

    results: list[dict[str, str] | None] = [None for _ in cleaned_strings]
    for match in matches:
        if not isinstance(match, dict):
            continue

        item_index = _parse_index(match.get("item_index"))
        pair_index = _parse_index(match.get("pair_index"))
        if item_index is None or item_index < 0 or item_index >= len(cleaned_strings):
            continue

        selected_pair = _pair_from_index(pair_index, unique_pairs)
        if selected_pair is None:
            results[item_index] = None
            continue

        os_string = cleaned_strings[item_index]
        if pair_match_percent(os_string, selected_pair) < threshold:
            results[item_index] = None
            continue

        results[item_index] = selected_pair

    return results
