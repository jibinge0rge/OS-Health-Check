"""Tests for endoflife.date product slug resolution."""

from __future__ import annotations

import unittest

from eol_service import (
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

    def test_windows_normalized_os_uses_label_not_slug(self) -> None:
        """release.name ("10-22h2") is an internal slug, never presentable —
        normalized_os must use release.label ("10 22H2") like the detailed
        name already does, not the raw slug."""
        product_result = {"label": "Microsoft Windows"}
        release = {"name": "10-22h2", "label": "10 22H2"}
        normalization = build_normalization_from_product(product_result, release)
        self.assertEqual(normalization["normalized_os"], "Microsoft Windows 10 22H2")
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
        self.assertEqual(normalization["normalized_os"], "Microsoft Windows Server 23H2 AC")
        self.assertEqual(
            normalization["normalized_os_detailed_name"], "Microsoft Windows Server 23H2 AC"
        )
        self.assertNotIn("Server Windows Server", normalization["normalized_os"])


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


if __name__ == "__main__":
    unittest.main()
