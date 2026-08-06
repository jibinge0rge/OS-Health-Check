"""Evidence classification and draft-vs-data diffing for the redesigned UI.

These are additive helpers consumed by app.py. They read the same evidence
sidecar shape (`by_os[os_string] -> {detailed, normalized, eol}`) that already
exists in `_data/eol_lookup_evidence.json` / `_draft/eol_lookup_evidence.json`
(see app.py's `load_evidence`/`save_evidence`) and the method vocabulary
already used client-side in templates/index.html's
`describeNormalizationMethod` (manual, loaded, none, fuzzy, ai, fuzzy+ai, eol,
ambiguous) plus the vendor source ids used as `proof.eol.method` when a vendor
cascade lookup resolved the row (eosl, microsoft-lifecycle, junos, suse,
layer23-switch, router-switch) or "api" for the primary endoflife.date lookup.
"""

from __future__ import annotations

from normalization_service import AMBIGUOUS_OS

CSV_HEADERS = [
    "os_string",
    "normalized_os_detailed_name",
    "normalized_os",
    "eol_date",
    "eol_status",
    "eoas_date",
    "eoas_status",
]


def is_ambiguous_row(row: dict) -> bool:
    """True when a row is marked Ambiguous OS (its OS value contains '/',
    so it could be more than one product) -- such a row must never receive
    EOL/EOAS enrichment. There's no way to know which of the ambiguous
    candidates a fetched date would even belong to, and querying a lifecycle
    source with the literal text "Ambiguous OS" has produced real false
    positives (e.g. matching an unrelated product by coincidental version
    number overlap once the lookup falls back to the raw OS string)."""
    return str(row.get("normalized_os_detailed_name") or "").strip().casefold() == AMBIGUOUS_OS


# Chip set from the design README's Column filters -> Matched by section.
# Vendor sources without a dedicated chip (layer23-switch, router-switch)
# still get a real category value; they just fall outside the fixed chip set
# and are only reachable via "All".
_METHOD_TO_MATCHED_BY = {
    "api": "endoflife.date",
    "eosl": "eosl.date",
    "microsoft-lifecycle": "Microsoft Lifecycle",
    "junos": "Juniper Junos",
    "suse": "SUSE Lifecycle",
    "layer23-switch": "Layer23-Switch EOL",
    "router-switch": "Router-Switch EOL",
    "fuzzy": "Fuzzy",
    "ai": "AI",
    "fuzzy+ai": "AI",
    "manual": "Manual",
    "ambiguous": "Ambiguous",
    # Retired method: no current code path writes this anymore (dates were
    # once copied from another row sharing the same normalized pair instead
    # of resolving from a real source). Old evidence still carries it, so it
    # must still classify as *something* other than "No match" -- that would
    # claim there's no recorded match at all, when there plainly is one
    # (see _method_summary's "lookup-fallback" branch for the recorded
    # fallbackFrom row). "Fuzzy" is the closest existing category: derived
    # from another matching row rather than a live vendor/API source.
    "lookup-fallback": "Fuzzy",
}

MATCHED_BY_CHIPS = [
    "All",
    "endoflife.date",
    "Fuzzy",
    "AI",
    "eosl.date",
    "Microsoft Lifecycle",
    "Juniper Junos",
    "SUSE Lifecycle",
    "Manual",
    "Ambiguous",
    "No match",
]

_FIELD_LABELS = {
    "detailed": "Normalized OS detailed name",
    "normalized": "Normalized OS",
    "eol": "EOL / EOAS lifecycle",
}

_METHOD_SUMMARIES = {
    "manual": "Edited by hand in this session.",
    "loaded": "Came from the saved lookup. No match evidence is stored for this row.",
    "none": "No normalized value is set.",
    "ambiguous": "Marked as Ambiguous OS because the OS value contains '/'.",
}


def _matched_record_note(slot: dict) -> str:
    """' -- matched "X" (query: "Y")' suffix naming exactly which vendor/API
    record a lookup resolved to, so the evidence detail isn't just "filled
    from eosl.date" with no way to tell which of its products/releases that
    means. Blank when the slot has neither a release label/slug nor the
    query text (e.g. an older evidence entry recorded before these fields
    existed)."""
    release_label = str(slot.get("releaseLabel") or "").strip()
    product_slug = str(slot.get("productSlug") or "").strip()
    query_used = str(slot.get("queryUsed") or "").strip()
    record = release_label or product_slug

    parts = []
    if record:
        parts.append(f'matched "{record}"')
    if query_used:
        parts.append(f'query: "{query_used}"')
    if not parts:
        return ""
    return " -- " + ", ".join(parts)


def _method_summary(method: str, slot: dict) -> str:
    if method in _METHOD_SUMMARIES:
        return _METHOD_SUMMARIES[method]
    if method in {"fuzzy", "ai", "fuzzy+ai"}:
        score = slot.get("score")
        score_note = f" Match score {score}%." if score is not None else ""
        matched_pair = str(slot.get("queryUsed") or "").strip()
        pair_note = f' Matched existing pair for "{matched_pair}".' if matched_pair else ""
        if method == "ai":
            return f"AI chose an existing normalized pair.{score_note}{pair_note}"
        if method == "fuzzy+ai":
            return f"Fuzzy found a candidate and AI confirmed it.{score_note}{pair_note}"
        return f"Fuzzy matched an existing lookup entry.{score_note}{pair_note}"
    if method == "api":
        return f"Filled from the endoflife.date lookup{_matched_record_note(slot)}."
    if method == "lookup-fallback":
        fallback_from = slot.get("fallbackFrom")
        source_os = str(fallback_from.get("os_string") or "").strip() if isinstance(fallback_from, dict) else ""
        source_note = f' from row "{source_os}"' if source_os else ""
        return (
            "Retired method: dates were copied from another row sharing the same "
            f"normalized OS pair{source_note}, not resolved from a live source. "
            "Re-run lookup to verify against current data."
        )
    if method in _METHOD_TO_MATCHED_BY:
        label = _METHOD_TO_MATCHED_BY[method]
        return f"Filled from the {label} local vendor lifecycle database{_matched_record_note(slot)}."
    return "No match evidence recorded."


def classify_matched_by(method: str | None) -> str:
    """Map an internal evidence method string to a Matched-by chip category."""
    normalized = str(method or "").strip().lower()
    return _METHOD_TO_MATCHED_BY.get(normalized, "No match")


def row_matched_by(evidence_entry: dict | None, row: dict | None = None) -> str:
    """The single overall Matched-by category shown for a row.

    Prefers the lifecycle (eol) slot's method since that is what the
    Matched-by filter is really describing (which source resolved the row);
    falls back to the normalization slots when there is no lifecycle match.

    Ambiguous rows are classified from the row itself, not evidence: no code
    path actually records a method="ambiguous" evidence entry (the field is
    set directly on the row, not via a lookup), so without this an ambiguous
    row with no other evidence would misreport as "No match" instead of
    "Ambiguous" -- exactly the kind of mismatch that made the Matched-by
    filter look unreliable.
    """
    if row is not None and is_ambiguous_row(row):
        return _METHOD_TO_MATCHED_BY.get("ambiguous", "Ambiguous")

    if not isinstance(evidence_entry, dict):
        return "No match"

    for slot_name in ("eol", "normalized", "detailed"):
        slot = evidence_entry.get(slot_name)
        if isinstance(slot, dict):
            method = str(slot.get("method") or "").strip().lower()
            if method and method not in {"none", "loaded"}:
                return classify_matched_by(method)
    return "No match"


# Which row field a normalization slot's value actually lives in -- used to
# catch a stale evidence slot (method "none") that no longer matches reality
# because the row field itself is non-empty. That combination only happens
# for values set before evidence tracking existed for this field (e.g. an
# older import, or a retired normalization path) -- "No normalized value is
# set." would be flatly false in that case, since the drawer shows the value
# right above the evidence list.
_ROW_FIELD_FOR_SLOT = {
    "detailed": "normalized_os_detailed_name",
    "normalized": "normalized_os",
}


def build_evidence_entries(evidence_entry: dict | None, row: dict) -> dict:
    """Shape one row's 3-slot evidence into the drawer's evidence list.

    Returns {"matched_by": str, "entries": [{"method", "field", "detail"}]}.
    """
    entry = evidence_entry if isinstance(evidence_entry, dict) else {}
    entries: list[dict[str, object]] = []

    for slot_name in ("detailed", "normalized", "eol"):
        slot = entry.get(slot_name)
        if not isinstance(slot, dict):
            continue
        method = str(slot.get("method") or "none").strip().lower()
        detail = _method_summary(method, slot)
        row_field = _ROW_FIELD_FOR_SLOT.get(slot_name)
        if method == "none" and row_field and str(row.get(row_field) or "").strip():
            detail = (
                "This value predates evidence tracking for this field -- no "
                "record of how it was set. Re-run lookup to verify it."
            )
        entries.append(
            {
                "method": method,
                "field": _FIELD_LABELS.get(slot_name, slot_name),
                "detail": detail,
                "query_used": str(slot.get("queryUsed") or "").strip(),
                "product_slug": str(slot.get("productSlug") or "").strip(),
                "release_label": str(slot.get("releaseLabel") or slot.get("releaseName") or "").strip(),
                "score": slot.get("score"),
            }
        )

    return {"matched_by": row_matched_by(entry, row), "entries": entries}


def _result_has_any_resolved_value(result: dict) -> bool:
    """True when a lookup result actually resolved *something* -- a
    date/status, or a normalized name -- as opposed to a genuine miss that
    still gets passed through build_eol_evidence_slot unconditionally (see
    app.py's _apply_lifecycle_result, which always writes the 'eol' slot
    for the endoflife.date direct-API attempt, hit or miss)."""
    return bool(
        str(result.get("eol_date") or "").strip()
        or str(result.get("eol_status") or "").strip()
        or str(result.get("eoas_date") or "").strip()
        or str(result.get("eoas_status") or "").strip()
        or str(result.get("normalized_os_detailed_name") or "").strip()
        or str(result.get("normalized_os") or "").strip()
    )


def build_eol_evidence_slot(result: dict) -> dict:
    """Shape one endoflife.date / vendor-cascade lookup result into the 'eol'
    evidence slot, matching the schema templates/index.html already writes
    (`buildEolProofMetaFromApi`/`buildVendorProofMetaFromApi`).

    Real incident: the endoflife.date direct-API path never sets a
    `"source"` key on its result dict at all (only the vendor cascade
    does, e.g. "eosl"/"junos") -- and this function used to default
    ``method`` to `"api"` whenever `source` was empty, with NO check for
    whether the lookup actually found anything. Since app.py's
    `_apply_lifecycle_result` writes this slot unconditionally after the
    endoflife.date attempt (hit or miss) -- only the vendor cascade skips
    writing it on ITS OWN miss -- a row that missed absolutely everywhere
    (no product resolved, no vendor fallback either) still ended up with
    `method: "api"`, which `row_matched_by`/`classify_matched_by` reads as
    a genuine "endoflife.date" match, not "No match". A completely
    fictional os_string (nothing could possibly resolve it) would
    silently show "Matched by: endoflife.date" with an evidence note
    claiming it was "Filled from the endoflife.date lookup" -- misleading,
    and it hid genuinely-unresolved rows from the "No match" filter.
    ``method`` now only defaults to "api" when the result actually
    resolved something (a date/status or a normalized name);
    otherwise it's left empty, which `row_matched_by` already treats the
    same way it treats "none"/"loaded" -- falls through instead of
    reporting a confirmed source that was never real.
    """
    source = str(result.get("source") or "").strip().lower()
    method = source or ("api" if _result_has_any_resolved_value(result) else "")
    return {
        "method": method,
        "queryUsed": str(result.get("query_used") or "").strip(),
        "queryField": str(result.get("query_field") or "").strip(),
        "productSlug": str(result.get("product_slug") or "").strip(),
        "releaseName": str(result.get("release_name") or "").strip(),
        "releaseLabel": str(result.get("release_label") or "").strip(),
        "apiNote": str(result.get("api_note") or "").strip(),
        "got": {
            "eol_date": str(result.get("eol_date") or ""),
            "eol_status": str(result.get("eol_status") or ""),
            "eoas_date": str(result.get("eoas_date") or ""),
            "eoas_status": str(result.get("eoas_status") or ""),
            "normalized_os_detailed_name": str(result.get("normalized_os_detailed_name") or ""),
            "normalized_os": str(result.get("normalized_os") or ""),
        },
    }


def _dedupe_key(os_string: str) -> str:
    return str(os_string or "").strip().lower()


def _comparable_cell(value: object) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered
    return text


def _rows_equal(data_row: dict, draft_row: dict) -> bool:
    return all(
        _comparable_cell(data_row.get(header)) == _comparable_cell(draft_row.get(header))
        for header in CSV_HEADERS
        if header != "os_string"
    )


def _row_unresolved(row: dict) -> bool:
    normalized_os = str(row.get("normalized_os") or "").strip()
    eol_date = str(row.get("eol_date") or "").strip()
    eoas_date = str(row.get("eoas_date") or "").strip()
    return not normalized_os or (not eol_date and not eoas_date)


def compute_lookup_diff(data_rows: list[dict], draft_rows: list[dict]) -> dict:
    """Draft-vs-Data diff: added / edited / deleted os_strings + unresolved count.

    Row identity is the trimmed, lowercased os_string (first match wins on
    duplicates) — the same key the existing client-side diff
    (`dataBaselineKey`/`findDataBaselineRow` in templates/index.html) already
    uses, so behaviour matches what NEW/EDITED flags meant before this was a
    server endpoint.
    """
    data_by_key: dict[str, dict] = {}
    for row in data_rows:
        key = _dedupe_key(row.get("os_string"))
        if key and key not in data_by_key:
            data_by_key[key] = row

    draft_keys_seen: set[str] = set()
    added: list[str] = []
    edited: list[str] = []
    unresolved = 0

    for row in draft_rows:
        key = _dedupe_key(row.get("os_string"))
        if key:
            draft_keys_seen.add(key)
            baseline = data_by_key.get(key)
            if baseline is None:
                added.append(row.get("os_string", ""))
            elif not _rows_equal(baseline, row):
                edited.append(row.get("os_string", ""))
        # Rows with a blank os_string have no stable identity to diff by —
        # a position-based fallback was tried, but position shifts with
        # every unrelated add/delete elsewhere in the file, so it flagged an
        # untouched blank row as newly "added" the moment anything before it
        # moved. Left out of added/edited entirely rather than guessed at;
        # still counted in unresolved since that check doesn't need identity.
        if _row_unresolved(row):
            unresolved += 1

    deleted = [
        row.get("os_string", "")
        for key, row in data_by_key.items()
        if key not in draft_keys_seen
    ]

    return {
        "added": added,
        "edited": edited,
        "deleted": deleted,
        "unresolved": unresolved,
        "added_count": len(added),
        "edited_count": len(edited),
        "deleted_count": len(deleted),
    }


