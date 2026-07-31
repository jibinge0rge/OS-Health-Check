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

CSV_HEADERS = [
    "os_string",
    "normalized_os_detailed_name",
    "normalized_os",
    "eol_date",
    "eol_status",
    "eoas_date",
    "eoas_status",
]

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


def _method_summary(method: str, slot: dict) -> str:
    if method in _METHOD_SUMMARIES:
        return _METHOD_SUMMARIES[method]
    if method in {"fuzzy", "ai", "fuzzy+ai"}:
        score = slot.get("score")
        score_note = f" Match score {score}%." if score is not None else ""
        if method == "ai":
            return f"AI chose an existing normalized pair.{score_note}"
        if method == "fuzzy+ai":
            return f"Fuzzy found a candidate and AI confirmed it.{score_note}"
        return f"Fuzzy matched an existing lookup entry.{score_note}"
    if method == "api":
        return "Filled from the endoflife.date lookup."
    if method in _METHOD_TO_MATCHED_BY:
        label = _METHOD_TO_MATCHED_BY[method]
        return f"Filled from the {label} local vendor lifecycle database."
    return "No match evidence recorded."


def classify_matched_by(method: str | None) -> str:
    """Map an internal evidence method string to a Matched-by chip category."""
    normalized = str(method or "").strip().lower()
    return _METHOD_TO_MATCHED_BY.get(normalized, "No match")


def row_matched_by(evidence_entry: dict | None) -> str:
    """The single overall Matched-by category shown for a row.

    Prefers the lifecycle (eol) slot's method since that is what the
    Matched-by filter is really describing (which source resolved the row);
    falls back to the normalization slots when there is no lifecycle match.
    """
    if not isinstance(evidence_entry, dict):
        return "No match"

    for slot_name in ("eol", "normalized", "detailed"):
        slot = evidence_entry.get(slot_name)
        if isinstance(slot, dict):
            method = str(slot.get("method") or "").strip().lower()
            if method and method not in {"none", "loaded"}:
                return classify_matched_by(method)
    return "No match"


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
        entries.append(
            {
                "method": method,
                "field": _FIELD_LABELS.get(slot_name, slot_name),
                "detail": _method_summary(method, slot),
                "query_used": str(slot.get("queryUsed") or "").strip(),
                "product_slug": str(slot.get("productSlug") or "").strip(),
                "release_label": str(slot.get("releaseLabel") or slot.get("releaseName") or "").strip(),
                "score": slot.get("score"),
            }
        )

    return {"matched_by": row_matched_by(entry), "entries": entries}


def build_eol_evidence_slot(result: dict) -> dict:
    """Shape one endoflife.date / vendor-cascade lookup result into the 'eol'
    evidence slot, matching the schema templates/index.html already writes
    (`buildEolProofMetaFromApi`/`buildVendorProofMetaFromApi`)."""
    method = str(result.get("source") or "").strip().lower() or "api"
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


def _group_by_key(rows: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
    """Groups rows by dedupe key, keeping ALL rows per key (not first-wins)
    so a real duplicate os_string is never silently collapsed. Returns
    (groups, blank_os_string_rows)."""
    groups: dict[str, list[dict]] = {}
    blanks: list[dict] = []
    for row in rows:
        key = _dedupe_key(row.get("os_string"))
        if not key:
            blanks.append(row)
            continue
        groups.setdefault(key, []).append(row)
    return groups, blanks


def _evidence_by_dedupe_key(evidence: dict) -> dict[str, dict]:
    by_os = evidence.get("by_os") if isinstance(evidence, dict) else None
    if not isinstance(by_os, dict):
        return {}
    result: dict[str, dict] = {}
    for os_string, entry in by_os.items():
        key = _dedupe_key(os_string)
        if key and key not in result:
            result[key] = entry
    return result


def merge_lookup_rows(
    base_rows: list[dict],
    current_rows: list[dict],
    draft_rows: list[dict],
    base_evidence: dict,
    current_evidence: dict,
    draft_evidence: dict,
) -> dict:
    """3-way merge for publish: reconciles what Data has become (`current`)
    against what the draft changed (`draft`), relative to what Data looked
    like when the draft started (`base`) -- so publishing never blindly
    overwrites changes someone else already published in the meantime.

    Row identity and equality reuse `_dedupe_key`/`_rows_equal`, the same
    semantics `compute_lookup_diff` already uses.

    Duplicate os_string handling: `compute_lookup_diff` accepts "first
    occurrence wins per key" as a read-only reporting simplification, which
    is fine for a KPI count but not for a merge that decides what gets
    written -- a dict keyed by identity would silently drop the second/
    third row under a duplicated key, and real data has duplicates. So any
    key present more than once in base, current, or draft is never
    content-diffed here; it's always surfaced as an "ambiguous_duplicate"
    conflict carrying every row under that key from both sides, and the
    caller's resolution decides what gets written.

    Blank-os_string rows have no stable identity (same reasoning
    `compute_lookup_diff` documents) and pass through from the draft
    untouched, never merge-compared.

    Evidence follows whichever row version wins for a given key rather than
    being unioned independently of the row merge -- otherwise a row's
    evidence could end up describing how a *different* version of that row
    was derived than the one actually kept.
    """
    base_groups, _ = _group_by_key(base_rows)
    current_groups, _ = _group_by_key(current_rows)
    draft_groups, draft_blanks = _group_by_key(draft_rows)

    current_evidence_by_key = _evidence_by_dedupe_key(current_evidence)
    draft_evidence_by_key = _evidence_by_dedupe_key(draft_evidence)

    all_keys = set(base_groups) | set(current_groups) | set(draft_groups)
    merged_rows: list[dict] = []
    merged_by_os: dict[str, dict] = {}
    conflicts: list[dict] = []

    for key in all_keys:
        base_group = base_groups.get(key, [])
        current_group = current_groups.get(key, [])
        draft_group = draft_groups.get(key, [])

        if len(base_group) > 1 or len(current_group) > 1 or len(draft_group) > 1:
            sample = (base_group + current_group + draft_group)[0]
            conflicts.append(
                {
                    "os_string": sample.get("os_string", ""),
                    "kind": "ambiguous_duplicate",
                    "mine": {"rows": draft_group, "evidence": draft_evidence_by_key.get(key, {})},
                    "theirs": {"rows": current_group, "evidence": current_evidence_by_key.get(key, {})},
                }
            )
            continue

        base_row = base_group[0] if base_group else None
        current_row = current_group[0] if current_group else None
        draft_row = draft_group[0] if draft_group else None

        def keep(row: dict, evidence_by_key: dict[str, dict]) -> None:
            merged_rows.append(row)
            entry = evidence_by_key.get(key)
            if entry:
                merged_by_os[row.get("os_string", "")] = entry

        def conflict(kind: str) -> None:
            conflicts.append(
                {
                    "os_string": (current_row or draft_row or base_row).get("os_string", ""),
                    "kind": kind,
                    "mine": {"row": draft_row, "evidence": draft_evidence_by_key.get(key, {})} if draft_row else None,
                    "theirs": {"row": current_row, "evidence": current_evidence_by_key.get(key, {})} if current_row else None,
                }
            )

        if base_row is None:
            if current_row is not None and draft_row is not None:
                if _rows_equal(current_row, draft_row):
                    keep(current_row, current_evidence_by_key)
                else:
                    conflict("added_both")
            elif current_row is not None:
                keep(current_row, current_evidence_by_key)
            elif draft_row is not None:
                keep(draft_row, draft_evidence_by_key)
            continue

        if current_row is None and draft_row is None:
            continue  # deleted on both sides -- nothing to keep

        if current_row is None:
            if _rows_equal(base_row, draft_row):
                continue  # local never touched it -- respect the upstream deletion
            conflict("edited_local_deleted_upstream")
            continue

        if draft_row is None:
            if _rows_equal(base_row, current_row):
                continue  # upstream never touched it -- respect the local deletion
            conflict("edited_upstream_deleted_local")
            continue

        upstream_changed = not _rows_equal(base_row, current_row)
        local_changed = not _rows_equal(base_row, draft_row)
        if not local_changed:
            # Unchanged locally: keep upstream's version (a no-op copy if
            # upstream didn't change either) -- always safe since local
            # never diverged from base for this row.
            keep(current_row, current_evidence_by_key)
        elif not upstream_changed:
            keep(draft_row, draft_evidence_by_key)
        elif _rows_equal(current_row, draft_row):
            keep(current_row, current_evidence_by_key)
        else:
            conflict("edited_both")

    merged_rows.extend(draft_blanks)

    return {
        "merged_rows": merged_rows,
        "merged_evidence": {"by_os": merged_by_os, "updated_at": ""},
        "conflicts": conflicts,
    }
