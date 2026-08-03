"""endoflife.date lookup helpers for UI-added operating systems."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any

import requests

from normalization_service import vendors_compatible
from version_match import score_release_against_hint

BASE_URL = "https://endoflife.date/api"
PRODUCT_V1_URL = f"{BASE_URL}/v1/products"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}
EOL_FETCH_WORKERS = 8

# Insert spaces at letter↔digit boundaries (Linux8.2 → linux 8.2).
_LETTER_DIGIT_BOUNDARY_RE = re.compile(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])")

# Regex overrides run before the API phrase index (disambiguation only).
_SLUG_PRIORITY_OVERRIDES: list[tuple[str, str]] = [
    (r"windows[\s-]?server", "windows-server"),
    (r"cisco[\s-]?ios[\s-]?xe|\bios[\s-]?xe\b", "cisco-ios-xe"),
    (r"centos[\s-]?stream", "centos-stream"),
    (
        r"\brhel\b|"
        r"(?:red\s*hat|redhat)(?:\s+enterprise[s]?)?\s+linux\b|"
        r"(?:red\s*hat|redhat)\s+linux\b",
        "rhel",
    ),
    (r"\bopenshift\b|\bred[\s-]?hat[\s-]?openshift\b", "red-hat-openshift"),
    (r"\bpalo\s+alto\b|\bpan[\s-]?os\b", "panos"),
]

# Inventory phrases not present as API labels/aliases (longest-match index).
_INVENTORY_PHRASE_EXTRAS: dict[str, tuple[str, ...]] = {
    "rhel": ("red hat linux", "redhat linux"),
    "sles": ("suse linux enterprise",),
    "amazon-linux": ("amzn",),
}

# Ignore very short slug phrases that cause false positives in free text.
_PHRASE_BLOCKLIST = frozenset({"go", "r", "xl", "z", "io", "os"})

# Common inventory typos / glued product tokens → spaced phrases.
_GLUED_PHRASE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (r"ubuntulinux", "ubuntu linux"),
    (r"redhatlinux", "red hat linux"),
    (r"rockylinux", "rocky linux"),
    (r"almalinux", "alma linux"),
    (r"oraclelinux", "oracle linux"),
    (r"amazonlinux", "amazon linux"),
    (r"windowsserver", "windows server"),
    (r"centosstream", "centos stream"),
    (r"suselinux", "suse linux"),
)

# Cached slug index entry: (phrase_length, slug, phrase, priority).
SlugIndexEntry = tuple[int, str, str, int]


def _normalize_phrase(value: str) -> str:
    text = _clean(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_for_slug_lookup(os_name: str) -> str:
    """Normalize messy inventory strings for product slug detection."""
    text = _clean(os_name).lower()
    # Hyphens often separate product tokens (PAN-OS) and hotfix markers (11.2.10-h3).
    text = text.replace("_", " ").replace("/", " ").replace("-", " ")
    for pattern, replacement in _GLUED_PHRASE_REPLACEMENTS:
        text = re.sub(pattern, replacement, text, flags=re.I)
    text = _LETTER_DIGIT_BOUNDARY_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=None)
def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Compile (and cache) the match pattern for one slug-index phrase.

    ``resolve_product_slug`` scans the *entire* slug index (thousands of
    phrases from ~300+ endoflife.date products) for every row, so this was
    the dominant cost of a Refresh: recompiling every phrase's regex from
    scratch on every call blew straight through Python's own internal
    ``re.compile`` cache (a few hundred entries) well before a single row's
    scan finished, so it never actually stayed cached. An explicit,
    unbounded cache here compiles each distinct phrase exactly once for the
    life of the process.
    """
    escaped = re.escape(phrase.strip().lower())
    if " " in phrase:
        return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.I)
    return re.compile(rf"\b{escaped}\b", re.I)


def _add_index_phrase(
    entries: dict[tuple[str, str], SlugIndexEntry],
    slug: str,
    phrase: str,
    *,
    priority: int = 0,
) -> None:
    normalized = _normalize_phrase(phrase)
    if not normalized or normalized in _PHRASE_BLOCKLIST:
        return
    if len(normalized) < 3 and " " not in normalized:
        return
    key = (slug, normalized)
    candidate: SlugIndexEntry = (len(normalized), slug, normalized, priority)
    existing = entries.get(key)
    if existing is None or candidate[3] > existing[3]:
        entries[key] = candidate


def build_slug_index(products: list[dict[str, Any]]) -> tuple[SlugIndexEntry, ...]:
    """Build phrase → slug index from endoflife.date v1 product catalog."""
    entries: dict[tuple[str, str], SlugIndexEntry] = {}

    for product in products:
        slug = _clean(product.get("name"))
        if not slug:
            continue

        _add_index_phrase(entries, slug, slug.replace("-", " "))
        label = _clean(product.get("label"))
        if label:
            _add_index_phrase(entries, slug, label)

        for alias in product.get("aliases") or []:
            cleaned_alias = _clean(alias)
            if not cleaned_alias:
                continue
            _add_index_phrase(entries, slug, cleaned_alias)
            if "-" in cleaned_alias:
                _add_index_phrase(entries, slug, cleaned_alias.replace("-", " "))

    for slug, phrases in _INVENTORY_PHRASE_EXTRAS.items():
        for phrase in phrases:
            _add_index_phrase(entries, slug, phrase, priority=10)

    return tuple(sorted(entries.values(), key=lambda item: (-item[0], -item[3], item[1])))


@lru_cache(maxsize=1)
def get_product_catalog() -> tuple[dict[str, Any], ...]:
    response = requests.get(PRODUCT_V1_URL, headers=HEADERS, timeout=60)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Product catalog response was not an object.")
    result = payload.get("result")
    if not isinstance(result, list):
        raise ValueError("Product catalog result was not a list.")
    products: list[dict[str, Any]] = []
    for item in result:
        if isinstance(item, dict) and _clean(item.get("name")):
            products.append(item)
    return tuple(products)


@lru_cache(maxsize=1)
def get_slug_index() -> tuple[SlugIndexEntry, ...]:
    return build_slug_index(list(get_product_catalog()))


def _match_slug_from_index(
    text: str,
    valid_slugs: frozenset[str],
    slug_index: tuple[SlugIndexEntry, ...] | None = None,
) -> str | None:
    index = slug_index if slug_index is not None else get_slug_index()
    best: SlugIndexEntry | None = None
    for entry in index:
        slug = entry[1]
        if slug not in valid_slugs:
            continue
        phrase = entry[2]
        if not _phrase_pattern(phrase).search(text):
            continue
        if best is None or (entry[0], entry[3], entry[1]) > (best[0], best[3], best[1]):
            best = entry
    return best[1] if best else None


def resolve_product_slug(
    os_name: str,
    valid_slugs: frozenset[str],
    slug_index: tuple[SlugIndexEntry, ...] | None = None,
) -> str | None:
    normalized = _normalize_for_slug_lookup(os_name)
    if not normalized:
        return None

    for pattern, slug in _SLUG_PRIORITY_OVERRIDES:
        if slug in valid_slugs and re.search(pattern, normalized, re.IGNORECASE):
            return slug

    matched = _match_slug_from_index(normalized, valid_slugs, slug_index=slug_index)
    if matched:
        return matched

    hyphenated = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    if hyphenated in valid_slugs:
        return hyphenated

    return None


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


@lru_cache(maxsize=1)
def get_valid_slugs() -> frozenset[str]:
    return frozenset(product["name"] for product in get_product_catalog())


def _merge_overlapping_words(first: str, second: str) -> str:
    """Merge two strings, collapsing a trailing run of ``first`` that
    duplicates a leading run of ``second`` (word-boundary, case-insensitive).

    e.g. ``Microsoft Windows Server`` + ``Windows Server 2019 (LTSC)``
    -> ``Microsoft Windows Server 2019 (LTSC)`` (not a naive concatenation,
    which would repeat "Windows Server").
    """
    first_words = first.split()
    second_words = second.split()
    max_overlap = min(len(first_words), len(second_words))
    for overlap in range(max_overlap, 0, -1):
        tail = [w.lower() for w in first_words[-overlap:]]
        head = [w.lower() for w in second_words[:overlap]]
        if tail == head:
            return " ".join(first_words[: len(first_words) - overlap] + second_words)
    return f"{first} {second}"


def join_labels(*parts: object) -> str:
    """Join product/release labels without duplicating an overlapping phrase.

    endoflife.date sometimes returns release labels that already include the
    product name, either as a whole-string prefix (product 'AlmaLinux OS' +
    release 'AlmaLinux OS 9') or as an internal run rather than a prefix
    (product 'Microsoft Windows Server' + release 'Windows Server 2019
    (LTSC)' -- "Windows Server" would otherwise repeat).
    """
    cleaned = [part for part in (_clean(value) for value in parts) if part]
    if not cleaned:
        return ""

    result = cleaned[0]
    for part in cleaned[1:]:
        lower_result = result.lower()
        lower_part = part.lower()
        if lower_part == lower_result or lower_part.startswith(f"{lower_result} "):
            result = part
        elif lower_result == lower_part or lower_result.startswith(f"{lower_part} "):
            continue
        else:
            result = _merge_overlapping_words(result, part)
    return result


def pick_api_os_value(
    os_string: str,
    normalized_os_detailed_name: str,
    normalized_os: str,
) -> str:
    value, _field = pick_api_os_value_with_field(
        os_string, normalized_os_detailed_name, normalized_os
    )
    return value


def pick_api_os_value_with_field(
    os_string: str,
    normalized_os_detailed_name: str,
    normalized_os: str,
) -> tuple[str, str]:
    """Prefer normalized fields, but never query EOL with a cross-vendor value.

    If Normalized OS was wrongly set (e.g. AlmaLinux for Oracle Linux), fall
    back to the raw OS string so the correct product slug is resolved.
    """
    normalized = _clean(normalized_os)
    detailed = _clean(normalized_os_detailed_name)
    source = _clean(os_string)

    candidates: list[tuple[str, str]] = []
    if normalized:
        candidates.append((normalized, "normalized_os"))
    if detailed:
        candidates.append((detailed, "normalized_os_detailed_name"))
    if source:
        candidates.append((source, "os_string"))

    for value, field in candidates:
        if source and field != "os_string" and not vendors_compatible(source, value):
            continue
        return value, field

    if source:
        return source, "os_string"
    return "", ""


# Numbers that *can* be architecture/bitness markers ("64-bit", "x86") rather
# than a real product version -- but a bare one of these is also a completely
# legitimate major version on its own (Android reached major version 16 in
# 2025; product version numbers keep climbing over time), so this set alone
# must never be enough to exclude a value -- see _looks_like_bitness_marker,
# which only excludes one of these when the surrounding text actually reads
# as a bitness marker.
_NON_VERSION_HINTS = frozenset({"16", "32", "64", "86", "128", "256"})

# Accept only strong release matches (exact or multi-segment prefix).
_MIN_RELEASE_SCORE = 80

# "-bit"/" bit"/"bit" (any amount of space/hyphen, or none) right after the
# number -- "64-bit", "64 bit", "64bit", "(64-bit)".
_BITNESS_SUFFIX_RE = re.compile(r"^[\s-]*bit\b", re.I)


def _looks_like_bitness_marker(text: str, match: re.Match[str]) -> bool:
    """True when a candidate bitness number (16/32/64/86/128/256) is actually
    being used as an architecture/bitness marker in this text ("64-bit",
    "32 bit", "x86", "x64") rather than a genuine product version. Real
    incident: Android's own major version reached 16 ('Baklava', 2025) --
    excluding every bare "16" unconditionally left "Android 16" completely
    unmatchable, forever, no matter how the product itself versions from
    here on."""
    suffix = text[match.end() : match.end() + 6]
    if _BITNESS_SUFFIX_RE.match(suffix):
        return True
    prefix = text[max(0, match.start() - 1) : match.start()]
    return prefix.lower() == "x"


def extract_version_hints(os_name: str) -> list[str]:
    """Numeric version tokens suitable for release matching.

    Drops architecture bitness (only when the surrounding text actually
    reads as one -- see ``_looks_like_bitness_marker``) and lone
    service-pack / update markers (``SP3``, ``R2``, ``U1``) so they cannot
    drive a false release pick.
    """
    text = str(os_name or "")
    hints: list[str] = []
    seen: set[str] = set()
    # Negative lookbehind so a compound release tag like "24H2" in the query
    # itself isn't split into "24" + a stray trailing "2" -- same fix as
    # eosl_service.py / microsoft_lifecycle_service.py's _version_tokens.
    for match in re.finditer(r"(?<![A-Za-z])\d+(?:\.\d+)*", text):
        value = match.group()
        if value in seen:
            continue
        if value in _NON_VERSION_HINTS and _looks_like_bitness_marker(text, match):
            continue
        # "3.x or later" is a range, not version 3.
        if re.search(rf"(?<!\d){re.escape(value)}\.x\b", text, re.I):
            continue
        # Lone digit after SP / R / U / (Service) Pack is a pack marker, not
        # a product version -- e.g. the "2" in "Service Pack 2" (spelled
        # out, not just "SP2") must not become a standalone hint either.
        if "." not in value:
            prefix = text[max(0, match.start() - 14) : match.start()]
            if re.search(r"(?:^|[^A-Za-z0-9])(?:SP|R|U|(?:Service\s+)?Pack)\s*$", prefix, re.I):
                continue
        seen.add(value)
        hints.append(value)
    return hints


def _release_name_tokens(text: str) -> list[str]:
    """Numeric tokens embedded in a release name/label/build string.

    A compound slug or label (endoflife.date's Windows releases are named
    like ``11-24h2-w`` / ``11 24H2 (W)``) doesn't parse as a single clean
    dotted version -- version_match.py's naive dot-only split treats the
    whole string as one non-numeric part, so it can never equal a bare hint
    like ``24`` no matter how good a match it actually is. Pulling the
    embedded numbers back out first (same approach eosl_service.py /
    microsoft_lifecycle_service.py use for release names) lets a name-based
    match succeed instead of only ever matching via the raw build number.
    Negative lookbehind keeps a compound tag like "24H2" from also yielding
    a stray trailing "2" token. Same bitness-marker context check as
    ``extract_version_hints`` -- a release token that happens to equal one
    of the bitness numbers (e.g. a product versioned "16") is still a real
    version unless the surrounding text actually reads as "16-bit"/"x16".
    """
    text = text or ""
    tokens: list[str] = []
    for match in re.finditer(r"(?<![A-Za-z])\d+(?:\.\d+)*", text):
        token = match.group()
        if token in _NON_VERSION_HINTS and _looks_like_bitness_marker(text, match):
            continue
        tokens.append(token)
    return tokens


def _release_score(release_name: str, hints: list[str]) -> int:
    """Score a release name/label/build string against every hint at once.

    - The whole string as one dot-aware version (a clean build like
      ``10.0.26100``, or a plain product version like ``24.04``): scored per
      hint via ``score_release_against_hint``, which already refuses a bare
      single-part hint against a multi-part release ("Windows 10" must not
      pick a specific "10.0.19045" build).
    - A compound slug/label with more than one embedded number run
      (endoflife.date's Windows releases: ``11-24h2-w`` / ``11 24H2 (W)`` ->
      tokens ["11", "24"]): this counts as a full match ONLY when every one
      of its tokens is present somewhere among the hints -- e.g. hints
      ["11", "24"] (from a query naming both "11" and "24H2", no build
      number at all) match it, but a single bare hint ["11"] alone must not,
      since that's exactly the over-eager guess a bare major is meant to be
      refused for. Requiring >1 token keeps a plain single-number name (which
      the first bullet already scores correctly) out of this path.
    """
    best = max((score_release_against_hint(release_name, hint) for hint in hints), default=0)
    tokens = _release_name_tokens(release_name)
    if len(tokens) > 1 and all(token in hints for token in tokens):
        best = max(best, 100)
    return best


def _release_latest_name(release: dict[str, Any]) -> str:
    """The release's ``latest.name`` (e.g. Windows' raw NT build number).

    Some products' release ``name``/``label`` is a marketing slug that never
    contains the version string inventory tools actually report (Windows'
    ``11-26h1-e`` release reports build ``10.0.28000`` in ``latest.name``).
    """
    latest = release.get("latest")
    if isinstance(latest, dict):
        return str(latest.get("name") or "")
    return ""


# Edition/channel hints in an OS string -> the release-label substring that
# edition implies. Checked in order: IoT is the more specific signal when
# both IoT and Enterprise appear together (e.g. "Windows 11 IoT Enterprise
# LTSC"), so it's matched first.
_EDITION_LABEL_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\biot\b", re.I), "iot"),
    (re.compile(r"\benterprise\b|\(e\)", re.I), "(e)"),
)


def _edition_label_substring(os_text: str) -> str | None:
    """The release-label substring an OS string's edition/channel implies.

    e.g. an ``os_string`` containing "Enterprise" (or literal "(E)") should
    prefer a release whose label contains "(E)" over the "(W)" consumer
    channel when a build number is otherwise shared by both.
    """
    text = str(os_text or "")
    for pattern, label_substring in _EDITION_LABEL_HINTS:
        if pattern.search(text):
            return label_substring
    return None


def _conservative_release(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Multiple releases tied on match strength (e.g. several Windows editions
    -- IoT LTS / Enterprise LTS / Enterprise / consumer "W" -- share one
    ``latest.name`` build) and we can't tell which the OS actually is.
    Assume the worst case: the earliest EOL/EOAS date among the tied
    releases, so support never looks longer than it might actually be.
    """
    if len(candidates) == 1:
        return candidates[0]

    def sort_key(release: dict[str, Any]) -> tuple[str, str]:
        eol = str(release.get("eolFrom") or "")
        eoas = str(release.get("eoasFrom") or "")
        # Releases with no date at all sort last (never picked as the base
        # over one that actually reports a worst-case date).
        return (eol or "9999-99-99", eoas or "9999-99-99")

    merged = dict(min(candidates, key=sort_key))
    eol_dates = [str(r["eolFrom"]) for r in candidates if r.get("eolFrom")]
    eoas_dates = [str(r["eoasFrom"]) for r in candidates if r.get("eoasFrom")]
    if eol_dates:
        merged["eolFrom"] = min(eol_dates)
    if eoas_dates:
        merged["eoasFrom"] = min(eoas_dates)
    merged["isEol"] = any(bool(r.get("isEol")) for r in candidates)
    merged["isEoas"] = any(bool(r.get("isEoas")) for r in candidates)
    return merged


def _release_candidate_strings(release: dict[str, Any]) -> list[str]:
    release_name = str(release.get("name", "") or "")
    release_label = str(release.get("label", "") or "")
    candidates = [release_name]
    if release_label and release_label != release_name:
        candidates.append(release_label)
    latest_name = _release_latest_name(release)
    if latest_name:
        candidates.append(latest_name)
    return candidates


def _release_required_hints(candidates: list[str], hints: list[str], target_score: int) -> frozenset[str]:
    """Which hint(s) actually explain this release's winning score.

    Used to tell "several editions tied because they all match the SAME
    signal" (safe to conservative-merge -- see the Windows 24H2 case, where
    every tied edition needs the exact same "11"+"24" pair) apart from "two
    DIFFERENT releases each independently matched by a DIFFERENT hint" (a
    sign the query itself names more than one distinct product/version --
    e.g. "Android 14-11" matching release "14" via hint "14" alone AND
    release "11" via hint "11" alone). The former shares a hint across every
    tied candidate; the latter doesn't share anything at all.
    """
    single_hint_matches = {
        hint
        for hint in hints
        if max((score_release_against_hint(name, hint) for name in candidates), default=0) >= target_score
    }
    if single_hint_matches:
        return frozenset(single_hint_matches)
    # No single hint alone reaches the score -- the compound-token "every
    # token present" rule fired instead (see _release_score), whose required
    # set is simply the release's own extracted tokens.
    required: set[str] = set()
    for name in candidates:
        tokens = _release_name_tokens(name)
        if len(tokens) > 1 and all(token in hints for token in tokens):
            required.update(tokens)
    return frozenset(required)


def pick_release(
    releases: list[dict[str, Any]],
    hints: list[str],
    os_text: str = "",
) -> dict[str, Any]:
    """Pick a release only when version evidence is strong.

    - No version hints → no match (never guess the first/latest release).
    - Best score must be >= ``_MIN_RELEASE_SCORE``.
    - Scored against ``release.name``, ``release.label``, and
      ``release.latest.name`` -- a good match on the release's own name/label
      (e.g. "24H2") is tried on equal footing with its raw build number, not
      only the build number, so a query with no build number at all can still
      resolve via the name alone.
    - Multiple releases tied for the best score (one build shared by several
      editions/channels): if ``os_text`` names an edition (Enterprise/(E)/IoT),
      narrow to releases whose label matches it first. Any remaining tie
      falls back to a conservative merge: earliest EOL/EOAS among the tied
      releases -- but ONLY when every tied release is actually explained by
      the same shared hint(s). If the tie is instead made up of genuinely
      different releases each matched via their OWN, non-overlapping hint
      (e.g. "Android 14-11" -> hints ["14", "11"] independently matching
      release "14" and release "11"), that's a sign the query itself names
      more than one distinct product/version -- refuse rather than silently
      picking whichever happens to have the earliest date.
    """
    if not releases or not hints:
        return {}

    best_score = 0
    best_candidates: list[dict[str, Any]] = []
    for release in releases:
        candidates = _release_candidate_strings(release)
        score = max((_release_score(name, hints) for name in candidates), default=0)
        if score > best_score:
            best_score = score
            best_candidates = [release]
        elif score == best_score and score > 0:
            best_candidates.append(release)

    if best_score < _MIN_RELEASE_SCORE or not best_candidates:
        return {}

    if len(best_candidates) > 1:
        edition_substring = _edition_label_substring(os_text)
        if edition_substring:
            edition_matches = [
                release
                for release in best_candidates
                if edition_substring in str(release.get("label") or "").lower()
            ]
            if edition_matches:
                best_candidates = edition_matches

    if len(best_candidates) > 1:
        required_sets = [
            _release_required_hints(_release_candidate_strings(release), hints, best_score)
            for release in best_candidates
        ]
        shared = set(required_sets[0]).intersection(*required_sets[1:]) if required_sets else set()
        if not shared:
            return {}

    return _conservative_release(best_candidates)


def fetch_product(slug: str) -> dict[str, Any]:
    response = requests.get(f"{PRODUCT_V1_URL}/{slug}", headers=HEADERS, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Product response was not an object.")
    return payload


def has_api_date(date_value: Any) -> bool:
    if date_value in (None, "", False, True):
        return False
    cleaned = _clean(date_value)
    if not cleaned:
        return False
    try:
        datetime.strptime(cleaned, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def resolve_lifecycle_status(
    date_value: Any,
    api_status: Any,
    reference_date: str | None = None,
) -> str:
    """
    Status rules:
    - Date present -> leave status blank (date is enough)
    - Date missing and API status true -> "true"
    - Date missing and API status false -> "false"
    - Date missing and API status missing -> blank
    """
    if has_api_date(date_value):
        return ""

    if api_status is True:
        return "true"
    if api_status is False:
        return "false"
    return ""


def iso_date_to_epoch(iso_value: Any) -> str:
    cleaned = _clean(iso_value)
    if not cleaned:
        return ""
    try:
        parsed = datetime.strptime(cleaned, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return str(int(parsed.timestamp()))
    except ValueError:
        return ""


# A clean, presentable version identifier (Ubuntu "24.04", RHEL "9", ...).
_CLEAN_VERSION_NAME_RE = re.compile(r"^\d+(?:\.\d+)*$")


def _presentable_release_name(release: dict[str, Any]) -> str:
    """The release identifier to show in ``normalized_os``.

    Most products' ``name`` is already a clean version (Ubuntu ``24.04``,
    RHEL ``9``) and reads fine on its own. Some products (Windows: ``11-26h1-e``,
    ``10-22h2``, ``7-sp1``) use an internal hyphenated slug for ``name`` that
    is never meant to be shown — ``label`` (``11 26H1 (E)``, ``10 22H2``,
    ``7 SP1``) is the presentable form there, so fall back to it whenever
    ``name`` isn't a plain dotted version number.
    """
    name = _clean(release.get("name"))
    if _CLEAN_VERSION_NAME_RE.match(name):
        return name
    return _clean(release.get("label")) or name


def build_normalization_from_product(
    product_result: dict[str, Any],
    release: dict[str, Any],
) -> dict[str, str]:
    product_label = _clean(product_result.get("label"))
    release_label = _clean(release.get("label"))
    release_name = _presentable_release_name(release)

    return {
        "normalized_os_detailed_name": join_labels(product_label, release_label),
        "normalized_os": join_labels(product_label, release_name),
    }


def lookup_os_eol(
    os_string: str,
    normalized_os_detailed_name: str,
    normalized_os: str,
    valid_slugs: frozenset[str],
    product_cache: dict[str, dict[str, Any]],
    reference_date: str | None = None,
) -> dict[str, str]:
    today = reference_date or date.today().isoformat()
    cleaned_name, query_field = pick_api_os_value_with_field(
        os_string, normalized_os_detailed_name, normalized_os
    )

    empty_result = {
        "eol_date": "",
        "eol_status": "",
        "eoas_date": "",
        "eoas_status": "",
        "normalized_os_detailed_name": "",
        "normalized_os": "",
        "api_note": "",
        "query_used": cleaned_name,
        "query_field": query_field,
        "product_slug": "",
        "release_name": "",
        "release_label": "",
    }

    if not cleaned_name:
        empty_result["api_note"] = "No OS value available"
        return empty_result

    slug = resolve_product_slug(cleaned_name, valid_slugs)
    if not slug:
        empty_result["api_note"] = "Product not found in endoflife.date registry"
        return empty_result

    empty_result["product_slug"] = slug

    try:
        if slug not in product_cache:
            product_cache[slug] = fetch_product(slug)
        product_payload = product_cache[slug]
    except (requests.RequestException, ValueError) as exc:
        empty_result["api_note"] = f"API error: {exc}"
        return empty_result

    product_result = product_payload.get("result")
    if not isinstance(product_result, dict):
        empty_result["api_note"] = "Product details were missing from endoflife.date"
        return empty_result

    releases = product_result.get("releases")
    if not isinstance(releases, list) or not releases:
        empty_result["api_note"] = "No releases found in endoflife.date product data"
        return empty_result

    # Hints from cleaned_name alone, not the raw os_string too. When the query
    # field is a normalized value (preferred above for cross-vendor safety),
    # that value can be coarser than the raw os_string -- Windows' own
    # normalized_os is deliberately family-level ("Microsoft Windows 11",
    # no build), so extract_version_hints(cleaned_name) yields only a bare
    # major like "11". That's too weak to score >= _MIN_RELEASE_SCORE against
    # any specific release (correctly -- a bare major must never match), so
    # release-level lookup would silently find nothing and leave whatever
    # release-level tag ("(W)" vs "(E)") the row already had, permanently,
    # since normalized_os never round-trips the OS's actual build number.
    # The raw os_string still has it ("10.0.22631"), so folding its hints in
    # too preserves the safer product-slug resolution above while restoring
    # the precision needed to pick or correct the specific release.
    release_hints = list(dict.fromkeys(extract_version_hints(os_string) + extract_version_hints(cleaned_name)))
    selected_release = pick_release(
        releases,
        release_hints,
        os_text=f"{os_string} {cleaned_name}",
    )
    if not selected_release:
        empty_result["api_note"] = "No matching release found in endoflife.date product data"
        return empty_result

    product_label = _clean(product_result.get("label"))
    source = _clean(os_string)
    if source and product_label and not vendors_compatible(source, product_label):
        # Wrong product family (e.g. AlmaLinux for Oracle Linux). Retry once with OS string.
        if query_field != "os_string" and source != cleaned_name:
            return lookup_os_eol(
                os_string,
                "",
                "",
                valid_slugs,
                product_cache,
                reference_date=today,
            )
        empty_result["api_note"] = (
            f"EOL product '{product_label}' does not match OS vendor for '{source}'"
        )
        return empty_result

    eol_from = selected_release.get("eolFrom")
    eoas_from = selected_release.get("eoasFrom")
    normalization = build_normalization_from_product(product_result, selected_release)
    release_name = _clean(selected_release.get("name"))
    release_label = _clean(selected_release.get("label"))

    # Never push cross-vendor normalized names even if slug matched loosely.
    if source and not vendors_compatible(
        source,
        " ".join(
            [
                normalization["normalized_os_detailed_name"],
                normalization["normalized_os"],
            ]
        ),
    ):
        normalization = {
            "normalized_os_detailed_name": "",
            "normalized_os": "",
        }

    eol_date = iso_date_to_epoch(eol_from)
    eoas_date = iso_date_to_epoch(eoas_from)
    eol_status = resolve_lifecycle_status(eol_from, selected_release.get("isEol"), today)
    eoas_status = resolve_lifecycle_status(eoas_from, selected_release.get("isEoas"), today)

    return {
        "eol_date": eol_date,
        "eol_status": eol_status,
        "eoas_date": eoas_date,
        "eoas_status": eoas_status,
        "normalized_os_detailed_name": normalization["normalized_os_detailed_name"],
        "normalized_os": normalization["normalized_os"],
        "api_note": "",
        "query_used": cleaned_name,
        "query_field": query_field,
        "product_slug": slug,
        "release_name": release_name,
        "release_label": release_label,
    }


def lookup_os_eol_batch(
    items: list[dict[str, str]],
    reference_date: str | None = None,
    product_cache: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Batch endoflife.date lookups, fetching each distinct product slug once.

    ``product_cache`` defaults to a fresh dict scoped to this call. Pass a
    dict shared across multiple calls (e.g. one Refresh run split into
    chunks) so a product already fetched by an earlier chunk is never
    re-fetched from the network by a later one -- the fix for Refresh being
    slow on a large lookup: without this, a common slug like "windows" or
    "ios" would get re-requested from endoflife.date once per chunk that
    happens to contain a matching row, instead of once for the whole run.
    """
    valid_slugs = get_valid_slugs()
    if product_cache is None:
        product_cache = {}
    fetch_errors: dict[str, Exception] = {}

    slugs_needed: set[str] = set()
    for item in items:
        cleaned_name = pick_api_os_value(
            item.get("os_string", ""),
            item.get("normalized_os_detailed_name", ""),
            item.get("normalized_os", ""),
        )
        if not cleaned_name:
            continue
        slug = resolve_product_slug(cleaned_name, valid_slugs)
        if slug:
            slugs_needed.add(slug)

    # Only fetch what a prior call on this same (shared) cache hasn't
    # already brought back -- the whole point of accepting an external cache.
    slugs_to_fetch = slugs_needed - product_cache.keys()
    if slugs_to_fetch:
        workers = min(EOL_FETCH_WORKERS, len(slugs_to_fetch))

        def fetch_slug(slug: str) -> tuple[str, dict[str, Any] | None, Exception | None]:
            try:
                return slug, fetch_product(slug), None
            except (requests.RequestException, ValueError) as exc:
                return slug, None, exc

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fetch_slug, slug) for slug in slugs_to_fetch]
            for future in as_completed(futures):
                slug, payload, error = future.result()
                if payload is not None:
                    product_cache[slug] = payload
                elif error is not None:
                    fetch_errors[slug] = error

    results: list[dict[str, str]] = []
    for item in items:
        cleaned_name = pick_api_os_value(
            item.get("os_string", ""),
            item.get("normalized_os_detailed_name", ""),
            item.get("normalized_os", ""),
        )
        slug = resolve_product_slug(cleaned_name, valid_slugs) if cleaned_name else None
        if slug and slug not in product_cache and slug in fetch_errors:
            _value, query_field = pick_api_os_value_with_field(
                item.get("os_string", ""),
                item.get("normalized_os_detailed_name", ""),
                item.get("normalized_os", ""),
            )
            results.append(
                {
                    "eol_date": "",
                    "eol_status": "",
                    "eoas_date": "",
                    "eoas_status": "",
                    "normalized_os_detailed_name": "",
                    "normalized_os": "",
                    "api_note": f"API error: {fetch_errors[slug]}",
                    "query_used": cleaned_name,
                    "query_field": query_field,
                    "product_slug": slug,
                    "release_name": "",
                    "release_label": "",
                }
            )
            continue

        results.append(
            lookup_os_eol(
                os_string=item.get("os_string", ""),
                normalized_os_detailed_name=item.get("normalized_os_detailed_name", ""),
                normalized_os=item.get("normalized_os", ""),
                valid_slugs=valid_slugs,
                product_cache=product_cache,
                reference_date=reference_date,
            )
        )

    return results
