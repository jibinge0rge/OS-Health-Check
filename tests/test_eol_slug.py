"""Tests for endoflife.date product slug resolution."""

from __future__ import annotations

import unittest

from eol_service import (
    _edition_label_substring,
    build_normalization_from_product,
    build_slug_index,
    extract_version_hints,
    join_labels,
    pick_release,
    resolve_product_slug,
)

TEST_CATALOG: list[dict[str, object]] = [
    {
        "name": "rhel",
        "label": "Red Hat Enterprise Linux",
        "aliases": ["redhat", "redhatlinux"],
    },
    {"name": "ubuntu", "label": "Ubuntu", "aliases": ["ubuntu-linux"]},
    {"name": "windows", "label": "Microsoft Windows", "aliases": []},
    {
        "name": "windows-server",
        "label": "Microsoft Windows Server",
        "aliases": ["windowsserver"],
    },
    {
        "name": "rocky-linux",
        "label": "Rocky Linux",
        "aliases": ["rocky", "rockylinux"],
    },
    {
        "name": "almalinux",
        "label": "AlmaLinux OS",
        "aliases": ["alma-linux", "alma"],
    },
    {
        "name": "oracle-linux",
        "label": "Oracle Linux",
        "aliases": ["oraclelinux"],
    },
    {"name": "amazon-linux", "label": "Amazon Linux", "aliases": []},
    {"name": "centos", "label": "CentOS", "aliases": []},
    {"name": "centos-stream", "label": "CentOS Stream", "aliases": []},
    {
        "name": "sles",
        "label": "SUSE Linux Enterprise Server",
        "aliases": [
            "suseenterpriseserver",
            "suseserver",
            "suselinuxenterpriseserver",
        ],
    },
    {"name": "debian", "label": "Debian", "aliases": []},
    {"name": "fedora", "label": "Fedora Linux", "aliases": []},
    {"name": "linux", "label": "Linux Kernel", "aliases": ["linuxkernel", "linux-kernel"]},
    {"name": "macos", "label": "Apple macOS", "aliases": ["mac"]},
    {"name": "ios", "label": "Apple iOS", "aliases": []},
    {"name": "android", "label": "Android OS", "aliases": ["aosp", "androidos"]},
    {
        "name": "esxi",
        "label": "VMware ESXi",
        "aliases": ["esx", "vmwareesxi", "vmesxi", "vmware-esxi"],
    },
    {"name": "cisco-ios-xe", "label": "Cisco IOS XE", "aliases": []},
    {"name": "fortios", "label": "FortiOS", "aliases": []},
    {
        "name": "red-hat-openshift",
        "label": "Red Hat OpenShift",
        "aliases": ["openshift", "rh-openshift"],
    },
    {
        "name": "panos",
        "label": "Palo Alto Networks PAN-OS",
        "aliases": ["pan-os"],
    },
]

VALID = frozenset(str(item["name"]) for item in TEST_CATALOG)
SLUG_INDEX = build_slug_index(TEST_CATALOG)


def resolve(os_name: str) -> str | None:
    return resolve_product_slug(os_name, VALID, slug_index=SLUG_INDEX)


class ResolveProductSlugTests(unittest.TestCase):
    def test_red_hat_variants(self) -> None:
        cases = {
            "RedHat Enterprise Linux AS/Intel": "rhel",
            "Red Hat Enterprises Linux 7.4": "rhel",
            "Red Hat Linux 7.4": "rhel",
            "Red Hat Linux8.2": "rhel",
            "Red Hat Enterprise Linux release 9.7 (Plow)": "rhel",
            "Red Hat Enterprise Linux release 9.8 (Plow)": "rhel",
            "Red Hat Linux9.3": "rhel",
            "Red Hat Linux9.4": "rhel",
            "RHEL 8.6": "rhel",
        }
        for os_name, expected in cases.items():
            with self.subTest(os_name=os_name):
                self.assertEqual(resolve(os_name), expected)

    def test_other_broadened_families(self) -> None:
        cases = {
            "OracleLinux8.5": "oracle-linux",
            "AmazonLinux2": "amazon-linux",
            "RockyLinux9.2": "rocky-linux",
            "AlmaLinux9.1": "almalinux",
            "UbuntuLinux22.04": "ubuntu",
            "WindowsServer2019": "windows-server",
            "Windows10": "windows",
        }
        for os_name, expected in cases.items():
            with self.subTest(os_name=os_name):
                self.assertEqual(resolve(os_name), expected)

    def test_server_generation_year_routes_to_windows_server_even_without_the_word_server(self) -> None:
        """Real incident: real-world inventory strings routinely drop the
        word "Server" entirely (a common shorthand) -- "Windows 2008 R2
        Standard", "Win 2008 R2", "Windows 2008 - Standard" all used to
        resolve to the generic "windows" (client) product instead, which
        then has no release named "2008" at all (client versions are
        "7"/"8"/"10"/"11", never year-based) -- a silent, total miss. None
        of these server-generation years (2008/2011/2012/2016/2019/2022/
        2025) is ever a Windows CLIENT version, so a year like this
        alongside any "win"/"windows" mention unambiguously means Server."""
        cases = {
            "Windows 2008 R2 Standard": "windows-server",
            "Win 2008 R2": "windows-server",
            "Win 2008 R2 - STD": "windows-server",
            "Windows 2008 - Standard": "windows-server",
            "Windows 2012 R2 Datacenter": "windows-server",
            # Sanity: a genuine client version must not be swept up.
            "Windows 10": "windows",
            "Windows 11 24H2": "windows",
            # Sanity: the typo "Widows" (missing "n") is a real, unfixable
            # data-quality issue -- must not accidentally resolve to
            # anything via this override (it requires \bwin(dows)?\b).
            "Widows 2008 STD-64 Bit": None,
        }
        for os_name, expected in cases.items():
            with self.subTest(os_name=os_name):
                self.assertEqual(resolve(os_name), expected)

    def test_openshift_not_rhel(self) -> None:
        self.assertEqual(resolve("Red Hat OpenShift 4.12"), "red-hat-openshift")

    def test_rhel_minor_maps_to_major_release(self) -> None:
        releases = [{"name": "7"}, {"name": "8"}, {"name": "9"}]
        picked = pick_release(releases, extract_version_hints("Red Hat Linux 7.4"))
        self.assertEqual(picked.get("name"), "7")

    def test_panos_slug_and_release_trains(self) -> None:
        cases = {
            "Palo Alto Networks PAN-OS 10.2.13-h7": "panos",
            "Palo Alto Networks PAN-OS 11.1.4-h7": "panos",
            "Palo Alto Networks PAN-OS 11.2.10-h3": "panos",
            "Palo Alto Networks PAN-OS 11.1.13": "panos",
        }
        for os_name, expected_slug in cases.items():
            with self.subTest(os_name=os_name):
                self.assertEqual(resolve(os_name), expected_slug)

        releases = [{"name": "11.2"}, {"name": "11.1"}, {"name": "10.2"}]
        picked = pick_release(
            releases,
            extract_version_hints("Palo Alto Networks PAN-OS 11.2.10-h3"),
        )
        self.assertEqual(picked.get("name"), "11.2")
        picked_11_1 = pick_release(
            releases,
            extract_version_hints("Palo Alto Networks PAN-OS 11.1.13-h3"),
        )
        self.assertEqual(picked_11_1.get("name"), "11.1")

    def test_windows_build_number_matches_via_latest_name(self) -> None:
        """A raw NT build (e.g. from ``winver``/WMI) only appears in
        ``latest.name`` — the release ``name``/``label`` is a marketing slug
        (``11-26h1-e`` / "11 26H1 (E)") that never contains it."""
        releases = [
            {
                "name": "11-26h1-e",
                "label": "11 26H1 (E)",
                "latest": {"name": "10.0.28000"},
            },
            {
                "name": "11-24h2-e",
                "label": "11 24H2 (E)",
                "latest": {"name": "10.0.26100"},
            },
            {
                "name": "10-22h2",
                "label": "10 22H2",
                "latest": {"name": "10.0.19045"},
            },
        ]

        picked = pick_release(releases, extract_version_hints("Windows 10.0.28000"))
        self.assertEqual(picked.get("name"), "11-26h1-e")

        picked_10 = pick_release(releases, extract_version_hints("Windows 10.0.19045"))
        self.assertEqual(picked_10.get("name"), "10-22h2")

        # A bare major (no build) must still refuse to guess a specific release.
        self.assertEqual(pick_release(releases, extract_version_hints("Windows 10")), {})

    def test_coarse_normalized_query_alone_loses_the_build_number(self) -> None:
        """Pins the exact hint-starvation bug: once normalized_os is set to
        Windows' own family-level form ("Microsoft Windows 11", no build --
        by design, see build_normalization_from_product), a lookup querying
        with only that value has no build number left to disambiguate a
        specific release; lookup_os_eol must fold in hints from the raw
        os_string too (which still has "10.0.22631") rather than passing
        pick_release only extract_version_hints(cleaned_name)."""
        releases = [
            {"name": "11-23h2-e", "label": "11 23H2 (E)", "latest": {"name": "10.0.22631"}},
            {"name": "11-23h2-w", "label": "11 23H2 (W)", "latest": {"name": "10.0.22631"}},
        ]
        os_string = "Windows Microsoft Windows 11 Enterprise multi-session 10.0.22631 Build 22631"
        cleaned_name = "Microsoft Windows 11"  # what a coarse, already-set normalized_os looks like

        # The bug: hints from cleaned_name alone can't score high enough to
        # pick anything (correctly conservative on a bare major) --
        # release-level info is lost entirely, not just edition info.
        self.assertEqual(pick_release(releases, extract_version_hints(cleaned_name)), {})

        # The fix: hints merged from os_string restore the build number, and
        # os_text (already os_string-inclusive) still narrows to the correct
        # edition instead of falling back to "take the minimum of both".
        merged_hints = list(dict.fromkeys(extract_version_hints(os_string) + extract_version_hints(cleaned_name)))
        picked = pick_release(releases, merged_hints, os_text=f"{os_string} {cleaned_name}")
        self.assertEqual(picked.get("name"), "11-23h2-e")

    @staticmethod
    def _windows_24h2_shared_build_releases() -> list[dict[str, object]]:
        """Build 10.0.26100 shared by four Windows 11 24H2 editions/channels
        (IoT LTS, Enterprise LTS, Enterprise, consumer "W") with different
        support windows."""
        return [
            {
                "name": "11-24h2-iot-lts",
                "label": "11 24H2 IoT (LTS)",
                "isEoas": False,
                "eoasFrom": "2029-10-09",
                "isEol": False,
                "eolFrom": "2034-10-10",
                "latest": {"name": "10.0.26100"},
            },
            {
                "name": "11-24h2-e-lts",
                "label": "11 24H2 (E) (LTS)",
                "isEoas": False,
                "eoasFrom": "2029-10-09",
                "isEol": False,
                "eolFrom": "2029-10-09",
                "latest": {"name": "10.0.26100"},
            },
            {
                "name": "11-24h2-e",
                "label": "11 24H2 (E)",
                "isEoas": False,
                "eoasFrom": "2027-10-12",
                "isEol": False,
                "eolFrom": "2027-10-12",
                "latest": {"name": "10.0.26100"},
            },
            {
                "name": "11-24h2-w",
                "label": "11 24H2 (W)",
                "isEoas": False,
                "eoasFrom": "2026-10-13",
                "isEol": False,
                "eolFrom": "2026-10-13",
                "latest": {"name": "10.0.26100"},
            },
        ]

    def test_windows_shared_build_picks_earliest_eol_eoas(self) -> None:
        """Since the build alone can't tell the editions apart, and the OS
        string names no edition, the conservative choice is the earliest
        EOL/EOAS among all the tied releases."""
        releases = self._windows_24h2_shared_build_releases()

        picked = pick_release(releases, extract_version_hints("Windows 10.0.26100"))
        self.assertEqual(picked.get("name"), "11-24h2-w")
        self.assertEqual(picked.get("eolFrom"), "2026-10-13")
        self.assertEqual(picked.get("eoasFrom"), "2026-10-13")

    def test_windows_edition_hint_narrows_before_taking_minimum(self) -> None:
        """An OS string naming an edition (Enterprise/(E)/IoT) must narrow the
        tied candidates to that edition first -- only falling back to
        "take the minimum" among whichever of that edition remain tied."""
        releases = self._windows_24h2_shared_build_releases()
        hints = extract_version_hints("Windows 10.0.26100")

        # "Enterprise" matches both (E) and (E) (LTS) -- take the min of those two.
        enterprise = pick_release(releases, hints, os_text="Windows 11 Enterprise 10.0.26100")
        self.assertEqual(enterprise.get("name"), "11-24h2-e")
        self.assertEqual(enterprise.get("eolFrom"), "2027-10-12")

        # Literal "(E)" is equivalent to the word "Enterprise".
        literal_e = pick_release(releases, hints, os_text="Windows 11 (E) 10.0.26100")
        self.assertEqual(literal_e.get("name"), "11-24h2-e")

        # IoT is unambiguous on its own -- exactly one release matches.
        iot = pick_release(releases, hints, os_text="Windows 11 IoT 10.0.26100")
        self.assertEqual(iot.get("name"), "11-24h2-iot-lts")

        # IoT + Enterprise together (real Windows IoT Enterprise LTSC naming)
        # -- IoT is the more specific signal and wins.
        iot_enterprise = pick_release(
            releases, hints, os_text="Windows 11 IoT Enterprise LTSC 10.0.26100"
        )
        self.assertEqual(iot_enterprise.get("name"), "11-24h2-iot-lts")

    def test_ltsc_narrows_past_the_enterprise_lts_tie(self) -> None:
        """Real incident: os_string "Microsoft Windows 10 Enterprise LTSC
        10.0.17763 0" was resolving to "Microsoft Windows 10 1809 (E)" (the
        PLAIN Enterprise release, EOL 2021) instead of the LTSC one actually
        named in the string (EOL 2029). Root cause: "Enterprise" alone
        narrows to the "(e)" label substring, which matches BOTH "10 1809
        (E) (LTS)" and "10 1809 (E)" -- they stay tied, and the
        conservative "earliest EOL" merge silently picks the non-LTS one.
        LTSC/LTS must be checked as a MORE SPECIFIC edition hint than bare
        Enterprise (every LTS release's label is a strict superset, "...
        (E) (LTS)"), the same way IoT already outranks Enterprise."""
        releases = [
            {"name": "10-1809-e-lts", "label": "10 1809 (E) (LTS)",
             "eolFrom": "2029-01-09", "eoasFrom": "2024-01-09", "latest": {"name": "10.0.17763"}},
            {"name": "10-1809-e", "label": "10 1809 (E)",
             "eolFrom": "2021-05-11", "eoasFrom": "2021-05-11", "latest": {"name": "10.0.17763"}},
            {"name": "10-1809-w", "label": "10 1809 (W)",
             "eolFrom": "2020-11-10", "eoasFrom": "2020-11-10", "latest": {"name": "10.0.17763"}},
        ]
        os_string = "Microsoft Windows 10 Enterprise LTSC 10.0.17763 0"
        hints = extract_version_hints(os_string)
        picked = pick_release(releases, hints, os_text=os_string)
        self.assertEqual(picked.get("name"), "10-1809-e-lts")
        self.assertEqual(picked.get("eolFrom"), "2029-01-09")

    def test_plain_enterprise_without_ltsc_is_unaffected(self) -> None:
        """Sanity check: when LTSC/LTS ISN'T mentioned at all, behavior is
        exactly as before -- "Enterprise" alone still narrows to (both
        LTS and non-LTS releases share "(e)") and conservative-merges to
        the earliest EOL among them, same as always."""
        releases = [
            {"name": "10-1809-e-lts", "label": "10 1809 (E) (LTS)", "eolFrom": "2029-01-09", "latest": {"name": "10.0.17763"}},
            {"name": "10-1809-e", "label": "10 1809 (E)", "eolFrom": "2021-05-11", "latest": {"name": "10.0.17763"}},
        ]
        os_string = "Microsoft Windows 10 Enterprise 10.0.17763"
        picked = pick_release(releases, extract_version_hints(os_string), os_text=os_string)
        self.assertEqual(picked.get("name"), "10-1809-e")

    def test_windows_edition_hint_with_no_matching_label_falls_back(self) -> None:
        """An edition keyword that matches none of the tied releases must not
        eliminate the whole candidate set -- fall back to the full tie-break."""
        releases = [
            {"name": "a", "label": "A (W)", "eolFrom": "2026-01-01", "eoasFrom": "2026-01-01",
             "latest": {"name": "1.2.3"}},
            {"name": "b", "label": "B (W)", "eolFrom": "2027-01-01", "eoasFrom": "2027-01-01",
             "latest": {"name": "1.2.3"}},
        ]
        picked = pick_release(
            releases, extract_version_hints("1.2.3"), os_text="Enterprise 1.2.3"
        )
        self.assertEqual(picked.get("name"), "a")
        self.assertEqual(picked.get("eolFrom"), "2026-01-01")

    def test_tie_break_takes_min_across_mismatched_eol_eoas(self) -> None:
        """When the tied releases' earliest EOL and earliest EOAS come from
        different releases, each date is still the minimum across all ties
        (not just copied whole from a single "winning" release)."""
        releases = [
            {"name": "a", "label": "A", "eoasFrom": "2025-01-01", "eolFrom": "2028-01-01",
             "latest": {"name": "1.2.3"}},
            {"name": "b", "label": "B", "eoasFrom": "2025-06-01", "eolFrom": "2027-01-01",
             "latest": {"name": "1.2.3"}},
        ]
        picked = pick_release(releases, extract_version_hints("1.2.3"))
        self.assertEqual(picked.get("eoasFrom"), "2025-01-01")
        self.assertEqual(picked.get("eolFrom"), "2027-01-01")

    def test_windows_normalized_os_truncates_at_first_hyphen(self) -> None:
        """Windows' release.name ("10-22h2") packs the feature update onto
        the major version with a hyphen -- normalized_os keeps only the
        leading token ("10") for a short, family-level name, while
        normalized_os_detailed_name still uses the full release.label
        ("10 22H2"), unaffected."""
        product_result = {"label": "Microsoft Windows"}
        release = {"name": "10-22h2", "label": "10 22H2"}
        normalization = build_normalization_from_product(product_result, release)
        self.assertEqual(normalization["normalized_os"], "Microsoft Windows 10")
        self.assertEqual(normalization["normalized_os_detailed_name"], "Microsoft Windows 10 22H2")

    def test_clean_version_names_are_unaffected(self) -> None:
        """A product whose release.name is already a plain version (Ubuntu's
        "24.04") should keep using it for the short normalized_os form."""
        product_result = {"label": "Ubuntu"}
        release = {"name": "24.04", "label": "24.04 'Noble Numbat' (LTS)"}
        normalization = build_normalization_from_product(product_result, release)
        self.assertEqual(normalization["normalized_os"], "Ubuntu 24.04")
        self.assertEqual(
            normalization["normalized_os_detailed_name"],
            "Ubuntu 24.04 'Noble Numbat' (LTS)",
        )

    def test_join_labels_collapses_overlapping_phrase(self) -> None:
        """Windows Server's release label already embeds "Windows Server"
        (not just a whole-string prefix like AlmaLinux) -- concatenating
        naively would repeat the phrase."""
        self.assertEqual(
            join_labels("Microsoft Windows Server", "Windows Server 2019 (LTSC)"),
            "Microsoft Windows Server 2019 (LTSC)",
        )
        self.assertEqual(
            join_labels("Microsoft Windows Server", "Windows Server 23H2 AC"),
            "Microsoft Windows Server 23H2 AC",
        )
        # Single-word overlap (the original supported case) still works.
        self.assertEqual(
            join_labels("Apple macOS", "macOS 26 (Tahoe)"),
            "Apple macOS 26 (Tahoe)",
        )

    def test_join_labels_whole_string_prefix_unaffected(self) -> None:
        """Whole-string prefix containment (the original two fast paths)
        must keep working exactly as before."""
        self.assertEqual(join_labels("AlmaLinux OS", "AlmaLinux OS 9"), "AlmaLinux OS 9")
        self.assertEqual(
            join_labels("Red Hat Enterprise Linux 9", "Red Hat"),
            "Red Hat Enterprise Linux 9",
        )

    def test_join_labels_no_overlap_falls_back_to_concatenation(self) -> None:
        self.assertEqual(join_labels("Ubuntu", "24.04 'Noble Numbat' (LTS)"), "Ubuntu 24.04 'Noble Numbat' (LTS)")
        self.assertEqual(join_labels("Microsoft Windows", "11 26H1 (E)"), "Microsoft Windows 11 26H1 (E)")

    def test_windows_server_normalized_os_no_duplicate_phrase(self) -> None:
        product_result = {"label": "Microsoft Windows Server"}
        release = {"name": "23h2-ac", "label": "Windows Server 23H2 AC"}
        normalization = build_normalization_from_product(product_result, release)
        self.assertEqual(normalization["normalized_os"], "Microsoft Windows Server 23H2")
        self.assertEqual(
            normalization["normalized_os_detailed_name"], "Microsoft Windows Server 23H2 AC"
        )
        self.assertNotIn("Server Windows Server", normalization["normalized_os_detailed_name"])

    def test_windows_embedded_normalized_os_keeps_leading_name_word(self) -> None:
        """Real report: release.name "standard-7-sp1" truncated at its
        first '-' gave the bare word "standard" (no version at all) --
        operating on release.label and stopping after the first digit-
        bearing token keeps the leading name word "Standard" AND the
        version "7", correctly dropping only the trailing "SP1"."""
        product_result = {"label": "Microsoft Windows Embedded"}
        release = {"name": "standard-7-sp1", "label": "Standard 7 SP1"}
        normalization = build_normalization_from_product(product_result, release)
        self.assertEqual(normalization["normalized_os"], "Microsoft Windows Embedded Standard 7")
        self.assertEqual(
            normalization["normalized_os_detailed_name"],
            "Microsoft Windows Embedded Standard 7 SP1",
        )

    def test_windows_server_normalized_os_truncates_r2_suffix(self) -> None:
        """The worked example this behavior was added for: "2012-r2" must
        become the short "2012", not the full "2012 R2 (LTSC)" label."""
        product_result = {"label": "Microsoft Windows Server"}
        release = {"name": "2012-r2", "label": "Windows Server 2012 R2 (LTSC)"}
        normalization = build_normalization_from_product(product_result, release)
        self.assertEqual(normalization["normalized_os"], "Microsoft Windows Server 2012")
        self.assertEqual(
            normalization["normalized_os_detailed_name"],
            "Microsoft Windows Server 2012 R2 (LTSC)",
        )


class GenericLinuxKernelRequiresTheWordKernelTests(unittest.TestCase):
    """Real, reported incident: "Linux 6.4.7.3762 7" resolved to
    endoflife.date's "linux" product (label "Linux Kernel") and adopted
    that specific kernel release's own EOL date -- even though nothing in
    the os_string ever said "kernel". endoflife.date's "linux" product
    tracks the Linux KERNEL project's own release schedule specifically,
    not any particular distribution -- but its slug AND label are both
    just the single, universally generic word "linux"/"Linux Kernel", so
    the phrase index matched it purely because that one common word
    happened to be present, the same way a distro whose real name never
    got recognized (or a vague inventory-tool placeholder) would also
    read. A bare "Linux <version>" is nowhere near as safe an assumption
    as "the reporter explicitly means the kernel project's own tracking
    page." Resolution now requires the word "kernel" (in any of its
    real-world glued/hyphenated/spaced forms -- matching endoflife.date's
    own recognized alias "linuxkernel") to actually appear before trusting
    this one product; a bare "Linux <version>" with no such word refuses
    instead of guessing, and falls through to the vendor cascade instead."""

    def test_bare_linux_with_no_kernel_word_refuses(self) -> None:
        self.assertIsNone(resolve("Linux 6.4.7.3762 7"))
        self.assertIsNone(resolve("Linux 6.4.7"))

    def test_the_word_kernel_in_any_form_is_trusted(self) -> None:
        for os_name in (
            "Linux kernel 6.4.7",
            "Linux Kernel 6.4.7",
            "Linux-kernel 6.4.7",
            "Linuxkernel 6.4.7",
        ):
            with self.subTest(os_name=os_name):
                self.assertEqual(resolve(os_name), "linux")

    def test_other_linux_distros_are_unaffected(self) -> None:
        """Sanity check: the guard is scoped to the "linux" slug alone --
        a real distro name resolving to ITS OWN, specific product must
        never be blocked just because the word "linux" also appears
        somewhere in the string."""
        self.assertEqual(resolve("Ubuntu Linux 22.04"), "ubuntu")
        self.assertEqual(resolve("Red Hat Linux 7.4"), "rhel")


class BitnessMarkerContextTests(unittest.TestCase):
    """Real incident: Android reached major version 16 ('Baklava', 2025), but
    16/32/64/86/128/256 were unconditionally treated as architecture/bitness
    noise, so a bare "Android 16" query could never extract "16" as a hint at
    all and permanently failed to match. The exclusion must only apply when
    the surrounding text actually reads as a bitness marker."""

    def test_bare_major_matching_a_bitness_number_is_kept(self) -> None:
        self.assertEqual(extract_version_hints("Android 16"), ["16"])
        self.assertEqual(extract_version_hints("Fedora 32"), ["32"])

    def test_genuine_bitness_markers_are_still_excluded(self) -> None:
        self.assertEqual(extract_version_hints("Windows 7 (32-bit)"), ["7"])
        self.assertEqual(extract_version_hints("Windows 7 (64-bit)"), ["7"])
        self.assertEqual(
            extract_version_hints("Windows Server 2016 Standard 64 bit Edition Version 1607"),
            ["2016", "1607"],
        )


class PickReleaseRefusesMixedIndependentHintsTests(unittest.TestCase):
    """Real incident: "Android 14-11" extracts two independent hints ("14"
    and "11"), each of which alone exactly matches a DIFFERENT, unrelated
    Android release -- release "14" via hint "14" alone, release "11" via
    hint "11" alone. The old tie-break couldn't tell this apart from the
    Windows 24H2 case (where every tied edition needs the SAME shared
    "11"+"24" pair) and silently picked whichever tied release had the
    earliest EOL date -- "Android 11" -- as if that were a confirmed match.
    A tie must only be conservative-merged when every tied release is
    explained by a hint (or hint-set) shared across all of them."""

    def test_two_independently_matched_releases_refuse_rather_than_guess(self) -> None:
        releases = [
            {"name": "14", "label": "14 'Upside Down Cake'", "eolFrom": "2024-06-10"},
            {"name": "11", "label": "11 'Red Velvet Cake'", "eolFrom": "2021-09-08"},
        ]
        hints = extract_version_hints("Android 14-11")
        self.assertEqual(sorted(hints), ["11", "14"])
        self.assertEqual(pick_release(releases, hints, os_text="Android 14-11"), {})

    def test_shared_hint_tie_still_conservative_merges(self) -> None:
        """Sanity check that the fix above doesn't over-refuse: a tie where
        every candidate genuinely shares the same explaining hint(s) (the
        Windows 24H2 shape) must still resolve via the existing "earliest
        date" conservative merge."""
        releases = [
            {"name": "11-24h2-e", "label": "11 24H2 (E)", "latest": {"name": "10.0.26100"}, "eolFrom": "2027-10-12"},
            {"name": "11-24h2-w", "label": "11 24H2 (W)", "latest": {"name": "10.0.26100"}, "eolFrom": "2026-10-13"},
        ]
        hints = extract_version_hints("Microsoft Windows 11 24H2")
        picked = pick_release(releases, hints, os_text="Microsoft Windows 11 24H2")
        self.assertEqual(picked.get("name"), "11-24h2-w")


class PickReleaseTieRequiresExactScoreTests(unittest.TestCase):
    """Real incident: os_string "Windows 10.0" (no build number at all) was
    resolving to "Microsoft Windows 10 1507" as a confirmed match. Root
    cause: "10.0" is a genuine numeric *prefix* of EVERY Windows 10/11 build
    (they all start "10.0."), so every release in the catalog ties at the
    SAME 90-point (prefix, not exact) score -- and the existing shared-hint
    tie-break considered this safe to conservative-merge, since every tied
    release genuinely IS explained by the same hint. But "explained by the
    same hint" isn't the same as "confirmed" when that hint is coarser than
    every tied release's own version: a tie must only conservative-merge
    when every tied release was matched via an EXACT signal (100) -- the
    literal same build/name, or the compound-token rule's full match --
    never merely the weaker 90-point prefix score."""

    def test_multiple_releases_tied_only_via_prefix_score_refuse(self) -> None:
        releases = [
            {"name": "10-1507", "latest": {"name": "10.0.10240"}, "eolFrom": "2017-05-09"},
            {"name": "10-1607", "latest": {"name": "10.0.14393"}, "eolFrom": "2018-10-09"},
        ]
        self.assertEqual(pick_release(releases, ["10.0"], os_text="Windows 10.0"), {})

    def test_single_non_tied_prefix_match_is_unaffected(self) -> None:
        """The guard only applies to a multi-candidate tie -- a UNIQUE
        90-scoring match (nothing else in the catalog ties with it) is
        unaffected, e.g. RHEL's own coarse-hint-vs-release-family shape."""
        releases = [{"name": "7"}, {"name": "8"}, {"name": "9"}]
        picked = pick_release(releases, extract_version_hints("Red Hat Linux 7.4"))
        self.assertEqual(picked.get("name"), "7")

    def test_exact_score_ties_still_conservative_merge(self) -> None:
        """Sanity check the guard doesn't over-refuse: a tie where every
        candidate scores a genuine 100 (compound-token full match) must
        still resolve via the existing earliest-EOL conservative merge."""
        releases = [
            {"name": "11-24h2-e", "label": "11 24H2 (E)", "latest": {"name": "10.0.26100"}, "eolFrom": "2027-10-12"},
            {"name": "11-24h2-w", "label": "11 24H2 (W)", "latest": {"name": "10.0.26100"}, "eolFrom": "2026-10-13"},
        ]
        hints = extract_version_hints("Microsoft Windows 11 24H2")
        picked = pick_release(releases, hints, os_text="Microsoft Windows 11 24H2")
        self.assertEqual(picked.get("name"), "11-24h2-w")


class PickReleaseDominantEvidenceTests(unittest.TestCase):
    """Real incident: os_string "Microsoft Windows Server 2019 Datacenter
    10.0.17763 0" was resolving to "Windows Server 1809 SAC" instead of
    "Windows Server 2019 (LTSC)" -- both share build 10.0.17763 (Server
    2019 LTSC and the 1809 Semi-Annual-Channel release happen to be the
    same underlying build), so they tie. The old shared-hint check only
    asked "is there SOME hint in common" (yes: "10.0.17763") and
    conservative-merged to whichever has the earliest EOL -- 1809-SAC's much
    shorter 18-month servicing window -- discarding the fact that the query
    ALSO explicitly said "2019", a hint only the 2019 release's own name
    matches at all. A tied candidate confirmed by strictly MORE evidence
    than every other tied candidate wins outright instead of being averaged
    with weaker-evidence candidates."""

    def test_strictly_more_evidence_wins_outright(self) -> None:
        releases = [
            {"name": "1809-sac", "label": "Windows Server 1809 SAC",
             "eolFrom": "2020-11-10", "latest": {"name": "10.0.17763"}},
            {"name": "2019", "label": "Windows Server 2019 (LTSC)",
             "eolFrom": "2029-01-09", "latest": {"name": "10.0.17763"}},
        ]
        os_string = "Microsoft Windows Server 2019 Datacenter 10.0.17763 0"
        cleaned_name = "Microsoft Windows Server 2019"
        hints = list(dict.fromkeys(extract_version_hints(os_string) + extract_version_hints(cleaned_name)))
        picked = pick_release(releases, hints, os_text=f"{os_string} {cleaned_name}")
        self.assertEqual(picked.get("name"), "2019")

    def test_three_way_tie_with_a_dominator_still_resolves_to_it(self) -> None:
        releases = [
            {"name": "1809-sac", "eolFrom": "2020-11-10", "latest": {"name": "10.0.17763"}},
            {"name": "1903-sac", "eolFrom": "2020-12-08", "latest": {"name": "10.0.17763"}},
            {"name": "2019", "eolFrom": "2029-01-09", "latest": {"name": "10.0.17763"}},
        ]
        picked = pick_release(releases, ["2019", "10.0.17763"], os_text="Windows Server 2019 10.0.17763")
        self.assertEqual(picked.get("name"), "2019")

    def test_identical_required_sets_have_no_dominator_and_still_merge(self) -> None:
        """Sanity check the guard doesn't over-fire: when every tied
        candidate is explained by the exact SAME evidence (no one has
        anything extra), none dominates -- falls through to the existing
        earliest-EOL conservative merge, unaffected."""
        releases = [
            {"name": "a-edition", "eolFrom": "2026-01-01", "latest": {"name": "1.2.3"}},
            {"name": "b-edition", "eolFrom": "2027-01-01", "latest": {"name": "1.2.3"}},
        ]
        picked = pick_release(releases, ["1.2.3"], os_text="a-edition b-edition 1.2.3")
        self.assertEqual(picked.get("name"), "a-edition")

    def test_disjoint_required_sets_still_refuse_before_dominance_is_even_checked(self) -> None:
        """Sanity check: two candidates with NOTHING in common (the
        Android 14-11 shape) must still refuse at the empty-intersection
        check -- dominance is only ever considered among candidates that
        already share at least one hint."""
        releases = [
            {"name": "14", "eolFrom": "2024-06-10"},
            {"name": "11", "eolFrom": "2021-09-08"},
        ]
        self.assertEqual(pick_release(releases, ["14", "11"], os_text="Android 14-11"), {})


class DottedHintOutranksCoincidentalBareMatchTests(unittest.TestCase):
    """Real incident: products whose entire catalog is bare, major-version-
    only release names (RHEL: "4".."10", CentOS: "5".."8", iOS: "5".."26")
    can never have a release that EXACTLY matches a dotted hint like "6.6"
    -- it only ever reaches the release's own bare major number via the
    weaker 90-point prefix score. Meanwhile a totally unrelated standalone
    bare number floating in the query (a kernel-version fragment, a space-
    separated point-release digit, ...) can coincidentally EXACT-match some
    OTHER release's own bare name with a full 100 -- outright outscoring the
    correct match, not even a tie. "RHEL 6.6 3 8" (kernel 3.8, space-
    separated) resolved to release "8" (bare "8" hint exact-matching it)
    instead of release "6" (the genuine "6.6" hint, scored only 90) --
    same shape broke "CentOS 7.9 5 4" and "iOS 16.7 10" (a real iOS
    16.7.10 point release, space- instead of dot-separated) too."""

    def test_rhel_kernel_version_fragment_does_not_steal_the_win(self) -> None:
        releases = [{"name": n} for n in ["10", "9", "8", "7", "6", "5", "4"]]
        hints = extract_version_hints("RHEL 6.6 3 8")
        self.assertEqual(sorted(hints), sorted(["6.6", "3", "8"]))
        picked = pick_release(releases, hints, os_text="RHEL 6.6 3 8")
        self.assertEqual(picked.get("name"), "6")

    def test_centos_kernel_version_fragment_does_not_steal_the_win(self) -> None:
        releases = [{"name": n} for n in ["8", "7", "6", "5"]]
        picked = pick_release(releases, ["7.9", "5", "4"], os_text="CentOS 7.9 5 4")
        self.assertEqual(picked.get("name"), "7")

    def test_ios_point_release_digit_does_not_steal_the_win(self) -> None:
        releases = [{"name": n} for n in ["18", "17", "16", "15", "14", "13", "12", "11", "10"]]
        picked = pick_release(releases, ["16.7", "10"], os_text="iOS 16.7 10")
        self.assertEqual(picked.get("name"), "16")

    def test_all_bare_hints_are_unaffected(self) -> None:
        """Sanity check: when there's no dotted hint at all (every hint is
        bare), this new check must never engage -- e.g. the Windows 24H2
        compound-token case, which resolves entirely via bare hints."""
        releases = [
            {"name": "11-24h2-e", "label": "11 24H2 (E)", "latest": {"name": "10.0.26100"}, "eolFrom": "2027-10-12"},
            {"name": "11-24h2-w", "label": "11 24H2 (W)", "latest": {"name": "10.0.26100"}, "eolFrom": "2026-10-13"},
        ]
        hints = extract_version_hints("Microsoft Windows 11 24H2")
        self.assertEqual(hints, ["11", "24"])
        picked = pick_release(releases, hints, os_text="Microsoft Windows 11 24H2")
        self.assertEqual(picked.get("name"), "11-24h2-w")

    def test_a_single_dotted_hint_with_no_bare_hints_is_unaffected(self) -> None:
        releases = [{"name": "7"}, {"name": "8"}, {"name": "9"}]
        picked = pick_release(releases, extract_version_hints("Red Hat Linux 7.4"))
        self.assertEqual(picked.get("name"), "7")

    def test_combined_build_number_hint_is_unaffected(self) -> None:
        """Sanity check: when the dotted hint set already includes the
        combined build number (e.g. "10.0.14393" from the "Windows 10.0
        (14393)" fix), the dotted-only pass agrees with the full-hint-set
        pass, so nothing changes."""
        releases = [
            {"name": "10-1507", "latest": {"name": "10.0.10240"}, "eolFrom": "2017-05-09"},
            {"name": "10-1607", "latest": {"name": "10.0.14393"}, "eolFrom": "2018-10-09"},
        ]
        os_string = "Windows 10.0 (14393)"
        picked = pick_release(releases, extract_version_hints(os_string), os_text=os_string)
        self.assertEqual(picked.get("name"), "10-1607")

    def test_coarse_family_wide_dotted_hint_does_not_override_a_unique_win(self) -> None:
        """Real regression, found while verifying the fix above against the
        live catalog: "WindowsServer2016 10.0" already resolves uniquely and
        correctly on the FULL hint set alone -- release "2016"'s own name
        token "2016" is one of the hints, a compound-token full match (100).
        But "10.0" (also a hint here) is a genuine numeric prefix of EVERY
        modern Windows Server release's build number, so scoring with ONLY
        the dotted hints ties roughly a dozen releases at 90 -- the dotted-
        only pass here is not "more specific," it's *less* specific than the
        full hint set. Without requiring the dotted-only pass to itself be
        unique, this coarse tie unconditionally clobbered the correct unique
        answer, and the resulting tie then failed the exact-score
        requirement -- silently turning a clean match into no match at all."""
        releases = [
            {"name": "2016", "label": "Windows Server 2016 (LTSC)", "eolFrom": "2027-01-12", "latest": {"name": "10.0.14393"}},
            {"name": "2019", "label": "Windows Server 2019 (LTSC)", "eolFrom": "2029-01-09", "latest": {"name": "10.0.17763"}},
            {"name": "2022", "label": "Windows Server 2022 (LTSC)", "eolFrom": "2031-10-14", "latest": {"name": "10.0.20348"}},
            {"name": "2025", "label": "Windows Server 2025 (LTSC)", "eolFrom": "2034-11-14", "latest": {"name": "10.0.26100"}},
        ]
        os_string = "WindowsServer2016 10.0"
        hints = extract_version_hints(os_string)
        self.assertEqual(hints, ["2016", "10.0"])
        picked = pick_release(releases, hints, os_text=os_string)
        self.assertEqual(picked.get("name"), "2016")


class GluedWordDigitTruncationTests(unittest.TestCase):
    """Real incident: a bulk-reported inventory string "Microsoft
    WindowsServer2008R2 Standard" (vendor tooling glued the words and
    version together, no spaces at all) extracted the hint "008" instead of
    "2008" -- a genuine, matchable major-version digit run got truncated
    down to garbage that could never match anything. Root cause: the
    digit-run regex's negative lookbehind was `(?<![A-Za-z])`, which was
    meant to protect compound tags like "24H2" (so "4" alone, from "24H2",
    is never extracted as its own hint) but was written too broadly -- it
    also excluded a genuine version number merely because SOME letter
    immediately preceded it, e.g. the "r" in "Server2008". Narrowed to a
    2-character lookbehind `(?<![0-9][A-Za-z])`, which only excludes a
    digit run preceded by exactly [digit][single-letter] -- the true
    compound-tag shape ("2" + "4H2" -> exclude the "4H2" run's leading
    digit) -- while still extracting a full version number glued directly
    to a preceding word ("Server" + "2008")."""

    def test_glued_server_year_extracts_in_full(self) -> None:
        self.assertEqual(extract_version_hints("WindowsServer2008R2"), ["2008"])
        self.assertEqual(extract_version_hints("Microsoft WindowsServer2012 Standard"), ["2012"])

    def test_compound_tag_truncation_is_still_protected(self) -> None:
        """Sanity check the narrower lookbehind doesn't over-widen: "24H2"
        must still yield only "24" (not also a spurious "4")."""
        self.assertEqual(extract_version_hints("Microsoft Windows 11 24H2"), ["11", "24"])


class CompoundSlugReleaseNameMatchingTests(unittest.TestCase):
    """Real incident: Windows Server's own endoflife.date release *names*
    are compound slugs -- "2008-sp2", "2008-r2-sp1", "2012-r2" -- not the
    bare year alone. `_release_name_tokens("2008-r2-sp1")` used to yield
    `["2008", "2", "1"]` (the "2" from "r2" and the "1" from "sp1" both
    read as version tokens), and the compound-token full-match rule
    required EVERY token to appear in the query's hints -- so unless the
    query happened to also contain a coincidental "2" and "1", the release
    could never score a full match at all and the whole product fell
    through to the eosl.date fallback. Fixed two ways: (1)
    `_release_name_tokens` now excludes SP/R/Pack marker digits the same
    way `extract_version_hints` already did (so "2008-r2-sp1" yields only
    `["2008"]`), and (2) the compound-token rule was relaxed from requiring
    more than one token to just requiring at least one -- a single
    genuine token is still a full, unambiguous match. Both `_release_score`
    AND its separate, un-synced twin `_release_required_hints` needed the
    same relaxation -- the first fix attempt only touched `_release_score`,
    which correctly raised the score to 100 but left the shared-hint
    tie-break using an empty required-set from the still-unfixed
    `_release_required_hints`, so `pick_release` refused anyway."""

    _RELEASES = [
        {"name": "2008-sp2", "label": "Windows Server 2008 SP2",
         "eolFrom": "2020-01-14", "latest": {"name": "6.0.6003"}},
        {"name": "2008-r2-sp1", "label": "Windows Server 2008 R2 SP1",
         "eolFrom": "2020-01-14", "latest": {"name": "6.1.7601"}},
        {"name": "2012", "label": "Windows Server 2012",
         "eolFrom": "2023-10-10", "latest": {"name": "6.2.9200"}},
        {"name": "2012-r2", "label": "Windows Server 2012 R2",
         "eolFrom": "2023-10-10", "latest": {"name": "6.3.9600"}},
    ]

    def test_bare_year_resolves_to_the_non_r2_compound_slug(self) -> None:
        os_string = "Windows Server 2008 Standard"
        picked = pick_release(self._RELEASES, extract_version_hints(os_string), os_text=os_string)
        self.assertEqual(picked.get("name"), "2008-sp2")

    def test_year_plus_r2_resolves_to_the_r2_compound_slug(self) -> None:
        os_string = "Microsoft Windows Server 2008 R2 Standard 7600"
        picked = pick_release(self._RELEASES, extract_version_hints(os_string), os_text=os_string)
        self.assertEqual(picked.get("name"), "2008-r2-sp1")

    def test_2012_r2_is_not_confused_with_plain_2012(self) -> None:
        os_string = "Windows Server 2012 R2 Standard"
        picked = pick_release(self._RELEASES, extract_version_hints(os_string), os_text=os_string)
        self.assertEqual(picked.get("name"), "2012-r2")


class WindowsServerR2EditionHintTests(unittest.TestCase):
    """Companion to the compound-slug fix above: "R2" was added to
    `_EDITION_LABEL_HINTS` so a query naming it narrows a same-build tie to
    the R2-labeled release specifically, rather than relying on the
    compound-slug relaxation alone. It must be checked BEFORE "Enterprise"
    in the hint list -- `_edition_label_substring` returns only the FIRST
    matching pattern, and "Windows Server 2008 R2 Enterprise 7600" contains
    both words, so if Enterprise were checked first it would win and R2
    would never get a chance to narrow anything."""

    def test_r2_and_enterprise_together_still_narrows_via_r2(self) -> None:
        releases = [
            {"name": "2008-sp2", "label": "Windows Server 2008 SP2",
             "eolFrom": "2020-01-14", "latest": {"name": "6.0.6003"}},
            {"name": "2008-r2-sp1", "label": "Windows Server 2008 R2 SP1",
             "eolFrom": "2020-01-14", "latest": {"name": "6.1.7601"}},
        ]
        os_string = "Windows Server 2008 R2 Enterprise 7600"
        picked = pick_release(releases, extract_version_hints(os_string), os_text=os_string)
        self.assertEqual(picked.get("name"), "2008-r2-sp1")

    def test_r2_glued_directly_to_the_preceding_year_still_narrows(self) -> None:
        """Real incident: "WindowsServer2012R2 9600" (a glued-word inventory
        string, same shape as the "WindowsServer2008R2" digit-truncation
        bug) has "R2" immediately preceded by the digit "2" -- \\b never
        fires between two word characters, so the old `\\br2\\b` pattern
        silently never matched, and edition narrowing never ran at all.
        Without it, "2012" (a clean exact-match on its own bare name) and
        "2012-r2" (confirmed via the build-suffix-match rule alone) tied
        with neither dominating the other -- resolving only by an
        accidental EOL-date coincidence in the real catalog (both "2012"
        and "2012-r2" happen to share the same eolFrom), not genuine
        confirmation. The new pattern recognizes "R2" as an edition marker
        even glued directly after a digit."""
        releases = [
            {"name": "2012", "label": "Windows Server 2012",
             "eolFrom": "2023-10-10", "latest": {"name": "6.2.9200"}},
            {"name": "2012-r2", "label": "Windows Server 2012 R2",
             "eolFrom": "2023-10-10", "latest": {"name": "6.3.9600"}},
        ]
        os_string = "WindowsServer2012R2 9600"
        picked = pick_release(releases, extract_version_hints(os_string), os_text=os_string)
        self.assertEqual(picked.get("name"), "2012-r2")

    def test_glued_r2_does_not_match_inside_an_unrelated_word(self) -> None:
        """Sanity check: the new pattern must not fire mid-word -- only
        when "r2" isn't immediately preceded by another letter and isn't
        immediately followed by another letter or digit."""
        self.assertIsNone(_edition_label_substring("R2D2"))
        self.assertIsNone(_edition_label_substring("SuperR2000"))


class BuildNumberSuffixMatchTests(unittest.TestCase):
    """Real incident: "Windows Server 2019 Datacenter AD Version 1809 Build
    17763" (and several sibling Datacenter/Standard, 64-bit-Edition, AD
    variants) refused to resolve at all. Hints: ["2019", "1809", "17763"].
    Release "2019" and release "1809-sac" share the exact same build
    (10.0.17763), and each one's own name is independently one of the
    query's hints -- "2019" scores 100 via its own compound-token name
    match, "1809-sac" scores 100 the same way via "1809" -- a genuine tie.
    But "17763" (the trailing, most memorable segment of the shared build
    number, with no adjacent "10.0" in the text for the existing
    parenthesized/trailing-build-number combining pass to stitch it onto)
    used to match NEITHER release at all -- score_release_against_hint only
    tests numeric PREFIX matches, never a suffix, so a bare "17763" could
    never confirm "10.0.17763". Without any hint recognized as common to
    both tied releases, the shared-hint tie-break saw an empty intersection
    and refused outright, before dominant-evidence (which would have
    correctly preferred "2019" for carrying the extra, more specific
    "2019" hint) ever got a chance to run. Recognizing a bare 4+-digit hint
    against a release's own trailing build segment restores that missing
    common ground."""

    _RELEASES = [
        {"name": "1809-sac", "label": "Windows Server 1809 SAC",
         "eolFrom": "2020-11-10", "latest": {"name": "10.0.17763"}},
        {"name": "2019", "label": "Windows Server 2019 (LTSC)",
         "eolFrom": "2029-01-09", "latest": {"name": "10.0.17763"}},
    ]

    def test_trailing_build_number_supplies_the_missing_shared_hint(self) -> None:
        os_string = "Windows Server 2019 Datacenter AD Version 1809 Build 17763"
        hints = extract_version_hints(os_string)
        self.assertEqual(hints, ["2019", "1809", "17763"])
        picked = pick_release(self._RELEASES, hints, os_text=os_string)
        self.assertEqual(picked.get("name"), "2019")

    def test_a_short_number_never_qualifies_as_a_build_suffix(self) -> None:
        """Sanity check: the 4+-digit floor means a short, low-entropy
        number never gets treated as if it identified a specific build --
        with no genuine shared/dominant evidence at all, this must still
        refuse (same shape as "Android 14-11")."""
        releases = [
            {"name": "8", "eolFrom": "2024-01-01"},
            {"name": "9", "eolFrom": "2025-01-01"},
        ]
        self.assertEqual(pick_release(releases, ["8", "9"], os_text="8 9"), {})

    def test_unrelated_products_are_unaffected(self) -> None:
        """Sanity check the new rule doesn't fire when there's no genuine
        multi-part release to match a bare hint's tail against at all."""
        releases = [{"name": "7"}, {"name": "8"}, {"name": "9"}]
        picked = pick_release(releases, extract_version_hints("Red Hat Linux 7.4"))
        self.assertEqual(picked.get("name"), "7")


class RequiredHintsUnionAndStrongEvidenceDominanceTests(unittest.TestCase):
    """Real incident, found immediately after the build-suffix-match fix
    above: "WindowsServer2008R2 7601" (and "WindowsServer2012R2 9600")
    still refused. Hints ["2008", "7601"] tie "2008-r2-sp1" (compound-token
    match on "2008", PLUS a build-suffix match on "7601" against its own
    build 6.1.7601) against "2008-sp2" (compound-token match on "2008"
    only -- its own build 6.0.6003 doesn't end in "7601" at all). Before
    this fix, `_release_required_hints` treated "reaches the score via a
    single hint alone" and "reaches it via the compound-token rule" as
    MUTUALLY EXCLUSIVE alternatives -- whichever came first won, the other
    was never even computed. Since "2008-r2-sp1" reached the score via the
    single-hint build-suffix match on "7601" alone, its required set was
    reported as just {"7601"} -- silently DROPPING the "2008" it was ALSO
    genuinely confirmed by -- leaving it with NOTHING in common with
    "2008-sp2"'s {"2008"}, an empty intersection, refusing a release with
    objectively MORE evidence than its tied sibling.

    Fixed by taking the UNION of both mechanisms in
    `_release_required_hints` (used for the shared-hint / empty-
    intersection check). But a blanket union alone reopened the "Windows
    Server 2019 vs 1809-sac, Build 17763" case from the class above:
    "1809-sac" would then ALSO gain "1809" (its own compound-token match),
    making it exactly as "evidenced" as "2019" (which gains its own "2019"
    via an ordinary EXACT match, not compound-token) -- neither would
    dominate, silently falling back to conservative-merging on 1809-sac's
    much-shorter EOL window. The dominant-evidence check itself was
    changed to compare `_release_strong_hints` (ordinary exact/prefix/
    suffix matches ONLY, excluding the weaker, name-only compound-token
    rule) instead of the full unioned set -- "1809-sac"'s strong evidence
    stays just {"17763"} (its "1809" match is compound-token-only, so it's
    excluded here), while "2019"'s strong evidence is {"2019", "17763"} (a
    genuine ordinary exact match on "2019", not compound-token) -- a true
    strict superset, correctly dominant. Both scenarios must resolve
    correctly at once."""

    def test_a_release_with_extra_build_suffix_evidence_dominates(self) -> None:
        releases = [
            {"name": "2008-sp2", "label": "Windows Server 2008 SP2",
             "eolFrom": "2020-01-14", "latest": {"name": "6.0.6003"}},
            {"name": "2008-r2-sp1", "label": "Windows Server 2008 R2 SP1",
             "eolFrom": "2020-01-14", "latest": {"name": "6.1.7601"}},
        ]
        os_string = "WindowsServer2008R2 7601"
        hints = extract_version_hints(os_string)
        self.assertEqual(sorted(hints), ["2008", "7601"])
        picked = pick_release(releases, hints, os_text=os_string)
        self.assertEqual(picked.get("name"), "2008-r2-sp1")

    def test_a_second_generation_with_extra_build_suffix_evidence_dominates(self) -> None:
        releases = [
            {"name": "2012", "label": "Windows Server 2012",
             "eolFrom": "2023-10-10", "latest": {"name": "6.2.9200"}},
            {"name": "2012-r2", "label": "Windows Server 2012 R2",
             "eolFrom": "2023-10-10", "latest": {"name": "6.3.9600"}},
        ]
        os_string = "WindowsServer2012R2 9600"
        hints = extract_version_hints(os_string)
        picked = pick_release(releases, hints, os_text=os_string)
        self.assertEqual(picked.get("name"), "2012-r2")

    def test_symmetric_compound_token_evidence_still_prefers_the_ordinary_exact_match(self) -> None:
        """Sanity check the two fixes coexist: the "2019 vs 1809-sac,
        Build 17763" case (class above) must still resolve to "2019", not
        regress to conservative-merging on "1809-sac" now that both
        releases' required sets are unioned."""
        releases = [
            {"name": "1809-sac", "label": "Windows Server 1809 SAC",
             "eolFrom": "2020-11-10", "latest": {"name": "10.0.17763"}},
            {"name": "2019", "label": "Windows Server 2019 (LTSC)",
             "eolFrom": "2029-01-09", "latest": {"name": "10.0.17763"}},
        ]
        os_string = "Windows Server 2019 Datacenter AD Version 1809 Build 17763"
        picked = pick_release(releases, extract_version_hints(os_string), os_text=os_string)
        self.assertEqual(picked.get("name"), "2019")


class SharedBuildBypassesEmptyIntersectionRefusalTests(unittest.TestCase):
    """Real incident: "Microsoft Hyper-V Windows Server 2019  Version 1809"
    (no build number at all) ties "2019" (LTSC) against "1809-sac" (Semi-
    Annual Channel) -- both share build 10.0.17763, but with no
    "17763"/"10.0.17763" hint anywhere in the query to act as common
    ground, "2019"'s required set is just {"2019"} and "1809-sac"'s is
    just {"1809"} -- an empty intersection, indistinguishable BY HINT ALONE
    from "Android 14-11" (two genuinely different releases each
    independently matched). But the catalog itself already proves these
    are the same underlying release under two different names -- Windows
    Server 2019 IS internally versioned "1809" (Microsoft's own docs call
    it "Windows Server 2019, Version 1809"); "1809 SAC" is a separate
    product that merely collides on that number. When every tied candidate
    shares the exact same `latest.name` (build), that structural fact --
    independent of which hints the query happens to contain -- is enough to
    proceed past the empty-intersection refusal and let the dominant-
    evidence check (which still requires "2019"'s ordinary exact-match
    evidence to strictly exceed "1809-sac"'s) decide the winner."""

    _SHARED_BUILD_RELEASES = [
        {"name": "1809-sac", "label": "Windows Server 1809 SAC",
         "eolFrom": "2020-11-10", "latest": {"name": "10.0.17763"}},
        {"name": "2019", "label": "Windows Server 2019 (LTSC)",
         "eolFrom": "2029-01-09", "latest": {"name": "10.0.17763"}},
    ]

    def test_no_build_number_at_all_still_resolves_via_the_shared_build(self) -> None:
        os_string = "Microsoft Hyper-V Windows Server 2019  Version 1809"
        hints = extract_version_hints(os_string)
        self.assertEqual(hints, ["2019", "1809"])
        picked = pick_release(self._SHARED_BUILD_RELEASES, hints, os_text=os_string)
        self.assertEqual(picked.get("name"), "2019")

    def test_genuinely_different_builds_still_refuse(self) -> None:
        """Sanity check: the bypass only applies when every tied candidate
        shares the SAME build -- a query genuinely naming two different
        Windows Server generations ties four releases with four DIFFERENT
        builds, and must still refuse. Hints passed directly (bypassing
        extract_version_hints) since "Microsoft Windows Server 2008 R2 -
        2012" itself no longer reaches this shape at all -- see
        TrailingDashYearIsMetadataNotASecondVersionTests below, the
        trailing " - 2012" is now recognized as a non-version stamp."""
        releases = [
            {"name": "2008-sp2", "eolFrom": "2020-01-14", "latest": {"name": "6.0.6003"}},
            {"name": "2008-r2-sp1", "eolFrom": "2020-01-14", "latest": {"name": "6.1.7601"}},
            {"name": "2012", "eolFrom": "2023-10-10", "latest": {"name": "6.2.9200"}},
            {"name": "2012-r2", "eolFrom": "2023-10-10", "latest": {"name": "6.3.9600"}},
        ]
        os_string = "Microsoft Windows Server 2008 R2 or 2012"
        picked = pick_release(releases, ["2008", "2012"], os_text=os_string)
        self.assertEqual(picked, {})

    def test_missing_latest_name_never_bypasses_the_refusal(self) -> None:
        """Sanity check: releases with no `latest.name` at all (e.g.
        Android) must never be treated as sharing a build just because
        they're both blank -- the bypass requires a genuine, non-empty
        shared build."""
        releases = [
            {"name": "14", "eolFrom": "2024-06-10"},
            {"name": "11", "eolFrom": "2021-09-08"},
        ]
        self.assertEqual(pick_release(releases, ["14", "11"], os_text="Android 14-11"), {})


class TrailingDashYearIsMetadataNotASecondVersionTests(unittest.TestCase):
    """Real incident: "Microsoft Windows Server 2008 R2 - 2012" was
    refusing to resolve at all, treated as if it named two different OS
    generations ("2008 R2" and "2012") with zero shared evidence -- the
    same shape as "Android 14-11". But per the user, real inventory data
    routinely appends a bare trailing " - <year>" to an already-complete
    OS name as metadata (an install/license/audit-year stamp), not a claim
    that the row is ALSO the other generation -- "2008 R2" is the entire,
    complete OS description here; "- 2012" isn't naming Windows Server
    2012 at all.

    `extract_version_hints` now drops a bare (undotted) 4-digit hint when
    it's the LAST token in the string, immediately preceded by a
    WHITESPACE-separated hyphen, and at least one other hint was already
    captured earlier in the string. The whitespace requirement is what
    keeps this from firing on "Android 14-11" (hyphen glued directly
    between two digits, no spaces -- a genuinely different shape, two
    independent version hints, not a name-vs-metadata split)."""

    def test_trailing_dash_year_is_dropped_after_an_earlier_hint(self) -> None:
        hints = extract_version_hints("Microsoft Windows Server 2008 R2 - 2012")
        self.assertEqual(hints, ["2008"])

    def test_resolves_to_2008_r2_via_edition_narrowing(self) -> None:
        releases = [
            {"name": "2008-sp2", "label": "Windows Server 2008 SP2",
             "eolFrom": "2020-01-14", "latest": {"name": "6.0.6003"}},
            {"name": "2008-r2-sp1", "label": "Windows Server 2008 R2 SP1",
             "eolFrom": "2020-01-14", "latest": {"name": "6.1.7601"}},
        ]
        os_string = "Microsoft Windows Server 2008 R2 - 2012"
        picked = pick_release(releases, extract_version_hints(os_string), os_text=os_string)
        self.assertEqual(picked.get("name"), "2008-r2-sp1")

    def test_unspaced_dash_between_two_digit_runs_is_unaffected(self) -> None:
        """Sanity check: "Android 14-11" (no spaces around the hyphen) must
        still yield BOTH hints -- this is a fundamentally different shape
        (two glued-together version numbers), not a name-then-metadata
        string."""
        self.assertEqual(sorted(extract_version_hints("Android 14-11")), ["11", "14"])

    def test_a_lone_trailing_year_with_no_earlier_hint_is_kept(self) -> None:
        """Sanity check: the exclusion never fires when there's no earlier
        hint to make this "the second one" -- a bare "- 2012" with nothing
        preceding it is still the only version information available and
        must not be discarded."""
        self.assertEqual(extract_version_hints("Server - 2012"), ["2012"])

    def test_a_dash_year_in_the_middle_of_the_string_is_unaffected(self) -> None:
        """Sanity check: the exclusion only applies to the trailing token --
        a dash-year appearing mid-string (with more text after it) is not
        assumed to be a trailing stamp."""
        hints = extract_version_hints("Windows Server 2008 R2 - 2012 Datacenter")
        self.assertEqual(sorted(hints), ["2008", "2012"])


class ParenthesizedBuildNumberHintTests(unittest.TestCase):
    """Real incident: "Windows 10.0 (14393)", "Windows 10.0 (15063)", etc.
    were all resolving to the SAME release (whichever has the earliest EOL)
    regardless of the actual build number in parens. Root cause: "10.0" and
    "14393" were extracted as two independent, disconnected hints -- "10.0"
    alone is a genuine numeric prefix of EVERY Windows 10/11 build (they all
    start "10.0."), so it ties across the whole family, and the bare
    "14393" never scored against a build number at all (only a prefix
    relationship is recognized, never "hint is the trailing segment of a
    longer release"). extract_version_hints must synthesize the combined
    "10.0.14393" hint so an exact match can win outright."""

    RELEASES = [
        {"name": "10-1507", "label": "10 1507", "latest": {"name": "10.0.10240"}, "eolFrom": "2017-05-09"},
        {"name": "10-1607", "label": "10 1607", "latest": {"name": "10.0.14393"}, "eolFrom": "2018-10-09"},
        {"name": "10-1703", "label": "10 1703", "latest": {"name": "10.0.15063"}, "eolFrom": "2019-10-08"},
        {"name": "10-1709", "label": "10 1709", "latest": {"name": "10.0.16299"}, "eolFrom": "2019-10-08"},
        {"name": "10-1809", "label": "10 1809", "latest": {"name": "10.0.17763"}, "eolFrom": "2029-01-09"},
        {"name": "10-1909", "label": "10 1909", "latest": {"name": "10.0.18363"}, "eolFrom": "2022-05-10"},
    ]

    def test_synthesizes_the_combined_build_number_hint(self) -> None:
        self.assertEqual(
            extract_version_hints("Windows 10.0 (14393)"),
            ["10.0", "14393", "10.0.14393"],
        )

    def test_each_parenthesized_build_resolves_to_its_own_release(self) -> None:
        cases = {
            "14393": "10-1607",
            "15063": "10-1703",
            "16299": "10-1709",
            "17763": "10-1809",
            "18363": "10-1909",
        }
        for build, expected_name in cases.items():
            with self.subTest(build=build):
                os_string = f"Windows 10.0 ({build})"
                picked = pick_release(self.RELEASES, extract_version_hints(os_string), os_text=os_string)
                self.assertEqual(picked.get("name"), expected_name)

    def test_bare_dotted_version_with_no_build_refuses_rather_than_guesses(self) -> None:
        """A query with NO build number at all must refuse outright, not
        conservative-merge to the earliest-EOL release among the tied
        family -- "10.0" alone ties every Windows 10/11 release via the
        same 90-point prefix score (never an exact/compound-token 100), so
        it isn't safe evidence for even the most conservative guess. See
        PickReleaseTieRequiresExactScoreTests for the general rule."""
        picked = pick_release(self.RELEASES, extract_version_hints("Windows 10.0"), os_text="Windows 10.0")
        self.assertEqual(picked, {})


class BareTrailingBuildNumberHintTests(unittest.TestCase):
    """Same root cause as ParenthesizedBuildNumberHintTests, no parens this
    time: "Windows 10.0 22631 64-bit" -- the trailing build number sits
    right after the dotted version with just a space, no parentheses."""

    RELEASES = [
        {"name": "10-1507", "label": "10 1507", "latest": {"name": "10.0.10240"}, "eolFrom": "2017-05-09"},
        {"name": "11-22h2", "label": "11 22H2", "latest": {"name": "10.0.22621"}, "eolFrom": "2024-10-08"},
        {"name": "11-23h2", "label": "11 23H2", "latest": {"name": "10.0.22631"}, "eolFrom": "2025-11-11"},
    ]

    def test_synthesizes_the_combined_build_number_hint(self) -> None:
        self.assertEqual(
            extract_version_hints("Windows 10.0 22631 64-bit"),
            ["10.0", "22631", "10.0.22631"],
        )

    def test_resolves_to_the_exact_matching_release(self) -> None:
        os_string = "Windows 10.0 22631 64-bit"
        picked = pick_release(self.RELEASES, extract_version_hints(os_string), os_text=os_string)
        self.assertEqual(picked.get("name"), "11-23h2")

    def test_genuine_bitness_marker_is_never_absorbed_into_the_version(self) -> None:
        """"64" in "64-bit" is excluded from `hints` entirely by the
        existing bitness check -- it must never become "10.0.64", since
        that isn't a real build number at all."""
        hints = extract_version_hints("Windows 10.0 64-bit")
        self.assertEqual(hints, ["10.0"])
        self.assertNotIn("10.0.64", hints)

    def test_short_trailing_numbers_are_not_combined(self) -> None:
        """Restricted to 4+ digit trailing numbers (real build numbers are
        always long) -- a short, likely-unrelated trailing number (e.g. a
        stray "4" in "AlmaLinux 8.10 4 18") must not be blindly combined."""
        hints = extract_version_hints("AlmaLinux 8.10 4 18")
        self.assertNotIn("8.10.4", hints)


class PickReleaseDotZeroFallbackTests(unittest.TestCase):
    """Real incident: os_string "SUSE Linux Enterprise Server 15 SP7" ->
    extract_version_hints drops the SP-marker digit and yields a bare "15",
    but endoflife.date's actual release for this product is named "15.0" --
    a bare hint can never score against a multi-part release name by design
    (the "bare major must not guess" rule), so this genuinely-resolvable row
    went unmatched. pick_release's dot-zero fallback retries with an
    explicit ".0" appended to any bare hint, only after the strict pass
    finds nothing at all."""

    def test_suse_sp7_bare_hint_matches_the_dot_zero_release(self) -> None:
        releases = [{"name": "15.0", "label": "15.0", "eolFrom": "2028-07-31", "eoasFrom": "2031-07-31"}]
        hints = extract_version_hints("SUSE Linux Enterprise Server 15 SP7")
        self.assertEqual(hints, ["15"])  # the SP7 digit is dropped as a service-pack marker
        picked = pick_release(releases, hints, os_text="SUSE Linux Enterprise Server 15 SP7")
        self.assertEqual(picked.get("name"), "15.0")

    def test_fallback_is_not_product_specific(self) -> None:
        """Same shape, unrelated product -- confirms this isn't hardcoded to SUSE."""
        releases = [{"name": "9.0", "label": "9.0", "eolFrom": "2030-01-01"}]
        picked = pick_release(releases, ["9"], os_text="Some Product 9")
        self.assertEqual(picked.get("name"), "9.0")

    def test_bare_hint_still_matches_a_genuinely_bare_release_without_the_fallback(self) -> None:
        """Sanity check: an exact bare-to-bare match succeeds on the strict
        first pass and must not need (or be affected by) the fallback."""
        releases = [{"name": "15", "label": "15"}, {"name": "16", "label": "16"}]
        picked = pick_release(releases, ["15"], os_text="Product 15")
        self.assertEqual(picked.get("name"), "15")

    def test_multiple_similarly_plausible_dotted_releases_still_refuse(self) -> None:
        """Several specific releases that are each only a WEAK ("shared
        major only") match against the synthesized "<n>.0" hint must not
        suddenly become a confident guess -- e.g. a bare "15" against a
        catalog that only has 15.1/15.2/15.3 (no bare "15" or "15.0" release
        at all) stays exactly as unresolved as before this fallback existed."""
        releases = [
            {"name": "15.1", "label": "15.1"},
            {"name": "15.2", "label": "15.2"},
            {"name": "15.3", "label": "15.3"},
        ]
        self.assertEqual(pick_release(releases, ["15"], os_text="Product 15"), {})

    def test_android_14_11_still_refuses_with_the_fallback_active(self) -> None:
        """Regression guard: the dot-zero fallback must not reopen the
        "Android 14-11" false-match bug (see
        PickReleaseRefusesMixedIndependentHintsTests) -- appending ".0" to
        each of the two independent bare hints must still fail the
        shared-hint tie-break, since neither release's winning score is ever
        explained by a ".0"-suffixed hint (only by its own exact bare hint,
        and those aren't shared across the tie)."""
        releases = [
            {"name": "14", "label": "14 'Upside Down Cake'", "eolFrom": "2024-06-10"},
            {"name": "11", "label": "11 'Red Velvet Cake'", "eolFrom": "2021-09-08"},
        ]
        hints = extract_version_hints("Android 14-11")
        self.assertEqual(pick_release(releases, hints, os_text="Android 14-11"), {})


if __name__ == "__main__":
    unittest.main()
