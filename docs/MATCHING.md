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
   - [Prior-value fallback](#prior-value-fallback-_pick_release_by_prior_value)
   - [Dot-zero fallback](#dot-zero-fallback-_pick_release_by_dot_zero_release_name)
   - [Product-level release fallback](#product-level-release-fallback-_product_release_fallback_slugs)
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

**A second safety net, in `lookup_os_eol` itself (`eol_service.py`,
right after the product first resolves):** `vendors_compatible` only
catches a *cross-vendor* mismatch — it does nothing for a normalized field
that names the wrong product *within the same vendor*. Real incident: a
row's `os_string="iPad 10.3.4"` had `normalized_os`/
`normalized_os_detailed_name` previously (manually, or from before the
`ipad`/`ipados` fix existed) set to `"Apple iOS 10"` — a real, valid Apple
product, just the *wrong* one. Both are `"apple"` vendor, so the
cross-vendor gate above never fires, and the lookup would confidently
query with the stale `"Apple iOS 10"` value and pull **iOS's own EOL/EOAS
dates** instead of iPadOS's. `lookup_os_eol` now checks: does the *raw*
`os_string` independently resolve to a product that has a deliberate
`_INVENTORY_PHRASE_EXTRAS` entry (currently just `ipados`, via the `"ipad"`
alias), and does that differ from what the preferred field resolved to? If
so, the preferred field is more likely stale than the deliberate override
is wrong — retry with `os_string` instead. A row genuinely and correctly
normalized to `"Apple iOS 10"` (e.g. a real iPhone) is unaffected: its raw
`os_string` doesn't independently resolve to `ipados` at all, so nothing
overrides it.

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

endoflife.date's product catalog (~460 products total, fetched once and
cached via `get_product_catalog`/`lru_cache`) covers far more than operating
systems — languages, frameworks, databases, server apps, services, and
(crucially) hardware **devices**, distinguished only by each product's own
`"category"` field. `get_product_catalog` filters to **`category == "os"`
only** (~66 products) before anything downstream ever sees the list — this
app's `os_string` is specifically an OS version string, so a non-OS category
product is never a valid match target, no matter how closely its name or
label happens to overlap with the query text.

**Real incident:** Apple's `ipad` product (`category: "device"`, tracking
hardware generations like "iPad (9th generation)", not software) has no
alias distinguishing it from `ipados` (`category: "os"`, the actual iPadOS
software lifecycle) — but its own slug/label *is* the bare word `"ipad"`,
so every real-world `"iPad <version>"`-style `os_string` (which never spells
out "iPadOS") matched the hardware product purely by coincidence of naming,
long before any release-level scoring even ran. Filtering by category
removes the entire class of hardware/device/non-OS products from
consideration at the source, rather than trying to out-prioritize each one
individually as they're discovered. `_INVENTORY_PHRASE_EXTRAS` now maps the
bare `"ipad"` phrase to `ipados` — safe only *because* the hardware product
it used to collide with is excluded first.

The filtered catalog is then turned into a **phrase index**: for every
remaining product, its slug (e.g. `windows-server`), its display label
(e.g. `"Windows Server"`), and all of its aliases become searchable phrases
mapped back to that slug (`build_slug_index`).

Resolution order:

1. **Priority overrides** (`_SLUG_PRIORITY_OVERRIDES`) — a short list of regex
   → slug rules checked first, to force disambiguation for products whose
   generic name collides with something else in the phrase index (e.g.
   `windows[\s-]?server` → `windows-server` outranks the bare `windows`
   product; `cisco-ios-xe` outranks generic Cisco IOS text). **Real
   incident:** real-world inventory strings routinely drop the word
   "Server" entirely for a server-only OS — `"Windows 2008 R2 Standard"`,
   `"Win 2008 R2"`, `"Windows 2008 - Standard"` — so the literal-`"server"`
   override above never fired, and these fell through to the generic
   `"windows"` (client) phrase-index entry, which has no release for a
   year it was never versioned by. A second, order-independent override
   now catches this: two zero-width lookaheads requiring both a
   `win`/`windows` mention *and* a server-only generation year (`2008`,
   `2011`, `2012`, `2016`, `2019`, `2022`, `2025` — client releases are
   only ever named `"7"`/`"8"`/`"10"`/`"11"` or `"XP"`/`"Vista"`, never a
   year) anywhere in the text, in either order → `windows-server`.
2. **Phrase index scan** — every phrase in the index that appears in the
   (normalized) query as a whole word/phrase is a candidate; the **longest**
   matching phrase wins (ties broken by an explicit priority number, then
   slug name). This means `"Windows Server 2019"` matches the `"windows
   server"` phrase (12 chars) rather than the shorter `"windows"` phrase
   (7 chars), even without needing a priority override for it.
3. **Hyphenated fallback** — if nothing in the index matched, the normalized
   query itself is hyphenated (`"foo bar"` → `"foo-bar"`) and tried directly
   as a slug.

Whichever of the three found a candidate slug is then checked against
`_generic_family_match_is_trustworthy` before being returned — a match to a
slug whose own name is a single, universally generic word is only trusted
if the query says a specific extra word too:

- **`linux` requires the word `"kernel"`** (in any glued/hyphenated/spaced
  form) to actually appear. **Real incident:** endoflife.date's `linux`
  product tracks the **Linux kernel project's own** release/EOL schedule —
  not any particular distribution — but its slug and label are both just
  the bare, generic word `"linux"`/`"Linux Kernel"`, so the phrase index
  matched it for `"Linux 6.4.7.3762 7"` purely because that one common word
  was present (a distro whose real name never got recognized, or a vague
  placeholder, would read exactly the same way) — resolving confidently to
  a specific kernel release and adopting *that* release's own EOL date,
  even though nothing in the string ever said "kernel". A real distro
  string that happens to also mention the word "linux" (`"Ubuntu Linux
  22.04"`, `"Red Hat Linux 7.4"`) is unaffected — the guard only applies to
  the `linux` slug's *own* match, never to a different product's.

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
| `"ipad 10.0.2"` | `ipados` | `_INVENTORY_PHRASE_EXTRAS` alias — the hardware `ipad` product is excluded by the category filter, so this alias is unambiguous |

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
# -> ["15.0", "2", "8", "15.0.2"]   (parenthesised segments don't stop
#                                    separate hints; the trailing "8" in
#                                    "SE8" IS extracted -- it's preceded by
#                                    two letters, not a digit+letter, so the
#                                    compound-tag lookbehind doesn't apply)

extract_version_hints("Android 16")
# -> ["16"]                 bare "16" is a real major version here -- see below

extract_version_hints("Windows 7 (64-bit)")
# -> ["7"]                  "64" is excluded -- it reads as a bitness marker in context

extract_version_hints("WindowsServer2008R2")
# -> ["2008"]               a genuine version glued directly onto the
#                            preceding word still extracts in full -- see below
```

Exclusions (all deliberate, each added after a real false-match):

- **Bitness / architecture numbers are dropped, but only in bitness
  *context*** — `16`, `32`, `64`, `86`, `128`, `256` are excluded when the
  text right around them actually reads as an architecture marker
  (`_looks_like_bitness_marker`: immediately followed by `bit`/`-bit`/` bit`,
  or immediately preceded by `x` as in `x86`/`x64`). A bare one of these
  numbers with no such context is kept, because it's also a completely
  legitimate major version on its own — Android's own major version reached
  **16** ('Baklava') in 2025, and product version numbers keep climbing over
  time. Blanket-excluding every bare `16`/`32`/`64`/… regardless of context
  used to make any product whose version happened to land on one of them
  (e.g. `"Android 16"`) permanently unmatchable, since `extract_version_hints`
  would give it zero usable hints at all, forever.
- **`N.x` ranges are dropped** — `"3.x or later"` is a range, not a specific
  version 3.
- **Lone SP/R/U/Pack digits are dropped** — the trailing digit in `SP2`,
  `R2`, `U1`, or spelled-out `"Service Pack 2"` is a patch marker, not a
  product version, so it's excluded when it's a single un-dotted number
  immediately preceded by one of those markers.
- **A compound tag doesn't leak a stray digit, but a glued-on version still
  extracts in full** — `"24H2"` yields the hint `"24"` only, never an extra
  `"2"`. Naive digit-scanning would find *both* `"24"` (before the `H`) *and*
  `"2"` (right after it, since it's still a run of digits) as two separate
  hints — and that stray `"2"` has, in the past, coincidentally matched
  something completely unrelated (e.g. a `"Service Pack 2"` hint on a
  totally different OS). The regex protects against this with a negative
  lookbehind that only excludes a digit run immediately preceded by
  **exactly** `[digit][single-letter]` (`(?<![0-9][A-Za-z])`) — the true
  compound-tag shape. **Real incident:** an earlier, broader version of this
  lookbehind (`(?<![A-Za-z])`, excluding a digit run preceded by *any*
  letter at all) also blocked genuine version numbers glued directly onto a
  preceding word with no space — a bulk-reported inventory string
  `"WindowsServer2008R2"` extracted the hint `"008"` (truncated, matching
  nothing) instead of `"2008"`, because the `"r"` in `"...Server2008..."`
  preceded it. Narrowing the lookbehind to require a digit immediately
  before the letter (not just any letter) still excludes `"24H2"`'s stray
  `"2"` — the `"4"` right before the `"H"` is a digit — while correctly
  extracting `"2008"` in full from `"...Server2008..."`, where the
  character before the `"r"` is another letter, not a digit.
- **A build number is combined with the dotted version right before it,
  parenthesized or not** — `"Windows 10.0 (14393)"` yields `["10.0",
  "14393", "10.0.14393"]`; `"Windows 10.0 22631 64-bit"` yields `["10.0",
  "22631", "10.0.22631"]` (the bare-number form is restricted to 4+ digit
  numbers that already survived the exclusions above, so a genuine bitness
  marker like the `"64"` in `"64-bit"` is never absorbed). A real incident:
  without the combined hint, `"10.0"` alone is a genuine numeric *prefix* of
  every Windows 10/11 build (`score_release_against_hint` scores a prefix
  match 90 regardless of which specific build follows), so it ties across
  the **entire** Windows 10/11 family — and the bare build number alone
  never breaks that tie, because the scoring function only recognizes a
  hint being a *prefix* of a release's version, never the *trailing*
  segment of one. Every row shaped this way was silently resolving to
  whichever release happens to have the conservatively-earliest EOL (e.g.
  `1507`), regardless of the actual build named — and this compounded with
  a second bug, since a bare `"Windows 10.0"` with no build number at all
  was *also* resolving this way (see the exact-score tie-break requirement,
  below). The synthesized combined hint exact-matches its one true release
  (score 100), which strictly outscores the family-wide tie (90) and
  resolves it correctly.
- **A trailing " - \<year\>" (spaced hyphen, bare 4-digit number, at the
  very end of the string) is dropped as metadata, not a second version** —
  only once a hint has already been captured earlier in the string. **Real
  incident:** `"Microsoft Windows Server 2008 R2 - 2012"` names exactly
  **one** OS — "2008 R2" — with `"- 2012"` appended as real-world inventory
  metadata (an install/license/audit-year stamp), not a claim that the row
  is *also* Windows Server 2012. Without this exclusion, `"2012"` was
  extracted as a second, independent hint, tying four different releases
  (`2008-sp2`, `2008-r2-sp1`, `2012`, `2012-r2`) with zero evidence in
  common — correctly refusing per the "two different releases" rule, since
  by hint alone this is indistinguishable from `"Android 14-11"` — except
  this string only ever named one OS. The **whitespace before the hyphen is
  what keeps this narrow**: `"Android 14-11"` (hyphen glued directly
  between two digits, no spaces) is a fundamentally different shape — two
  independent version hints — and is completely unaffected. A dash-year
  appearing mid-string (more text after it) or with no earlier hint at all
  is also unaffected — this only ever drops the LAST token, and only when
  it's the *second* piece of version-shaped text in the string.

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
   embedded number tokens are pulled out (`_release_name_tokens`,
   e.g. `["11", "24"]`) and checked as a *set*: if the release has **at
   least one** token and **every** one of them is present somewhere among
   the hints, that's scored as a full match (100). `_release_name_tokens`
   applies the same SP/R/U/Pack marker-digit exclusion as
   `extract_version_hints` (so `"2008-r2-sp1"` yields only `["2008"]`, not
   `["2008", "2", "1"]`) — a release's own edition/patch suffix is never
   itself part of its version.

   **Real incident:** Windows Server's own release *names* are compound
   slugs like `"2008-sp2"` and `"2008-r2-sp1"`, not the bare year alone.
   Before the marker-digit exclusion existed, `_release_name_tokens` read
   `"2008-r2-sp1"` as `["2008", "2", "1"]` and required **every** one of
   those three present in the query's hints — so unless the query
   happened to also contain a coincidental `"2"` and `"1"`, it could never
   score a full match, and the entire Windows Server 2008/2012 family fell
   through to the eosl.date fallback for any query naming only the year.
   The rule *also* used to require **more than one** token — once the
   marker digits are correctly excluded, a release like plain `"2008-sp2"`
   has only one genuine token (`"2008"`) left, so that restriction was
   relaxed to just "at least one": a single confirmed token is still an
   unambiguous full match. This exact relaxation had to be applied to
   **two separate copies** of the same restriction — `_release_score`
   above, and `_release_required_hints` (tie-breaker 3 below), which
   computes required-hint sets independently. Fixing only the first left
   the shared-hint tie-break still seeing an empty required set (from the
   still-unfixed second copy) even after the score correctly reached 100,
   so `pick_release` refused anyway — the subtlest part of this fix.

   This second rule is what makes a **name-only** query (no build number at
   all) able to resolve a release at all — see the worked example below —
   while still refusing to guess from a bare major alone, because that rule
   explicitly requires **every** token of a multi-token release, not just
   one of them.
3. **Build-number-suffix match** (`_hint_matches_build_suffix`) — comparison
   #1 above only ever tests whether a hint is a numeric *prefix* of the
   release (or vice versa) — never whether it matches the release's
   trailing, most granular segment. A bare, undotted hint of **4+ digits**
   that exactly equals the **last** segment of a multi-part release version
   (e.g. hint `"17763"` against release `"10.0.17763"`) is treated as a
   match at the same **100** confidence as an exact match — a build number
   this specific is effectively a unique identifier, the same reasoning
   already trusted for the existing dotted+trailing-build-number combining
   pass in `extract_version_hints`.

   **Real incident:** `"Windows Server 2019 Datacenter AD Version 1809
   Build 17763"` refused to resolve at all. Hints `["2019", "1809",
   "17763"]`. Releases `"2019"` and `"1809-sac"` share build `10.0.17763`
   and each independently scores 100 via its own name (`"2019"` matches
   hint `"2019"`, `"1809-sac"` matches hint `"1809"`) — a genuine tie. But
   `"17763"` — the trailing segment of the shared build, quoted standalone
   with no adjacent `"10.0"` for the existing combining pass to stitch it
   onto — matched **neither** release at all under the old prefix-only
   comparison, so no hint was recognized as common to both, the shared-hint
   tie-break (below) saw an empty intersection, and `pick_release` refused
   *before* the dominant-evidence check ever got a chance to correctly
   prefer `"2019"` (the release with the additional, more specific `"2019"`
   hint). Recognizing `"17763"` as confirming both releases restores the
   common ground the dominant-evidence check needs.

The release with the single **highest** score across all its candidates
wins, provided that score is **≥ 80** (`_MIN_RELEASE_SCORE`). Below that, or
if there are no hints at all, `pick_release` returns nothing rather than
guess.

**Ties.** More than one release can legitimately tie for the best score —
most commonly because several editions/channels share the exact same raw
build (`latest.name`), or several editions share the same marketing name
tokens. Six tie-breakers are tried, in order:

1. **Dotted-hint preference** — before anything else, the whole scoring
   pass above is re-run using **only** the dotted hints (those containing a
   `.`), and if that dotted-only pass resolves to a single, **unique**
   release, scoring **≥ 80**, that disagrees with the full-hint-set result,
   the dotted-only result is preferred outright. **Real incident:** products
   whose entire release catalog is bare, major-version-only names (RHEL:
   `"4"`–`"10"`, CentOS: `"5"`–`"8"`, iOS: `"5"`–`"26"`) can never have a
   release that *exactly* matches a dotted hint like `"6.6"` — the best it
   ever reaches is the release's own bare major number via the weaker
   90-point *prefix* score. Meanwhile a totally unrelated standalone bare
   number elsewhere in the query — a kernel-version fragment, a space-
   separated point-release digit — can coincidentally **exact**-match some
   *other* release's own bare name at a full 100, outright outscoring the
   correct match (not even a tie). `"RHEL 6.6 3 8"` (kernel `3.8`, space-
   separated instead of dotted) resolved to release `"8"` (the bare `"8"`
   hint exact-matching it) instead of release `"6"` (the genuine `"6.6"`
   hint, only scored 90) — the same shape broke `"CentOS 7.9 5 4"` and
   `"iOS 16.7 10"` (a real iOS 16.7.10 point release, space- instead of
   dot-separated) too. Since a dotted hint is always at least as specific as
   a bare one, and the product's own catalog never HAS a dotted release name
   to compare against, preferring the dotted-only pass recovers the correct
   release without needing to know in advance which products are
   bare-major-only.

   **The dotted-only pass must itself be unique before it's trusted — a
   real regression found while verifying this fix against the live catalog:**
   `"WindowsServer2016 10.0"` (hints `["2016", "10.0"]`) already resolves
   uniquely and correctly on the *full* hint set alone — release `"2016"`'s
   own name is itself one of the hints, a compound-token full match (100).
   But `"10.0"` is a genuine numeric prefix of **every** modern Windows
   Server release's build number (they all start `10.0.`), so scoring with
   *only* the dotted hint ties roughly a dozen releases at 90 — here the
   dotted-only pass isn't more specific, it's *less* specific than the full
   hint set. The first version of this fix unconditionally preferred the
   dotted-only pass whenever it merely *disagreed*, so this 12-way tie
   silently clobbered the correct, unique 100-score answer — and the
   resulting tie then failed tie-breaker 5 (exact-score requirement) below,
   turning a clean match into "no match found" and sending the row to the
   eosl.date fallback despite endoflife.date having the right answer all
   along. Requiring the dotted-only pass to *itself* resolve to exactly one
   release before it's trusted fixes this: RHEL/CentOS/iOS's bare-major-only
   catalogs always give a unique dotted-only winner (`"6.6"` can only ever
   numeric-prefix-match release `"6"`, never `"7"` or `"8"`), so the
   original fix stays intact, while a coarse hint like `"10.0"` that ties
   nearly the whole catalog no longer gets to override an already-
   unambiguous answer.
2. **Edition narrowing** (`_edition_label_substring`) — if `os_text`
   contains an edition/channel marker (`"IoT"`, `"LTSC"`/`"LTS"`, `"R2"`, or
   `"Enterprise"`/literal `"(E)"`), the tie is narrowed to whichever tied
   release's `label` contains that same substring. Checked in order, most
   specific first: **IoT**, then **LTSC/LTS**, then **R2**, then bare
   **Enterprise** — a string naming more than one (`"Windows 11 IoT
   Enterprise LTSC"`) prefers the more specific signal. LTSC/LTS is checked
   before bare Enterprise because every LTS release's label is a strict
   superset of Enterprise's (`"... (E) (LTS)"` vs `"... (E)"`) — narrowing
   only as far as `"(e)"` when the string also says `"LTSC"` would leave the
   LTS and non-LTS releases tied against each other. **Real incident:**
   `"Microsoft Windows 10 Enterprise LTSC 10.0.17763 0"` narrowed only to
   `"(e)"` (matching both `"10 1809 (E) (LTS)"` and `"10 1809 (E)"`), so the
   conservative "earliest EOL" merge below silently picked the *non-LTS*
   release (EOL 2021) instead of the LTSC one actually named in the string
   (EOL 2029). **R2 must be checked before Enterprise, not after** —
   `_edition_label_substring` returns only the *first* matching pattern, so
   for `"Windows Server 2008 R2 Enterprise 7600"` (a real edition name —
   Windows Server 2008 R2 genuinely ships an "Enterprise" SKU), checking
   Enterprise first would match immediately and R2 would never get a
   chance to narrow the 2008-vs-2008-R2 compound-slug tie at all. **R2's own
   pattern tolerates being glued directly to the preceding digit** —
   `(?<![A-Za-z])r2(?![0-9A-Za-z])`, not `\br2\b` — since `\b` never fires
   between two word characters (a digit and a letter both count), so
   `\br2\b` silently never matched `"WindowsServer2012R2 9600"` (the same
   glued-word shape as the `"WindowsServer2008R2"` digit-truncation
   incident). Without edition narrowing here, `"2012"` (a clean exact-match
   on its own bare release name) and `"2012-r2"` (confirmed only via the
   build-number-suffix rule on `"9600"`) tied with **neither** dominating
   the other at the next check below — each has its own genuine, distinct
   evidence the other lacks — so the row only resolved correctly by an
   *accidental* coincidence in the real catalog (both releases happen to
   share the exact same `eolFrom`), not genuine confirmation. The new
   pattern excludes "r2" only when a **letter** (not a digit) immediately
   precedes it, so `"2012R2"` now correctly reads as an edition marker
   while still declining to match mid-word (`"R2D2"`, `"SuperR2000"`).
3. **Shared-hint check** (`_release_required_hints`) — a tie is only safe to
   resolve further when every tied release is actually explained by the
   *same* hint (or hint-set). For each still-tied release, this works out
   which hint(s) are responsible for its winning score, as the **union** of
   every hint that reaches the score alone (an ordinary exact/prefix/suffix
   match) *and* every token the compound-token "every token present" rule
   confirms — a release can be confirmed by both mechanisms at once, and
   dropping either one can silently make two genuinely-related releases
   look unrelated (see **Real incident** below). If the **intersection**
   across every tied release's requirement is empty — no hint is common to
   all of them — the tie isn't "several editions of one thing," it's **two
   or more genuinely different releases each independently matched by a
   different hint**, and `pick_release` refuses outright (returns nothing)
   rather than guess one. See the `"Android 14-11"` example below.

   **Exception — an empty intersection is bypassed when every tied
   candidate shares the exact same `latest.name` (build).** No hint tying
   two releases together isn't proof they're unrelated when the *catalog
   itself* already proves otherwise — a shared build is a structural fact,
   independent of which hints happen to be in the query. **Real incident:**
   `"Microsoft Hyper-V Windows Server 2019  Version 1809"` (no build number
   anywhere) ties `"2019"` (required `{"2019"}`) against `"1809-sac"`
   (required `{"1809"}`) — an empty intersection, indistinguishable by hint
   alone from `"Android 14-11"`. But both releases share build
   `10.0.17763` — Windows Server 2019 genuinely *is* internally versioned
   "1809" (Microsoft's own docs call it "Windows Server 2019, Version
   1809"); "1809 SAC" is a separate product that merely collides on that
   number. When every tied candidate's build matches, the refusal is
   skipped and the dominant-evidence check (tie-breaker 4, still using
   strong-only evidence) decides the winner — `"2019"`'s ordinary exact
   match still outweighs `"1809-sac"`'s compound-token-only match, so it
   still wins outright rather than being guessed. This never applies when
   the tied releases have genuinely *different* builds (or no `latest.name`
   at all) — see the `"Windows Server 2008 R2 - 2012"` example below.

   **Real incident:** before taking the union, this function returned
   **either** the single-hint matches **or** the compound-token tokens,
   whichever came first — never both. `"WindowsServer2008R2 7601"` ties
   `"2008-r2-sp1"` (confirmed via compound-token on `"2008"` *and*
   build-suffix-match on `"7601"`) against `"2008-sp2"` (confirmed via
   compound-token on `"2008"` only — its own build `6.0.6003` doesn't end
   in `"7601"`). Since `"2008-r2-sp1"` reached its score via the single-hint
   build-suffix match on `"7601"` alone, its required set was reported as
   just `{"7601"}` — silently **dropping** the `"2008"` it was *also*
   genuinely confirmed by — leaving it with nothing in common with
   `"2008-sp2"`'s `{"2008"}`, an empty intersection, refusing a release
   with objectively *more* evidence than its tied sibling.
4. **Dominant-evidence check** — a tied candidate confirmed by *strictly
   more* evidence than every other tied candidate isn't "one of several
   equally-plausible editions" — it's simply the better-supported match, and
   wins outright instead of being averaged with the others. If exactly one
   tied release's required-hint set is a **strict superset** of every other
   tied release's required set, narrow to just that one. This never fires
   when every tied release needs the identical hint-set (e.g. the Windows
   24H2 case below — no superset relationship exists there at all). See the
   `"Windows Server 2019"` example below.

   Compares only the **strong** hints (ordinary exact/prefix/suffix
   matches) here, not the fuller union from tie-breaker 3 above, which also
   includes compound-token-only evidence. **Real incident:** naively
   comparing the full union (to fix the `"2008-r2-sp1"` case just above)
   reopened the `"Windows Server 2019"` case below — `"1809-sac"` would
   then *also* gain `"1809"` (its own compound-token match, from its bare
   `"1809-sac"` slug), making it exactly as "evidenced" as `"2019"` (which
   gains `"2019"` via a genuine ordinary *exact* match, not compound-token)
   — neither would dominate, silently falling back to conservative-merging
   on `1809-sac`'s much shorter EOL window. A compound-token match is a
   looser, name-only heuristic — built specifically for slugs that aren't
   clean dotted versions at all — and shouldn't by itself outweigh another
   tied release's equally-weak compound-token match; comparing strong-only
   evidence keeps `1809-sac`'s dominance-relevant evidence to just its
   shared `"17763"`, while `2019`'s stays `{"2019", "17763"}` — a genuine
   strict superset.
5. **Exact-score requirement** — even when every tied release *does* share a
   hint (and no single one dominates), the tie is only safe to merge when
   that shared best score is a genuine **100** (an exact string match, or
   the compound-token rule's "every token present" full match) — never the
   *weaker* 90-point numeric prefix score. A hint that only ever reaches 90
   against a release is, by definition, *coarser* than that release's own
   version — "explained by the same hint" isn't the same as "confirmed"
   when the hint itself doesn't pin down any one release. See the
   `"Windows 10.0"` example below.
6. **Conservative merge — "least date" picking** (`_conservative_release`) —
   a tie that survives both checks above is resolved by assuming the
   **worst case**: the tied release with the **earliest** EOL date is used
   as the base result, and its EOL/EOAS dates are the *minimum* across every
   tied release. The reasoning: if we genuinely can't tell which of several
   editions this OS actually is, support should never be reported as
   lasting *longer* than it might actually be.

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

**Example — a tie that must refuse instead of guess (regression-tested):**
Query `"Android 14-11"` → hints `["14", "11"]` (two independent digit runs —
the hyphen isn't a dot, so this is never one compound version). Android
release `"14"` scores 100 against hint `"14"` alone; release `"11"` scores
100 against hint `"11"` alone — both hit the same top score, so they tie.
But **no hint is shared between them**: release `14`'s requirement is
`{"14"}`, release `11`'s requirement is `{"11"}`, and their intersection is
empty. This is not "several editions of one release" (like the 24H2 case
above, where every tied candidate needs the *same* `"11"`+`"24"` pair) — it's
two genuinely different, unrelated releases each independently explained by
a different piece of the query. `pick_release` returns nothing rather than
picking whichever has the earliest EOL date (which, before this rule
existed, silently produced `"Android 11"` — a specific, confident, and
wrong answer for a string that plausibly names two different versions at
once).

**Example — a tie that must refuse because the shared hint isn't specific
enough (regression-tested):**
Query `"Windows 10.0"` (no build number at all) → hint `["10.0"]` only.
Scoring: `"10.0"` (2-part) is a genuine numeric *prefix* of `[10, 0, 10240]`,
`[10, 0, 14393]`, `[10, 0, 15063]`, … — **every** Windows 10/11 release's
`latest.name` starts `"10.0."` — so it scores exactly 90 against the entire
catalog at once. Every one of those releases ties at the same score, and
the shared-hint check passes (they genuinely are all explained by the same
`"10.0"` hint) — but the exact-score requirement above catches this: 90 is
the weaker *prefix* score, never the genuine 100 an exact match or
compound-token full match would give. `pick_release` returns nothing rather
than conservative-merging to whichever release has the earliest EOL (which,
before this fix, silently produced `"Microsoft Windows 10 1507"` for a
query that named no build at all). A row with a real build number attached —
`"Windows 10.0 (14393)"`, `"Windows 10.0 22631 64-bit"` — still resolves
correctly: `extract_version_hints` combines the dotted version with an
adjacent parenthesized or bare trailing build number into one hint
(`"10.0.14393"`, `"10.0.22631"`), which scores a genuine 100 against its one
true release, strictly beating the rest of the family's 90-point tie.

**Example — a tie resolved by strictly-more evidence, not by date
(regression-tested):**
Query `"Microsoft Windows Server 2019 Datacenter 10.0.17763 0"` → hints
`["2019", "10.0.17763", "0"]`. Windows Server's `2019` release (label
`"Windows Server 2019 (LTSC)"`) and `1809-sac` release (label `"Windows
Server 1809 SAC"`) share the **exact same** `latest.name`, `"10.0.17763"` —
Server 2019 LTSC and the 1809 Semi-Annual-Channel release happen to be the
same underlying build. Both score 100 (exact match via `"10.0.17763"`), so
they tie. Their required-hint sets, though, are **not equal**: `1809-sac`'s
name/label never match `"2019"` at all, so its required set is just
`{"10.0.17763"}`; release `2019`'s own `name` is literally `"2019"`, an
*additional* exact match, giving it required set `{"2019", "10.0.17763"}` —
a strict superset. The dominant-evidence check catches this: `2019` is
confirmed by strictly more of the query's own hints than `1809-sac` is, so
it wins outright. Before this fix, the shared-hint check only asked "is
there some common hint" (yes, `"10.0.17763"`) and conservative-merged to
whichever has the earliest EOL — `1809-sac`'s much shorter 18-month
Semi-Annual-Channel window — silently discarding the `"2019"` the query
explicitly named, and reporting `"Windows Server 1809 SAC"` for a string
that plainly said `"2019"`.

**Example — a dotted hint outranks a coincidental bare exact-match
(regression-tested):**
Query `"RHEL 6.6 3 8"` (kernel `3.8`, space- not dot-separated) → hints
`["6.6", "3", "8"]`. RHEL's entire catalog is bare major-version names —
`"10"`, `"9"`, `"8"`, `"7"`, `"6"`, … On the full hint set, release `"8"`
scores a full **100** (exact match against the bare `"8"` hint), while the
genuinely correct release `"6"` only scores **90** (`"6.6"` is a numeric
prefix of `"6"`, not an exact match) — release `"8"` would win outright,
not even a tie. The dotted-hint-preference pass reruns scoring using only
`["6.6"]`: now `"6"` is the unique 90-scoring winner, which disagrees with
the full-hint-set result, so `pick_release` prefers it — correctly landing
on RHEL 6. The identical shape affected CentOS (`"CentOS 7.9 5 4"` → wrongly
`"5"` instead of `"7"`) and iOS (`"iOS 16.7 10"`, a genuine 16.7.10 point
release rendered space-separated → wrongly `"10"` instead of `"16"`).

**Example — a compound-slug release name resolves from the year alone
(regression-tested):**
Query `"Windows Server 2008 Standard"` → hint `["2008"]`. Windows Server's
real release name for this generation is the compound slug `"2008-sp2"`,
not bare `"2008"` — `_release_name_tokens("2008-sp2")` yields `["2008"]`
(the `"2"` in `"sp2"` is excluded as a Service-Pack marker digit), and that
single token is present in the hints → scores 100 via the compound-token
rule, picked outright. Naming the R2 generation too —
`"Microsoft Windows Server 2008 R2 Standard 7600"` → hints `["2008",
"7600"]` — ties `"2008-sp2"` against `"2008-r2-sp1"` (both reduce to the
single token `["2008"]`, both score 100); edition narrowing (tie-breaker 2)
sees `"R2"` in `os_text` and narrows to `"2008-r2-sp1"` specifically, since
only its label contains `"r2"`.

**Example — a trailing build number supplies the missing shared hint
(regression-tested):**
Query `"Windows Server 2019 Datacenter AD Version 1809 Build 17763"` →
hints `["2019", "1809", "17763"]`. Releases `"2019"` and `"1809-sac"` share
build `10.0.17763` and each independently scores 100 via its own name
(`"2019"` matches hint `"2019"`; `"1809-sac"` matches hint `"1809"`) — a
tie. `"17763"` alone doesn't prefix-match `"10.0.17763"` (it's the
*trailing* segment, not a prefix), so before the build-number-suffix rule
existed, no hint was recognized as common to both tied releases at all —
the shared-hint check (tie-breaker 3) saw an empty intersection and
refused, never even reaching the dominant-evidence check. With the new
rule, `"17763"` scores 100 against both releases' shared `"10.0.17763"`
build, giving both a required set that includes `"17763"` — a non-empty
intersection — and `"2019"`'s *strong* evidence (`{"2019", "17763"}` — both
via an ordinary exact/suffix match, not compound-token) is then a strict
*superset* of `"1809-sac"`'s strong evidence (`{"17763"}` alone — its own
`"1809"` match only ever reaches 100 via the weaker compound-token rule, so
it's excluded from the strong-evidence comparison tie-breaker 4 uses), so
the dominant-evidence check correctly narrows to `"2019"`.

**Example — a release with extra build-suffix evidence dominates
(regression-tested):**
Query `"WindowsServer2008R2 7601"` → hints `["2008", "7601"]`. Releases
`"2008-sp2"` and `"2008-r2-sp1"` both score 100 via the compound-token rule
on `"2008"` — a tie. But `"2008-r2-sp1"`'s own build (`6.1.7601`) *also*
ends in `"7601"`, a build-suffix match `"2008-sp2"`'s build (`6.0.6003`)
doesn't share at all. `_release_required_hints` (tie-breaker 3) takes the
**union** of every mechanism that confirms a release, not just whichever
happens to reach the score first — `"2008-r2-sp1"`'s required set is
`{"2008", "7601"}` (both compound-token *and* build-suffix), a strict
superset of `"2008-sp2"`'s `{"2008"}` alone, so the dominant-evidence check
correctly prefers `"2008-r2-sp1"`. Naming R2 with the year glued directly
together (`"WindowsServer2012R2 9600"`, no space) resolves the same way —
plus edition narrowing (tie-breaker 2) recognizes `"R2"` even glued
directly to the preceding digit, giving a second, independent path to the
same correct answer.

**Example — a shared build bypasses the empty-intersection refusal
(regression-tested):**
Query `"Microsoft Hyper-V Windows Server 2019  Version 1809"` — no build
number at all → hints `["2019", "1809"]`. Releases `"2019"` (required
`{"2019"}`) and `"1809-sac"` (required `{"1809"}`) share **no** hint at
all — by hint alone, indistinguishable from `"Android 14-11"`. But both
releases' `latest.name` is the identical `"10.0.17763"` — the catalog
itself proves these are the same underlying release under two names
(Windows Server 2019 genuinely *is* internally versioned "1809"), so the
empty-intersection refusal is skipped, and the dominant-evidence check
(strong-only: `"2019"` has `{"2019"}` via an ordinary exact match,
`"1809-sac"` has `{}` since its own match is compound-token-only) still
correctly prefers `"2019"`. Contrast with a query that genuinely ties four
releases with four **different** builds (e.g. explicit hints `["2008",
"2012"]` against `2008-sp2`/`2008-r2-sp1`/`2012`/`2012-r2`, all four
different builds) — the bypass never applies (no shared build to prove
relatedness), so it correctly refuses: a string that genuinely names two
different OS generations at once still gets no answer.

**Example — a trailing " - \<year\>" is metadata, not a second version
(regression-tested):**
Query `"Microsoft Windows Server 2008 R2 - 2012"` → `extract_version_hints`
now yields `["2008"]` only — the trailing, space-separated `"- 2012"` is
dropped as inventory metadata (an install/license/audit-year stamp) rather
than a second OS-version claim, since `"2008 R2"` already fully names the
OS on its own. This used to yield `["2008", "2012"]`, tying **four**
releases with **four different** builds — no shared build to fall back
on, and no hint in common either (`"2008"` vs `"2012"`), so it refused
outright, exactly as it should for a string that genuinely names two
different generations. With only `"2008"` extracted, the tie is between
`"2008-sp2"` and `"2008-r2-sp1"` alone — resolved by edition narrowing
(tie-breaker 2), which sees the literal `"R2"` in the string and narrows to
`"2008-r2-sp1"` specifically. The **whitespace before the hyphen is what
keeps this exclusion narrow**: `"Android 14-11"` (no spaces around its
hyphen) is a fundamentally different shape and is completely unaffected —
see the exclusion bullet above for the full reasoning.

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

### Prior-value fallback (`_pick_release_by_prior_value`)

endoflife.date's own catalog gets more precise over time — a release once
tracked generically (e.g. SUSE Linux Enterprise Server's `"15"`) can later be
split into per-service-pack releases (`"15.2"`, `"15.3"`, …) once the
maintainers start tracking them individually. A row resolved against the old,
coarser name has `normalized_os`/`normalized_os_detailed_name` ending in
`"...15"`; a refresh's extracted hints are then just a bare `"15"`, which
correctly scores **0** against every one of today's multi-part releases (the
"bare major must not guess" rule above) — so the row would go permanently
unresolved despite endoflife.date clearly still tracking that exact OS, just
under a more specific name.

When ordinary hint scoring (`pick_release`) finds nothing, `lookup_os_eol`
tries one more fallback before giving up: for each release, build its
*prospective* new name the same way `build_normalization_from_product` would
(product label + release label/name) and compare it against whatever the row
already had on record (`normalized_os_detailed_name`/`normalized_os`), using
a plain textual similarity ratio (`difflib.SequenceMatcher`). If **exactly
one** release's prospective name is a near-exact (**≥ 95%**) match to the
row's existing value **and** that release's own version number is a genuine
numeric prefix/extension of the prior value's version (see below), that
release is adopted — endoflife.date's fresh name and dates overwrite the
row's stale ones, the same as any other resolved match (`_apply_lifecycle_result`
in `app.py` already overwrites unconditionally whenever a lookup produces a
name/date, regardless of what was there before).

**The version-extension check (`_is_plausible_version_extension`) — real
incident:** text similarity alone can't tell "the catalog got more specific"
(`"15"` → `"15.2"`, the genuine case this fallback exists for) apart from
"two completely unrelated version numbers that merely look similar as flat
strings." A row's prior value was `"Apple iOS 27"` (an invalid/future
version someone typed) — and endoflife.date's real release `"7"` (iOS 7,
from 2013) scored a **95.65%** match against it via `difflib.SequenceMatcher`
alone, purely because `"Apple iOS 7"` is one character *shorter* than
`"Apple iOS 27"` (the ratio formula rewards the shorter total-length
pairing) — while every other, equally plausible release (`"17"`, `"20"`
through `"26"`) scored under 92%, comfortably below the bar. `"27"` and
`"7"` have no genuine prefix/extension relationship at all; the old,
text-only check confidently (and wrongly) rewrote the row to iOS 7's
decade-old EOL/EOAS dates. The fix extracts the prior value's own version
hint and the release's bare/dotted version number, and requires one to be a
genuine numeric prefix of the other (in *either* direction — `"15"` → `"15.2"`
or `"15.2"` → `"15"`) before accepting — `"27"` is not a prefix of `"7"`,
nor `"7"` of `"27"`, so this now correctly refuses. Only applies when the
release's own name is cleanly numeric — a compound slug (Windows Server's
`"2008-sp2"`) can't be parsed this way, so it's left to the text-similarity
check alone, unaffected by this fix.

This only ever fires when the row already has *some* prior normalized value
to anchor to (a placeholder/junk value, or a blank one, doesn't count) — a
brand-new, never-matched row still goes through ordinary hint scoring or
nothing at all, exactly as before. And it only accepts when the match is
**unambiguous**: if the catalog now lists *several* similarly-named releases
(e.g. multiple SUSE service packs all close to a bare `"15"`), that's genuine
ambiguity — this fallback refuses rather than guess, same philosophy as
`pick_release`'s own tie-break rules.

**Example:** row has `normalized_os = "SUSE Linux Enterprise Server 15"`.
endoflife.date's `sles` product now only lists release `"15.2"` (no bare
`"15"` release anymore). Hint scoring: bare `"15"` vs multi-part `"15.2"` →
score 0 → `pick_release` returns nothing. Fallback: only one release exists,
and `"SUSE Linux Enterprise Server 15.2"` (its prospective new name) is a
96.9% textual match to the row's existing `"SUSE Linux Enterprise Server
15"` → accepted. Row's normalized fields and dates update to the `15.2`
release. If the catalog instead listed `15.1`, `15.2`, *and* `15.3` (all
similarly close to the old bare `"15"`), the fallback would refuse — genuine
ambiguity about which specific service pack this row actually is.

This fallback is specific to `eol_service.py` (the direct endoflife.date
API) — the vendor cascade sources each have their own, unrelated
`_pick_release` implementations and are not affected.

### Dot-zero fallback (`_pick_release_by_dot_zero_release_name`)

A second, narrower fallback, tried only after both the strict pass above
*and* the prior-value fallback find nothing: a bare, dot-less version hint
(e.g. `"15"`) can never score against a multi-part release name (the "bare
major must not guess" rule) — but endoflife.date sometimes only lists that
exact release as `"<version>.0"`.

**Example:** `os_string = "SUSE Linux Enterprise Server 15 SP7"`.
`extract_version_hints` drops the SP-marker digit (per the "lone SP/R/U/Pack
digit" exclusion) and yields a bare `"15"` alone. endoflife.date's actual
SLES release for this row is named `"15.0"` — a bare `"15"` hint scores 0
against it (multi-part release), even though `"15"` and `"15.0"` plainly
mean the same release.

This fallback checks, for a bare hint `"15"`, whether **exactly one**
release's own `name` or `label` (never `latest.name`, the raw build field)
is *literally* `"15.0"`. If so, that release is accepted; if zero or more
than one release matches, it refuses — same "never guess among ambiguous
candidates" philosophy as everywhere else in this module.

**Deliberately not implemented as "append `.0` and re-run the normal scoring
pipeline"** — that pipeline's genuine numeric-prefix rule (a 90-point score)
would let a synthesized `"15.0"` hint match any *longer* release/build
string that merely starts with `"15.0…"`. Windows' own NT kernel numbering
is `"10.0.NNNNN"` for every 10/11 build, so a bare `"Windows 10"` query
(correctly refused everywhere else in this module) would wrongly resolve to
a specific build via a fake `"10.0"` hint prefix-matching `"10.0.26100"`,
etc. — a real regression caught by this exact scenario while building the
fallback. Restricting the check to an *exact* string match on `name`/`label`
only avoids this entirely.

Not product-specific — applies to any product via a bare numeric hint, not
just SUSE.

### Product-level release fallback (`_PRODUCT_RELEASE_FALLBACK_SLUGS`)

A third fallback, this one at the *product* level rather than the release
level — tried only when the resolved product has **no release at all**
matching the query (both fallbacks above also came up empty).

**Real incident:** endoflife.date's `ipados` product only tracks major
version **12 and up** — Apple didn't introduce "iPadOS" as a distinct
product name until 2019 (what would otherwise have been "iOS 13"); before
that, iPads genuinely ran plain "iOS", and there's no such thing as
"iPadOS 10"/"iPadOS 11" in real life. So `"iPad 10.0.2"`/`"iPad 11.4.1"`
correctly resolve to product `ipados` (via the `_INVENTORY_PHRASE_EXTRAS`
alias), but `ipados` has nothing before major 12 — the ordinary hint-scoring
pass and the prior-value fallback both find nothing, and the row fell all
the way through to the local vendor cascade (eosl.date) for a lookup
endoflife.date could actually answer directly, just under its **older**
`ios` product name for that version range.

`_PRODUCT_RELEASE_FALLBACK_SLUGS` maps `ipados` → `ios`: when a product
resolves but yields zero matching releases, `lookup_os_eol` retries the
*same* hints against the fallback product's own release list — still
entirely within the direct endoflife.date path, never touching the vendor
cascade. If the fallback product has a genuine match, its data is used (and
`product_slug` in the row's evidence correctly reports `ios`, not `ipados`,
so it's clear which product actually answered); if it doesn't either, the
row falls through to the vendor cascade exactly as before. A version
`ipados` *does* cover (major 12+) never reaches this fallback at all, since
the ordinary scoring pass already succeeds first.

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

- `eol_date` / `eol_status` / `eoas_date` / `eoas_status` are overwritten
  **only when this lookup actually resolved a lifecycle state**
  (`_row_has_lifecycle_data`, true for an explicit date *or* an explicit
  true/false status with no date — a genuinely confirmed "not end-of-life"
  answer per `resolve_lifecycle_status` counts the same as a date would). A
  genuine no-match leaves the row's existing values alone entirely, rather
  than wiping perfectly good, previously-resolved dates just because this
  particular refresh run's cascade happened to find nothing.
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
| Bitness numbers (16/32/64/86/128/256) only excluded in bitness *context* | `extract_version_hints`/`_release_name_tokens` (`_looks_like_bitness_marker`) | `"64-bit"` being read as "version 64" — **without** also making a real version number that happens to be 16/32/64/… (e.g. `"Android 16"`) permanently unmatchable |
| `N.x` ranges are dropped | same | `"3.x or later"` being read as version 3 |
| Lone SP/R/U/Pack digit dropped | same | `"Service Pack 2"` / `"SP2"` / `"R2"` / `"U1"` contributing a bogus version 2 hint |
| Compound tag doesn't leak a trailing digit, narrowed to a 2-char lookbehind so a glued-on version still extracts in full | same (`(?<![0-9][A-Za-z])`) | `"24H2"` also yielding a spurious `"2"` hint, **without** also truncating `"WindowsServer2008R2"`'s glued `"2008"` down to `"008"` |
| Bare single-part hint never matches a multi-part release | `score_release_against_hint` | `"Windows 10"` (bare) picking one specific Windows 10 build/release at random |
| Compound-token full match requires *every* token, and at least one (marker digits like SP/R/Pack numbers excluded first) | `eol_service.py::_release_score`/`_release_name_tokens`/`_release_required_hints` | a bare major alone claiming a specific name-based release the same way the rule above prevents it for builds — while still letting Windows Server's compound slugs (`"2008-sp2"`, `"2008-r2-sp1"`) resolve from the year alone, since their SP/R2 marker digits are noise, not version tokens |
| A tie only conservative-merges when every tied release shares a common explaining hint | `pick_release`/`_release_required_hints` | `"Android 14-11"` (hints `"14"`+`"11"`, each independently matching a *different* release) silently resolving to whichever tied release has the earliest date, as if that were confirmed |
| A dotted hint outranks a coincidental bare exact-match, but only when the dotted-only pass is itself unique | `pick_release` (dotted-hint preference) | products with bare major-version-only catalogs (RHEL/CentOS/iOS) resolving to an unrelated release because a stray bare number elsewhere in the query (a kernel-version fragment, a space-separated point-release digit) exact-matched it while the genuine dotted hint only prefix-matched (90) the correct one — **and**, the uniqueness requirement itself prevents a coarse dotted hint that ties nearly the whole catalog (e.g. Windows Server's shared `"10.0"` build prefix) from overriding an already-unique, correct 100-score match (`"WindowsServer2016 10.0"` → `"2016"`) |
| A server-generation year alongside any "win"/"windows" mention routes to `windows-server` even without the literal word "Server" | `resolve_product_slug`/`_SLUG_PRIORITY_OVERRIDES` | `"Windows 2008 R2 Standard"`/`"Win 2008 R2"` (a common real-world shorthand that drops "Server" entirely) resolving to the generic client `windows` product, which has no release for that year at all |
| A tie only conservative-merges when the shared score is a genuine 100, never the weaker 90-point prefix score | `pick_release` (exact-score requirement) | A bare `"10.0"` hint (a genuine numeric prefix of every Windows 10/11 build) tying the entire family and resolving to whichever has the earliest EOL, as if a query naming no build number at all had confirmed a specific one |
| A tied candidate confirmed by strictly more evidence than every other tied candidate wins outright, not averaged with them | `pick_release` (dominant-evidence check) | Windows Server 2019 (LTSC) and 1809 SAC sharing build `10.0.17763` resolving to 1809 SAC's much shorter EOL window, discarding the "2019" hint the query explicitly gave |
| A bare 4+-digit hint also matches a release's own trailing build segment, not just a leading prefix | `eol_service.py::_hint_matches_build_suffix`/`_score_release_candidate` | `"Windows Server 2019 ... Version 1809 Build 17763"` refusing outright, since the standalone `"17763"` (no adjacent `"10.0"` to combine with) couldn't confirm either of the two tied, same-build releases, leaving the dominant-evidence check above with no shared hint to even evaluate |
| A product with no matching release at all retries against a designated fallback product, still inside the direct endoflife.date path | `eol_service.py::_PRODUCT_RELEASE_FALLBACK_SLUGS` | `"iPad 10.0.2"`/`"iPad 11.4.1"` resolving to `ipados` (correctly) but finding nothing there (`ipados` only tracks major 12+) and falling all the way to the eosl.date vendor cascade for an answer endoflife.date's own `ios` product could give directly |
| A release's required-hint set is the UNION of single-hint and compound-token evidence, never just whichever came first | `eol_service.py::_release_required_hints` | `"WindowsServer2008R2 7601"` reporting `"2008-r2-sp1"`'s evidence as only `{"7601"}` (dropping the ALSO-genuine `"2008"` compound-token match), leaving it with nothing in common with `"2008-sp2"`'s `{"2008"}` and refusing a release with strictly more evidence |
| Dominant-evidence comparison uses STRONG hints only (ordinary exact/prefix/suffix), not the fuller union above | `eol_service.py::_release_strong_hints` | the union fix above (naively applied to dominance too) reopening the `"Windows Server 2019"` case: `"1809-sac"` gaining its own compound-token `"1809"` match would make it exactly as "evidenced" as `"2019"`, so neither dominates and it silently falls back to `1809-sac`'s much shorter EOL window |
| "R2" edition marker recognized even glued directly to the preceding digit | `eol_service.py::_EDITION_LABEL_HINTS` (`(?<![A-Za-z])r2(?![0-9A-Za-z])`, not `\br2\b`) | `"WindowsServer2012R2 9600"` — `\b` never fires between two word characters (digit + letter), so the old pattern silently never matched, and the `"2012"` vs `"2012-r2"` tie resolved only by an accidental EOL-date coincidence in the real catalog, not genuine confirmation |
| An empty shared-hint intersection is bypassed when every tied candidate has the exact same `latest.name` (build) | `pick_release` (shared-build exception) | `"Microsoft Hyper-V Windows Server 2019  Version 1809"` (no build number at all) refusing outright even though the catalog itself proves `"2019"` and `"1809-sac"` are the same underlying release (shared build `10.0.17763`) — genuinely different-build ties are unaffected and still correctly refuse |
| A trailing, whitespace-separated `"- <year>"` at the end of a string is dropped as metadata once an earlier hint exists, never treated as a second version | `extract_version_hints` | `"Microsoft Windows Server 2008 R2 - 2012"` extracting a spurious second `"2012"` hint and tying four different-build releases with nothing in common, refusing a string that only ever names ONE OS ("2008 R2") — the required whitespace-before-hyphen keeps `"Android 14-11"` (no spaces, two genuine independent hints) completely unaffected |
| A parenthesized or bare trailing build number is combined with the dotted version right before it into one hint | `extract_version_hints` | `"Windows 10.0 (14393)"`/`(15063)`/`(16299)`/`(17763)`/`(18363)`/`"22631 64-bit"` all resolving to the SAME earliest-EOL release regardless of the actual build named |
| Ties resolved by edition first, then (only if still sharing a hint) earliest-date | `pick_release`/`_conservative_release` | reporting longer support than an ambiguous edition might actually have |
| LTSC/LTS, then R2, both narrow before bare Enterprise | `pick_release`/`_EDITION_LABEL_HINTS` | `"Windows 10 Enterprise LTSC 10.0.17763"` narrowing only to `"(e)"` (matching both the LTS and non-LTS releases) and conservative-merging to the non-LTS release's earlier EOL, instead of the LTSC one actually named; and `"Windows Server 2008 R2 Enterprise"` matching Enterprise first (a real edition name, so it never gets a chance to narrow anything for 2008 vs. 2008-R2) instead of R2 |
| A resolved status with no date still counts as real lifecycle data | `app.py::_row_has_lifecycle_data` | a genuinely resolved `eol_status`/`eoas_status` (e.g. `isEol: false`, no `eolFrom`) being silently dropped because only dates were checked |
| Full product name must literally appear in the query (≥95, except SUSE's edition-aware 60) | every `_resolve_product_slug` | a short/generic query fragment being treated as "contained in" an unrelated, much longer product name |
| Only `category == "os"` endoflife.date products are ever considered | `get_product_catalog` | Apple's hardware `ipad` product (category `device`) winning product resolution for every `"iPad <version>"` os_string, since its own slug/label is the bare word `"ipad"` |
| A product whose own slug/label is a single generic word requires an extra trust word before its match is accepted | `resolve_product_slug`/`_generic_family_match_is_trustworthy` | `"Linux 6.4.7.3762 7"` (no mention of "kernel" anywhere) resolving to endoflife.date's `linux` product (label "Linux Kernel") purely because the common word "linux" was present, and adopting that specific kernel release's own EOL date |
| A stale normalized field naming an alias-covered product's sibling is overridden by the raw os_string | `lookup_os_eol` (same-vendor override, next to Step 0) | `normalized_os="Apple iOS 10"` sitting on a row whose real `os_string="iPad 10.3.4"` silently pulling iOS's own EOL/EOAS instead of iPadOS's — `vendors_compatible` alone doesn't catch this since both are "apple" vendor |
| Vendor compatibility gate | `vendors_compatible`, checked before product-field selection and again after product resolution | Cisco IOS ↔ Apple iOS, VMware ↔ Microsoft, Cisco Firepower ↔ Google Container-Optimized OS, and similar cross-vendor false matches |
| Generic family guard (`linux`/`windows`/`unix`) | `eosl_service.py::_query_targets_generic_family` | a vague `"Other ... Linux"` string silently absorbing into the generic Linux kernel product page |
| `windows` family excluded entirely | `microsoft_lifecycle_service.py::_EXCLUDED_FAMILIES` | a bare-major Windows build matching dozens of unrelated tool releases that happen to share a "10-…" slug |
| Juniper X-train mismatch refused | `junos_service.py::_version_match_score` | `15.1X49` matching a hint that actually names `15.1X53` |
| Ambiguous OS rows skipped entirely | `is_ambiguous_row` | writing a real date onto a row whose product literally can't be determined |
| No hints at all → no match | every `pick_release`/`_pick_release` | ever guessing the first/latest release when there's no version evidence whatsoever |
| Prior-value fallback only accepts an unambiguous (single-candidate) ≥95% textual rename | `eol_service.py::_pick_release_by_prior_value` | a coarser old catalog entry (e.g. SUSE `"15"`) becoming permanently unmatchable once endoflife.date splits it into specific releases (`"15.2"`, …), while still refusing to guess among several similarly-named candidates |
| Prior-value fallback's ≥95% textual match must ALSO be a genuine numeric prefix/extension, not just similar-looking text | `eol_service.py::_is_plausible_version_extension` | a prior value of `"Apple iOS 27"` (an invalid/future version) scoring 95.65% *text* similarity against unrelated release `"7"` (iOS 7, 2013) purely because it's one character shorter — silently rewriting the row to iOS 7's decade-old EOL/EOAS dates |
| Dot-zero fallback only accepts an *exact* `name`/`label` match on `"<bare hint>.0"`, never routed through the general scoring pipeline's prefix rule | `eol_service.py::_pick_release_by_dot_zero_release_name` | a bare hint like `"15"` (e.g. from "SUSE Linux Enterprise Server 15 SP7") never matching endoflife.date's `"15.0"`-named release, while NOT letting a synthesized `"10.0"` hint prefix-match a long build number like Windows' `"10.0.26100"` |

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

## Secondary: row-identity matching (Data-vs-Draft diff)

A third, much simpler kind of "matching": deciding whether a row in Draft is
the "same" row as one in Data, for the Draft-vs-Data diff panel. Lives in
`lookup_extras.py`.

- **Identity key** = the row's `os_string`, trimmed and lowercased
  (`_dedupe_key`). Two rows with the same key are the same row; a row with a
  blank `os_string` has no stable identity and is never diffed by content —
  it just passes through untouched.
- **Equality** (`_rows_equal`) compares every CSV column except `os_string`
  itself (boolean-ish status cells compared case-insensitively). A key that
  appears more than once on either side is never content-diffed at all —
  it's counted as `unresolved` rather than picking one arbitrarily.
- **Publish** itself has no per-row merge: it's a revision-guarded Postgres
  transaction (`lookup_db.db_publish`) that rejects outright if Data moved
  since the draft's expected revision, rather than merging row-by-row.

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
| `_PRIOR_VALUE_SIMILARITY_THRESHOLD` (eol_service.py) | 0.95 (95%) | Minimum textual similarity between a row's existing normalized name and a release's prospective new name for the prior-value fallback to adopt it |
