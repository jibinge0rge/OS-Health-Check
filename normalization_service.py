"""AI-backed normalization fallback constrained to existing lookup pairs.

Supports OpenAI and Google Gemini. Provider is selected by the caller
(persisted in app settings); API keys come from environment variables.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Literal

import requests
from openai import OpenAI


AMBIGUOUS_OS = "ambiguous os"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
DEFAULT_FUZZY_MATCH_THRESHOLD = 95
AiProvider = Literal["openai", "gemini"]
GEMINI_GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

# Strong vendor / product-family signals seen in _data/eol_lookup.csv.
# Shared words like "ios" are intentionally NOT enough by themselves.
_VENDOR_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cisco", (r"\bcisco\b", r"\bios[\s\-]?xe\b", r"\bios[\s\-]?xr\b", r"\bnx[\s\-]?os\b", r"\bciscoios\b", r"\bciscoxe\b", r"\bciscoxr\b")),
    ("apple", (r"\bapple\b", r"\biphone\b", r"\bipad\b", r"\bipod\b", r"\bmacos\b", r"\bmac\s*os\b", r"\bvisionos\b", r"\bappleios\b")),
    ("microsoft", (r"\bmicrosoft\b", r"\bwindows\b", r"\bwin(?:dows)?(?:\s|$)", r"\bmsft\b")),
    ("android", (r"\bandroid\b",)),
    ("redhat", (r"\bred\s*hat\b", r"\brhel\b", r"\bredhat\b")),
    ("almalinux", (r"\balmalinux\b", r"\balma\s*linux\b")),
    ("rocky", (r"\brocky\s*linux\b", r"\brockylinux\b")),
    ("ubuntu", (r"\bubuntu\b",)),
    ("debian", (r"\bdebian\b",)),
    ("centos", (r"\bcentos\b",)),
    ("oracle", (r"\boracle\b", r"\bsolaris\b")),
    ("vmware", (r"\bvmware\b", r"\besxi\b", r"\bvsphere\b")),
    ("amazon", (r"\bamazon\s*linux\b", r"\bamzn\b", r"\baws\b")),
    ("suse", (r"\bsuse\b", r"\bsles\b", r"\bopensuse\b")),
    ("fortinet", (r"\bfortinet\b", r"\bfortios\b", r"\bfortigate\b")),
    ("paloalto", (r"\bpalo\s*alto\b", r"\bpan[\s\-]?os\b")),
    ("ibm", (r"\bibm\b", r"\baix\b")),
    ("hp", (r"\bhp[\s\-]?ux\b", r"\bhewlett\b")),
    ("freebsd", (r"\bfreebsd\b",)),
    ("f5", (r"\bf5\b", r"\bbig[\s\-]?ip\b")),
    ("citrix", (r"\bcitrix\b", r"\bxenserver\b")),
)


def _collapse_trailing_version_zeros(version: str) -> str:
    parts = version.split(".")
    while len(parts) > 1 and re.fullmatch(r"0+", parts[-1]):
        parts.pop()
    return ".".join(parts)


def _protect_versions(text: str) -> str:
    """Keep dotted versions as atomic tokens and treat trailing .0 as equal.

    Examples: 3.2 and 3.2.0 both become v3x2; 3.2.1 stays v3x2x1.
    """

    def replace(match: re.Match[str]) -> str:
        collapsed = _collapse_trailing_version_zeros(match.group(0))
        return "v" + collapsed.replace(".", "x")

    return re.sub(r"\b\d+(?:\.\d+)+\b", replace, text)


def _normalize_for_match(value: object) -> str:
    protected = _protect_versions(_clean(value).lower())
    normalized = re.sub(r"[^a-z0-9]+", " ", protected).strip()
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


def normalize_ai_provider(value: object) -> AiProvider:
    cleaned = _clean(value).lower()
    if cleaned == "gemini":
        return "gemini"
    return "openai"


def openai_api_key() -> str:
    return _clean(os.environ.get("OPENAI_API_KEY"))


def gemini_api_key() -> str:
    return _clean(os.environ.get("GEMINI_API_KEY")) or _clean(
        os.environ.get("GOOGLE_API_KEY")
    )


def provider_api_key_configured(provider: object) -> bool:
    selected = normalize_ai_provider(provider)
    if selected == "gemini":
        return bool(gemini_api_key())
    return bool(openai_api_key())


def openai_model_name() -> str:
    return _clean(os.environ.get("OPENAI_MODEL")) or DEFAULT_OPENAI_MODEL


def gemini_model_name() -> str:
    return _clean(os.environ.get("GEMINI_MODEL")) or DEFAULT_GEMINI_MODEL


def _extract_json_object(text: str) -> dict[str, Any] | None:
    cleaned = _clean(text)
    if not cleaned:
        return None

    try:
        payload = json.loads(cleaned)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(cleaned[start : end + 1])
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        return None


def _complete_json_openai(system_prompt: str, user_prompt: str) -> dict[str, Any] | None:
    api_key = openai_api_key()
    if not api_key:
        return None

    client = OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model=openai_model_name(),
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
    except Exception:
        return None

    content = _clean(response.choices[0].message.content)
    return _extract_json_object(content)


def _complete_json_gemini(system_prompt: str, user_prompt: str) -> dict[str, Any] | None:
    api_key = gemini_api_key()
    if not api_key:
        return None

    model = gemini_model_name()
    url = GEMINI_GENERATE_URL.format(model=model)
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }

    try:
        response = requests.post(url, params={"key": api_key}, json=payload, timeout=90)
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError, TypeError):
        return None

    candidates = body.get("candidates") if isinstance(body, dict) else None
    if not isinstance(candidates, list) or not candidates:
        return None

    first = candidates[0]
    if not isinstance(first, dict):
        return None
    content = first.get("content")
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        return None

    texts: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            text = _clean(part.get("text"))
            if text:
                texts.append(text)
    return _extract_json_object("\n".join(texts))


def complete_json(
    system_prompt: str,
    user_prompt: str,
    provider: object = "openai",
) -> dict[str, Any] | None:
    selected = normalize_ai_provider(provider)
    if selected == "gemini":
        return _complete_json_gemini(system_prompt, user_prompt)
    return _complete_json_openai(system_prompt, user_prompt)


def _vendor_tags(value: object) -> set[str]:
    text = _clean(value).lower()
    if not text:
        return set()

    tags: set[str] = set()
    for vendor, patterns in _VENDOR_PATTERNS:
        if any(re.search(pattern, text) for pattern in patterns):
            tags.add(vendor)

    # "Apple iOS" / "iOS 17" style mobile OS without saying Apple explicitly is still Apple
    # when Cisco markers are absent.
    if "apple" not in tags and "cisco" not in tags:
        if re.search(r"\bios\b", text) and not re.search(r"\bios[\s\-]?x[er]\b", text):
            if re.search(r"\b(?:iphone|ipad|ipod|apple)\b", text) or re.search(
                r"\bios\s+\d+\b", text
            ):
                tags.add("apple")

    return tags


def vendors_compatible(left: object, right: object) -> bool:
    """Reject cross-vendor matches (e.g. Cisco IOS vs Apple iOS)."""
    left_tags = _vendor_tags(left)
    right_tags = _vendor_tags(right)
    if not left_tags or not right_tags:
        return True
    return bool(left_tags & right_tags)


def pair_compatible_with_os(os_string: str, pair: dict[str, str]) -> bool:
    blob = " ".join(
        [
            _clean(pair.get("normalized_os_detailed_name")),
            _clean(pair.get("normalized_os")),
        ]
    )
    return vendors_compatible(os_string, blob)


def filter_pairs_for_os(os_string: str, pairs: list[dict[str, str]]) -> list[dict[str, str]]:
    """Prefer same-vendor pairs so the model never sees Apple iOS for Cisco IOS."""
    query_tags = _vendor_tags(os_string)
    if not query_tags:
        return pairs

    filtered = [pair for pair in pairs if pair_compatible_with_os(os_string, pair)]
    return filtered


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


def detect_ambiguous_os_batch(
    os_strings: list[str],
    provider: object = "openai",
) -> list[bool]:
    """Return True when an OS string lists multiple distinct products separated by '/'."""
    cleaned_strings = [_clean(value) for value in os_strings]
    results = [False for _ in cleaned_strings]
    indexed_items = [
        {"item_index": index, "os_string": value}
        for index, value in enumerate(cleaned_strings)
        if value and "/" in value
    ]
    if not indexed_items:
        return results

    selected = normalize_ai_provider(provider)
    if not provider_api_key_configured(selected):
        return results

    system_prompt = (
        "Decide whether each operating system string lists multiple distinct "
        "operating systems or products separated by '/'. "
        "Return ambiguous=true only when '/' separates different OS products "
        "or major OS families that should not share one lifecycle record. "
        "Return ambiguous=false when '/' is part of a single product name, "
        "version path, model range, protocol list, or similar. "
        "Examples of ambiguous=true: "
        "'AIX 5.x / AIX 6.x / Sidewinder G2', "
        "'Cisco IOS 12.1 / Cisco IOS 12.2', "
        "'EulerOS / Ubuntu / Fedora'. "
        "Examples of ambiguous=false: "
        "'Debian GNU/Linux 10', "
        "'FreeBSD/12.2-STABLE', "
        "'Canon LBP245/246/248 /P', "
        "'EPSON 11a/b/g/n & 10/100 Print Server', "
        "'FUJIFILM Apeos C325/328 dw'. "
        'Respond with JSON: {"results":[{"item_index":0,"ambiguous":true}]}'
    )
    user_prompt = json.dumps({"items": indexed_items}, ensure_ascii=True)
    payload = complete_json(system_prompt, user_prompt, selected)
    if not payload:
        return results

    payload_results = payload.get("results")
    if not isinstance(payload_results, list):
        return results

    for item in payload_results:
        if not isinstance(item, dict):
            continue

        item_index = _parse_index(item.get("item_index"))
        if item_index is None or item_index < 0 or item_index >= len(cleaned_strings):
            continue

        ambiguous = item.get("ambiguous")
        if isinstance(ambiguous, bool):
            results[item_index] = ambiguous
        elif isinstance(ambiguous, str):
            results[item_index] = ambiguous.strip().lower() in {"true", "1", "yes"}

    return results


def suggest_normalization_batch(
    os_strings: list[str],
    allowed_pairs: list[dict[str, str]],
    fuzzy_match_threshold: int = DEFAULT_FUZZY_MATCH_THRESHOLD,
    provider: object = "openai",
) -> list[dict[str, str] | None]:
    threshold = max(50, min(100, int(fuzzy_match_threshold)))
    cleaned_strings = [_clean(value) for value in os_strings]
    if not any(cleaned_strings):
        return [None for _ in cleaned_strings]

    unique_pairs = unique_allowed_pairs(allowed_pairs)
    if not unique_pairs:
        return [None for _ in cleaned_strings]

    selected = normalize_ai_provider(provider)
    if not provider_api_key_configured(selected):
        return [None for _ in cleaned_strings]

    results: list[dict[str, str] | None] = [None for _ in cleaned_strings]

    # Group by vendor so Oracle batches never see AlmaLinux pairs in the same prompt.
    groups: dict[tuple[str, ...], list[int]] = {}
    for index, value in enumerate(cleaned_strings):
        if not value:
            continue
        key = tuple(sorted(_vendor_tags(value)))
        groups.setdefault(key, []).append(index)

    system_prompt = (
        "Match each operating system string to one existing normalization pair only when "
        f"you are at least {threshold}% confident it is the same product family and edition. "
        "You must only choose pair_index values from the allowed_pairs list. "
        "Never invent normalized values. "
        "Vendor / product family is mandatory. Shared tokens are not enough. "
        "Examples of INVALID matches: "
        "'Cisco IOS 12.2(55)SE9' must NOT match 'Apple iOS 12'; "
        "'Cisco IOS-XE 17.09.05a' must NOT match 'Apple iOS 17'; "
        "'Oracle Linux Server 9.5' must NOT match 'AlmaLinux OS 9.5' or 'AlmaLinux OS 9'; "
        "'Windows Server 2019' must NOT match 'Red Hat Enterprise Linux 9'. "
        "Examples of VALID matches from this lookup style: "
        "'Cisco IOS 12.2(55)SE9' -> 'CISCO IOS 12.2'; "
        "'Cisco IOS-XE 17.09.05a' -> 'Cisco IOS XE 17.9'; "
        "'Cisco IOS XE 17.03.04a' -> 'Cisco IOS XE 17.3'; "
        "'Oracle Linux Server 9.5' -> 'Oracle Linux 9'. "
        "Treat dotted versions that differ only by trailing .0 as the same "
        "(for example 3.2 and 3.2.0; also 17.03 ~= 17.3 when the pair uses the short form). "
        "Edition, SKU, and qualifier words matter. "
        "For example, 'Windows 11 Pro' must NOT match 'Windows 11 Pro Enterprise'. "
        "If the OS string is missing qualifiers present in a candidate, reject that candidate. "
        "If no pair is a very sure same-vendor match, return pair_index as null for that item. "
        'Respond with JSON: {"matches":[{"item_index":0,"pair_index":1}]}'
    )

    for indexes in groups.values():
        group_strings = [cleaned_strings[index] for index in indexes]
        scoped_pairs = unique_allowed_pairs(
            [
                pair
                for value in group_strings
                for pair in filter_pairs_for_os(value, unique_pairs)
            ]
        )
        if not scoped_pairs:
            continue

        indexed_items = [
            {"item_index": local_index, "os_string": group_strings[local_index]}
            for local_index in range(len(group_strings))
        ]
        pair_catalog = [
            {
                "pair_index": pair_index,
                "normalized_os_detailed_name": pair["normalized_os_detailed_name"],
                "normalized_os": pair["normalized_os"],
            }
            for pair_index, pair in enumerate(scoped_pairs)
        ]

        payload = complete_json(
            system_prompt,
            json.dumps(
                {
                    "items": indexed_items,
                    "allowed_pairs": pair_catalog,
                },
                ensure_ascii=True,
            ),
            selected,
        )
        if not payload:
            continue

        matches = payload.get("matches")
        if not isinstance(matches, list):
            continue

        for match in matches:
            if not isinstance(match, dict):
                continue

            local_index = _parse_index(match.get("item_index"))
            pair_index = _parse_index(match.get("pair_index"))
            if local_index is None or local_index < 0 or local_index >= len(indexes):
                continue

            selected_pair = _pair_from_index(pair_index, scoped_pairs)
            if selected_pair is None:
                continue

            global_index = indexes[local_index]
            os_string = cleaned_strings[global_index]
            if not pair_compatible_with_os(os_string, selected_pair):
                continue

            results[global_index] = selected_pair

    return results
