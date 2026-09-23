"""Tests for the bump-versions script (OpenShell version bumping)."""

import importlib.util
from unittest import mock

import pytest


@pytest.fixture()
def bump_versions():
    """Import bump-versions.py as a module."""
    spec = importlib.util.spec_from_file_location(
        "bump_versions",
        "scripts/bump-versions.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


QUAY_RESPONSE_MIXED_TAGS = {
    "tags": [
        {"name": "sha256-abc123.att"},
        {"name": "v0.0.99-rhaiv.0"},
        {"name": "v0.0.101-rhaiv.0"},
        {"name": "v0.0.101-rhaiv.0-linux-arm64"},
        {"name": "v0.0.101-rhaiv.0-linux-x86-64"},
        {"name": "v0.0.101-rhaiv.0.prefetch"},
        {"name": "v0.0.101-rhaiv.0.git"},
        {"name": "v0.0.100-rhaiv.0"},
        {"name": "odh-openshell-cli-on-pull-request-nssvm-build-image-index"},
    ],
    "has_additional": False,
}


class TestQuayLatestOpenshell:
    def test_picks_highest_version(self, bump_versions):
        with mock.patch.object(bump_versions, "_fetch_json", return_value=QUAY_RESPONSE_MIXED_TAGS):
            tag = bump_versions._quay_latest_openshell()
        assert tag == "v0.0.101-rhaiv.0"

    def test_ignores_arch_suffixes(self, bump_versions):
        data = {
            "tags": [
                {"name": "v0.0.50-rhaiv.0-linux-arm64"},
                {"name": "v0.0.50-rhaiv.0-linux-x86-64"},
                {"name": "v0.0.49-rhaiv.0"},
            ],
            "has_additional": False,
        }
        with mock.patch.object(bump_versions, "_fetch_json", return_value=data):
            tag = bump_versions._quay_latest_openshell()
        assert tag == "v0.0.49-rhaiv.0"

    def test_ignores_build_metadata_suffixes(self, bump_versions):
        data = {
            "tags": [
                {"name": "v0.0.80-rhaiv.0.prefetch"},
                {"name": "v0.0.80-rhaiv.0.git"},
                {"name": "v0.0.79-rhaiv.0"},
            ],
            "has_additional": False,
        }
        with mock.patch.object(bump_versions, "_fetch_json", return_value=data):
            tag = bump_versions._quay_latest_openshell()
        assert tag == "v0.0.79-rhaiv.0"

    def test_compares_rhaiv_suffix_numerically(self, bump_versions):
        data = {
            "tags": [
                {"name": "v0.0.100-rhaiv.0"},
                {"name": "v0.0.100-rhaiv.1"},
            ],
            "has_additional": False,
        }
        with mock.patch.object(bump_versions, "_fetch_json", return_value=data):
            tag = bump_versions._quay_latest_openshell()
        assert tag == "v0.0.100-rhaiv.1"

    def test_raises_when_no_matching_tags(self, bump_versions):
        data = {
            "tags": [
                {"name": "sha256-abc.att"},
                {"name": "latest"},
            ],
            "has_additional": False,
        }
        with mock.patch.object(bump_versions, "_fetch_json", return_value=data):
            with pytest.raises(RuntimeError, match="openshell tag not found"):
                bump_versions._quay_latest_openshell()

    def test_raises_on_empty_response(self, bump_versions):
        with mock.patch.object(
            bump_versions, "_fetch_json", return_value={"tags": [], "has_additional": False}
        ):
            with pytest.raises(RuntimeError, match="openshell tag not found"):
                bump_versions._quay_latest_openshell()

    def test_paginates_through_all_pages(self, bump_versions):
        page1 = {
            "tags": [
                {"name": "v0.0.99-rhaiv.0"},
                {"name": "v0.0.100-rhaiv.0"},
            ],
            "has_additional": True,
        }
        page2 = {
            "tags": [
                {"name": "v0.0.101-rhaiv.0"},
                {"name": "v0.0.101-rhaiv.0-linux-arm64"},
            ],
            "has_additional": False,
        }
        with mock.patch.object(bump_versions, "_fetch_json", side_effect=[page1, page2]):
            tag = bump_versions._quay_latest_openshell()
        assert tag == "v0.0.101-rhaiv.0"

    def test_highest_on_second_page_wins(self, bump_versions):
        page1 = {
            "tags": [{"name": "v0.0.50-rhaiv.0"}],
            "has_additional": True,
        }
        page2 = {
            "tags": [{"name": "v0.0.200-rhaiv.0"}],
            "has_additional": False,
        }
        with mock.patch.object(bump_versions, "_fetch_json", side_effect=[page1, page2]):
            tag = bump_versions._quay_latest_openshell()
        assert tag == "v0.0.200-rhaiv.0"


class TestBumpOpenshell:
    def test_updates_image_tag_arg(self, bump_versions, tmp_path):
        cf = tmp_path / "Containerfile.openshell"
        cf.write_text("ARG OPENSHELL_IMAGE_TAG=v0.0.90-rhaiv.0\n")

        with (
            mock.patch.object(
                bump_versions, "_quay_latest_openshell", return_value="v0.0.101-rhaiv.0"
            ),
            mock.patch.object(bump_versions, "OPENSHELL_CI_CF", cf),
        ):
            result = bump_versions.bump_openshell(check_only=False)

        assert result == {"tool": "openshell", "version": "v0.0.101-rhaiv.0"}
        assert "ARG OPENSHELL_IMAGE_TAG=v0.0.101-rhaiv.0" in cf.read_text()

    def test_check_only_does_not_modify(self, bump_versions, tmp_path):
        cf = tmp_path / "Containerfile.openshell"
        cf.write_text("ARG OPENSHELL_IMAGE_TAG=v0.0.90-rhaiv.0\n")

        with (
            mock.patch.object(
                bump_versions, "_quay_latest_openshell", return_value="v0.0.101-rhaiv.0"
            ),
            mock.patch.object(bump_versions, "OPENSHELL_CI_CF", cf),
        ):
            result = bump_versions.bump_openshell(check_only=True)

        assert result["version"] == "v0.0.101-rhaiv.0"
        assert "v0.0.90-rhaiv.0" in cf.read_text()

    def test_no_openshell_version_arg_left(self, bump_versions, tmp_path):
        """Verify bump_openshell no longer writes OPENSHELL_VERSION."""
        cf = tmp_path / "Containerfile.openshell"
        cf.write_text("ARG OPENSHELL_IMAGE_TAG=v0.0.90-rhaiv.0\n")

        with (
            mock.patch.object(
                bump_versions, "_quay_latest_openshell", return_value="v0.0.101-rhaiv.0"
            ),
            mock.patch.object(bump_versions, "OPENSHELL_CI_CF", cf),
        ):
            bump_versions.bump_openshell(check_only=False)

        content = cf.read_text()
        assert "OPENSHELL_VERSION" not in content
