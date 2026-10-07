"""Tests for the toolchain catalog, version resolution, verification and the host cache.

HTTP is faked: :class:`FakeFetcher` serves bytes by URL and applies the same
host check as the real fetcher.
"""

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import pytest
import requests

from agentic_ci import sandbox_profile, toolchains
from agentic_ci.sandbox_profile import KNOWN_TOOLCHAINS, SandboxProfile, parse_profile
from agentic_ci.toolchains import (
    CATALOG,
    AutoVersion,
    HttpFetcher,
    InstallPlan,
    Resolved,
    ToolchainEnv,
    ToolchainError,
    ToolchainResult,
    auto_version,
    fetch_archive,
    provision,
    resolve,
    toolchain_env,
    write_results,
)

# Every host the catalog may contact. Nothing else is ever fetched.
OFFICIAL_HOSTS = {
    "go.dev",
    "dl.google.com",
    "nodejs.org",
    "registry.npmjs.org",
    "get.helm.sh",
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "static.rust-lang.org",
}

GO_ARCHIVE = b"go archive bytes"
NODE_ARCHIVE = b"node archive bytes"
PNPM_ARCHIVE = b"pnpm tarball bytes"
SHFMT_BINARY = b"shfmt binary bytes"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha512(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


def integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


class FakeFetcher:
    """Serves ``responses[url]`` (bytes, or an exception to raise); records every URL."""

    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.calls = []

    def stream(self, url, *, hosts, limit, timeout, headers=None, path_prefix=""):
        toolchains._check_url(url, hosts, path_prefix=path_prefix)
        self.calls.append((url, dict(headers or {})))
        body = self.responses.get(url)
        if body is None:
            raise ToolchainError("download failed with HTTP status 404")
        if isinstance(body, Exception):
            raise body
        if len(body) > limit:
            raise ToolchainError("too large")
        yield body[: len(body) // 2]
        yield body[len(body) // 2 :]

    def urls(self):
        return [url for url, _ in self.calls]


def go_index(*releases):
    """go.dev JSON for (version, stable, sha256-or-None) triples."""
    data = []
    for version, stable, digest in releases:
        files = []
        if digest is not None:
            files.append(
                {
                    "filename": f"go{version}.linux-amd64.tar.gz",
                    "os": "linux",
                    "arch": "amd64",
                    "version": f"go{version}",
                    "sha256": digest,
                    "size": len(GO_ARCHIVE),
                    "kind": "archive",
                }
            )
        data.append({"version": f"go{version}", "stable": stable, "files": files})
    return json.dumps(data).encode()


GO_URL = toolchains.GO_INDEX_URL


def go_fetcher(**extra):
    return FakeFetcher(
        {
            GO_URL: go_index(
                ("1.27rc1", False, sha256(b"rc")),
                ("1.26.5", True, sha256(GO_ARCHIVE)),
                ("1.26.4", True, sha256(b"old")),
                ("1.25.9", True, sha256(b"125")),
                ("1.20", True, sha256(b"120")),
            ),
            "https://dl.google.com/go/go1.26.5.linux-amd64.tar.gz": GO_ARCHIVE,
            **extra,
        }
    )


NODE_INDEX = json.dumps(
    [
        {"version": "v24.1.0", "files": ["linux-x64"]},
        {"version": "v22.20.0", "files": ["linux-arm64"]},
        {"version": "v22.19.0", "files": ["linux-x64", "linux-arm64"]},
        {"version": "v22.9.0", "files": ["linux-x64"]},
        {"version": "v20.1.0", "files": ["linux-x64"]},
    ]
).encode()


def node_sums(version, digest):
    return (
        f"{'0' * 64}  node-v{version}-linux-arm64.tar.gz\n"
        f"{digest}  node-v{version}-linux-x64.tar.gz\n"
        f"{'1' * 64}  node-v{version}-linux-x64.tar.xz\n"
    ).encode()


def node_fetcher(version="22.19.0", digest=None):
    base = f"https://nodejs.org/dist/v{version}"
    return FakeFetcher(
        {
            toolchains.NODE_INDEX_URL: NODE_INDEX,
            f"{base}/SHASUMS256.txt": node_sums(version, digest or sha256(NODE_ARCHIVE)),
            f"{base}/node-v{version}-linux-x64.tar.gz": NODE_ARCHIVE,
        }
    )


def pnpm_fetcher(version="10.34.6", data=PNPM_ARCHIVE, tarball=None):
    dist = {
        "integrity": integrity(data),
        "tarball": tarball or f"https://registry.npmjs.org/pnpm/-/pnpm-{version}.tgz",
    }
    versions = {
        "10.34.6": {"version": "10.34.6", "dist": dist},
        "10.2.0": {"version": "10.2.0", "dist": {"integrity": integrity(b"x")}},
        "10.99.0-rc.1": {"version": "10.99.0-rc.1", "dist": {}},
        "9.15.9": {"version": "9.15.9", "dist": {}},
    }
    return FakeFetcher(
        {
            "https://registry.npmjs.org/pnpm": json.dumps({"versions": versions}).encode(),
            f"https://registry.npmjs.org/pnpm/{version}": json.dumps(
                {"version": version, "dist": dist}
            ).encode(),
            f"https://registry.npmjs.org/pnpm/-/pnpm-{version}.tgz": data,
        }
    )


SHFMT_BASE = "https://github.com/mvdan/sh/releases/download/v3.12.0"


def shfmt_fetcher(sums=None):
    if sums is None:
        sums = (
            f"{'a' * 64}  shfmt_v3.12.0_darwin_amd64\n"
            f"{sha256(SHFMT_BINARY)}  shfmt_v3.12.0_linux_amd64\n"
        )
    return FakeFetcher(
        {
            f"{SHFMT_BASE}/sha256sums.txt": sums.encode(),
            f"{SHFMT_BASE}/shfmt_v3.12.0_linux_amd64": SHFMT_BINARY,
        }
    )


@pytest.fixture(autouse=True)
def x86_64():
    with mock.patch("agentic_ci.toolchains.platform.machine", return_value="x86_64"):
        yield


def res(name, requested, fetcher, workdir=Path("/nonexistent")):
    return resolve(name, requested, workdir=workdir, fetcher=fetcher)[0]


class TestCatalog:
    def test_profile_toolchain_names_come_from_the_catalog(self):
        assert KNOWN_TOOLCHAINS == frozenset(CATALOG)
        assert set(CATALOG) == {
            "buf",
            "go",
            "golangci-lint",
            "helm",
            "kustomize",
            "node",
            "pnpm",
            "protoc",
            "python",
            "rust",
            "shfmt",
            "yq",
        }

    def test_profile_validation_uses_the_catalog_and_its_version_regex(self):
        assert sandbox_profile.TOOLCHAIN_CATALOG is toolchains.CATALOG
        assert sandbox_profile._VERSION_RE is toolchains.VERSION_RE

    @pytest.mark.parametrize("name", sorted(CATALOG))
    def test_urls_stay_on_the_entry_hosts(self, name):
        entry = CATALOG[name]
        assert entry.hosts <= OFFICIAL_HOSTS
        for machine in ("x86_64", "aarch64"):
            arch = entry.arch[machine]
            for template in filter(None, (entry.url, entry.checksum_url)):
                url = template.format(version="1.2.3", arch=arch, tag="20260924")
                parts = urlsplit(url)
                assert parts.scheme == "https"
                assert parts.hostname in entry.hosts
                assert parts.port is None and parts.username is None

    @pytest.mark.parametrize("name", sorted(CATALOG))
    def test_github_entries_are_pinned_to_their_release_path(self, name):
        entry = CATALOG[name]
        if "github.com" not in entry.hosts:
            assert entry.path_prefix == ""
            return
        assert entry.path_prefix.count("/") == 4
        assert entry.path_prefix.endswith("/releases/")
        for template in filter(None, (entry.url, entry.checksum_url)):
            url = template.format(version="1.2.3", arch="x", tag="20260924")
            assert urlsplit(url).path.startswith(entry.path_prefix)

    def test_index_urls_are_official(self):
        for url in (toolchains.GO_INDEX_URL, toolchains.NODE_INDEX_URL, toolchains.NPM_REGISTRY):
            assert urlsplit(url).hostname in OFFICIAL_HOSTS
        assert urlsplit(toolchains.PYTHON_SUMS_URL).hostname == "github.com"

    def test_partial_versions_only_where_an_index_exists(self):
        assert {n for n, e in CATALOG.items() if e.partial} == {
            "go",
            "node",
            "pnpm",
            "python",
            "rust",
        }

    def test_auto_sources(self):
        assert {n for n, e in CATALOG.items() if e.auto} == {"go", "node", "pnpm", "python", "rust"}

    def test_protoc_table_has_both_machines_and_valid_digests(self):
        versions = {name.split("-")[1] for name in toolchains.PROTOC_SHA256}
        for version in versions:
            for arch in ("x86_64", "aarch_64"):
                digest = toolchains.PROTOC_SHA256[f"protoc-{version}-linux-{arch}.zip"]
                assert toolchains._HEX64.fullmatch(digest)

    @pytest.mark.parametrize(
        ("machine", "expected"),
        [("x86_64", "x86_64"), ("AMD64", "x86_64"), ("aarch64", "aarch64"), ("arm64", "aarch64")],
    )
    def test_host_machine(self, machine, expected):
        with mock.patch("agentic_ci.toolchains.platform.machine", return_value=machine):
            assert toolchains.host_machine() == expected

    def test_other_machines_are_refused(self):
        with mock.patch("agentic_ci.toolchains.platform.machine", return_value="s390x"):
            with pytest.raises(ToolchainError, match="not x86_64 or aarch64"):
                toolchains.host_machine()


class TestUrlCheck:
    @pytest.mark.parametrize(
        "url",
        [
            "http://go.dev/dl/",
            "https://evil.example/go.tar.gz",
            "https://user:pw@go.dev/dl/",
            "https://go.dev:8443/dl/",
            "https://go.dev.evil.example/",
        ],
    )
    def test_refused(self, url):
        with pytest.raises(ToolchainError, match="left the tool's official hosts"):
            toolchains._check_url(url, frozenset({"go.dev"}))

    def test_allowed(self):
        toolchains._check_url("https://GO.dev/dl/?mode=json", frozenset({"go.dev"}))

    def test_malformed_port_is_a_refusal(self):
        with pytest.raises(ToolchainError, match="left the tool's official hosts"):
            toolchains._check_url("https://go.dev:99999999/dl/", frozenset({"go.dev"}))

    PREFIX = "/mvdan/sh/releases/"
    GH = frozenset({"github.com", "release-assets.githubusercontent.com"})

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/other/sh/releases/download/v1/x",
            "https://github.com/mvdan/sh-renamed/releases/download/v1/x",
            "https://github.com/mvdan/sh/archive/v1.tar.gz",
            "https://github.com/mvdan/sh/releases/../../../other/repo/releases/x",
            "https://github.com/mvdan/sh/releases/download/%2e%2e/%2E%2E/x",
        ],
    )
    def test_github_urls_must_stay_in_the_entry_release_path(self, url):
        with pytest.raises(ToolchainError, match="left the tool's official hosts"):
            toolchains._check_url(url, self.GH, path_prefix=self.PREFIX, redirected=True)

    def test_github_release_path_is_compared_without_case(self):
        toolchains._check_url(
            "https://github.com/MVDAN/sh/releases/download/v3/x", self.GH, path_prefix=self.PREFIX
        )

    def test_asset_hosts_only_after_a_redirect(self):
        url = "https://release-assets.githubusercontent.com/x"
        with pytest.raises(ToolchainError, match="left the tool's official hosts"):
            toolchains._check_url(url, self.GH, path_prefix=self.PREFIX)
        toolchains._check_url(url, self.GH, path_prefix=self.PREFIX, redirected=True)


class _Response:
    def __init__(self, status=200, body=b"", headers=None, error=None):
        self.status_code = status
        self.body = body
        self.headers = headers or {}
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        if self.error is not None:
            raise self.error
        for i in range(0, len(self.body), 4):
            yield self.body[i : i + 4]

    def close(self):
        pass


class TestHttpFetcher:
    HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com"})

    @pytest.fixture(autouse=True)
    def sleep(self):
        with mock.patch("agentic_ci.toolchains.time.sleep") as sleep:
            yield sleep

    def fetch(self, responses, url="https://github.com/a", limit=100, timeout=60, prefix=""):
        session = mock.Mock()
        session.get.side_effect = responses
        fetcher = HttpFetcher(session=session)
        data = b"".join(
            fetcher.stream(url, hosts=self.HOSTS, limit=limit, timeout=timeout, path_prefix=prefix)
        )
        return data, session

    def test_follows_redirects_within_the_hosts(self):
        data, session = self.fetch(
            [
                _Response(
                    302, headers={"Location": "https://release-assets.githubusercontent.com/x"}
                ),
                _Response(200, b"payload"),
            ]
        )
        assert data == b"payload"
        assert (
            session.get.call_args_list[1].args[0]
            == "https://release-assets.githubusercontent.com/x"
        )
        kwargs = session.get.call_args.kwargs
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        assert kwargs["timeout"] == (30, 60)

    def test_redirect_to_another_host_is_refused(self):
        with pytest.raises(ToolchainError, match="left the tool's official hosts"):
            self.fetch([_Response(302, headers={"Location": "https://evil.example/x"})])

    def test_redirect_to_http_is_refused(self):
        with pytest.raises(ToolchainError, match="official hosts"):
            self.fetch([_Response(301, headers={"Location": "http://github.com/x"})])

    def test_too_many_redirects(self):
        loop = [_Response(302, headers={"Location": "/again"}) for _ in range(10)]
        with pytest.raises(ToolchainError, match="redirected too many times"):
            self.fetch(loop)

    def test_status_other_than_200_fails(self):
        with pytest.raises(ToolchainError, match="HTTP status 404"):
            self.fetch([_Response(404)])

    def test_declared_size_over_the_limit(self):
        with pytest.raises(ToolchainError, match="larger than the 100 byte limit"):
            self.fetch([_Response(200, b"x", headers={"Content-Length": "101"})])

    def test_streamed_size_over_the_limit(self):
        with pytest.raises(ToolchainError, match="larger than the 10 byte limit"):
            self.fetch([_Response(200, b"x" * 11)], limit=10)

    def test_deadline(self):
        clock = iter([0.0, 0.0, 100.0, 200.0])
        with mock.patch("agentic_ci.toolchains.time.monotonic", lambda: next(clock)):
            with pytest.raises(ToolchainError, match="took longer than 5s"):
                self.fetch([_Response(200, b"12345678")], timeout=5)

    def test_transport_errors_name_only_the_class(self, sleep):
        err = requests.ConnectionError("secret-host.internal refused")
        with pytest.raises(ToolchainError) as excinfo:
            self.fetch([err, err, err])
        assert str(excinfo.value) == "download failed (ConnectionError)"
        assert [c.args[0] for c in sleep.call_args_list] == [2.0, 5.0]

    def test_transient_errors_are_retried(self, sleep):
        data, session = self.fetch(
            [requests.ConnectionError("reset"), _Response(503), _Response(200, b"ok")]
        )
        assert data == b"ok"
        assert session.get.call_count == 3

    def test_server_errors_fail_after_the_retries(self):
        with pytest.raises(ToolchainError, match="HTTP status 502"):
            self.fetch([_Response(502), _Response(502), _Response(502)])

    def test_client_errors_are_not_retried(self, sleep):
        with pytest.raises(ToolchainError, match="HTTP status 404"):
            self.fetch([_Response(404), _Response(200, b"late")])
        sleep.assert_not_called()

    def test_no_retry_past_the_deadline(self, sleep):
        with pytest.raises(ToolchainError, match="HTTP status 503"):
            self.fetch([_Response(503), _Response(200, b"late")], timeout=1)
        sleep.assert_not_called()

    def test_redirect_out_of_the_release_path_is_refused(self):
        with pytest.raises(ToolchainError, match="left the tool's official hosts"):
            self.fetch(
                [_Response(302, headers={"Location": "https://github.com/squatter/r/releases/x"})],
                url="https://github.com/a/r/releases/download/v1/x",
                prefix="/a/r/releases/",
            )

    def test_redirect_from_an_asset_host_is_refused(self):
        with pytest.raises(ToolchainError, match="left the tool's official hosts"):
            self.fetch(
                [
                    _Response(
                        302, headers={"Location": "https://release-assets.githubusercontent.com/x"}
                    ),
                    _Response(302, headers={"Location": "https://github.com/a/r/releases/y"}),
                ],
                url="https://github.com/a/r/releases/download/v1/x",
                prefix="/a/r/releases/",
            )

    def test_an_asset_host_cannot_be_the_first_hop(self):
        with pytest.raises(ToolchainError, match="left the tool's official hosts"):
            self.fetch([_Response(200, b"x")], url="https://release-assets.githubusercontent.com/x")

    def test_close_closes_the_session(self):
        session = mock.Mock()
        HttpFetcher(session=session).close()
        session.close.assert_called_once_with()

    def test_read_errors_name_only_the_class(self):
        err = requests.exceptions.ChunkedEncodingError("boom detail")
        with pytest.raises(ToolchainError) as excinfo:
            self.fetch([_Response(200, b"abc", error=err)])
        assert str(excinfo.value) == "download failed (ChunkedEncodingError)"

    def test_default_session_ignores_netrc(self):
        assert HttpFetcher()._session.trust_env is False


class TestResolveGo:
    def test_exact(self):
        resolved = res("go", "1.26.5", go_fetcher())
        assert resolved == Resolved(
            "go",
            "1.26.5",
            "https://dl.google.com/go/go1.26.5.linux-amd64.tar.gz",
            "go1.26.5.linux-amd64.tar.gz",
            "sha256",
            sha256(GO_ARCHIVE),
            len(GO_ARCHIVE),
        )

    def test_partial_picks_the_newest_stable_release(self):
        assert res("go", "1.26", go_fetcher()).version == "1.26.5"
        assert res("go", "1", go_fetcher()).version == "1.26.5"

    def test_release_candidates_are_never_picked(self):
        with pytest.raises(ToolchainError, match="no published release matches 1.27"):
            res("go", "1.27", go_fetcher())

    def test_exact_x_y_0_matches_an_old_style_release_name(self):
        assert res("go", "1.20.0", go_fetcher()).version == "1.20"

    def test_unknown_exact_version(self):
        with pytest.raises(ToolchainError, match="go 1.26.9 is not a published release"):
            res("go", "1.26.9", go_fetcher())

    def test_missing_checksum_refuses(self):
        fetcher = FakeFetcher({GO_URL: go_index(("1.26.5", True, None))})
        with pytest.raises(ToolchainError, match="no sha256"):
            res("go", "1.26.5", fetcher)

    def test_malformed_checksum_refuses(self):
        fetcher = FakeFetcher({GO_URL: go_index(("1.26.5", True, "not-hex"))})
        with pytest.raises(ToolchainError, match="no sha256"):
            res("go", "1.26.5", fetcher)

    def test_garbage_index(self):
        with pytest.raises(ToolchainError, match="not valid JSON"):
            res("go", "1.26.5", FakeFetcher({GO_URL: b"<html>"}))
        with pytest.raises(ToolchainError, match="not a list"):
            res("go", "1.26.5", FakeFetcher({GO_URL: b"{}"}))

    def test_arm64_file(self):
        index = json.dumps(
            [
                {
                    "version": "go1.26.5",
                    "stable": True,
                    "files": [
                        {
                            "filename": "go1.26.5.linux-arm64.tar.gz",
                            "sha256": "b" * 64,
                            "kind": "archive",
                        }
                    ],
                }
            ]
        ).encode()
        with mock.patch("agentic_ci.toolchains.platform.machine", return_value="aarch64"):
            resolved = res("go", "1.26.5", FakeFetcher({GO_URL: index}))
        assert resolved.url.endswith("go1.26.5.linux-arm64.tar.gz")
        assert resolved.digest == "b" * 64


class TestResolveNode:
    def test_exact_needs_no_index(self):
        fetcher = node_fetcher("22.19.0")
        resolved = res("node", "22.19.0", fetcher)
        assert resolved.digest == sha256(NODE_ARCHIVE)
        assert resolved.filename == "node-v22.19.0-linux-x64.tar.gz"
        assert toolchains.NODE_INDEX_URL not in fetcher.urls()

    def test_major_picks_the_newest_release_with_a_linux_build(self):
        # 22.20.0 has no linux-x64 build in the index, and 22.19.0 > 22.9.0.
        assert res("node", "22", node_fetcher()).version == "22.19.0"

    def test_no_match(self):
        with pytest.raises(ToolchainError, match="no published release matches 21"):
            res("node", "21", node_fetcher())

    def test_missing_checksum_refuses(self):
        fetcher = node_fetcher()
        fetcher.responses["https://nodejs.org/dist/v22.19.0/SHASUMS256.txt"] = b"nothing here\n"
        with pytest.raises(ToolchainError, match="no sha256"):
            res("node", "22.19.0", fetcher)

    def test_conflicting_checksums_refuse(self):
        fetcher = node_fetcher()
        fetcher.responses["https://nodejs.org/dist/v22.19.0/SHASUMS256.txt"] = (
            f"{'a' * 64}  node-v22.19.0-linux-x64.tar.gz\n"
            f"{'b' * 64}  node-v22.19.0-linux-x64.tar.gz\n"
        ).encode()
        with pytest.raises(ToolchainError, match="no sha256"):
            res("node", "22.19.0", fetcher)


class TestResolvePnpm:
    def test_exact(self):
        resolved = res("pnpm", "10.34.6", pnpm_fetcher())
        assert resolved.algorithm == "sha512"
        assert resolved.digest == sha512(PNPM_ARCHIVE)
        assert resolved.url == "https://registry.npmjs.org/pnpm/-/pnpm-10.34.6.tgz"

    def test_partial_uses_the_abbreviated_packument_and_skips_prereleases(self):
        fetcher = pnpm_fetcher()
        assert res("pnpm", "10", fetcher).version == "10.34.6"
        url, headers = fetcher.calls[0]
        assert url == "https://registry.npmjs.org/pnpm"
        assert headers == {"Accept": "application/vnd.npm.install-v1+json"}

    def test_unexpected_tarball_url_refuses(self):
        fetcher = pnpm_fetcher(tarball="https://registry.npmjs.org/evil/-/evil-1.0.0.tgz")
        with pytest.raises(ToolchainError, match="tarball URL is unexpected"):
            res("pnpm", "10.34.6", fetcher)

    def test_missing_integrity_refuses(self):
        fetcher = pnpm_fetcher()
        fetcher.responses["https://registry.npmjs.org/pnpm/10.34.6"] = json.dumps(
            {
                "version": "10.34.6",
                "dist": {
                    "tarball": "https://registry.npmjs.org/pnpm/-/pnpm-10.34.6.tgz",
                    "integrity": "sha1-AAAA",
                },
            }
        ).encode()
        with pytest.raises(ToolchainError, match="no sha512 integrity"):
            res("pnpm", "10.34.6", fetcher)

    def test_version_mismatch_refuses(self):
        fetcher = pnpm_fetcher()
        fetcher.responses["https://registry.npmjs.org/pnpm/10.34.6"] = b'{"version": "1.0.0"}'
        with pytest.raises(ToolchainError, match="not a published release"):
            res("pnpm", "10.34.6", fetcher)


class TestResolveGithubSums:
    def test_exact(self):
        resolved = res("shfmt", "3.12.0", shfmt_fetcher())
        assert resolved.url == f"{SHFMT_BASE}/shfmt_v3.12.0_linux_amd64"
        assert resolved.digest == sha256(SHFMT_BINARY)

    def test_binary_marker_in_the_manifest(self):
        sums = f"{sha256(SHFMT_BINARY)} *shfmt_v3.12.0_linux_amd64\n"
        assert res("shfmt", "3.12.0", shfmt_fetcher(sums)).digest == sha256(SHFMT_BINARY)

    @pytest.mark.parametrize("name", ["shfmt", "golangci-lint", "helm", "kustomize", "buf", "yq"])
    @pytest.mark.parametrize("version", ["3", "3.12"])
    def test_partial_versions_are_an_error(self, name, version):
        fetcher = FakeFetcher()
        with pytest.raises(ToolchainError, match="partial version .* use an exact"):
            res(name, version, fetcher)
        assert fetcher.calls == []

    def test_missing_checksum_refuses(self):
        with pytest.raises(ToolchainError, match="no sha256"):
            res("shfmt", "3.12.0", shfmt_fetcher(sums=f"{'a' * 64}  other\n"))

    def test_missing_manifest_refuses(self):
        fetcher = shfmt_fetcher()
        del fetcher.responses[f"{SHFMT_BASE}/sha256sums.txt"]
        with pytest.raises(ToolchainError, match="HTTP status 404"):
            res("shfmt", "3.12.0", fetcher)

    def test_kustomize_tag_and_file_name(self):
        base = "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv5.8.1"
        fetcher = FakeFetcher(
            {f"{base}/checksums.txt": f"{'c' * 64}  kustomize_v5.8.1_linux_amd64.tar.gz\n".encode()}
        )
        resolved = res("kustomize", "5.8.1", fetcher)
        assert resolved.url == f"{base}/kustomize_v5.8.1_linux_amd64.tar.gz"
        assert resolved.digest == "c" * 64

    def test_helm_sha256sum_file(self):
        url = "https://get.helm.sh/helm-v4.3.0-linux-amd64.tar.gz"
        fetcher = FakeFetcher(
            {url + ".sha256sum": f"{'d' * 64}  helm-v4.3.0-linux-amd64.tar.gz\n".encode()}
        )
        assert res("helm", "4.3.0", fetcher).digest == "d" * 64

    def test_yq_uses_the_sha256_column(self):
        base = "https://github.com/mikefarah/yq/releases/download/v4.54.1"
        order = "CRC32\nMD5\nSHA-256\nSHA-512\n"
        line = f"yq_linux_amd64  1234abcd {'e' * 32} {'f' * 64} {'9' * 128}\n"
        fetcher = FakeFetcher(
            {f"{base}/checksums_hashes_order": order.encode(), f"{base}/checksums": line.encode()}
        )
        resolved = res("yq", "4.54.1", fetcher)
        assert resolved.url == f"{base}/yq_linux_amd64"
        assert resolved.digest == "f" * 64

    def test_yq_without_sha256_refuses(self):
        base = "https://github.com/mikefarah/yq/releases/download/v4.54.1"
        fetcher = FakeFetcher(
            {
                f"{base}/checksums_hashes_order": b"MD5\n",
                f"{base}/checksums": f"yq_linux_amd64  {'e' * 32}\n".encode(),
            }
        )
        with pytest.raises(ToolchainError, match="no sha256"):
            res("yq", "4.54.1", fetcher)


class TestResolveProtoc:
    def test_vendored_version_needs_no_network(self):
        fetcher = FakeFetcher()
        resolved = res("protoc", "33.0", fetcher)
        assert resolved.digest == toolchains.PROTOC_SHA256["protoc-33.0-linux-x86_64.zip"]
        assert fetcher.calls == []

    def test_vendored_aarch64(self):
        with mock.patch("agentic_ci.toolchains.platform.machine", return_value="aarch64"):
            resolved = res("protoc", "33.0", FakeFetcher())
        assert resolved.filename == "protoc-33.0-linux-aarch_64.zip"

    def test_version_without_manifest_or_table_is_refused(self):
        fetcher = FakeFetcher()
        with pytest.raises(ToolchainError, match="publishes no checksum"):
            res("protoc", "34.2", fetcher)
        assert fetcher.calls == []

    def test_table_holds_only_cross_checked_releases(self):
        versions = {name.split("-")[1] for name in toolchains.PROTOC_SHA256}
        assert versions == {
            "28.3",
            "29.5",
            "30.2",
            "31.1",
            "32.1",
            "33.0",
            "33.6",
            "34.1",
            "35.1",
        }

    def test_newer_versions_use_the_official_manifest(self):
        base = "https://github.com/protocolbuffers/protobuf/releases/download/v36.2"
        manifest = (
            'RELEASED_BINARY_INTEGRITY = {\n  "protoc-36.2-linux-x86_64.zip": "'
            + "a" * 64
            + '",\n  "protoc-36.2-win64.zip": "'
            + "b" * 64
            + '",\n}\n'
        )
        fetcher = FakeFetcher({f"{base}/tool_integrity.bzl": manifest.encode()})
        resolved = res("protoc", "36.2", fetcher)
        assert resolved.digest == "a" * 64
        assert resolved.url == f"{base}/protoc-36.2-linux-x86_64.zip"

    def test_manifest_without_the_file_refuses(self):
        base = "https://github.com/protocolbuffers/protobuf/releases/download/v36.2"
        fetcher = FakeFetcher({f"{base}/tool_integrity.bzl": b"{}"})
        with pytest.raises(ToolchainError, match="no sha256"):
            res("protoc", "36.2", fetcher)

    def test_major_only_is_partial(self):
        with pytest.raises(ToolchainError, match="use an exact MAJOR.MINOR version"):
            res("protoc", "33", FakeFetcher())

    @pytest.mark.parametrize("version", ["33.0.0", "36.2.0"])
    def test_three_parts_are_refused(self, version):
        fetcher = FakeFetcher()
        with pytest.raises(ToolchainError) as excinfo:
            res("protoc", version, fetcher)
        assert str(excinfo.value) == (
            f"protoc: {version} has too many parts; use an exact MAJOR.MINOR version"
        )
        assert fetcher.calls == []


_GNU = "unknown-linux-gnu"
PY_SUMS = "\n".join(
    [
        f"{'1' * 64}  cpython-3.12.14+20260924-x86_64-{_GNU}-install_only.tar.gz",
        f"{'2' * 64}  cpython-3.12.14+20260924-x86_64-{_GNU}-install_only_stripped.tar.gz",
        f"{'3' * 64}  cpython-3.13.9+20260924-x86_64-{_GNU}-install_only.tar.gz",
        f"{'4' * 64}  cpython-3.13.9+20260924-aarch64-{_GNU}-install_only.tar.gz",
        f"{'5' * 64}  cpython-3.14.1+20260924-x86_64-{_GNU}-freethreaded-install_only.tar.gz",
    ]
).encode()


class TestResolvePython:
    def fetcher(self):
        return FakeFetcher({toolchains.PYTHON_SUMS_URL: PY_SUMS})

    def test_minor_picks_the_build_in_the_newest_release(self):
        resolved = res("python", "3.12", self.fetcher())
        assert resolved.version == "3.12.14"
        assert resolved.digest == "1" * 64
        assert resolved.url == (
            "https://github.com/astral-sh/python-build-standalone/releases/download/20260924/"
            "cpython-3.12.14%2B20260924-x86_64-unknown-linux-gnu-install_only.tar.gz"
        )
        assert resolved.filename == (
            "cpython-3.12.14+20260924-x86_64-unknown-linux-gnu-install_only.tar.gz"
        )

    def test_exact(self):
        assert res("python", "3.13.9", self.fetcher()).digest == "3" * 64

    def test_freethreaded_builds_are_not_picked(self):
        with pytest.raises(ToolchainError, match="newest python-build-standalone release"):
            res("python", "3.14", self.fetcher())

    def test_an_exact_pin_the_newest_release_lacks_uses_the_vendored_tag(self):
        assert toolchains.PYTHON_BUILD_TAGS["3.12.3"] == "20240415"
        older = "https://github.com/astral-sh/python-build-standalone/releases/download/20240415"
        sums = "\n".join(
            [
                f"{'6' * 64}  cpython-3.12.3+20240415-x86_64-{_GNU}-install_only.tar.gz",
                f"{'7' * 64}  cpython-3.12.3+20240415-aarch64-{_GNU}-install_only.tar.gz",
            ]
        ).encode()
        fetcher = FakeFetcher({toolchains.PYTHON_SUMS_URL: PY_SUMS, f"{older}/SHA256SUMS": sums})
        resolved = res("python", "3.12.3", fetcher)
        assert resolved.version == "3.12.3"
        assert resolved.digest == "6" * 64
        assert resolved.url == (
            f"{older}/cpython-3.12.3%2B20240415-x86_64-unknown-linux-gnu-install_only.tar.gz"
        )
        assert fetcher.urls() == [toolchains.PYTHON_SUMS_URL, f"{older}/SHA256SUMS"]

    def test_vendored_tag_without_the_build_refuses(self):
        older = "https://github.com/astral-sh/python-build-standalone/releases/download/20240415"
        fetcher = FakeFetcher({toolchains.PYTHON_SUMS_URL: PY_SUMS, f"{older}/SHA256SUMS": b""})
        with pytest.raises(ToolchainError, match="checksum file has no sha256"):
            res("python", "3.12.3", fetcher)

    def test_an_exact_version_in_neither_is_refused(self):
        with pytest.raises(ToolchainError, match="python 3.12.99 is neither in the newest"):
            res("python", "3.12.99", self.fetcher())

    def test_a_partial_version_uses_only_the_newest_release(self):
        with pytest.raises(ToolchainError, match="python 3.8 matches no build in the newest"):
            res("python", "3.8", self.fetcher())

    def test_vendored_tags_are_well_formed(self):
        assert len(toolchains.PYTHON_BUILD_TAGS) > 80
        for version, tag in toolchains.PYTHON_BUILD_TAGS.items():
            assert re.fullmatch(r"3\.[0-9]+\.[0-9]+", version)
            assert re.fullmatch(r"20[0-9]{6}", tag)


class TestResolveInputs:
    @pytest.mark.parametrize(
        "version",
        ["latest", "1.x", "^1.2", ">=1", "1.2.3.4", "", "v1.2", "1" * 10, "1.2." + "3" * 5000],
    )
    def test_garbage_versions(self, version):
        with pytest.raises(ToolchainError, match="must be auto or") as excinfo:
            res("go", version, FakeFetcher())
        assert len(str(excinfo.value)) < 100

    def test_version_parts_are_bounded_in_the_profile_too(self):
        assert toolchains.VERSION_RE.fullmatch("123456789.123456789.123456789")
        with pytest.raises(sandbox_profile.SandboxProfileError):
            parse_profile({"toolchains": {"go": "1.2." + "3" * 10}}, source="central")

    def test_unknown_toolchain(self):
        with pytest.raises(ToolchainError, match="unknown toolchain"):
            res("cobol", "1.80.0", FakeFetcher())

    @pytest.mark.parametrize("name", ["shfmt", "golangci-lint", "helm", "yq", "kustomize", "buf"])
    def test_auto_without_a_source(self, name, tmp_path):
        with pytest.raises(ToolchainError, match=f"{name}: auto is not supported"):
            res(name, "auto", FakeFetcher(), tmp_path)


class TestAuto:
    def test_go_toolchain_line_wins(self, tmp_path):
        (tmp_path / "go.mod").write_text(
            "module x // go 1.1\n\ngo 1.25 // comment\n\ntoolchain go1.26.5\n"
        )
        assert auto_version("go", tmp_path) == AutoVersion("1.26.5", "go.mod")

    def test_go_line(self, tmp_path):
        (tmp_path / "go.mod").write_text("module x\n\ngo 1.26\n")
        assert auto_version("go", tmp_path).version == "1.26"

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            # A toolchain older than the go line: the go command needs the go line.
            ("module x\ngo 1.26.2\ntoolchain go1.25.7\n", "1.26.2"),
            ("module x\ngo 1.26\ntoolchain go1.25.7\n", "1.26"),
            ("module x\ngo 1.26.0\ntoolchain go1.26.0\n", "1.26.0"),
            ("module x\ngo 1.26\ntoolchain go1.26.3\n", "1.26.3"),
            # `toolchain default` names no version.
            ("module x\ngo 1.26\ntoolchain default\n", "1.26"),
        ],
    )
    def test_go_takes_the_higher_of_the_two_lines(self, tmp_path, content, expected):
        (tmp_path / "go.mod").write_text(content)
        assert auto_version("go", tmp_path) == AutoVersion(expected, "go.mod")

    def test_go_auto_resolves_the_partial_go_line(self, tmp_path):
        (tmp_path / "go.mod").write_text("module x\n\ngo 1.26\n")
        resolved, auto = resolve("go", "auto", workdir=tmp_path, fetcher=go_fetcher())
        assert resolved.version == "1.26.5"
        assert auto.source == "go.mod"

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            ("module x\n", "no toolchain or go line"),
            ("module x\ngo 1.27rc1\n", "not digits"),
            ("module x\ntoolchain default\n", "no toolchain or go line"),
            ("module x\ngo 1.26\ntoolchain gccgo-default\n", "not goX.Y.Z"),
            ("module x\ntoolchain go1.26.5-custom\n", "not digits"),
            ("module x\ngo 1." + "9" * 200 + "\n", "not digits"),
        ],
    )
    def test_go_bad_inputs(self, tmp_path, content, message):
        (tmp_path / "go.mod").write_text(content)
        with pytest.raises(ToolchainError, match=message) as excinfo:
            auto_version("go", tmp_path)
        assert "custom" not in str(excinfo.value) and "default" not in str(excinfo.value)

    def test_go_without_go_mod(self, tmp_path):
        with pytest.raises(ToolchainError, match="needs a go.mod"):
            auto_version("go", tmp_path)

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            ({".nvmrc": "v22.18.0\n", "package.json": '{"engines": {"node": "20"}}'}, "22.18.0"),
            ({".nvmrc": "# comment\n22\n"}, "22"),
            ({".node-version": "20.11.1"}, "20.11.1"),
            ({"package.json": '{"engines": {"node": "22.18.0"}}'}, "22.18.0"),
            ({"package.json": '{"engines": {"node": "22"}}'}, "22"),
            ({"package.json": '{"engines": {"node": "^22"}}'}, "22"),
            ({"package.json": '{"engines": {"node": "~ 20"}}'}, "20"),
            ({"package.json": '{"engines": {"node": ">=22"}}'}, "22"),
            ({".tool-versions": "python 3.12.1\nnodejs 22.18.0 20.1.0\n"}, "22.18.0"),
        ],
    )
    def test_node_sources(self, tmp_path, files, expected):
        for name, content in files.items():
            (tmp_path / name).write_text(content)
        assert auto_version("node", tmp_path).version == expected

    @pytest.mark.parametrize(
        ("files", "message"),
        [
            ({".nvmrc": "lts/*\n"}, "from .nvmrc that is not digits"),
            ({"package.json": '{"engines": {"node": ">=22.18.0 <23"}}'}, "not a simple version"),
            ({"package.json": '{"engines": {"node": "^22.18.0"}}'}, "not a simple version"),
            ({"package.json": '{"engines": {"node": 22}}'}, "not a simple version"),
            ({"package.json": "[1, 2]"}, "not a JSON object"),
            ({"package.json": "{nope"}, "not valid JSON"),
            ({}, "found no .nvmrc"),
        ],
    )
    def test_node_bad_inputs(self, tmp_path, files, message):
        for name, content in files.items():
            (tmp_path / name).write_text(content)
        with pytest.raises(ToolchainError, match=message) as excinfo:
            auto_version("node", tmp_path)
        assert "lts" not in str(excinfo.value) and "<23" not in str(excinfo.value)

    def test_pnpm_package_manager(self, tmp_path):
        (tmp_path / "package.json").write_text('{"packageManager": "pnpm@10.34.6"}')
        assert auto_version("pnpm", tmp_path) == AutoVersion("10.34.6", "package.json")

    def test_pnpm_hash_must_match_the_registry(self, tmp_path):
        good = sha512(PNPM_ARCHIVE)
        (tmp_path / "package.json").write_text(
            json.dumps({"packageManager": f"pnpm@10.34.6+sha512.{good}"})
        )
        resolved, auto = resolve("pnpm", "auto", workdir=tmp_path, fetcher=pnpm_fetcher())
        assert auto.sha512 == good and resolved.digest == good

        (tmp_path / "package.json").write_text(
            json.dumps({"packageManager": f"pnpm@10.34.6+sha512.{'0' * 128}"})
        )
        with pytest.raises(ToolchainError, match="packageManager hash .* does not match"):
            resolve("pnpm", "auto", workdir=tmp_path, fetcher=pnpm_fetcher())

    @pytest.mark.parametrize(
        ("package", "message"),
        [
            ({}, "needs packageManager"),
            ({"packageManager": "yarn@4.1.0"}, "is not pnpm@X.Y.Z"),
            ({"packageManager": "pnpm@10"}, "is not pnpm@X.Y.Z"),
            ({"packageManager": "pnpm@10.34.6+sha256.abcd"}, "is not pnpm@X.Y.Z"),
            ({"packageManager": ["pnpm@10.34.6"]}, "is not pnpm@X.Y.Z"),
        ],
    )
    def test_pnpm_bad_inputs(self, tmp_path, package, message):
        (tmp_path / "package.json").write_text(json.dumps(package))
        with pytest.raises(ToolchainError, match=message) as excinfo:
            auto_version("pnpm", tmp_path)
        assert "yarn" not in str(excinfo.value)

    def test_pnpm_without_package_json(self, tmp_path):
        with pytest.raises(ToolchainError, match="needs packageManager"):
            auto_version("pnpm", tmp_path)

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            ({".python-version": "3.12\n", ".tool-versions": "python 3.11.2"}, "3.12"),
            ({".tool-versions": "nodejs 22\npython 3.11.2 3.10.1\n"}, "3.11.2"),
        ],
    )
    def test_python_sources(self, tmp_path, files, expected):
        for name, content in files.items():
            (tmp_path / name).write_text(content)
        assert auto_version("python", tmp_path).version == expected

    @pytest.mark.parametrize(
        ("files", "message"),
        [
            ({".python-version": "pypy3.10\n"}, "not digits"),
            ({".python-version": "\n"}, "not digits"),
            ({".tool-versions": "nodejs 22\n"}, "found no .python-version"),
        ],
    )
    def test_python_bad_inputs(self, tmp_path, files, message):
        for name, content in files.items():
            (tmp_path / name).write_text(content)
        with pytest.raises(ToolchainError, match=message) as excinfo:
            auto_version("python", tmp_path)
        assert "pypy" not in str(excinfo.value)

    def test_oversize_file_is_refused(self, tmp_path):
        (tmp_path / "go.mod").write_text("go 1.26\n" + "/" * toolchains.REPO_FILE_MAX_BYTES)
        with pytest.raises(ToolchainError, match="go.mod is larger than"):
            auto_version("go", tmp_path)

    def test_oversize_package_json_is_refused(self, tmp_path):
        (tmp_path / "package.json").write_text(" " * (toolchains.PACKAGE_JSON_MAX_BYTES + 1))
        with pytest.raises(ToolchainError, match="package.json is larger than"):
            auto_version("pnpm", tmp_path)

    def test_symlink_out_of_the_workdir_is_refused(self, tmp_path):
        outside = tmp_path / "outside.mod"
        outside.write_text("go 1.26\n")
        work = tmp_path / "work"
        work.mkdir()
        (work / "go.mod").symlink_to(outside)
        with pytest.raises(ToolchainError, match="symlink that leads out of the workdir"):
            auto_version("go", work)

    def test_dangling_or_absolute_symlinks_are_refused(self, tmp_path):
        (tmp_path / ".nvmrc").symlink_to("/etc/hostname")
        with pytest.raises(ToolchainError, match="leads out of the workdir"):
            auto_version("node", tmp_path)

    def test_symlink_inside_the_workdir_is_read(self, tmp_path):
        (tmp_path / "real.mod").write_text("go 1.26.5\n")
        (tmp_path / "go.mod").symlink_to("real.mod")
        assert auto_version("go", tmp_path).version == "1.26.5"

    def test_non_regular_files_are_refused(self, tmp_path):
        (tmp_path / "go.mod").mkdir()
        with pytest.raises(ToolchainError, match="go.mod is not a regular file"):
            auto_version("go", tmp_path)

    def test_fifo_is_refused_without_blocking(self, tmp_path):
        os.mkfifo(tmp_path / ".python-version")
        with pytest.raises(ToolchainError, match="not a regular file"):
            auto_version("python", tmp_path)

    def test_non_utf8_is_refused(self, tmp_path):
        (tmp_path / ".nvmrc").write_bytes(b"\xff\xfe22")
        with pytest.raises(ToolchainError, match=".nvmrc is not UTF-8"):
            auto_version("node", tmp_path)


def resolved_shfmt():
    return res("shfmt", "3.12.0", shfmt_fetcher())


class TestFetchArchive:
    def test_download_verifies_and_caches_by_sha256(self, tmp_path):
        fetcher = shfmt_fetcher()
        path, digest = fetch_archive(resolved_shfmt(), fetcher=fetcher, cache_dir=tmp_path)
        assert digest == sha256(SHFMT_BINARY)
        assert path == tmp_path / digest
        assert path.read_bytes() == SHFMT_BINARY
        assert sorted(os.listdir(tmp_path)) == [digest]

    def test_cache_hit_skips_the_download(self, tmp_path):
        resolved = resolved_shfmt()
        fetch_archive(resolved, fetcher=shfmt_fetcher(), cache_dir=tmp_path)
        fetcher = FakeFetcher()
        path, digest = fetch_archive(resolved, fetcher=fetcher, cache_dir=tmp_path)
        assert fetcher.calls == []
        assert path.read_bytes() == SHFMT_BINARY

    def test_a_tampered_cache_entry_is_downloaded_again(self, tmp_path):
        resolved = resolved_shfmt()
        (tmp_path / resolved.digest).write_bytes(b"tampered")
        fetcher = shfmt_fetcher()
        path, _ = fetch_archive(resolved, fetcher=fetcher, cache_dir=tmp_path)
        assert path.read_bytes() == SHFMT_BINARY
        assert resolved.url in fetcher.urls()

    def test_checksum_mismatch_refuses_and_keeps_nothing(self, tmp_path):
        resolved = resolved_shfmt()
        fetcher = shfmt_fetcher()
        fetcher.responses[resolved.url] = b"evil binary"
        with pytest.raises(ToolchainError, match="checksum mismatch; the download was discarded"):
            fetch_archive(resolved, fetcher=fetcher, cache_dir=tmp_path)
        assert os.listdir(tmp_path) == []

    def test_size_mismatch_refuses(self, tmp_path):
        resolved = res("go", "1.26.5", go_fetcher())
        bad = Resolved(**{**resolved.__dict__, "size": 1})
        with pytest.raises(ToolchainError, match="size differs"):
            fetch_archive(bad, fetcher=go_fetcher(), cache_dir=tmp_path)
        assert os.listdir(tmp_path) == []

    def test_interrupted_download_keeps_nothing(self, tmp_path):
        resolved = resolved_shfmt()
        fetcher = shfmt_fetcher()
        fetcher.responses[resolved.url] = ToolchainError("download took longer than 900s")
        with pytest.raises(ToolchainError, match="took longer"):
            fetch_archive(resolved, fetcher=fetcher, cache_dir=tmp_path)
        assert os.listdir(tmp_path) == []

    def test_sha512_sources_get_an_alias_and_hit_the_cache(self, tmp_path):
        resolved = res("pnpm", "10.34.6", pnpm_fetcher())
        path, digest = fetch_archive(resolved, fetcher=pnpm_fetcher(), cache_dir=tmp_path)
        assert path == tmp_path / sha256(PNPM_ARCHIVE)
        assert (tmp_path / "sha512" / sha512(PNPM_ARCHIVE)).read_text() == digest
        fetcher = FakeFetcher()
        assert fetch_archive(resolved, fetcher=fetcher, cache_dir=tmp_path) == (path, digest)
        assert fetcher.calls == []

    def test_archive_download_uses_the_entry_hosts_and_limits(self, tmp_path):
        fetcher = mock.Mock()
        fetcher.stream.return_value = iter([SHFMT_BINARY])
        fetch_archive(resolved_shfmt(), fetcher=fetcher, cache_dir=tmp_path)
        kwargs = fetcher.stream.call_args.kwargs
        assert kwargs["hosts"] == CATALOG["shfmt"].hosts
        assert kwargs["limit"] == toolchains.ARCHIVE_MAX_BYTES
        assert kwargs["timeout"] == toolchains.ARCHIVE_TIMEOUT_SECONDS
        assert kwargs["path_prefix"] == "/mvdan/sh/releases/"

    def test_default_cache_dir(self, monkeypatch, tmp_path):
        monkeypatch.delenv(toolchains.CACHE_ENV, raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert toolchains.default_cache_dir() == tmp_path / ".cache/agentic-ci/toolchains"
        monkeypatch.setenv(toolchains.CACHE_ENV, "/elsewhere")
        assert toolchains.default_cache_dir() == Path("/elsewhere")


class FakeInstaller:
    def __init__(self, present=(), fail=()):
        self.present = set(present)
        self.fail = set(fail)
        self.installed = []
        self.checked = []

    def is_installed(self, plan):
        self.checked.append(plan)
        return plan.name in self.present

    def install(self, plan, archive):
        if plan.name in self.fail:
            raise ToolchainError(
                f"{plan.name}: an archive member points outside; nothing installed"
            )
        self.installed.append((plan, archive.read_bytes()))


class TestProvision:
    def fetcher(self):
        fetcher = go_fetcher()
        fetcher.responses.update(shfmt_fetcher().responses)
        return fetcher

    def test_installs_and_reports(self, tmp_path):
        installer = FakeInstaller()
        result = provision(
            {"go": "1.26", "shfmt": "3.12.0"},
            workdir=tmp_path,
            installer=installer,
            fetcher=self.fetcher(),
            cache_dir=tmp_path / "cache",
        )
        assert [r.to_dict() for r in result.results] == [
            {
                "name": "go",
                "requested": "1.26",
                "resolved": "1.26.5",
                "sha256": sha256(GO_ARCHIVE),
                "status": "installed",
                "reason": "",
            },
            {
                "name": "shfmt",
                "requested": "3.12.0",
                "resolved": "3.12.0",
                "sha256": sha256(SHFMT_BINARY),
                "status": "installed",
                "reason": "",
            },
        ]
        go_plan, go_bytes = installer.installed[0]
        assert go_plan == InstallPlan(
            name="go",
            version="1.26.5",
            sha256=sha256(GO_ARCHIVE),
            archive="tar.gz",
            target="/sandbox/.local/toolchains/go-1.26.5",
        )
        assert go_bytes == GO_ARCHIVE
        shfmt_plan = installer.installed[1][0]
        assert shfmt_plan.archive == "binary" and shfmt_plan.binary == "shfmt"
        assert result.env.path == (
            "/sandbox/.local/toolchains/go-1.26.5/go/bin",
            "/sandbox/.local/toolchains/shfmt-3.12.0/.agentic-ci-bin",
            "/sandbox/.local/gopath/bin",
        )

    def test_a_failure_is_recorded_and_the_rest_continue(self, tmp_path):
        fetcher = self.fetcher()
        fetcher.responses["https://dl.google.com/go/go1.26.5.linux-amd64.tar.gz"] = b"evil"
        installer = FakeInstaller()
        result = provision(
            {"go": "1.26.5", "node": "auto", "shfmt": "3.12.0"},
            workdir=tmp_path,
            installer=installer,
            fetcher=fetcher,
            cache_dir=tmp_path / "cache",
        )
        by_name = {r.name: r for r in result.results}
        assert by_name["go"].status == "failed"
        assert by_name["go"].resolved == "1.26.5"
        assert "checksum mismatch" in by_name["go"].reason
        assert by_name["node"].status == "failed"
        assert "found no .nvmrc" in by_name["node"].reason
        assert by_name["shfmt"].status == "installed"
        assert [p.name for p, _ in installer.installed] == ["shfmt"]
        assert "GOTOOLCHAIN" not in result.env.variables
        assert result.env.path == ("/sandbox/.local/toolchains/shfmt-3.12.0/.agentic-ci-bin",)

    def test_present_toolchains_are_not_installed_again(self, tmp_path):
        installer = FakeInstaller(present={"go"})
        result = provision(
            {"go": "1.26.5"},
            workdir=tmp_path,
            installer=installer,
            fetcher=self.fetcher(),
            cache_dir=tmp_path / "cache",
        )
        assert result.results[0].status == "present"
        assert result.results[0].sha256 == sha256(GO_ARCHIVE)
        assert installer.installed == []
        assert result.env.variables["GOTOOLCHAIN"] == "local"

    def test_present_toolchains_are_checked_before_any_download(self, tmp_path):
        # A cold host cache and a failing archive host do not fail a toolchain
        # the sandbox already has.
        fetcher = self.fetcher()
        archive = "https://dl.google.com/go/go1.26.5.linux-amd64.tar.gz"
        fetcher.responses[archive] = ToolchainError("download failed (ConnectionError)")
        installer = FakeInstaller(present={"go"})
        result = provision(
            {"go": "1.26.5"},
            workdir=tmp_path,
            installer=installer,
            fetcher=fetcher,
            cache_dir=tmp_path / "cache",
        )
        assert result.results[0].status == "present"
        assert archive not in fetcher.urls()
        assert installer.checked[0].sha256 == sha256(GO_ARCHIVE)
        assert not (tmp_path / "cache").exists()

    def test_sha512_toolchains_are_checked_after_the_fetch(self, tmp_path):
        installer = FakeInstaller(present={"pnpm"})
        result = provision(
            {"pnpm": "10.34.6"},
            workdir=tmp_path,
            installer=installer,
            fetcher=pnpm_fetcher(),
            cache_dir=tmp_path / "cache",
        )
        assert result.results[0].status == "present"
        assert result.results[0].sha256 == sha256(PNPM_ARCHIVE)
        assert [p.sha256 for p in installer.checked] == [sha256(PNPM_ARCHIVE)]

    def test_installer_refusal_is_recorded(self, tmp_path):
        result = provision(
            {"shfmt": "3.12.0"},
            workdir=tmp_path,
            installer=FakeInstaller(fail={"shfmt"}),
            fetcher=self.fetcher(),
            cache_dir=tmp_path / "cache",
        )
        assert result.results[0].status == "failed"
        assert "nothing installed" in result.results[0].reason
        assert not result.env

    def test_unexpected_errors_name_only_the_class(self, tmp_path):
        installer = mock.Mock()
        installer.is_installed.side_effect = OSError("/secret/path leaked")
        result = provision(
            {"shfmt": "3.12.0"},
            workdir=tmp_path,
            installer=installer,
            fetcher=self.fetcher(),
            cache_dir=tmp_path / "cache",
        )
        assert result.results[0].reason == "shfmt: unexpected OSError; see the job log"

    def test_outside_names_and_invalid_versions_are_skipped(self, tmp_path):
        result = provision(
            {"cobol": "1.0.0", "go": "latest; rm -rf /"},
            workdir=tmp_path,
            installer=FakeInstaller(),
            fetcher=FakeFetcher(),
            cache_dir=tmp_path / "cache",
        )
        assert [r.to_dict() for r in result.results] == [
            {
                "name": "go",
                "requested": "invalid",
                "resolved": "",
                "sha256": "",
                "status": "failed",
                "reason": "go: invalid version",
            }
        ]

    def test_default_fetcher_and_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv(toolchains.CACHE_ENV, str(tmp_path / "cache"))
        fetcher = self.fetcher()
        fetcher.close = mock.Mock()
        with mock.patch.object(toolchains, "HttpFetcher", return_value=fetcher):
            result = provision({"shfmt": "3.12.0"}, workdir=tmp_path, installer=FakeInstaller())
        assert result.results[0].status == "installed"
        assert (tmp_path / "cache" / sha256(SHFMT_BINARY)).is_file()
        fetcher.close.assert_called_once_with()

    def test_a_given_fetcher_is_not_closed(self, tmp_path):
        fetcher = self.fetcher()
        fetcher.close = mock.Mock()
        provision(
            {"shfmt": "3.12.0"},
            workdir=tmp_path,
            installer=FakeInstaller(),
            fetcher=fetcher,
            cache_dir=tmp_path / "cache",
        )
        fetcher.close.assert_not_called()

    def test_write_results(self, tmp_path):
        path = tmp_path / "_run" / "toolchains.json"
        write_results(path, [ToolchainResult("go", "auto", "1.26.5", "a" * 64, "installed")])
        assert json.loads(path.read_text()) == [
            {
                "name": "go",
                "requested": "auto",
                "resolved": "1.26.5",
                "sha256": "a" * 64,
                "status": "installed",
                "reason": "",
            }
        ]
        assert os.listdir(path.parent) == ["toolchains.json"]


class TestToolchainEnv:
    def resolved(self, name, version):
        return Resolved(name, version, "", "", "sha256", "0" * 64)

    def test_no_toolchains_no_lines(self):
        env = toolchain_env([])
        assert not env
        assert env.script_lines() == []

    def test_go_node_pnpm(self):
        env = toolchain_env(
            [
                self.resolved("go", "1.26.5"),
                self.resolved("node", "22.19.0"),
                self.resolved("pnpm", "10.34.6"),
            ]
        )
        assert env.script_lines() == [
            "export PATH=/sandbox/.local/toolchains/go-1.26.5/go/bin:"
            "/sandbox/.local/toolchains/node-22.19.0/node-v22.19.0-linux-x64/bin:"
            "/sandbox/.local/toolchains/pnpm-10.34.6/.agentic-ci-bin:"
            '/sandbox/.local/gopath/bin:"$PATH"',
            "export GOTOOLCHAIN=local",
            "export GOPATH=/sandbox/.local/gopath",
            "export GOMODCACHE=/sandbox/.cache/go-mod",
            "export GOCACHE=/sandbox/.cache/go-build",
            "export npm_config_cache=/sandbox/.cache/npm",
            "export npm_config_store_dir=/sandbox/.cache/pnpm-store",
            "export pnpm_config_store_dir=/sandbox/.cache/pnpm-store",
        ]

    def test_every_path_is_outside_any_workdir(self):
        env = toolchain_env([self.resolved(n, "1.2.3") for n in CATALOG])
        for value in (*env.path, *env.variables.values()):
            if value.startswith("/"):
                assert value.startswith(("/sandbox/.local/", "/sandbox/.cache/"))

    def test_bin_dirs_per_entry(self):
        env = toolchain_env([self.resolved(n, "1.2.3") for n in ("kustomize", "helm", "python")])
        assert env.path == (
            "/sandbox/.local/toolchains/kustomize-1.2.3",
            "/sandbox/.local/toolchains/helm-1.2.3/linux-amd64",
            "/sandbox/.local/toolchains/python-1.2.3/python/bin",
        )

    def test_to_dict(self):
        env = ToolchainEnv(path=("/a",), variables={"X": "1"})
        assert env.to_dict() == {"path": ["/a"], "variables": {"X": "1"}}


def test_parse_profile_accepts_every_catalog_entry():
    profile = parse_profile({"toolchains": dict.fromkeys(CATALOG, "auto")}, source="central")
    assert set(profile.profile.toolchains) == set(CATALOG)
    assert SandboxProfile(toolchains={"go": "1.26.5"}).toolchains["go"] == "1.26.5"


RUST_ARCHIVE = b"rust archive bytes"
RUST_DIST = "https://static.rust-lang.org/dist"


def rust_channel(version):
    return (
        'manifest-version = "2"\ndate = "2025-03-18"\n'
        '[pkg.cargo]\nversion = "0.86.0 (adf9b6ad1 2025-02-28)"\n'
        f'[pkg.rust]\nversion = "{version} (4eb161250 2025-03-15)"\n'
        "[pkg.rust.target.x86_64-unknown-linux-gnu]\navailable = true\n"
    ).encode()


def rust_fetcher(version="1.86.0", channel=None):
    name = f"rust-{version}-x86_64-unknown-linux-gnu.tar.xz"
    responses = {
        f"{RUST_DIST}/{name}": RUST_ARCHIVE,
        f"{RUST_DIST}/{name}.sha256": f"{sha256(RUST_ARCHIVE)}  {name}\n".encode(),
    }
    if channel is not None:
        minor = ".".join(version.split(".")[:2])
        responses[f"{RUST_DIST}/channel-rust-{minor}.toml"] = channel
    return FakeFetcher(responses)


class TestResolveRust:
    def test_exact_uses_the_sha256_file_and_no_manifest(self):
        fetcher = rust_fetcher()
        resolved = res("rust", "1.86.0", fetcher)
        assert resolved.url == f"{RUST_DIST}/rust-1.86.0-x86_64-unknown-linux-gnu.tar.xz"
        assert resolved.digest == sha256(RUST_ARCHIVE)
        assert fetcher.urls() == [f"{resolved.url}.sha256"]

    def test_minor_resolves_through_the_channel_manifest(self):
        fetcher = rust_fetcher("1.85.1", channel=rust_channel("1.85.1"))
        resolved = res("rust", "1.85", fetcher)
        assert resolved.version == "1.85.1"
        assert fetcher.urls()[0] == f"{RUST_DIST}/channel-rust-1.85.toml"

    @pytest.mark.parametrize(
        "channel",
        [
            rust_channel("1.84.1"),
            b'[pkg.cargo]\nversion = "1.85.1 (x)"\n',
            b"not a manifest",
        ],
    )
    def test_a_manifest_naming_no_matching_release_refuses(self, channel):
        fetcher = rust_fetcher("1.85.1", channel=channel)
        with pytest.raises(ToolchainError, match="channel manifest for 1.85 names no release"):
            res("rust", "1.85", fetcher)

    @pytest.mark.parametrize("version", ["1.67", "1.67.1", "1.56.0", "0.9"])
    def test_versions_before_1_68_are_refused_without_a_download(self, version):
        fetcher = FakeFetcher()
        with pytest.raises(ToolchainError, match="before 1.68 are not supported"):
            res("rust", version, fetcher)
        assert fetcher.calls == []

    def test_1_68_is_the_first_supported_release(self):
        assert res("rust", "1.68.0", rust_fetcher("1.68.0")).version == "1.68.0"
        fetcher = rust_fetcher("1.68.2", channel=rust_channel("1.68.2"))
        assert res("rust", "1.68", fetcher).version == "1.68.2"

    def test_a_major_version_is_refused_without_a_download(self):
        fetcher = FakeFetcher()
        with pytest.raises(ToolchainError, match="use MAJOR.MINOR"):
            res("rust", "1", fetcher)
        assert fetcher.calls == []

    def test_missing_checksum_refuses(self):
        fetcher = rust_fetcher()
        name = "rust-1.86.0-x86_64-unknown-linux-gnu.tar.xz"
        fetcher.responses[f"{RUST_DIST}/{name}.sha256"] = b"0" * 64 + b"  other.tar.xz\n"
        with pytest.raises(ToolchainError, match="no sha256"):
            res("rust", "1.86.0", fetcher)

    def test_aarch64(self):
        name = "rust-1.86.0-aarch64-unknown-linux-gnu.tar.xz"
        fetcher = FakeFetcher({f"{RUST_DIST}/{name}.sha256": f"{'a' * 64}  {name}\n".encode()})
        resolved = resolve("rust", "1.86.0", workdir=Path("."), fetcher=fetcher, arch="aarch64")
        assert resolved[0].filename == name


class TestAutoRust:
    @pytest.mark.parametrize(
        ("files", "expected", "source"),
        [
            (
                {"rust-toolchain.toml": '[toolchain]\nchannel = "1.86.0"  # pin\n'},
                "1.86.0",
                "rust-toolchain.toml",
            ),
            (
                {"rust-toolchain.toml": "[toolchain]\ncomponents = ['clippy']\nchannel = '1.85'\n"},
                "1.85",
                "rust-toolchain.toml",
            ),
            ({"rust-toolchain": "1.84.1\n"}, "1.84.1", "rust-toolchain"),
            (
                {"rust-toolchain.toml": '[toolchain] # pinned\nchannel = "1.86.0"\n'},
                "1.86.0",
                "rust-toolchain.toml",
            ),
            (
                {"rust-toolchain": '[toolchain]\nchannel = "1.83"\n', "rust-toolchain.toml": "x"},
                "1.83",
                "rust-toolchain",
            ),
            ({".tool-versions": "nodejs 22\nrust 1.86.0\n"}, "1.86.0", ".tool-versions"),
            (
                {
                    "rust-toolchain.toml": '[toolchain]\nchannel = "1.82"\n',
                    ".tool-versions": "rust 1",
                },
                "1.82",
                "rust-toolchain.toml",
            ),
        ],
    )
    def test_sources(self, tmp_path, files, expected, source):
        for name, content in files.items():
            (tmp_path / name).write_text(content)
        assert auto_version("rust", tmp_path) == AutoVersion(expected, source)

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            ('[toolchain]\nchannel = "stable"\n', "not digits"),
            ('[toolchain]\nchannel = "nightly-2025-01-01"\n', "not digits"),
            ('[toolchain]\nchannel = "1.86.0-x86_64-unknown-linux-gnu"\n', "not digits"),
            ('[toolchain]\npath = "/opt/rust"\n', "found no \\[toolchain\\] channel"),
            ('[other]\nchannel = "1.86.0"\n[toolchain]\n', "found no \\[toolchain\\] channel"),
        ],
    )
    def test_channels_that_are_not_versions_are_refused(self, tmp_path, content, message):
        (tmp_path / "rust-toolchain.toml").write_text(content)
        with pytest.raises(ToolchainError, match=message) as excinfo:
            auto_version("rust", tmp_path)
        for value in ("stable", "nightly", "x86_64", "/opt/rust"):
            assert value not in str(excinfo.value)

    def test_without_a_source(self, tmp_path):
        with pytest.raises(ToolchainError, match="found no rust-toolchain"):
            auto_version("rust", tmp_path)

    def test_auto_resolves_a_minor_channel(self, tmp_path):
        (tmp_path / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.85"\n')
        fetcher = rust_fetcher("1.85.1", channel=rust_channel("1.85.1"))
        resolved, auto = resolve("rust", "auto", workdir=tmp_path, fetcher=fetcher)
        assert (resolved.version, auto.source) == ("1.85.1", "rust-toolchain.toml")


class TestRustInstall:
    def test_the_plan_lists_the_components_to_merge(self, tmp_path):
        installer = FakeInstaller()
        result = provision(
            {"rust": "1.86.0"},
            workdir=tmp_path,
            installer=installer,
            fetcher=rust_fetcher(),
            cache_dir=tmp_path / "cache",
        )
        assert result.results[0].status == "installed"
        plan, data = installer.installed[0]
        assert data == RUST_ARCHIVE
        assert plan.archive == "tar.xz"
        assert plan.target == "/sandbox/.local/toolchains/rust-1.86.0"
        assert plan.component_root == "rust-1.86.0-x86_64-unknown-linux-gnu"
        assert plan.components == (
            "rustc",
            "rust-std-x86_64-unknown-linux-gnu",
            "cargo",
            "clippy-preview",
            "rustfmt-preview",
        )
        data = json.loads(plan.to_json())
        assert data["components"] == list(plan.components)
        assert data["component_root"] == plan.component_root

    def test_other_plans_have_no_components(self, tmp_path):
        installer = FakeInstaller()
        provision(
            {"shfmt": "3.12.0"},
            workdir=tmp_path,
            installer=installer,
            fetcher=shfmt_fetcher(),
            cache_dir=tmp_path / "cache",
        )
        plan = installer.installed[0][0]
        assert plan.components == () and plan.component_root == ""
        assert json.loads(plan.to_json())["components"] == []

    def test_env(self):
        env = toolchain_env([Resolved("rust", "1.86.0", "", "", "sha256", "0" * 64)])
        assert env.script_lines() == [
            "export PATH=/sandbox/.local/toolchains/rust-1.86.0/bin:"
            '/sandbox/.local/cargo/bin:"$PATH"',
            "export CARGO_HOME=/sandbox/.local/cargo",
            "export CARGO_REGISTRIES_CRATES_IO_PROTOCOL=sparse",
        ]
