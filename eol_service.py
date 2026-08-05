"""endoflife.date lookup helpers for UI-added operating systems."""

from __future__ import annotations

import difflib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any

import requests

from normalization_service import is_placeholder_os_value, vendors_compatible
from version_match import numeric_version_parts, score_release_against_hint

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
    # A server-generation year (2008/2011/2012/2016/2019/2022/2025) alongside
    # any mention of "win"/"windows" means Windows SERVER even when the
    # inventory string drops the word "Server" entirely (a common real-world
    # shorthand: "Windows 2008 R2 Standard", "Win 2008 R2", "Windows 2008 -
    # Standard") -- none of these years is ever a Windows CLIENT version
    # (client releases are named "7"/"8"/"10"/"11", or "XP"/"Vista"), so this
    # is unambiguous. Without it, these fell through to the generic "windows"
    # (client) phrase-index entry instead, which then has no matching release
    # at all for a year it was never versioned by. Both lookaheads are
    # order-independent (zero-width) since "2008"/"R2"/"Win" can appear in
    # either order across real inventory strings.
    (r"(?=.*\bwin(?:dows)?\b)(?=.*\b(?:2008|2011|2012|2016|2019|2022|2025)\b)", "windows-server"),
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
    # Real-world os_string never spells out "iPadOS" -- it's always just
    # "iPad <version>". Safe to add now that get_product_catalog() filters
    # to category "os" only: the bare word "ipad" used to also be the
    # hardware "ipad" product's own slug/label (category "device"), so this
    # alias would have been ambiguous before that filter existed.
    "ipados": ("ipad",),
}

# Product to retry against, still within the endoflife.date direct-API path,
# when the resolved product has NO release matching the query's hints at
# all. "ipados" as a distinct endoflife.date product only tracks major
# version 12 and up -- Apple didn't introduce "iPadOS" as a separate product
# name until 2019 (what would otherwise have been "iOS 13"). A real iPad
# running an earlier version genuinely ran plain "iOS" at the time, and
# endoflife.date's own "ios" product has real release/EOL data for those
# earlier majors. Without this, a query like "iPad 10.0.2" resolves to
# "ipados" (correctly, via the alias above), finds nothing there, and falls
# all the way through to the local vendor cascade (eosl.date) for a lookup
# endoflife.date itself can actually answer directly -- just under a
# different, older product name for that specific version range.
_PRODUCT_RELEASE_FALLBACK_SLUGS: dict[str, str] = {
    "ipados": "ios",
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
    """endoflife.date's catalog covers far more than operating systems --
    languages, frameworks, databases, server apps, services, standards, and
    (crucially) hardware *devices* all share the same /v1/products list,
    distinguished only by a "category" field. This app's os_string field is
    specifically an OS version string, so only ``category == "os"`` products
    are ever valid match targets here -- everything else is filtered out at
    the source, before it can ever reach the phrase index or valid-slugs set.

    Real incident: Apple's "ipad" product (``category: "device"``, tracking
    hardware generations, not software) shares the bare word "ipad" with
    every "iPad <version>"-style os_string in the wild -- since it has no
    alias to disambiguate it from "ipados" (``category: "os"``, the actual
    iPadOS software lifecycle), it was winning the phrase-index match purely
    because "ipad" also happens to be its own slug/label. Filtering to
    category "os" removes the hardware product from consideration entirely,
    rather than trying to out-prioritize it release by release.
    """
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
        if isinstance(item, dict) and _clean(item.get("name")) and item.get("category") == "os":
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
            return slug if _generic_family_match_is_trustworthy(slug, os_name) else None

    matched = _match_slug_from_index(normalized, valid_slugs, slug_index=slug_index)
    if matched:
        return matched if _generic_family_match_is_trustworthy(matched, os_name) else None

    hyphenated = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    if hyphenated in valid_slugs:
        return hyphenated if _generic_family_match_is_trustworthy(hyphenated, os_name) else None

    return None


# Products whose own slug/label is a single, universally generic word that
# a real-world os_string can easily contain WITHOUT actually meaning "the
# thing this endoflife.date page specifically tracks." Mapped to the word
# that must literally appear in the query before the match is trusted.
_GENERIC_FAMILY_TRUST_WORDS: dict[str, str] = {
    # endoflife.date's "linux" product tracks the Linux KERNEL's own
    # release/EOL schedule -- not any particular distribution. Its slug
    # AND label are just "linux"/"Linux Kernel", so the phrase index (and
    # the hyphenated fallback) would otherwise match ANY os_string
    # containing that one common word. Real incident: "Linux 6.4.7.3762 7"
    # (a distro whose specific name never got recognized, or a generic
    # placeholder) resolved confidently to "Linux Kernel 6.4" and adopted
    # the kernel project's own EOL date -- nothing in the os_string
    # actually said "kernel" at all.
    "linux": "kernel",
}


def _generic_family_match_is_trustworthy(slug: str, os_name: str) -> bool:
    trust_word = _GENERIC_FAMILY_TRUST_WORDS.get(slug)
    if trust_word is None:
        return True
    # Plain substring, not word-bounded -- endoflife.date's own recognized
    # alias for this product is the glued "linuxkernel" (no separator at
    # all), so a strict \bkernel\b would itself refuse a real-world string
    # shaped exactly like that alias.
    return trust_word.lower() in os_name.lower()


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

# How close a release's prospective new normalized name must be to the row's
# existing one for _pick_release_by_prior_value to accept it as "the same
# release, endoflife.date just renamed it" rather than a genuinely different
# release. See that function's docstring.
_PRIOR_VALUE_SIMILARITY_THRESHOLD = 0.95

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
    # Deliberately a 2-character lookbehind, not a blanket "any letter" one:
    # it only excludes a digit run whose start is preceded by exactly
    # [digit][single letter] -- the "24H2" shape (digit, one-letter suffix
    # marker, digit) -- not by a real multi-letter WORD glued directly to a
    # number with no space (e.g. "WindowsServer2008R2", "CentOS7.9"). A
    # blanket "any letter" exclusion blocked the digit run's true start in
    # that case too, so the regex instead started matching one character in
    # -- "WindowsServer2008R2" yielded the hint "008", not "2008" -- a real,
    # reported incident that silently broke product/release matching for
    # every glued inventory string shaped like this.
    for match in re.finditer(r"(?<![0-9][A-Za-z])\d+(?:\.\d+)*", text):
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
            # A bare 4-digit trailing " - <year>" (SPACED dash) at the very
            # END of the string, coming after an already-complete OS name,
            # is real-world inventory metadata (an install/license/audit-
            # year stamp) -- not a second named OS version. Real incident:
            # "Microsoft Windows Server 2008 R2 - 2012" names ONE OS (2008
            # R2); the "- 2012" isn't claiming this is ALSO Windows Server
            # 2012. Without this exclusion, "2012" tied against the genuine
            # "2008" hint with zero evidence in common, correctly (per the
            # "two different releases" rule) refusing to guess between them
            # -- except this string only ever named one. Scoped narrowly:
            # only fires when a hint has ALREADY been captured earlier in
            # the string (never discards the only version information
            # present), only for the LAST token in the string (a trailing
            # stamp, not a version mentioned mid-string), and REQUIRES
            # whitespace before the dash -- "Android 14-11"'s unspaced
            # "14-11" must NOT be caught by this (that hyphen separates two
            # independent version hints, not a name-vs-metadata split).
            if hints and re.search(r"\s-\s*$", prefix) and not text[match.end() :].strip():
                continue
        seen.add(value)
        hints.append(value)

    # "<dotted version> (<build number>)" -- e.g. "Windows 10.0 (14393)" -- is
    # a common inventory-tool rendering of one combined build number
    # ("10.0.14393") split across a trailing parenthetical. Without this, the
    # two halves are extracted as independent, disconnected hints above:
    # "10.0" alone is a genuine numeric *prefix* of every Windows 10/11 build
    # (they all start "10.0."), so it ties across the ENTIRE family, and the
    # bare "14393" never scores against a build number at all --
    # score_release_against_hint only recognizes prefix relationships, never
    # "hint is the trailing segment of a longer dotted release" -- so every
    # such row fell back to whichever release has the conservatively-earliest
    # EOL, regardless of which specific build was actually named. Adding the
    # combined dotted hint alongside the two separate ones lets an exact
    # match win outright over the family-wide tie.
    for match in re.finditer(r"(\d+(?:\.\d+)+)\s*\((\d+)\)", text):
        combined = f"{match.group(1)}.{match.group(2)}"
        if combined not in seen:
            seen.add(combined)
            hints.append(combined)

    # Same idea, no parentheses -- e.g. "Windows 10.0 22631 64-bit". The
    # trailing number must already be a *kept* hint (present in `hints`
    # above) before combining: that's what stops this from ever absorbing a
    # genuine bitness/SP marker -- the "64" in "64-bit" was already excluded
    # from `hints` by the bitness check above, so it's never a candidate
    # here, only the real build number ("22631") is. Restricted to 4+ digit
    # trailing numbers (real build numbers are always long) to keep this
    # from firing on short, likely-unrelated trailing digits.
    for match in re.finditer(r"(\d+(?:\.\d+)+)\s+(\d{4,})\b", text):
        prefix, trailing = match.group(1), match.group(2)
        if trailing in hints:
            combined = f"{prefix}.{trailing}"
            if combined not in seen:
                seen.add(combined)
                hints.append(combined)
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
    a stray trailing "2" token -- narrowed to a 2-character lookbehind (only
    excludes a run preceded by exactly [digit][single letter]), same as
    ``extract_version_hints``, so a name/label glued directly to a number
    with no separator isn't truncated the same way "WindowsServer2008R2"
    used to be. Same bitness-marker context check as ``extract_version_hints``
    -- a release token that happens to equal one of the bitness numbers
    (e.g. a product versioned "16") is still a real version unless the
    surrounding text actually reads as "16-bit"/"x16". Same lone SP/R/U/Pack
    marker-digit exclusion as ``extract_version_hints`` too -- endoflife.date's
    own Windows Server release names are themselves compound slugs like
    ``2008-r2-sp1``/``2008-sp2``/``2012-r2``, whose embedded "2"/"1" (from
    "r2"/"sp2"/"sp1") are patch/edition markers, not real, independent
    version tokens; without excluding them, the compound-token rule below
    (which requires *every* token present in the query's hints) never fires
    for these names at all -- a hint set of just ``["2008"]`` doesn't
    include the release's own spurious "2", so matching failed entirely (a
    real, reported incident).
    """
    text = text or ""
    tokens: list[str] = []
    for match in re.finditer(r"(?<![0-9][A-Za-z])\d+(?:\.\d+)*", text):
        token = match.group()
        if token in _NON_VERSION_HINTS and _looks_like_bitness_marker(text, match):
            continue
        if "." not in token:
            prefix = text[max(0, match.start() - 14) : match.start()]
            if re.search(r"(?:^|[^A-Za-z0-9])(?:SP|R|U|(?:Service\s+)?Pack)\s*$", prefix, re.I):
                continue
        tokens.append(token)
    return tokens


def _hint_matches_build_suffix(release_name: str, hint: str) -> bool:
    """A bare, undotted, 4+-digit hint that exactly equals the LAST segment
    of a multi-part release version -- e.g. hint ``"17763"`` against release
    ``"10.0.17763"``.

    Real incident: real-world inventory text often quotes only the
    trailing, most memorable segment of a Windows build number ("Build
    17763") without the leading "10.0" anywhere nearby -- so the existing
    "combine an adjacent dotted-version + trailing build number into one
    hint" pass in ``extract_version_hints`` never fires (there's no dotted
    version right next to it to combine with), and a bare hint can only
    ever *prefix*-match a release from the front (``score_release_against_hint``
    only tests "is one side a numeric prefix of the other", never a
    suffix) -- so "17763" alone never matched Windows Server release
    "2019" (``latest.name`` "10.0.17763") at all. This was the missing
    piece that made "Windows Server 2019 ... Version 1809 Build 17763"
    (hints ``["2019", "1809", "17763"]``) refuse outright: releases "2019"
    and "1809-sac" share that exact build, so the query's genuine "2019"
    hint and the coincidentally-present "1809" hint (also a real generation
    marker, just the wrong one to prefer) each independently matched only
    their OWN release's name, with no hint recognized as common to both --
    the shared-hint tie-break saw an empty intersection and refused before
    dominant-evidence ever got a chance to prefer "2019". Recognizing
    "17763" as confirming BOTH releases (since it's their literal shared
    build) restores the common ground the dominant-evidence check needs, so
    it can then correctly prefer "2019" for carrying the additional,
    more-specific "2019" hint that "1809-sac" doesn't. A 4+ digit floor
    (matching the same threshold already used for the dotted+trailing
    build-number combination) keeps this from ever firing on a short,
    low-entropy number where a coincidental match would be a real concern.
    """
    if "." in hint:
        return False
    hint_nums = numeric_version_parts(hint)
    rel_nums = numeric_version_parts(release_name)
    if not hint_nums or not rel_nums:
        return False
    if len(hint_nums) != 1 or len(rel_nums) <= 1:
        return False
    if hint_nums[0] < 1000:
        return False
    return hint_nums[0] == rel_nums[-1]


def _score_release_candidate(release_name: str, hint: str) -> int:
    """``score_release_against_hint``, plus the build-number-suffix rule above."""
    score = score_release_against_hint(release_name, hint)
    if score < 100 and _hint_matches_build_suffix(release_name, hint):
        return 100
    return score


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
      refused for.
    - A SINGLE embedded token also counts as a full match when it's exactly
      present in the hints -- for a release name that's cleanly numeric on
      its own (a plain "8"), this is redundant with the first bullet (which
      already scores it correctly) and changes nothing. It matters for a
      release name that ISN'T clean (endoflife.date's Windows Server names
      are compound slugs: ``2008-r2-sp1``/``2008-sp2``/``2012-r2`` -- the
      first bullet can never score these at all, since the whole string
      isn't a dotted version). ``_release_name_tokens`` already strips lone
      SP/R/U/Pack marker digits, so what's left for e.g. ``2008-sp2`` is a
      single real token, ``["2008"]`` -- an exact hint match here is exactly
      as strict as the first bullet's own exact-match tier, not a guess.
    """
    best = max((_score_release_candidate(release_name, hint) for hint in hints), default=0)
    tokens = _release_name_tokens(release_name)
    if tokens and all(token in hints for token in tokens):
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
# edition implies. Checked in order, most specific first: IoT is the more
# specific signal when both IoT and Enterprise appear together (e.g.
# "Windows 11 IoT Enterprise LTSC"); LTSC/LTS is more specific than a bare
# Enterprise mention, since every LTS release IS also an Enterprise release
# (its own label is "... (E) (LTS)", a strict superset of "(E)") -- without
# checking it first, "Windows 10 Enterprise LTSC 10.0.17763" would narrow
# only as far as "(e)", which matches BOTH the LTS release ("10 1809 (E)
# (LTS)") and the plain Enterprise one ("10 1809 (E)") sharing that same
# build, leaving them tied and letting the conservative "earliest EOL"
# merge silently pick the plain Enterprise release (2021) over the LTS one
# actually named in the string (2029) -- a real, reported incident.
# "R2" (Windows Server's own generation marker, e.g. "2008 R2" vs plain
# "2008", "2012 R2" vs plain "2012") is checked before Enterprise: this
# function returns the FIRST matching pattern and stops, so if Enterprise
# were checked first for a query like "Windows Server 2008 R2 Enterprise
# 7600", it would win the priority check even though neither 2008 release's
# label contains "(E)" at all (2008/2008 R2 only differ by SP/R2 level, not
# by SKU) -- a harmless no-op narrowing that still means R2 never gets a
# chance to fire. Checking R2 first avoids that.
#
# R2's own pattern uses `(?<![A-Za-z])r2(?![0-9A-Za-z])`, not `\br2\b` --
# real incident: "WindowsServer2012R2 9600" (a glued-word inventory string,
# same shape as the "WindowsServer2008R2" digit-truncation bug) has "R2"
# immediately preceded by the digit "2" -- both are \w characters, so \b
# never fires between them, and `\br2\b` silently never matched at all.
# Without edition narrowing, "2012" (whose OWN bare name is a clean numeric
# exact match on hint "2012") and "2012-r2" (confirmed via the build-
# suffix-match rule on "9600") tied with NEITHER dominating the other under
# the dominant-evidence check either (each has its own distinct, genuine
# piece of strong evidence the other lacks) -- resolving only by
# accidentally-matching conservative-merge EOL-date tiebreaking, not
# genuine confirmation. The new pattern only excludes "r2" when a LETTER
# (not a digit) immediately precedes it, so "2012R2" now correctly
# recognizes "R2" as an edition marker while still declining to match
# inside an unrelated word ending "...r2" or followed by more letters/digits
# (e.g. "R2D2").
_EDITION_LABEL_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\biot\b", re.I), "iot"),
    (re.compile(r"\bltsc\b|\blts\b", re.I), "(lts)"),
    (re.compile(r"(?<![A-Za-z])r2(?![0-9A-Za-z])", re.I), "r2"),
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


def _release_strong_hints(candidates: list[str], hints: list[str], target_score: int) -> frozenset[str]:
    """Hints that confirm this release via an ordinary exact/prefix/suffix
    match (`_score_release_candidate`) against one of its OWN literal
    candidate strings -- i.e. everything except the compound-token "every
    token present somewhere" rule, which is a looser, name-only heuristic
    (see `_release_required_hints` below for why that distinction matters).
    """
    return frozenset(
        hint
        for hint in hints
        if max((_score_release_candidate(name, hint) for name in candidates), default=0) >= target_score
    )


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

    Takes the UNION of every mechanism that can confirm this release --
    `_release_strong_hints` (ordinary exact/prefix/suffix match) AND
    the compound-token "every token present" rule -- rather than only
    falling back to the compound-token rule when NO single hint reaches the
    score alone. Real incident: Windows Server's compound slugs "2008-sp2"/
    "2008-r2-sp1" only reach 100 via the compound-token rule (their own
    slug isn't a clean dotted version at all), while a coincidentally-
    present build number like "7601" can ALSO reach 100 via the build-
    suffix-match rule -- treating these as alternatives (the pre-union
    logic) meant "2008-r2-sp1" reported a required set of just {"7601"},
    silently dropping the "2008" it's ALSO genuinely confirmed by, and
    "2008-sp2" (confirmed only via "2008") no longer shared anything with
    it -- an empty intersection, refusing a release with objectively
    *more*, not less, evidence than its tied sibling. Unioning both gives
    "2008-r2-sp1" the full {"2008", "7601"} it's actually confirmed by, a
    strict superset of "2008-sp2"'s {"2008"} alone, letting the dominant-
    evidence check (right after this one) correctly prefer it.

    Used ONLY for the shared-hint / empty-intersection check -- the
    dominant-evidence check itself compares `_release_strong_hints` instead
    (see the comment where it's called), since a compound-token match is a
    weaker, name-only heuristic that shouldn't by itself outweigh another
    tied release's OWN equally-weak compound-token match.
    """
    required: set[str] = set(_release_strong_hints(candidates, hints, target_score))
    for name in candidates:
        tokens = _release_name_tokens(name)
        if tokens and all(token in hints for token in tokens):
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
    - If that strict pass finds nothing at all, a bare/dot-less hint (e.g.
      "15") gets one more try against a release literally named "<hint>.0"
      (see the dot-zero fallback below) before finally giving up.
    """
    if not releases or not hints:
        return {}

    result = _pick_release_with_hints(releases, hints, os_text)
    if result:
        return result

    return _pick_release_by_dot_zero_release_name(releases, hints)


def _pick_release_by_dot_zero_release_name(releases: list[dict[str, Any]], hints: list[str]) -> dict[str, Any]:
    """Fallback for a bare, dot-less hint (e.g. "15") against a catalog whose
    release for that exact version is named "<hint>.0" (e.g. "15.0") rather
    than the bare number itself. Real case: os_string "SUSE Linux Enterprise
    Server 15 SP7" -> extract_version_hints drops the SP-marker digit and
    yields a bare "15" alone, while endoflife.date's actual SLES release for
    this row is named "15.0" -- a bare hint can't score against *any*
    multi-part release name by design (the "bare major must not guess" rule
    above), even though "15" and "15.0" plainly mean the same release.

    Deliberately NOT implemented by just appending ".0" and re-running the
    general scoring pipeline: that pipeline's genuine "numeric prefix" rule
    (a 90-point score) would then let a synthesized "15.0" hint match any
    LONGER build/release string that merely *starts* with "15.0..." -- e.g.
    Windows' own NT kernel numbering is "10.0.NNNNN" for every 10/11 build,
    so a bare "Windows 10" query (correctly refused everywhere else in this
    module -- see test_windows_build_number_matches_via_latest_name) would
    wrongly resolve to a specific build via a fake "10.0" hint prefix-
    matching "10.0.26100" etc. Instead, this only accepts an EXACT string
    match against a release's own ``name``/``label`` (never ``latest.name``,
    which is where those long build numbers live), and only when exactly
    one release matches -- a genuinely ambiguous catalog (several
    candidates) still refuses, same as everywhere else in this module.
    """
    bare_hints = {hint for hint in hints if hint.isdigit()}
    if not bare_hints:
        return {}
    dot_zero_targets = {f"{hint}.0" for hint in bare_hints}

    matches = [
        release
        for release in releases
        if _clean(release.get("name")) in dot_zero_targets or _clean(release.get("label")) in dot_zero_targets
    ]
    if len(matches) != 1:
        return {}
    return matches[0]


def _best_scoring_releases(
    releases: list[dict[str, Any]], hints: list[str]
) -> tuple[int, list[dict[str, Any]]]:
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
    return best_score, best_candidates


def _pick_release_with_hints(
    releases: list[dict[str, Any]],
    hints: list[str],
    os_text: str,
) -> dict[str, Any]:
    best_score, best_candidates = _best_scoring_releases(releases, hints)

    # A product whose entire catalog is bare, major-version-only release
    # names (RHEL: "4".."10", CentOS: "5".."8", iOS: "5".."26", ...) can
    # never have a release that EXACTLY matches a dotted hint like "6.6" --
    # the dotted hint only ever reaches the release's own bare major number
    # via the weaker 90-point prefix score. Meanwhile a totally unrelated
    # standalone bare number floating in the query (a kernel-version
    # fragment, a build counter, ...) can coincidentally EXACT-match some
    # OTHER release's own bare name with a full 100 -- outright outscoring
    # the correct match, not even a tie. Real incident: "RHEL 6.6 3 8" (kernel
    # 3.8, space-separated instead of dotted) resolved to release "8" (from
    # the bare "8" hint exact-matching it) instead of release "6" (from the
    # genuine "6.6" hint, prefix-scored at only 90) -- same shape broke
    # "CentOS 7.9 5 4" and "iOS 16.7 10" (a real iOS 16.7.10 point release,
    # space- instead of dot-separated) too. A standalone bare number is
    # inherently far more likely to coincidentally collide with an unrelated
    # release's bare name than a genuine dotted version is, so when scoring
    # using ONLY the dotted hint(s) resolves to a DIFFERENT release than
    # scoring with the full hint set, prefer the dotted-only result --
    # but ONLY when the dotted-only pass itself lands on a single, unique
    # release. Real regression: "WindowsServer2016 10.0" (hints ["2016",
    # "10.0"]) already resolves uniquely and correctly to release "2016" on
    # the full hint set (its own name token "2016" is a hint, scored 100 via
    # the compound-token rule) -- but "10.0" alone is a genuine numeric
    # prefix of EVERY modern Windows Server release's build number, so the
    # dotted-only pass ties roughly a dozen releases at 90. Without the
    # uniqueness requirement below, that coarse 12-way tie unconditionally
    # replaced the correct, unique 100-score answer, and the tie then failed
    # the later exact-score requirement -- silently turning a clean match
    # into "no match found". Requiring the dotted-only pass to itself
    # resolve to exactly one release keeps the original RHEL/CentOS/iOS fix
    # intact (their bare-major-only catalogs always give a unique dotted-only
    # winner -- "6.6" can only ever numeric-prefix-match release "6", never
    # "7" or "8") while no longer overriding an already-unambiguous answer
    # with a hint that's too coarse to mean anything on its own.
    dotted_hints = [hint for hint in hints if "." in hint]
    if dotted_hints and dotted_hints != hints:
        dotted_score, dotted_candidates = _best_scoring_releases(releases, dotted_hints)
        if (
            dotted_score >= _MIN_RELEASE_SCORE
            and len(dotted_candidates) == 1
            and {r.get("name") for r in dotted_candidates} != {r.get("name") for r in best_candidates}
        ):
            best_score, best_candidates = dotted_score, dotted_candidates

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
            # No hint literally ties these together -- but if every tied
            # candidate is the exact SAME underlying release (identical
            # latest.name/build), that's structural evidence they're
            # editions/names of one thing, independent of which hints the
            # query happens to contain. Real incident: "Microsoft Hyper-V
            # Windows Server 2019  Version 1809" (no build number at all)
            # ties "2019" (LTSC) against "1809-sac" (Semi-Annual Channel) --
            # both build 10.0.17763 -- but with no "17763"/"10.0.17763" hint
            # in the query to act as common ground, "2019"'s required set is
            # {"2019"} and "1809-sac"'s is {"1809"}: an empty intersection,
            # indistinguishable by hint alone from "Android 14-11" (two
            # genuinely different releases). The catalog itself already
            # proves they're the same release under two names -- Windows
            # Server 2019 IS internally versioned "1809" (Microsoft's own
            # docs call it "Windows Server 2019, Version 1809"); "1809 SAC"
            # is a separate product that happens to collide on that number.
            # Falling through here still requires the dominant-evidence
            # check right below to actually pick a winner -- this only
            # avoids refusing outright when refusing would discard a
            # correct answer the catalog can already prove is available.
            latest_names = {_release_latest_name(release) for release in best_candidates}
            if len(latest_names) != 1 or not next(iter(latest_names)):
                return {}

        # A tied candidate confirmed by STRICTLY MORE evidence than every
        # other tied candidate isn't "one of several equally-plausible
        # editions" -- it's simply the better-supported match, and wins
        # outright rather than being averaged with the others. Real
        # incident: os_string "Microsoft Windows Server 2019 Datacenter
        # 10.0.17763 0" ties release "2019" (Windows Server 2019 LTSC)
        # against "1809-sac" (Windows Server 1809 Semi-Annual Channel) --
        # both share build 10.0.17763 (required hint {"10.0.17763"} for
        # 1809-sac), but "2019" is ALSO independently confirmed by the "2019"
        # hint (required {"2019", "10.0.17763"}), a hint 1809-sac's own name/
        # label never matches at all. The old shared-hint check only asked
        # "is there SOME common hint" (yes) and conservative-merged to
        # whichever has the earliest EOL -- 1809-sac's much shorter
        # Semi-Annual-Channel support window -- discarding the "2019" hint
        # the query actually gave. A tied candidate whose required-hint set
        # is a strict superset of every other tied candidate's is preferred
        # outright; this never fires for genuinely equal editions (e.g. the
        # Windows 24H2 case, where every tied release needs the identical
        # "11"+"24" pair -- no superset relationship exists there at all).
        #
        # Compares STRONG hints only (`_release_strong_hints` -- ordinary
        # exact/prefix/suffix matches), not the full `required_sets` above
        # (which also includes compound-token-only evidence). Real
        # incident: "Windows Server 2019 Datacenter AD Version 1809 Build
        # 17763" (no adjacent "10.0" for the dotted+trailing-build combining
        # pass to attach "17763" to) ties "2019" against "1809-sac" the same
        # way -- but "1809-sac" is ALSO independently confirmed by its OWN
        # name via the compound-token rule (a bare release slug "1809-sac"
        # matching hint "1809"), just as "2019"'s name matches hint "2019"
        # via an ordinary exact match. Weighing both as equally-strong
        # "extra" evidence makes NEITHER dominate (each has one hint the
        # other lacks) -- but a compound-token match is a looser, name-only
        # heuristic (built for slugs that aren't clean versions at all),
        # while "2019"'s exact match is a literal, unambiguous
        # identification. Comparing strong-only evidence keeps "1809-sac"'s
        # required set to just its shared "17763" (no ordinary match on
        # "1809" against its own non-numeric slug), while "2019" still has
        # both "2019" and "17763" -- a genuine strict superset.
        strong_sets = [
            _release_strong_hints(_release_candidate_strings(release), hints, best_score)
            for release in best_candidates
        ]
        if len(best_candidates) > 1:
            dominant = [
                i
                for i, req in enumerate(strong_sets)
                if all(req >= other for j, other in enumerate(strong_sets) if j != i)
                and any(req > other for j, other in enumerate(strong_sets) if j != i)
            ]
            if len(dominant) == 1:
                best_candidates = [best_candidates[dominant[0]]]

        # A tie is only safe to conservative-merge (earliest EOL/EOAS) when
        # every tied release was matched via an EXACT signal (100) -- either
        # the literal same build/name, or the compound-token rule's "every
        # token present" full match. A tie that only ever reached the
        # *weaker* prefix-match score (90, from score_release_against_hint)
        # means the hint was coarser than every tied release's own version,
        # not that they're "several editions of one confirmed thing." Real
        # incident: a bare "10.0" hint is a genuine numeric prefix of EVERY
        # Windows 10/11 build (they all start "10.0."), so it tied across
        # the entire family and conservative-merged to whichever release
        # has the earliest EOL -- "Windows 10.0" alone (no build number at
        # all) was resolving to "Microsoft Windows 10 1507" as if that were
        # a confirmed match. If the hint can't even pin down ONE specific
        # release, several different releases sharing that same coarse
        # hint is not "safe to average away" -- it's simply "we don't know
        # which one," and must refuse instead. (A single, non-tied 90-score
        # match is unaffected -- see e.g. "RHEL 7.4" resolving to release
        # "7" -- this only guards the *multi-candidate* case.)
        if best_score < 100:
            return {}

    return _conservative_release(best_candidates)


def _text_similarity(a: str, b: str) -> float:
    a_clean = _clean(a).casefold()
    b_clean = _clean(b).casefold()
    if not a_clean or not b_clean:
        return 0.0
    return difflib.SequenceMatcher(None, a_clean, b_clean).ratio()


def _is_plausible_version_extension(prior_value: str, release: dict[str, Any]) -> bool:
    """True when the release's own bare version number is a genuine
    numeric prefix/extension of one of the prior value's extracted version
    hints, or vice versa -- e.g. "15" -> "15.2" (the catalog got more
    specific) or "15.2" -> "15" (coarser). This is the actual relationship
    _pick_release_by_prior_value exists to recover; character-level text
    similarity alone can't tell it apart from two completely UNRELATED
    version numbers that just happen to look similar as flat strings.

    Real incident: a row's prior normalized value was "Apple iOS 27" (an
    invalid/future version number someone typed) -- and endoflife.date's
    real release "7" (iOS 7, from 2013) scored a 95.65% text-similarity
    match against it, PURELY because "Apple iOS 7" is one character
    shorter than "Apple iOS 27" (SequenceMatcher's ratio formula rewards
    the shorter total-length pairing), while every other, equally
    plausible release ("17", "20"..."26") scored under 92%. "27" and "7"
    have no genuine prefix/extension relationship at all -- this check
    catches that "27" is not "15"-style precursor of "7" (nor "7" of
    "27") and refuses, where the old text-only check would confidently
    (and wrongly) rewrite the row to iOS 7's decade-old EOL/EOAS dates.

    Only applies when the release's own name is cleanly numeric (a bare
    or dotted version, e.g. SUSE's "15.2") -- a compound slug (Windows
    Server's "2008-sp2") can't be parsed this way at all, so it's left to
    the existing text-similarity check alone, unchanged.
    """
    release_name = _clean(release.get("name"))
    release_nums = numeric_version_parts(release_name)
    if release_nums is None:
        return True

    for hint in extract_version_hints(prior_value):
        hint_nums = numeric_version_parts(hint)
        if hint_nums is None:
            continue
        if len(hint_nums) <= len(release_nums):
            shorter, longer = hint_nums, release_nums
        else:
            shorter, longer = release_nums, hint_nums
        if longer[: len(shorter)] == shorter:
            return True
    return False


def _pick_release_by_prior_value(
    releases: list[dict[str, Any]],
    product_label: str,
    prior_detailed: str,
    prior_normalized: str,
) -> dict[str, Any]:
    """Fallback for when ``pick_release``'s version-hint scoring finds
    nothing at all, used only when the row already has a prior normalized
    value on record to compare against.

    endoflife.date's own catalog gets more precise over time -- a release
    once named e.g. "15" can later be renamed "15.2" once the maintainers
    start tracking service packs individually. A hint set that used to score
    a clean match against the old, coarser release name no longer scores
    against any of today's more specific releases at all (a bare major must
    never match a multi-part release -- see ``pick_release``'s "bare major"
    rule), so the row would otherwise sit permanently unresolved despite
    endoflife.date clearly still tracking it, just under a more specific name.

    This only fires when exactly ONE release's prospective new name (product
    label + release label/name, the same shape ``build_normalization_from_product``
    writes) is a near-exact (>=95%) textual match to what the row already
    had, AND that release's own version number is a genuine numeric
    prefix/extension of the prior value's version (see
    ``_is_plausible_version_extension`` -- guards against two unrelated
    version numbers that merely look similar as flat text, e.g. a prior
    value of "27" scoring 95%+ against release "7" purely from shared
    length/characters, with no real "15" -> "15.2"-style relationship at
    all). If the catalog now lists *several* similarly-named releases (e.g.
    multiple SUSE service packs all close to a bare "15"), that's genuine
    ambiguity this fallback must refuse rather than guess -- same philosophy
    as the tie-break rules in ``pick_release`` above.
    """
    prior_values = [
        value for value in (prior_detailed, prior_normalized)
        if value and not is_placeholder_os_value(value)
    ]
    if not prior_values:
        return {}

    matches: list[dict[str, Any]] = []
    for release in releases:
        prospective = (
            join_labels(product_label, _clean(release.get("label"))),
            join_labels(product_label, _presentable_release_name(release)),
        )
        # Require the SAME prior value that clears the similarity bar to
        # also pass the version-extension check below -- not just "some
        # prior value is textually similar AND some (possibly different)
        # prior value is a plausible extension." See
        # _is_plausible_version_extension for why both checks are needed.
        qualifies = any(
            _text_similarity(prior, candidate) >= _PRIOR_VALUE_SIMILARITY_THRESHOLD
            and _is_plausible_version_extension(prior, release)
            for prior in prior_values
            for candidate in prospective
        )
        if qualifies:
            matches.append(release)

    if len(matches) != 1:
        return {}
    return matches[0]


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

    # A stale or manually-wrong normalized field can name the WRONG product
    # within the SAME vendor family -- vendors_compatible (checked further
    # below) only catches cross-VENDOR mismatches (AlmaLinux vs Oracle
    # Linux), not this. Real incident: os_string "iPad 10.3.4" had
    # normalized_os previously set to "Apple iOS 10" -- both are "apple"
    # vendor, so the cross-vendor gate never fires, and the row would
    # confidently pull iOS's own (wrong) EOL/EOAS instead of iPadOS's.
    # Products with a deliberate inventory-alias entry in
    # _INVENTORY_PHRASE_EXTRAS (currently just "ipados", via the "ipad"
    # alias) are a strong, hand-curated signal -- if the raw os_string
    # independently resolves to one of these but the preferred field
    # resolved to something else, the preferred field is more likely stale
    # than this deliberate override is wrong.
    if query_field != "os_string" and slug not in _INVENTORY_PHRASE_EXTRAS:
        source_value = _clean(os_string)
        if source_value and source_value != cleaned_name:
            os_string_slug = resolve_product_slug(source_value, valid_slugs)
            if os_string_slug and os_string_slug != slug and os_string_slug in _INVENTORY_PHRASE_EXTRAS:
                return lookup_os_eol(
                    os_string, "", "", valid_slugs, product_cache, reference_date=today
                )

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
    product_label = _clean(product_result.get("label"))
    selected_release = pick_release(
        releases,
        release_hints,
        os_text=f"{os_string} {cleaned_name}",
    )
    if not selected_release:
        # Ordinary hint scoring found nothing -- try the prior-value fallback
        # before giving up (see _pick_release_by_prior_value: only fires when
        # exactly one release is a near-exact rename of what the row already
        # had, e.g. a coarser "15" catalog entry becoming "15.2").
        selected_release = _pick_release_by_prior_value(
            releases, product_label, normalized_os_detailed_name, normalized_os
        )
    if not selected_release and slug in _PRODUCT_RELEASE_FALLBACK_SLUGS:
        # This product resolved correctly but has NO release covering this
        # query at all (e.g. "ipados" has nothing before major 12) -- retry
        # against its designated fallback product before giving up, still
        # entirely within the direct endoflife.date path.
        fallback_slug = _PRODUCT_RELEASE_FALLBACK_SLUGS[slug]
        if fallback_slug in valid_slugs:
            try:
                if fallback_slug not in product_cache:
                    product_cache[fallback_slug] = fetch_product(fallback_slug)
                fallback_payload = product_cache[fallback_slug]
            except (requests.RequestException, ValueError):
                fallback_payload = None
            fallback_result = fallback_payload.get("result") if fallback_payload else None
            fallback_releases = (
                fallback_result.get("releases") if isinstance(fallback_result, dict) else None
            )
            if isinstance(fallback_releases, list) and fallback_releases:
                fallback_release = pick_release(
                    fallback_releases,
                    release_hints,
                    os_text=f"{os_string} {cleaned_name}",
                )
                if fallback_release:
                    slug = fallback_slug
                    product_result = fallback_result
                    product_label = _clean(product_result.get("label"))
                    selected_release = fallback_release
    if not selected_release:
        empty_result["api_note"] = "No matching release found in endoflife.date product data"
        return empty_result

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
