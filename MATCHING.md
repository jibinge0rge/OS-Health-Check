# How matching works

This document explains, in detail, how this app decides that a raw inventory
string (`os_string`) corresponds to a specific product/release in a lifecycle
source, and therefore which **EOL** / **EOAS** dates and normalized names get
written onto a row. It also covers the two other kinds of "matching" in the
app — AI/fuzzy normalization matching, and row-identity matching used for
Draft/Data diffing and publish — more briefly, since they're less central and
much simpler.

If you only read one section, read [The lifecycle-matching cascade](#the-lifecycle-matching-cascade)
and [Picking a release](#picking-a-release-pick_release--_pick_release) — that's
where almost every past false-match/no-match bug in this app has lived, and
where the safety rules below were each added in direct response to one.

Everything here is driven from three fields already on a row:

- `os_string` — the raw inventory string, e.g. `"Windows 10.0.26100.7171"`
- `normalized_os_detailed_name` — e.g. `"Microsoft Windows 11 24H2 (W)"`
- `normalized_os` — e.g. `"Microsoft Windows 11 24H2"`

---

## Table of contents

1. [Where matching happens in the UI](#where-matching-happens-in-the-ui)
2. [Step 0 — which field gets queried](#step-0--which-field-gets-queried-pick_api_os_value_with_field)
3. [The lifecycle-matching cascade](#the-lifecycle-matching-cascade)
4. [Source 1 — endoflife.date direct API](#source-1--endoflifedate-direct-api-eol_servicepy)
   - [Resolving the product](#resolving-the-product-resolve_product_slug)
   - [Extracting version hints](#extracting-version-hints-extract_version_hints)
   - [Picking a release (`pick_release` / `_pick_release`)](#picking-a-release-pick_release--_pick_release)
   - [Vendor compatibility gate](#vendor-compatibility-gate-vendors_compatible)
5. [Source 2 — the local vendor cascade](#source-2--the-local-vendor-cascade)
   - [eosl.date (local mirror)](#eosldate-local-mirror-eosl_servicepy)
   - [Microsoft Lifecycle](#microsoft-lifecycle-microsoft_lifecycle_servicepy)
   - [Juniper Junos](#juniper-junos-junos_servicepy)
   - [SUSE Lifecycle](#suse-lifecycle-suse_servicepy)
   - [Layer23-Switch / Router-Switch](#layer23-switch--router-switch-hardware-eol)
6. [Ambiguous OS rows](#ambiguous-os-rows-are-never-queried)
7. [What gets written to the row, and evidence](#what-gets-written-to-the-row-and-evidence)
8. [Every safety rule, in one table](#every-safety-rule-in-one-table)
9. [Worked example: the "Windows 11 24H2" case](#worked-example-the-windows-11-24h2-case)
10. [Secondary: normalization matching (fuzzy + AI)](#secondary-normalization-matching-fuzzy--ai)
11. [Secondary: row-identity matching (diff / publish merge)](#secondary-row-identity-matching-diff--publish-merge)
12. [Glossary of thresholds](#glossary-of-thresholds)

---

## Where matching happens in the UI

| UI action | Code path | Scope |
|---|---|---|
| Toolbar **Refresh EOL/EOAS** (no selection) | `lookup_refresh_events` → `refresh_rows_lifecycle_chunk` | every row in Data/Draft, chunked |
| Toolbar **Refresh EOL/EOAS** (with a selection) / bulk bar **Refresh lifecycle** | same, `rows` limited to the selection | selected rows only |
| Row drawer **Re-run lookup** | `POST /api/lookup/row/refresh` → `refresh_rows_lifecycle_chunk` (chunk of 1) | one row |
| **Add OS** pipeline's final step | `lookup_rows_refresh_events` → `refresh_rows_lifecycle_chunk` | newly added rows |

All four ultimately call the same function, `refresh_rows_lifecycle_chunk`
(`app.py`), which runs the same two-stage cascade for every row:

```
1. endoflife.date direct API   (lookup_os_eol_batch)
2. Local vendor cascade        (lookup_vendor_batch) -- only for rows still
                                unresolved after step 1
```

A row is "still unresolved" when it has neither an `eol_date` nor an
`eoas_date` after step 1 (`_row_has_lifecycle_data`). If endoflife.date
resolved it, the vendor cascade is never even tried for that row.

---

## Step 0 — which field gets queried (`pick_api_os_value_with_field`)

Every lookup function (`lookup_os_eol`, `lookup_os_eosl`,
`lookup_os_microsoft_lifecycle`, `lookup_os_junos`, `lookup_os_suse`, …) first
picks **one** value to actually query with, in this priority order:

1. `normalized_os` (if set)
2. `normalized_os_detailed_name` (if set)
3. `os_string` (always available as the final fallback)

But a candidate is only accepted if it's **vendor-compatible** with the raw
`os_string` (see [vendor compatibility](#vendor-compatibility-gate-vendors_compatible)
below) — this stops a wrongly-set `normalized_os` from making the whole
lookup query with the wrong vendor's product entirely.

**Example — normal case:**
`os_string="Oracle Linux Server 9.5"`, `normalized_os="Oracle Linux 9"` →
queries with `"Oracle Linux 9"` (`normalized_os`, since it's vendor-compatible).

**Example — the safety net firing:**
`os_string="Oracle Linux Server 9.5"`, `normalized_os="AlmaLinux OS 9"`
(wrongly set, e.g. by a bad earlier fuzzy match) → `normalized_os` is
rejected (AlmaLinux ≠ Oracle), falls through to `os_string` instead, so the
lookup still resolves against the *correct* vendor.

**Important:** the value chosen here (`query_used`, shown in evidence) is
what's used to resolve the **product**. Release-level matching (which exact
release/build) later folds in hints from `os_string` too — see
[picking a release](#picking-a-release-pick_release--_pick_release).

---

## The lifecycle-matching cascade

```
                         ┌─────────────────────────┐
                         │  endoflife.date (API)    │  always tried first
                         └────────────┬─────────────┘
                                      │ no match?
                                      ▼
        ┌──────────────────────────────────────────────────────┐
        │        Local vendor cascade (fixed order, per-source  │
        │        enable flag + keyword gate from Settings)      │
        │                                                        │
        │   eosl.date → microsoft-lifecycle → junos → suse       │
        │        → layer23-switch → router-switch                │
        │                                                        │
        │   Stops at the first source that produces a real       │
        │   eol_date/eoas_date. Sources with no keyword match     │
        │   (or disabled) are skipped entirely.                  │
        └──────────────────────────────────────────────────────┘
```

The vendor cascade order is **fixed** and not user-configurable
(`vendor_lookups/vendor_settings.py`'s `VENDOR_FALLBACK_ORDER`). What *is*
configurable per source, in Settings → Vendor lookups:

| Source | Enabled by default | Keyword-gated? | Default keywords |
|---|---|---|---|
| `eosl` (eosl.date) | ✅ | no (empty list = always eligible) | — |
| `microsoft-lifecycle` | ✅ | no (empty list = always eligible) | — |
| `junos` | ✅ | yes | `junos`, `juniper` |
| `suse` | ✅ | yes | `suse`, `sles`, `opensuse` |
| `layer23-switch` | ❌ off by default | yes | cisco, arista, aruba, dell, fortinet, h3c, hpe, juniper, mellanox, palo alto/pan-os, ruckus, ios-xe/xr, nx-os |
| `router-switch` | ❌ off by default | yes | same list as layer23-switch |

A keyword-gated source is only tried when **at least one** of its keywords
appears (as a whole word/phrase, case-insensitive) in `os_string`,
`normalized_os_detailed_name`, or `normalized_os` — see
`query_matches_keywords` in `vendor_lookups/vendor_settings.py`. This is why
enabling `junos` doesn't slow down every single non-Juniper row: it's simply
skipped unless the query actually mentions "junos"/"juniper" somewhere.

Within the cascade, each source is tried **in order**, and the loop stops at
the first one that returns real `eol_date`/`eoas_date` data
(`_has_lifecycle_data` in `vendor_lookup_service.py::lookup_vendor_batch`). If
every source misses, the row's `api_note` is a concatenation of every
source's own miss reason (`_with_fallback_note`), so the evidence panel can
show *why* each one failed, not just a generic "no match."

---

## Source 1 — endoflife.date direct API (`eol_service.py`)

### Resolving the product (`resolve_product_slug`)

endoflife.date's product catalog (~300+ products, fetched once and cached via
`get_product_catalog`/`lru_cache`) is turned into a **phrase index**:
for every product, its slug (e.g. `windows-server`), its display label
(e.g. `"Windows Server"`), and all of its aliases become searchable phrases
mapped back to that slug (`build_slug_index`).

Resolution order:

1. **Priority overrides** (`_SLUG_PRIORITY_OVERRIDES`) — a short list of regex
   → slug rules checked first, to force disambiguation for products whose
   generic name collides with something else in the phrase index (e.g.
   `windows[\s-]?server` → `windows-server` outranks the bare `windows`
   product; `cisco-ios-xe` outranks generic Cisco IOS text).
2. **Phrase index scan** — every phrase in the index that appears in the
   (normalized) query as a whole word/phrase is a candidate; the **longest**
   matching phrase wins (ties broken by an explicit priority number, then
   slug name). This means `"Windows Server 2019"` matches the `"windows
   server"` phrase (12 chars) rather than the shorter `"windows"` phrase
   (7 chars), even without needing a priority override for it.
3. **Hyphenated fallback** — if nothing in the index matched, the normalized
   query itself is hyphenated (`"foo bar"` → `"foo-bar"`) and tried directly
   as a slug.

Before any of this, the query text is cleaned up
(`_normalize_for_slug_lookup`): underscores/slashes/hyphens become spaces,
common glued product names are un-glued (`ubuntulinux` → `ubuntu linux`,
`windowsserver` → `windows server`, …), and a letter↔digit boundary gets a
space inserted (`Linux8.2` → `linux 8.2`).

**Examples:**

| Query (after cleanup) | Resolves to slug | Why |
|---|---|---|
| `"windows server 2019 datacenter"` | `windows-server` | priority override |
| `"microsoft windows 11"` | `windows` | phrase index, `"windows"` label |
| `"red hat enterprise linux 9"` | `rhel` | priority override (handles `redhat`/`red hat` spelling variants) |
| `"oracle linux server 9.5"` | `oracle-linux` | phrase index |
| `"amzn2023"` (after letter/digit split → `"amzn 2023"`) | `amazon-linux` | `_INVENTORY_PHRASE_EXTRAS` alias |

If no slug resolves at all, the row's `api_note` is `"Product not found in
endoflife.date registry"` and the row moves on to the vendor cascade — it is
**never** left to guess the "closest" product.

### Extracting version hints (`extract_version_hints`)

Once a product is found, endoflife.date returns its list of releases (each
with a `name` slug, a human `label`, `eolFrom`/`eoasFrom` dates, and often a
`latest.name` — the release's actual raw build, e.g. Windows' NT build
number). To pick *which* release, the query text is turned into a list of
**version hints**: every run of digits (optionally dotted) found in the text,
with several deliberate exclusions.

```python
extract_version_hints("Microsoft Windows 10 Build 26100")
# -> ["10", "26100"]

extract_version_hints("Windows 10.0.26100.7171")
# -> ["10.0.26100.7171"]   (one hint -- the whole dotted run)

extract_version_hints("Microsoft Windows 11 24H2")
# -> ["11", "24"]           ("H" breaks the digit run; see below)

extract_version_hints("Microsoft Windows 7 Service Pack 2")
# -> ["7"]                  ("2" is a Service Pack marker, dropped

extract_version_hints("Cisco IOS 15.0(2)SE8")
# -> ["15.0", "2"]          (parenthesised segments don't stop separate hints;
#                            the trailing "8" in "SE8" is preceded by a letter,
#                            so the negative lookbehind excludes it too)
```

Exclusions (all deliberate, each added after a real false-match):

- **Bitness / architecture numbers are dropped** — `16`, `32`, `64`, `86`,
  `128`, `256` never become hints (a `"64-bit"` query must never be treated
  as "version 64").
- **`N.x` ranges are dropped** — `"3.x or later"` is a range, not a specific
  version 3.
- **Lone SP/R/U/Pack digits are dropped** — the trailing digit in `SP2`,
  `R2`, `U1`, or spelled-out `"Service Pack 2"` is a patch marker, not a
  product version, so it's excluded when it's a single un-dotted number
  immediately preceded by one of those markers.
- **A compound tag doesn't leak a stray digit** — `"24H2"` yields the hint
  `"24"` only, never an extra `"2"`. Without the negative lookbehind
  (`(?<![A-Za-z])`) in the regex, naive digit-scanning would find *both*
  `"24"` (before the `H`) *and* `"2"` (right after it, since it's still a run
  of digits) as two separate hints — and that stray `"2"` has, in the past,
  coincidentally matched something completely unrelated (e.g. a `"Service
  Pack 2"` hint on a totally different OS).

### Picking a release (`pick_release` / `_pick_release`)

This is the single most safety-critical piece of matching logic in the app,
and the one most of this document's examples come back to.

**Inputs:** the product's `releases` list, the hints extracted above (merged
from *both* `os_string` and the field actually queried — see the callout
below), and `os_text` (used only for edition narrowing).

**Scoring, per release, per candidate string:**

For each release, up to three strings are tried as match candidates:
`release.name` (an internal slug, e.g. `11-24h2-w`), `release.label` (the
human string, e.g. `"11 24H2 (W)"`), and `release.latest.name` (the raw
build, e.g. `"10.0.26100"`, when present). Each candidate is scored against
**all** the hints at once (`_release_score`) two different ways:

1. **Whole-string, dot-aware comparison** (`score_release_against_hint`,
   `version_match.py`) — treats the candidate as a dotted version and the
   hint as a dotted version, and scores:
   - **100** — exact match.
   - **90** — one is a genuine numeric *prefix* of the other (e.g. release
     `17.9` vs hint `17.09.08`), **except** a single bare number can never
     match a multi-part release this way (`"11"` must not match `"11.4"`).
   - **55** — both sides have more than one part and share only the first
     (major) number — a weak, tie-only signal.
   - **0** — anything else, including a bare single-part hint against a
     multi-part release (the "bare major must not guess" rule).
2. **Compound-token full match** — a slug/label like `11-24h2-w` doesn't
   parse as one dotted version at all (hyphens/spaces aren't dots), so
   comparison #1 above always scores it 0 against a bare hint. Instead, its
   embedded number tokens are pulled out (`["11", "24"]`) and checked as a
   *set*: if the release has **more than one** token and **every** one of
   them is present somewhere among the hints, that's scored as a full match
   (100). A release with only a single embedded number never uses this
   path — comparison #1 alone already handles a clean version number
   correctly.

   This second rule is what makes a **name-only** query (no build number at
   all) able to resolve a release at all — see the worked example below —
   while still refusing to guess from a bare major alone, because that rule
   explicitly requires **every** token of a multi-token release, not just
   one of them.

The release with the single **highest** score across all its candidates
wins, provided that score is **≥ 80** (`_MIN_RELEASE_SCORE`). Below that, or
if there are no hints at all, `pick_release` returns nothing rather than
guess.

**Ties.** More than one release can legitimately tie for the best score —
most commonly because several editions/channels share the exact same raw
build (`latest.name`), or several editions share the same marketing name
tokens. Two tie-breakers are tried, in order:

1. **Edition narrowing** (`_edition_label_substring`) — if `os_text`
   contains an edition/channel marker (`"IoT"`, or `"Enterprise"`/literal
   `"(E)"`), the tie is narrowed to whichever tied release's `label`
   contains that same substring. IoT is checked before Enterprise, since a
   string naming both (`"Windows 11 IoT Enterprise LTSC"`) should prefer the
   more specific IoT release.
2. **Conservative merge — "least date" picking** (`_conservative_release`) —
   any tie left after edition narrowing (or when no edition was named at
   all) is resolved by assuming the **worst case**: the tied release with
   the **earliest** EOL date is used as the base result, and its EOL/EOAS
   dates are the *minimum* across every tied release. The reasoning: if we
   genuinely can't tell which of several editions this OS actually is,
   support should never be reported as lasting *longer* than it might
   actually be.

**Example — exact/build match, no tie:**
Query `"Windows 10.0.19045"` → hint `"10.0.19045"` → the `10-22h2` release's
`latest.name` is exactly `"10.0.19045"` → scores 100 → picked outright.

**Example — bare major, correctly refused (regression-tested):**
Query `"Windows 10"` → hint `["10"]` only. Every Windows 10 release's slug
(`10-22h2`, `10-21h2`, …) has *two* embedded tokens (`["10","22"]`,
`["10","21"]`, …) — the compound-token rule needs **both** present, and only
`"10"` is in the hint list, so it scores 0. The build (`latest.name`) also
scores 0 against a bare `"10"` (the "bare major" rule). **Nothing** is
picked; the row is left unresolved for manual review rather than guessing
one Windows 10 release at random.

**Example — name-only match, no build number needed at all** (the bug this
document was written to explain — see the [full worked example](#worked-example-the-windows-11-24h2-case)
below):
Query `"Microsoft Windows 11 24H2"` → hints `["11", "24"]`. Release
`11-24h2-w`'s slug tokens are exactly `["11", "24"]` — both present in the
hints → scores 100 via the compound-token rule, with **no build number
anywhere in the query**. The same is true for `11-24h2-e` and
`11-24h2-e-lts` (they share the same `11`/`24` tokens), so all three tie →
no edition named in the query → conservative merge picks the **earliest**
EOL among the three, which is the `(W)` consumer channel.

**Example — tie resolved by edition, not by date:**
Releases `11-23h2-e` and `11-23h2-w` share the same build `10.0.22631`.
Query `"...Windows 11 Enterprise multi-session 10.0.22631 Build 22631..."` →
both tie on the build match → `os_text` contains `"Enterprise"` → narrowed to
`11-23h2-e` specifically, *not* the earliest-date fallback.

> **Why hints are merged from *both* fields, not just the one queried:**
> Windows' own `normalized_os` is deliberately coarse/family-level (e.g.
> `"Microsoft Windows 11"`, no build) — see
> [`build_normalization_from_product`](#what-gets-written-to-the-row-and-evidence).
> If release-level hints came *only* from that coarse value, they'd always
> be a bare major and release-level lookups would silently find nothing on
> every subsequent refresh, permanently freezing whatever release tag the
> row happened to have. `lookup_os_eol` merges
> `extract_version_hints(os_string)` with `extract_version_hints(cleaned_name)`
> specifically so the raw `os_string`'s build number (or, as of this fix,
> release name) is never lost just because a coarser normalized value was
> preferred for product resolution.

### Vendor compatibility gate (`vendors_compatible`)

`normalization_service.py`'s `_vendor_tags()` scans a string for known
vendor/product-family signal words (regex patterns per vendor — `cisco`,
`apple`, `microsoft`, `android`, `redhat`, `ubuntu`, `oracle`/`solaris`,
`vmware`, `suse`, `juniper`, `google`/ChromeOS, … — 20 vendors as of this
writing) and returns the set of vendors it recognizes. `vendors_compatible(a,
b)` is true when:

- **neither** side has any recognized vendor tag (nothing to disagree about
  — treated as compatible by design, since a hostname/serial-number-only
  `os_string` shouldn't block an otherwise-good match), **or**
- the two tag sets **share at least one** vendor.

This gate is checked in two places for every lifecycle source:

1. Before trusting a coarser field over `os_string` in
   [Step 0](#step-0--which-field-gets-queried-pick_api_os_value_with_field).
2. After a product resolves, comparing the **query actually used**
   (`cleaned_name`, not the raw `os_string`) against the resolved product's
   own name/label. A mismatch means the row is left unresolved with an
   `api_note` naming the mismatch, rather than silently attaching the wrong
   vendor's dates.

**Examples:**

| Query | Candidate product | Compatible? | Why |
|---|---|---|---|
| `"Cisco IOS 15.0(2)SE8"` | `"Apple iOS"` | ❌ | `cisco` vs `apple`, no overlap |
| `"VMware vSphere 4"` | some Microsoft product | ❌ | `vmware` vs `microsoft`, no overlap |
| `"SunOS caesar01 5.10"` | `"Oracle Solaris 10"` | ✅ | `oracle` tag recognizes "Solaris"; query has no vendor tag of its own, so it's auto-compatible anyway |
| `"Cisco Firepower Threat Defense"` | `"Google Container-Optimized OS"` | ❌ | `cisco` vs `google` (this exact false match was found and fixed by adding a `google`/ChromeOS/COS vendor pattern) |
| a bare hostname/serial with no vendor keyword at all | anything | ✅ | neither side has a tag — no basis to reject |

---

## Source 2 — the local vendor cascade

All local sources share the same broad shape as the direct API (product
resolution → version-hint extraction → scored release picking → vendor
compatibility gate), each scraped into its own Postgres schema
(`vendor_lookups/db.py`) and refreshed via **Vendor Lookups → Update**. What
differs per source is noted below.

### eosl.date (local mirror) (`eosl_service.py`)

A scraped mirror of [eosl.date](https://eosl.date)'s own product pages
(itself largely sourced from endoflife.date), used as a fallback when the
live API misses (network issue, a product not in endoflife.date's catalog,
etc.).

- **Product resolution** (`_resolve_product_slug`): a simpler, direct
  substring-scored match (not a phrase index) — the full product name must
  literally appear in the query to reach the accept threshold of **95**;
  weaker 70/80/85-scoring tiers exist only to break ties toward the more
  specific (longest) product name once 95 is already reached elsewhere.
- **Vague-query guard** (`_query_is_vague` / `_query_targets_generic_family`)
  — three ultra-generic product pages (`linux`, `windows`, `unix`) must not
  silently absorb a vague query. `"Other ... Linux"`, `"unknown"`,
  `"or later"` style text is refused outright; even a *non-vague* query only
  resolves to these generic pages when it explicitly looks like one
  (`"linux 5.10"`, `"windows server"`, `"windows xp"`, `"unix 7"`, …) — a
  bare `"Linux"` with a version number attached is accepted, but `"Other
  Embedded Linux"` is not.
- **Release picking** (`_pick_release`): same 80-point threshold, but here
  ties are **not** explicitly detected — the release with the strictly
  highest score wins, and if several tie, whichever the database's own
  `ORDER BY released_date DESC, release_name DESC` happened to return first
  is kept. (In practice, for same-build Windows editions this still tends to
  land on the `(W)` consumer channel, since `"W"` sorts after `"E"`/`"LTS"`
  alphabetically in `DESC` order — but this is an accident of SQL ordering,
  not a deliberate "least date" rule the way the direct API path has.)

### Microsoft Lifecycle (`microsoft_lifecycle_service.py`)

Scraped from `learn.microsoft.com/lifecycle` — **every Microsoft product
family except `"windows"` itself** (`_EXCLUDED_FAMILIES`). This is
deliberate: Microsoft's own "windows" family here mixes real OS releases
with unrelated tools (PowerShell, FSLogix, Windows Defender Exploit Guard,
…) under one family, and every OS release's *own name* is a slug like
`10-1709-w` whose extracted numeric tokens include the bare major (`"10"`)
shared by **every** Windows 10/11 release — a bare-major hint would then
score a perfect match against dozens of unrelated releases at once, with no
real signal left to pick among them. (A real incident once matched a 2017
build to a 2021 release this way.) endoflife.date's own dedicated
`windows`/`windows-server` products (tried first, above) already cover this
ground far more precisely. Microsoft Lifecycle's `windows` family data is
still scraped and viewable in the Vendor Lookups screen — it's excluded only
from *automated matching*.

- **Product resolution threshold:** 95, same substring-scored approach as
  eosl.date.
- **Release picking:** same shape as eosl.date's `_pick_release` (ties not
  explicitly detected, first-by-score wins).

### Juniper Junos (`junos_service.py`)

A single fixed product ("Junos OS") — no product-family resolution needed at
all, just the keyword gate (`junos`/`juniper`) plus `vendors_compatible`.

Juniper's own version scheme needs a dedicated comparison
(`_version_match_score`) beyond the generic `score_release_against_hint`:
releases like `15.1X49` and `15.1X53` are **different, non-overlapping
"X-trains"** that happen to share the `15.1` base — the code explicitly
refuses to match one against a hint naming the other, and also refuses to
let a family-only hint (`"15.1"`) guess a specific X-train release, or an
X-train hint match a plain non-X release with the same base number. Only
once neither side is an X-train mismatch does it fall back to the ordinary
dot-aware score.

### SUSE Lifecycle (`suse_service.py`)

Multiple products (SLES, SLES for SAP, SLES for HPC, openSUSE Leap, Desktop,
…), resolved with its own scoring rules tuned for SUSE's naming conventions:
explicit edition keywords (`"sap"`, `"hpc"`, `"desktop"`) get a strong bonus
toward the matching edition slug, and a generic `"SUSE"/"SLES"` mention (with
no edition keyword) is steered toward the *core* SLES product rather than
one of the specialized editions. Its accept threshold is lower (**60**, vs
95 elsewhere) because these edition-aware bonuses already do more of the
disambiguation work up front.

### Layer23-Switch / Router-Switch (hardware EOL)

Both scrape hardware/model EOL data from third-party trackers
(layer23-switch.com, router-switch.com), organized by manufacturer rather
than OS product family, and matched by model/part-number rather than a
clean OS version scheme. **Off by default** (large, hardware-heavy
catalogs) — an operator opts in per source in Settings, and separately
selects which manufacturers to sync. Because they're keyword-gated on
network-vendor names (Cisco, Arista, Juniper, Palo Alto/PAN-OS, …), turning
them on doesn't add lookup cost to non-networking rows.

---

## Ambiguous OS rows are never queried

A row whose `normalized_os_detailed_name` is exactly `"Ambiguous OS"`
(`is_ambiguous_row`, set when an OS string contains `/` and looks like it
lists more than one product) is **skipped entirely** by every lookup stage
— not queried with a fallback value, not left to resolve "whichever" product
it might be. Querying a lifecycle source with the literal text "Ambiguous
OS" doesn't fail cleanly: it has, in the past, fallen back to the raw (also
ambiguous) `os_string` and picked up a real but unrelated product via
coincidental version-number overlap — silently writing a wrong date onto a
row that was flagged specifically *because* its product can't be determined.

---

## What gets written to the row, and evidence

When a source resolves a row (`_apply_lifecycle_result` in `app.py`):

- `eol_date` / `eol_status` / `eoas_date` / `eoas_status` are always
  overwritten with whatever this lookup produced (a genuine no-match leaves
  these blank in the result, so the row's existing values are left alone).
- `normalized_os_detailed_name` / `normalized_os` are overwritten **only
  when the lookup actually produced a name** for that field — a match
  names one specific release, so refusing to correct an already-non-blank
  name would leave the row's displayed release tag silently out of sync
  with the dates that *did* get updated.
- An **evidence** entry is recorded per field (`detailed`, `normalized`,
  `eol`), each with a `method` (`api` for the direct endoflife.date lookup,
  or the vendor source id — `eosl`, `microsoft-lifecycle`, `junos`, `suse`,
  `layer23-switch`, `router-switch` — for a cascade match), the exact query
  string used, the resolved `product_slug`/`release_label`, and (for
  fuzzy/AI methods) a confidence score. This is what powers the row
  drawer's evidence panel and the **Matched by** column filter
  (`lookup_extras.py::build_evidence_entries`/`row_matched_by`).

---

## Every safety rule, in one table

| Rule | Where | Prevents |
|---|---|---|
| Bitness numbers (16/32/64/86/128/256) never become version hints | `extract_version_hints` and every source's own hint extractor | `"64-bit"` being read as "version 64" |
| `N.x` ranges are dropped | same | `"3.x or later"` being read as version 3 |
| Lone SP/R/U/Pack digit dropped | same | `"Service Pack 2"` / `"SP2"` / `"R2"` / `"U1"` contributing a bogus version 2 hint |
| Compound tag doesn't leak a trailing digit | same (negative lookbehind) | `"24H2"` also yielding a spurious `"2"` hint |
| Bare single-part hint never matches a multi-part release | `score_release_against_hint` | `"Windows 10"` (bare) picking one specific Windows 10 build/release at random |
| Compound-token full match requires *every* token, and only when there's more than one | `eol_service.py::_release_score` | a bare major alone claiming a specific name-based release the same way the rule above prevents it for builds |
| Ties resolved by edition first, then earliest-date | `pick_release`/`_conservative_release` | reporting longer support than an ambiguous edition might actually have |
| Full product name must literally appear in the query (≥95, except SUSE's edition-aware 60) | every `_resolve_product_slug` | a short/generic query fragment being treated as "contained in" an unrelated, much longer product name |
| Vendor compatibility gate | `vendors_compatible`, checked before product-field selection and again after product resolution | Cisco IOS ↔ Apple iOS, VMware ↔ Microsoft, Cisco Firepower ↔ Google Container-Optimized OS, and similar cross-vendor false matches |
| Generic family guard (`linux`/`windows`/`unix`) | `eosl_service.py::_query_targets_generic_family` | a vague `"Other ... Linux"` string silently absorbing into the generic Linux kernel product page |
| `windows` family excluded entirely | `microsoft_lifecycle_service.py::_EXCLUDED_FAMILIES` | a bare-major Windows build matching dozens of unrelated tool releases that happen to share a "10-…" slug |
| Juniper X-train mismatch refused | `junos_service.py::_version_match_score` | `15.1X49` matching a hint that actually names `15.1X53` |
| Ambiguous OS rows skipped entirely | `is_ambiguous_row` | writing a real date onto a row whose product literally can't be determined |
| No hints at all → no match | every `pick_release`/`_pick_release` | ever guessing the first/latest release when there's no version evidence whatsoever |

---

## Worked example: the "Windows 11 24H2" case

This is the exact scenario that motivated the fix underlying most of
[picking a release](#picking-a-release-pick_release--_pick_release) above.

**Input row:** `os_string = "Microsoft Windows 11 24H2"`, no build number
anywhere, `normalized_os`/`normalized_os_detailed_name` both blank.

**endoflife.date's `windows` product** has (among others) these three
releases:

| `name` (slug) | `label` | `latest.name` | `eolFrom` |
|---|---|---|---|
| `11-24h2-e-lts` | `11 24H2 (E) (LTS)` | `10.0.26100` | `2029-10-09` |
| `11-24h2-e` | `11 24H2 (E)` | `10.0.26100` | `2027-10-12` |
| `11-24h2-w` | `11 24H2 (W)` | `10.0.26100` | `2026-10-13` |

**Before the fix:** `pick_release` only ever compared each candidate as one
whole dotted-version string. `"11-24h2-w"` isn't a clean dotted version (it
has hyphens and a letter in it), so `version_match.py`'s naive dot-only
split treated it as a single non-numeric blob — it could never equal a bare
hint like `"11"` or `"24"` no matter how good a match it conceptually was.
The build candidate (`"10.0.26100"`) also couldn't help, because the query
has no build number at all to compare against it. Every candidate for every
release scored 0. **Nothing matched — the row stayed unresolved despite
endoflife.date clearly having the data.**

**After the fix:**

1. `extract_version_hints("Microsoft Windows 11 24H2")` → `["11", "24"]`.
2. Product resolves to `windows` as before.
3. For each of the three releases, the compound-token rule extracts
   `["11", "24"]` from the slug — both tokens are present in the hints, and
   there's more than one token, so each release scores **100**.
4. All three tie at 100. `os_text` is just `"Microsoft Windows 11 24H2
   Microsoft Windows 11 24H2"` — no `"Enterprise"`/`"(E)"`/`"IoT"` substring
   — so edition narrowing doesn't apply.
5. Conservative merge picks the **earliest** `eolFrom` among the tied three
   — `2026-10-13`, belonging to `11-24h2-w` — and that's the release
   returned.

**Result:** `normalized_os_detailed_name = "Microsoft Windows 11 24H2 (W)"`,
`eol_date`/`eoas_date` = `2026-10-13`, evidence method `api`, matched
`"11 24H2 (W)"`, query `"Microsoft Windows 11 24H2"`.

The regression guard from before the fix still holds: a query of just
`"Microsoft Windows 11"` (bare major, no `"24"` at all) still correctly
resolves to **nothing** — the compound-token rule needs *both* `"11"` and
`"24"` present, and a bare major alone was always meant to be refused.

---

## Secondary: normalization matching (fuzzy + AI)

This is a *different* kind of matching — not "which lifecycle record does
this OS belong to," but "does this new OS string already match an existing
`(normalized_os_detailed_name, normalized_os)` pair already in the lookup,"
used by the **Add OS** flow before ever calling a lifecycle source at all.
Lives in `normalization_service.py`.

- **Fuzzy match** (`strict_match_percent`) — both the query and the
  candidate pair are normalized (lowercased, punctuation collapsed,
  dotted versions protected as atomic tokens so `3.2` and `3.2.0` compare
  equal), then tokenized. Every one of the *query's* tokens must appear in
  the candidate; the score is the ratio of query-token-count to
  candidate-token-count (whichever is longer becomes the denominator), so a
  candidate that adds *extra* tokens the query doesn't have scores below
  100 — a full 100 requires the same token set on both sides.
- **AI match** (optional, OpenAI/Gemini/OpenRouter) — given a curated list
  of *allowed* pairs (filtered to the same vendor via `vendors_compatible`,
  never invents a new pair) and a configurable confidence threshold. Every
  AI pick is still re-validated in code afterward
  (`ai_pair_acceptable`): rejects rubbish/placeholder strings on either
  side, rejects if the pick isn't vendor-compatible, rejects if the
  candidate introduces an edition/SKU word the query doesn't have (e.g.
  `"Windows 11 Pro"` must not match `"Windows 11 Pro Enterprise"`), and
  rejects if the version families don't line up (`"Ubuntu 20.04"` must not
  match `"Ubuntu 22.04"`) — because model providers, OpenAI especially,
  tend to over-match otherwise.
- **Rubbish/placeholder filtering** (`is_rubbish_os_value`/
  `is_placeholder_os_value`) — hex dumps, GUIDs, and junk strings (`"-"`,
  `"n/a"`, `"<!-- default -->"`, …) are never offered as normalization
  targets and never accepted as a match source, on either side.

---

## Secondary: row-identity matching (diff / publish merge)

A third, much simpler kind of "matching": deciding whether a row in Draft is
the "same" row as one in Data, for the Draft-vs-Data diff panel and for the
3-way publish merge. Lives in `lookup_extras.py`.

- **Identity key** = the row's `os_string`, trimmed and lowercased
  (`_dedupe_key`). Two rows with the same key are the same row; a row with a
  blank `os_string` has no stable identity and is never diffed/merged by
  content — it just passes through untouched.
- **Equality** (`_rows_equal`) compares every CSV column except `os_string`
  itself (boolean-ish status cells compared case-insensitively).
- **Publish's 3-way merge** (`merge_lookup_rows`) additionally needs a
  *base* snapshot (what Data looked like when the draft was created) to tell
  "changed here," "changed upstream," and "changed both — conflict" apart
  safely; a key that appears more than once on any side is never
  content-diffed at all — it's always surfaced as an `ambiguous_duplicate`
  conflict for a human to resolve, since a dict-keyed merge would silently
  drop a genuine duplicate row.

This has nothing to do with lifecycle/vendor matching above — it never looks
at EOL dates, product names, or versions at all, only row identity and
content equality.

---

## Glossary of thresholds

| Constant | Value | Meaning |
|---|---|---|
| `_MIN_RELEASE_SCORE` (eol_service.py, eosl_service.py, microsoft_lifecycle_service.py, junos_service.py) | 80 | Minimum release-match score to accept a release at all |
| Product-name-in-query threshold (eosl.date, Microsoft Lifecycle) | 95 | The full product name/label must literally appear in the query |
| Product-name-in-query threshold (SUSE) | 60 | Lower because edition-aware bonuses (SAP/HPC/Desktop/core-SLES) already do most of the disambiguation |
| `score_release_against_hint` exact match | 100 | Identical string, or identical numeric parts |
| `score_release_against_hint` prefix match | 90 | One side is a genuine numeric prefix of the other (never for a bare single-part hint against a multi-part release) |
| `score_release_against_hint` shared-major-only | 55 | Both sides multi-part, share only the leading number — weak, tie-only signal |
| Compound-token full match (name/label) | 100 | Every embedded token of a multi-token release name present among the hints |
| `_normalize_ai_provider` default fuzzy threshold | 95 | Default confidence bar for AI/fuzzy Add-OS matching (configurable in Settings, 50–100) |
