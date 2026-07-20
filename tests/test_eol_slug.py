"""Tests for endoflife.date product slug resolution."""

from __future__ import annotations

import unittest

from eol_service import (
    build_slug_index,
    extract_version_hints,
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


if __name__ == "__main__":
    unittest.main()
