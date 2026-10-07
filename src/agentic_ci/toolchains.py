"""Toolchain catalog and host-side provisioning for sandbox profiles.

A sandbox profile's ``toolchains`` map names an entry in :data:`CATALOG` to a
version: exact (``1.26.5``), partial (``22``, ``1.26``) where the tool's
official index lists releases without an API token, or ``auto``, read from
repo files (``go.mod``, ``package.json``, ``.nvmrc``, ``.node-version``,
``.python-version``, ``rust-toolchain``, ``rust-toolchain.toml``,
``.tool-versions``).

Everything here runs on the host and never executes anything from the repo
or from an archive:

1. :func:`resolve` turns the requested version into an exact release, the
   archive URL on the tool's fixed official host and the digest published by
   the tool's official checksum source (a vendored table for protoc versions
   without one). Repo files are read as data, with a size cap, and a symlink
   that leads out of the workdir is refused.
2. :func:`fetch_archive` downloads the archive (size and time limits, HTTPS
   only, redirects only to the entry's own hosts and, on GitHub, its own
   release path), verifies it and stores it in the host cache
   (``~/.cache/agentic-ci/toolchains/``, keyed by sha256, atomic writes).
   Nothing unverified is ever kept or installed.
3. :func:`provision` does both for every toolchain of a profile and hands
   each verified archive to a backend :class:`Installer`, which extracts it
   inside the sandbox; one the installer already has is skipped without a
   download when its official digest is a sha256. A failure is recorded in
   the toolchain's :class:`ToolchainResult` and that toolchain is skipped;
   the others go on.

:class:`ToolchainEnv` holds the variables a provisioned toolchain needs
(``PATH`` prepends, ``GOTOOLCHAIN=local``, Go and npm caches, the pnpm
store, ``CARGO_HOME``), all under ``/sandbox/.local`` or ``/sandbox/.cache`` so they stay out
of the workdir that is downloaded back.

Messages never carry repo-controlled text: they are fixed strings plus
validated versions and file names chosen here. Details (exception classes,
HTTP hosts) go to the job log.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import posixpath
import re
import shlex
import stat
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn, Protocol
from urllib.parse import unquote, urljoin, urlsplit

import requests

from agentic_ci import log

VERSION_RE = re.compile(r"[0-9]{1,9}(\.[0-9]{1,9}){0,2}")
"""A toolchain version (use ``fullmatch``): ``MAJOR[.MINOR[.PATCH]]``, at most 9 digits each.

The bound keeps a repo file from putting an arbitrarily long number into
URLs, results and messages.
"""

AUTO = "auto"

SANDBOX_TOOLCHAIN_ROOT = "/sandbox/.local/toolchains"
"""Where toolchains are extracted inside the sandbox, one ``<name>-<version>`` dir each."""

SANDBOX_GOPATH = "/sandbox/.local/gopath"
SANDBOX_GOMODCACHE = "/sandbox/.cache/go-mod"
SANDBOX_GOCACHE = "/sandbox/.cache/go-build"
SANDBOX_NPM_CACHE = "/sandbox/.cache/npm"
SANDBOX_PNPM_STORE = "/sandbox/.cache/pnpm-store"
SANDBOX_CARGO_HOME = "/sandbox/.local/cargo"

# Directory, inside a toolchain dir, that holds the launchers written for
# tools run with node (pnpm) and single-binary downloads.
WRAPPER_DIR = ".agentic-ci-bin"

CACHE_ENV = "AGENTIC_CI_TOOLCHAIN_CACHE"
"""Environment variable that moves the host cache (default ``~/.cache/agentic-ci/toolchains``)."""

# Download limits. Metadata covers indexes and checksum manifests (the go.dev
# JSON with every release is about 2 MB, pnpm's abbreviated packument about
# 2 MB, a Rust channel manifest about 1 MB); archives cover the largest catalog
# archive (Rust, about 210 MB) with room.
METADATA_MAX_BYTES = 32 << 20
ARCHIVE_MAX_BYTES = 512 << 20
METADATA_TIMEOUT_SECONDS = 120
ARCHIVE_TIMEOUT_SECONDS = 900
_CONNECT_TIMEOUT_SECONDS = 30
_READ_TIMEOUT_SECONDS = 60
_MAX_REDIRECTS = 5
_CHUNK = 1 << 16
# A connection error or a 5xx response before any byte of the body arrived is
# retried after these delays (seconds), within the transfer's time limit.
_RETRY_DELAYS = (2.0, 5.0)

# Repo files read for ``auto``.
REPO_FILE_MAX_BYTES = 256 << 10
PACKAGE_JSON_MAX_BYTES = 1 << 20

_HEX64 = re.compile(r"[0-9a-f]{64}")
# Where github.com sends release downloads. They are allowed only as the last
# hop of a redirect from the entry's own release path on github.com.
_GITHUB_ASSET_HOSTS = frozenset(
    {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
)
_GITHUB_HOSTS = frozenset({"github.com"}) | _GITHUB_ASSET_HOSTS
_GITHUB = "https://github.com"


class ToolchainError(Exception):
    """A toolchain could not be resolved, downloaded, verified or installed.

    The message is a fixed string plus validated versions and file names, so
    it can be shown to users and recorded as a result's ``reason``.
    """


@dataclass(frozen=True)
class CatalogEntry:
    """How to fetch and verify one toolchain.

    ``url`` is the archive URL template (``{version}``, ``{arch}`` and, for
    python, ``{tag}``) on a fixed official host; its last path segment,
    URL-decoded, is the file name the checksum source lists. ``hosts`` is
    every host the entry may contact, redirects included. ``source`` names
    the resolver and checksum source (see :func:`resolve`). ``bin_dirs`` are
    the directories, relative to the extracted toolchain, prepended to
    ``PATH``. ``archive`` is ``tar.gz``, ``tar.xz``, ``zip`` or ``binary`` (a
    single executable stored as ``<WRAPPER_DIR>/<binary>``). ``components``
    (with ``{arch}``) names the directories under the archive's
    ``component_root`` (with ``{version}`` and ``{arch}``) that are merged
    into the toolchain directory, as the tool's own installer would; the rest
    of the archive is not installed. ``node_scripts`` maps a
    launcher name to a script in the archive that the launcher runs with
    ``node``. ``arch`` maps the host machine (``x86_64``, ``aarch64``) to the
    name the tool uses. ``partial`` says whether a partial version resolves.
    ``auto`` names the repo files ``auto`` reads (empty: ``auto`` is an
    error). ``min_parts`` is the fewest version parts an exact version has,
    ``max_parts`` the most a version may have.
    """

    name: str
    url: str
    hosts: frozenset[str]
    source: str
    bin_dirs: tuple[str, ...]
    arch: Mapping[str, str]
    archive: str = "tar.gz"
    checksum_url: str = ""
    binary: str = ""
    node_scripts: Mapping[str, str] = field(default_factory=dict)
    components: tuple[str, ...] = ()
    component_root: str = ""
    partial: bool = False
    auto: str = ""
    min_parts: int = 3
    max_parts: int = 3

    @property
    def path_prefix(self) -> str:
        """``/<owner>/<repo>/releases/`` for an entry on github.com, else ``""``.

        Every github.com URL the entry fetches, redirects included, must start
        with it, so a renamed or transferred repo cannot supply the archive.
        """
        parts = urlsplit(self.url)
        if parts.hostname != "github.com":
            return ""
        owner, repo = parts.path.split("/")[1:3]
        return f"/{owner}/{repo}/releases/"


_AMD = MappingProxyType({"x86_64": "amd64", "aarch64": "arm64"})
_GNU = MappingProxyType({"x86_64": "x86_64", "aarch64": "aarch64"})


def _github(repo: str, path: str) -> str:
    return f"{_GITHUB}/{repo}/releases/download/{path}"


# protoc releases publish no checksum manifest asset before v36.0 (from v36.0
# on, the release's tool_integrity.bzl lists every binary's sha256). Every
# sha256 below was computed from the GitHub release download on 2026-09-29
# and cross-checked against a second source, so none is trust on first use:
# - 28.3 to 33.0: the aspect-build/toolchains_protoc mirror table (sha384);
# - 33.6, 34.1 and 35.1: the integrity table inside protobuf's own release
#   source archive (protobuf-<version>.bazel.tar.gz), whose digest the Bazel
#   Central Registry pins in modules/protobuf/<version>/source.json.
# Any other version before 36.0 is refused.
PROTOC_SHA256: Mapping[str, str] = MappingProxyType(
    {
        "protoc-28.3-linux-x86_64.zip": (
            "0ad949f04a6a174da83cdcbdb36dee0a4925272a5b6d83f79a6bf9852076d53f"
        ),
        "protoc-28.3-linux-aarch_64.zip": (
            "1de522032a8b194002fe35cab86d747848238b5e4de4f99648372079f5b46f9a"
        ),
        "protoc-29.5-linux-x86_64.zip": (
            "a3f094363cd205c6f7af0d1b9305cb4c8517043f265cdb188f098cae93e8b217"
        ),
        "protoc-29.5-linux-aarch_64.zip": (
            "25eb0848ff13a90a0b2d2a3b4a9d2babc7fbfe158f596c00d8c26a21028dd6f5"
        ),
        "protoc-30.2-linux-x86_64.zip": (
            "327e9397c6fb3ea2a542513a3221334c6f76f7aa524a7d2561142b67b312a01f"
        ),
        "protoc-30.2-linux-aarch_64.zip": (
            "a3173ea338ef91b1605b88c4f8120d6c8ccf36f744d9081991d595d0d4352996"
        ),
        "protoc-31.1-linux-x86_64.zip": (
            "96553041f1a91ea0efee963cb16f462f5985b4d65365f3907414c360044d8065"
        ),
        "protoc-31.1-linux-aarch_64.zip": (
            "6c554de11cea04c56ebf8e45b54434019b1cd85223d4bbd25c282425e306ecc2"
        ),
        "protoc-32.1-linux-x86_64.zip": (
            "e9c129c176bb7df02546c4cd6185126ca53c89e7d2f09511e209319704b5dd7e"
        ),
        "protoc-32.1-linux-aarch_64.zip": (
            "4a802ed23d70f7bad7eb19e5a3e724b3aa967250d572cadfd537c1ba939aee6a"
        ),
        "protoc-33.0-linux-x86_64.zip": (
            "d99c011b799e9e412064244f0be417e5d76c9b6ace13a2ac735330fa7d57ad8f"
        ),
        "protoc-33.0-linux-aarch_64.zip": (
            "4b96bc91f8b54d829b8c3ca2207ff1ceb774843321e4fa5a68502faece584272"
        ),
        "protoc-33.6-linux-x86_64.zip": (
            "9c49962391ff8b5754342509efc3885db91dfe11a358e1e98c9a71518f0ad199"
        ),
        "protoc-33.6-linux-aarch_64.zip": (
            "4016cd2ad24c6a2405332a2ef7e04b719240e40d811587c6a1c4bbe514cd668c"
        ),
        "protoc-34.1-linux-x86_64.zip": (
            "af27ea66cd26938fe48587804ca7d4817457a08350021a1c6e23a27ccc8c6904"
        ),
        "protoc-34.1-linux-aarch_64.zip": (
            "31c5e9e3c7bf013cf41fb97765ee255c140024a6b175b6cc9b64beddd7c23ba7"
        ),
        "protoc-35.1-linux-x86_64.zip": (
            "6930ebf62bd4ea607b98fff052596c6ee564b9835b4ce172c75a3f53ae9d91b7"
        ),
        "protoc-35.1-linux-aarch_64.zip": (
            "01bf9d08808c7f96678b63f4bd8efa559bb4f83d5a7a270d5edaf507f9d5d9cf"
        ),
    }
)
"""Vendored, cross-checked sha256 of protoc releases that predate protobuf's checksum manifest."""

# The first protoc release whose GitHub release carries tool_integrity.bzl.
_PROTOC_MANIFEST_SINCE = (36, 0)

GO_INDEX_URL = "https://go.dev/dl/?mode=json&include=all"
NODE_INDEX_URL = "https://nodejs.org/dist/index.json"
NPM_REGISTRY = "https://registry.npmjs.org"
# The newest python-build-standalone release (GitHub redirects to its tag).
PYTHON_SUMS_URL = f"{_GITHUB}/astral-sh/python-build-standalone/releases/latest/download/SHA256SUMS"
# The channel manifest of a Rust minor release; its [pkg.rust] version is the
# newest patch release of that minor version.
RUST_CHANNEL_URL = "https://static.rust-lang.org/dist/channel-rust-{version}.toml"
# The first Rust release whose cargo can use the sparse crates.io index.
RUST_MIN_VERSION = (1, 68)
# The SHA256SUMS of one python-build-standalone release.
PYTHON_TAG_SUMS_URL = _github("astral-sh/python-build-standalone", "{tag}/SHA256SUMS")

# The python-build-standalone release holding the newest build of each CPython
# release from 3.9 on, taken from uv's download metadata (the build uv itself
# picks) on 2026-09-29; every release listed was checked to carry SHA256SUMS
# with both machines' install_only builds of these versions. Only the release
# tag is vendored: the sha256 still comes from that release's SHA256SUMS. It
# keeps an exact version working once the newest release moves past it.
_PYTHON_BUILDS = MappingProxyType(
    {
        "20220227": "3.9.10 3.10.2",
        "20220318": "3.9.11 3.10.3",
        "20220502": "3.9.12",
        "20220528": "3.10.4",
        "20220630": "3.10.5",
        "20220802": "3.9.13 3.10.6",
        "20221002": "3.9.14 3.10.7",
        "20221106": "3.9.15 3.10.8",
        "20230116": "3.10.9 3.11.1",
        "20230507": "3.9.16 3.10.11 3.11.3",
        "20230726": "3.9.17 3.10.12 3.11.4",
        "20230826": "3.11.5",
        "20231002": "3.11.6 3.12.0",
        "20240107": "3.11.7 3.12.1",
        "20240224": "3.9.18 3.10.13 3.11.8 3.12.2",
        "20240415": "3.12.3",
        "20240726": "3.12.4",
        "20240814": "3.9.19 3.10.14 3.11.9 3.12.5",
        "20240909": "3.12.6",
        "20241016": "3.9.20 3.10.15 3.11.10 3.12.7 3.13.0",
        "20250115": "3.12.8 3.13.1",
        "20250317": "3.9.21 3.10.16 3.11.11 3.12.9 3.13.2",
        "20250529": "3.9.22 3.10.17 3.11.12 3.12.10 3.13.3",
        "20250610": "3.13.4",
        "20250723": "3.13.5",
        "20250814": "3.13.6",
        "20250918": "3.13.7",
        "20251007": "3.9.23 3.10.18 3.11.13 3.12.11",
        "20251010": "3.13.8",
        "20251028": "3.9.24",
        "20251031": "3.9.25",
        "20251120": "3.13.9 3.14.0",
        "20251202": "3.13.10 3.14.1",
        "20260127": "3.13.11 3.14.2",
        "20260211": "3.10.19 3.11.14 3.12.12",
        "20260325": "3.13.12 3.14.3",
        "20260414": "3.14.4",
        "20260602": "3.13.13 3.14.5",
        "20260804": "3.14.6",
        "20260805": "3.13.14",
        "20260807": "3.10.20 3.11.15 3.12.13",
        "20260924": "3.10.21 3.11.16 3.12.14 3.13.15 3.14.7",
    }
)
PYTHON_BUILD_TAGS: Mapping[str, str] = MappingProxyType(
    {version: tag for tag, versions in _PYTHON_BUILDS.items() for version in versions.split()}
)
"""python-build-standalone release tag by exact CPython version, for pins the newest lacks."""

_ENTRIES = (
    CatalogEntry(
        name="buf",
        url=_github("bufbuild/buf", "v{version}/buf-Linux-{arch}.tar.gz"),
        checksum_url=_github("bufbuild/buf", "v{version}/sha256.txt"),
        hosts=_GITHUB_HOSTS,
        source="sums",
        bin_dirs=("buf/bin",),
        arch=_GNU,
    ),
    CatalogEntry(
        name="go",
        url="https://dl.google.com/go/go{version}.linux-{arch}.tar.gz",
        hosts=frozenset({"go.dev", "dl.google.com"}),
        source="go",
        bin_dirs=("go/bin",),
        arch=_AMD,
        partial=True,
        auto="go",
    ),
    CatalogEntry(
        name="golangci-lint",
        url=_github(
            "golangci/golangci-lint", "v{version}/golangci-lint-{version}-linux-{arch}.tar.gz"
        ),
        checksum_url=_github(
            "golangci/golangci-lint", "v{version}/golangci-lint-{version}-checksums.txt"
        ),
        hosts=_GITHUB_HOSTS,
        source="sums",
        bin_dirs=("golangci-lint-{version}-linux-{arch}",),
        arch=_AMD,
    ),
    CatalogEntry(
        name="helm",
        url="https://get.helm.sh/helm-v{version}-linux-{arch}.tar.gz",
        checksum_url="https://get.helm.sh/helm-v{version}-linux-{arch}.tar.gz.sha256sum",
        hosts=frozenset({"get.helm.sh"}),
        source="sums",
        bin_dirs=("linux-{arch}",),
        arch=_AMD,
    ),
    CatalogEntry(
        name="kustomize",
        url=_github(
            "kubernetes-sigs/kustomize",
            "kustomize%2Fv{version}/kustomize_v{version}_linux_{arch}.tar.gz",
        ),
        checksum_url=_github("kubernetes-sigs/kustomize", "kustomize%2Fv{version}/checksums.txt"),
        hosts=_GITHUB_HOSTS,
        source="sums",
        bin_dirs=(".",),
        arch=_AMD,
    ),
    CatalogEntry(
        name="node",
        url="https://nodejs.org/dist/v{version}/node-v{version}-linux-{arch}.tar.gz",
        checksum_url="https://nodejs.org/dist/v{version}/SHASUMS256.txt",
        hosts=frozenset({"nodejs.org"}),
        source="node",
        bin_dirs=("node-v{version}-linux-{arch}/bin",),
        arch=MappingProxyType({"x86_64": "x64", "aarch64": "arm64"}),
        partial=True,
        auto="node",
    ),
    CatalogEntry(
        name="pnpm",
        url=NPM_REGISTRY + "/pnpm/-/pnpm-{version}.tgz",
        hosts=frozenset({"registry.npmjs.org"}),
        source="npm",
        bin_dirs=(WRAPPER_DIR,),
        # pnpm is JavaScript: the same archive for every machine.
        arch=MappingProxyType({"x86_64": "", "aarch64": ""}),
        node_scripts=MappingProxyType(
            {"pnpm": "package/bin/pnpm.cjs", "pnpx": "package/bin/pnpx.cjs"}
        ),
        partial=True,
        auto="pnpm",
    ),
    CatalogEntry(
        name="protoc",
        url=_github("protocolbuffers/protobuf", "v{version}/protoc-{version}-linux-{arch}.zip"),
        checksum_url=_github("protocolbuffers/protobuf", "v{version}/tool_integrity.bzl"),
        hosts=_GITHUB_HOSTS,
        source="protoc",
        bin_dirs=("bin",),
        arch=MappingProxyType({"x86_64": "x86_64", "aarch64": "aarch_64"}),
        archive="zip",
        min_parts=2,
        max_parts=2,
    ),
    CatalogEntry(
        name="python",
        url=_github(
            "astral-sh/python-build-standalone",
            "{tag}/cpython-{version}%2B{tag}-{arch}-unknown-linux-gnu-install_only.tar.gz",
        ),
        checksum_url=PYTHON_SUMS_URL,
        hosts=_GITHUB_HOSTS,
        source="python",
        bin_dirs=("python/bin",),
        arch=_GNU,
        partial=True,
        auto="python",
    ),
    CatalogEntry(
        name="rust",
        url="https://static.rust-lang.org/dist/rust-{version}-{arch}-unknown-linux-gnu.tar.xz",
        checksum_url=(
            "https://static.rust-lang.org/dist/rust-{version}-{arch}-unknown-linux-gnu.tar.xz.sha256"
        ),
        hosts=frozenset({"static.rust-lang.org"}),
        source="rust",
        bin_dirs=("bin",),
        arch=_GNU,
        archive="tar.xz",
        # The standalone installer's components without docs, sources or LLVM
        # tools: rustc and its std, cargo, clippy and rustfmt.
        components=(
            "rustc",
            "rust-std-{arch}-unknown-linux-gnu",
            "cargo",
            "clippy-preview",
            "rustfmt-preview",
        ),
        component_root="rust-{version}-{arch}-unknown-linux-gnu",
        partial=True,
        auto="rust",
    ),
    CatalogEntry(
        name="shfmt",
        url=_github("mvdan/sh", "v{version}/shfmt_v{version}_linux_{arch}"),
        checksum_url=_github("mvdan/sh", "v{version}/sha256sums.txt"),
        hosts=_GITHUB_HOSTS,
        source="sums",
        bin_dirs=(WRAPPER_DIR,),
        arch=_AMD,
        archive="binary",
        binary="shfmt",
    ),
    CatalogEntry(
        name="yq",
        url=_github("mikefarah/yq", "v{version}/yq_linux_{arch}"),
        checksum_url=_github("mikefarah/yq", "v{version}/checksums"),
        hosts=_GITHUB_HOSTS,
        source="yq",
        bin_dirs=(WRAPPER_DIR,),
        arch=_AMD,
        archive="binary",
        binary="yq",
    ),
)

CATALOG: Mapping[str, CatalogEntry] = MappingProxyType({e.name: e for e in _ENTRIES})
"""Every toolchain a sandbox profile may request, by name."""


# --- Host machine -----------------------------------------------------------

_MACHINES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


def host_machine() -> str:
    """The sandbox's machine (the host's, OpenShell runs it natively): ``x86_64`` or ``aarch64``.

    Raises :class:`ToolchainError` on any other machine.
    """
    machine = _MACHINES.get(platform.machine().lower())
    if machine is None:
        raise ToolchainError("the host machine is not x86_64 or aarch64")
    return machine


# --- Fetching ---------------------------------------------------------------


class Fetcher(Protocol):
    """Streams an HTTPS resource; the tests replace the real one."""

    def stream(
        self,
        url: str,
        *,
        hosts: frozenset[str],
        limit: int,
        timeout: float,
        headers: Mapping[str, str] | None = None,
        path_prefix: str = "",
    ) -> Iterator[bytes]:
        """Yield the body of *url* in chunks.

        Raises :class:`ToolchainError` when *url* or a redirect breaks
        :func:`_check_url` (with *hosts* and *path_prefix*), the status is
        not 200, the body exceeds *limit* bytes or the whole transfer takes
        longer than *timeout* seconds.
        """
        ...


def _refuse(detail: str) -> NoReturn:
    log.detail("toolchain download refused", detail)
    raise ToolchainError("a download left the tool's official hosts; refused")


def _check_url(
    url: str, hosts: frozenset[str], *, path_prefix: str = "", redirected: bool = False
) -> None:
    """Refuse *url* unless it is plain HTTPS on one of *hosts*.

    On github.com the path must start with *path_prefix* (the entry's
    ``/<owner>/<repo>/releases/``, compared without case, as GitHub does)
    and have no dot segments. The GitHub asset hosts are allowed only as a
    redirect target (*redirected*); the fetcher refuses a redirect from them.
    """
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        _refuse("malformed URL")
    if (
        parts.scheme != "https"
        or host not in hosts
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
    ):
        _refuse(f"{parts.scheme}://{host}")
    if host in _GITHUB_ASSET_HOSTS and not redirected:
        _refuse(f"{host} without a redirect from github.com")
    if host == "github.com" and path_prefix:
        pieces = unquote(parts.path).split("/")
        if any(piece in (".", "..") for piece in pieces) or not parts.path.lower().startswith(
            path_prefix.lower()
        ):
            _refuse("github.com outside the entry's release path")


class HttpFetcher:
    """:class:`Fetcher` over ``requests``, following redirects only within *hosts*."""

    def __init__(self, session: requests.Session | None = None) -> None:
        if session is None:
            session = requests.Session()
            # No ~/.netrc credentials for the official hosts; proxies and the
            # CA bundle still come from the environment (see _request_settings).
            session.trust_env = False
        self._session = session

    @staticmethod
    def _request_settings(url: str) -> dict[str, Any]:
        verify: str | bool = (
            os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE") or True
        )
        return {"proxies": requests.utils.get_environ_proxies(url), "verify": verify}

    def close(self) -> None:
        """Close the HTTP session."""
        self._session.close()

    def _get(
        self, url: str, headers: Mapping[str, str] | None, deadline: float
    ) -> requests.Response:
        """GET *url*, retrying a connection error or a 5xx status (see ``_RETRY_DELAYS``)."""
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                response = self._session.get(
                    url,
                    stream=True,
                    allow_redirects=False,
                    timeout=(_CONNECT_TIMEOUT_SECONDS, _READ_TIMEOUT_SECONDS),
                    headers=dict(headers or {}),
                    **self._request_settings(url),
                )
            except requests.RequestException as exc:
                log.detail("toolchain fetch failed", type(exc).__name__)
                error = ToolchainError(f"download failed ({type(exc).__name__})")
                cause: BaseException | None = exc
            else:
                if response.status_code < 500:
                    return response
                response.close()
                error = ToolchainError(f"download failed with HTTP status {response.status_code}")
                cause = None
            if attempt == len(_RETRY_DELAYS):
                raise error from cause
            delay = _RETRY_DELAYS[attempt]
            if time.monotonic() + delay > deadline:
                raise error from cause
            log.detail("toolchain fetch retry", f"attempt {attempt + 2} in {delay:g}s")
            time.sleep(delay)
        raise AssertionError("unreachable")

    def stream(
        self,
        url: str,
        *,
        hosts: frozenset[str],
        limit: int,
        timeout: float,
        headers: Mapping[str, str] | None = None,
        path_prefix: str = "",
    ) -> Iterator[bytes]:
        deadline = time.monotonic() + timeout
        for hop in range(_MAX_REDIRECTS + 1):
            _check_url(url, hosts, path_prefix=path_prefix, redirected=hop > 0)
            log.detail("toolchain fetch", url.split("?", 1)[0])
            response = self._get(url, headers, deadline)
            with response:
                if response.status_code in (301, 302, 303, 307, 308):
                    if (urlsplit(url).hostname or "").lower() in _GITHUB_ASSET_HOSTS:
                        _refuse("a redirect from a GitHub asset host")
                    location = response.headers.get("Location", "")
                    url = urljoin(url, location)
                    continue
                if response.status_code != 200:
                    raise ToolchainError(f"download failed with HTTP status {response.status_code}")
                declared = response.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > limit:
                    raise ToolchainError(f"download larger than the {limit} byte limit")
                total = 0
                try:
                    for chunk in response.iter_content(_CHUNK):
                        total += len(chunk)
                        if total > limit:
                            raise ToolchainError(f"download larger than the {limit} byte limit")
                        if time.monotonic() > deadline:
                            raise ToolchainError(f"download took longer than {int(timeout)}s")
                        yield chunk
                except requests.RequestException as exc:
                    log.detail("toolchain fetch failed", type(exc).__name__)
                    raise ToolchainError(f"download failed ({type(exc).__name__})") from exc
                return
        raise ToolchainError("download redirected too many times")


def _fetch(
    fetcher: Fetcher, url: str, entry: CatalogEntry, headers: Mapping[str, str] | None = None
) -> bytes:
    return b"".join(
        fetcher.stream(
            url,
            hosts=entry.hosts,
            limit=METADATA_MAX_BYTES,
            timeout=METADATA_TIMEOUT_SECONDS,
            headers=headers,
            path_prefix=entry.path_prefix,
        )
    )


def _fetch_json(
    fetcher: Fetcher, url: str, entry: CatalogEntry, headers: Mapping[str, str] | None = None
) -> Any:
    try:
        return json.loads(_fetch(fetcher, url, entry, headers))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ToolchainError(f"{entry.name}: the release index is not valid JSON") from exc


def _fetch_text(fetcher: Fetcher, url: str, entry: CatalogEntry) -> str:
    try:
        return _fetch(fetcher, url, entry).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolchainError(f"{entry.name}: the checksum file is not UTF-8") from exc


# --- Versions ---------------------------------------------------------------


def _parts(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def _padded(version: str) -> tuple[int, ...]:
    parts = _parts(version)
    return parts + (0,) * (3 - len(parts))


def _pick(candidates: Sequence[str], requested: str, entry: CatalogEntry) -> str:
    """Pick the release for *requested* among *candidates* (exact release names).

    An exact version matches the candidate with the same numbers (``1.20.0``
    matches Go's ``1.20``); a partial one matches the newest candidate that
    starts with its parts.
    """
    wanted = _parts(requested)
    valid = [c for c in candidates if VERSION_RE.fullmatch(c)]
    if len(wanted) >= entry.min_parts:
        exact = [c for c in valid if _padded(c) == _padded(requested)]
        if not exact:
            raise ToolchainError(f"{entry.name} {requested} is not a published release")
        return max(exact, key=lambda c: len(c))
    matching = [c for c in valid if _parts(c)[: len(wanted)] == wanted]
    if not matching:
        raise ToolchainError(f"{entry.name}: no published release matches {requested}")
    return max(matching, key=_padded)


def _require_exact(entry: CatalogEntry, version: str) -> None:
    if len(_parts(version)) < entry.min_parts:
        form = ".".join(["MAJOR", "MINOR", "PATCH"][: entry.min_parts])
        raise ToolchainError(
            f"{entry.name}: partial version {version} cannot be resolved; "
            f"use an exact {form} version"
        )


# --- Resolution -------------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    """An exact release and the digest its official checksum source publishes."""

    name: str
    version: str
    url: str
    filename: str
    algorithm: str
    digest: str
    """Hex digest of the archive in :attr:`algorithm` (``sha256`` or ``sha512``)."""
    size: int | None = None


def _url(entry: CatalogEntry, version: str, arch: str, tag: str = "") -> tuple[str, str]:
    url = entry.url.format(version=version, arch=arch, tag=tag)
    return url, unquote(url.rsplit("/", 1)[1])


def _sums_digest(text: str, filename: str, entry: CatalogEntry) -> str:
    """Find *filename* in a ``sha256sum``-style manifest (``HEX  NAME`` or ``HEX *NAME``)."""
    found = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*") == filename:
            found.add(fields[0].lower())
    if len(found) != 1 or not _HEX64.fullmatch(next(iter(found))):
        raise ToolchainError(f"{entry.name}: the official checksum file has no sha256 for it")
    return found.pop()


def _resolve_sums(entry: CatalogEntry, version: str, arch: str, fetcher: Fetcher) -> Resolved:
    _require_exact(entry, version)
    url, filename = _url(entry, version, arch)
    sums_url = entry.checksum_url.format(version=version, arch=arch)
    digest = _sums_digest(_fetch_text(fetcher, sums_url, entry), filename, entry)
    return Resolved(entry.name, version, url, filename, "sha256", digest)


def _resolve_yq(entry: CatalogEntry, version: str, arch: str, fetcher: Fetcher) -> Resolved:
    """yq lists many digests per file, in the order ``checksums_hashes_order`` names."""
    _require_exact(entry, version)
    url, filename = _url(entry, version, arch)
    base = entry.checksum_url.format(version=version)
    order = _fetch_text(fetcher, base + "_hashes_order", entry).split()
    if order.count("SHA-256") != 1:
        raise ToolchainError(f"{entry.name}: the official checksum file has no sha256 for it")
    column = order.index("SHA-256") + 1
    found = set()
    for line in _fetch_text(fetcher, base, entry).splitlines():
        fields = line.split()
        if len(fields) == len(order) + 1 and fields[0] == filename:
            found.add(fields[column].lower())
    if len(found) != 1 or not _HEX64.fullmatch(next(iter(found))):
        raise ToolchainError(f"{entry.name}: the official checksum file has no sha256 for it")
    return Resolved(entry.name, version, url, filename, "sha256", found.pop())


def _resolve_go(entry: CatalogEntry, version: str, arch: str, fetcher: Fetcher) -> Resolved:
    releases = _fetch_json(fetcher, GO_INDEX_URL, entry)
    if not isinstance(releases, list):
        raise ToolchainError("go: the release index is not a list")
    by_version = {}
    for release in releases:
        if not isinstance(release, dict) or release.get("stable") is not True:
            continue
        name = release.get("version")
        if isinstance(name, str) and name.startswith("go"):
            by_version[name[2:]] = release
    chosen = _pick(list(by_version), version, entry)
    url, filename = _url(entry, chosen, arch)
    for item in by_version[chosen].get("files") or ():
        if (
            isinstance(item, dict)
            and item.get("filename") == filename
            and item.get("kind") == "archive"
        ):
            digest = str(item.get("sha256", "")).lower()
            size = item.get("size")
            if not _HEX64.fullmatch(digest):
                break
            return Resolved(
                entry.name,
                chosen,
                url,
                filename,
                "sha256",
                digest,
                size if isinstance(size, int) and not isinstance(size, bool) else None,
            )
    raise ToolchainError(f"go {chosen}: the release index has no sha256 for this archive")


def _resolve_node(entry: CatalogEntry, version: str, arch: str, fetcher: Fetcher) -> Resolved:
    if len(_parts(version)) < entry.min_parts:
        index = _fetch_json(fetcher, NODE_INDEX_URL, entry)
        if not isinstance(index, list):
            raise ToolchainError("node: the release index is not a list")
        candidates = [
            str(item["version"])[1:]
            for item in index
            if isinstance(item, dict)
            and isinstance(item.get("version"), str)
            and item["version"].startswith("v")
            and f"linux-{arch}" in (item.get("files") or ())
        ]
        version = _pick(candidates, version, entry)
    return _resolve_sums(entry, version, arch, fetcher)


def _npm_integrity(dist: object, entry: CatalogEntry, version: str) -> str:
    """Return the sha512 hex of ``dist.integrity`` after checking ``dist.tarball``."""
    if not isinstance(dist, dict):
        raise ToolchainError(f"{entry.name} {version}: the registry lists no integrity")
    expected_url, _ = _url(entry, version, "")
    if dist.get("tarball") != expected_url:
        raise ToolchainError(f"{entry.name} {version}: the registry tarball URL is unexpected")
    for item in str(dist.get("integrity", "")).split():
        algorithm, _, value = item.partition("-")
        if algorithm != "sha512":
            continue
        try:
            raw = base64.b64decode(value, validate=True)
        except ValueError:
            break
        if len(raw) == 64:
            return raw.hex()
    raise ToolchainError(f"{entry.name} {version}: the registry lists no sha512 integrity")


def _resolve_npm(entry: CatalogEntry, version: str, arch: str, fetcher: Fetcher) -> Resolved:
    package = f"{NPM_REGISTRY}/{entry.name}"
    if len(_parts(version)) < entry.min_parts:
        # The abbreviated packument lists every version with its dist.
        doc = _fetch_json(
            fetcher, package, entry, headers={"Accept": "application/vnd.npm.install-v1+json"}
        )
        versions = doc.get("versions") if isinstance(doc, dict) else None
        if not isinstance(versions, dict):
            raise ToolchainError(f"{entry.name}: the registry lists no versions")
        version = _pick(list(versions), version, entry)
        info = versions[version]
    else:
        info = _fetch_json(fetcher, f"{package}/{version}", entry)
        if not isinstance(info, dict) or info.get("version") != version:
            raise ToolchainError(f"{entry.name} {version} is not a published release")
    digest = _npm_integrity(info.get("dist") if isinstance(info, dict) else None, entry, version)
    url, filename = _url(entry, version, "")
    return Resolved(entry.name, version, url, filename, "sha512", digest)


def _resolve_protoc(entry: CatalogEntry, version: str, arch: str, fetcher: Fetcher) -> Resolved:
    _require_exact(entry, version)
    url, filename = _url(entry, version, arch)
    if filename in PROTOC_SHA256:
        return Resolved(entry.name, version, url, filename, "sha256", PROTOC_SHA256[filename])
    if _padded(version)[:2] < _PROTOC_MANIFEST_SINCE:
        raise ToolchainError(
            f"protoc {version} publishes no checksum and is not in agentic-ci's vendored table"
        )
    text = _fetch_text(fetcher, entry.checksum_url.format(version=version), entry)
    found = set(re.findall(rf'"{re.escape(filename)}"\s*:\s*"([0-9a-f]{{64}})"', text))
    if len(found) != 1:
        raise ToolchainError(f"{entry.name}: the official checksum file has no sha256 for it")
    return Resolved(entry.name, version, url, filename, "sha256", found.pop())


def _python_builds(text: str, arch: str) -> dict[str, tuple[str, str]]:
    """``{version: (sha256, tag)}`` of the install_only builds a ``SHA256SUMS`` lists."""
    pattern = re.compile(
        rf"([0-9a-f]{{64}})\s+\*?cpython-([0-9]+\.[0-9]+\.[0-9]+)\+([0-9]{{8}})-"
        rf"{re.escape(arch)}-unknown-linux-gnu-install_only\.tar\.gz"
    )
    builds = {}
    for line in text.splitlines():
        match = pattern.fullmatch(line.strip())
        if match:
            builds[match.group(2)] = (match.group(1), match.group(3))
    return builds


def _resolve_python(entry: CatalogEntry, version: str, arch: str, fetcher: Fetcher) -> Resolved:
    """python-build-standalone builds.

    The newest release's ``SHA256SUMS`` lists one build per minor version; a
    partial version resolves there. An exact version it lacks is looked up in
    :data:`PYTHON_BUILD_TAGS` and verified against that release's
    ``SHA256SUMS``.
    """
    builds = _python_builds(_fetch_text(fetcher, entry.checksum_url, entry), arch)
    try:
        chosen = _pick(list(builds), version, entry)
    except ToolchainError as exc:
        exact = len(_parts(version)) >= entry.min_parts
        tag = PYTHON_BUILD_TAGS.get(".".join(str(p) for p in _parts(version))) if exact else None
        if tag is None:
            where = (
                "is neither in the newest python-build-standalone release nor in "
                "agentic-ci's table of earlier builds"
                if exact
                else "matches no build in the newest python-build-standalone release"
            )
            raise ToolchainError(f"python {version} {where}") from exc
        sums = _fetch_text(fetcher, PYTHON_TAG_SUMS_URL.format(tag=tag), entry)
        builds = {v: b for v, b in _python_builds(sums, arch).items() if b[1] == tag}
        try:
            chosen = _pick(list(builds), version, entry)
        except ToolchainError:
            raise ToolchainError(
                f"python {version}: the official checksum file has no sha256 for it"
            ) from exc
    digest, tag = builds[chosen]
    url, filename = _url(entry, chosen, arch, tag)
    return Resolved(entry.name, chosen, url, filename, "sha256", digest)


_RUST_PKG_VERSION = re.compile(r'version\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+) ')


def _resolve_rust(entry: CatalogEntry, version: str, arch: str, fetcher: Fetcher) -> Resolved:
    """Rust standalone installers, verified by the ``.sha256`` file next to each.

    A ``MAJOR.MINOR`` version resolves through that minor release's channel
    manifest, whose ``[pkg.rust]`` version is its newest patch release.
    Releases before 1.68 are refused before any download: their cargo cannot
    use the sparse crates.io index, the only one the ``crates`` preset opens.
    """
    wanted = _parts(version)
    if len(wanted) == 1:
        raise ToolchainError("rust: a major version cannot be resolved; use MAJOR.MINOR")
    if wanted[:2] < RUST_MIN_VERSION:
        raise ToolchainError(
            "rust: versions before 1.68 are not supported "
            "(their cargo cannot use the sparse crates.io index)"
        )
    if len(wanted) == 2:
        text = _fetch_text(fetcher, RUST_CHANNEL_URL.format(version=version), entry)
        section = text.partition("\n[pkg.rust]\n")[2].partition("\n[")[0]
        match = _RUST_PKG_VERSION.search(section)
        if match is None or _parts(match.group(1))[:2] != wanted:
            raise ToolchainError(f"rust: the channel manifest for {version} names no release")
        version = match.group(1)
    return _resolve_sums(entry, version, arch, fetcher)


_RESOLVERS: Mapping[str, Callable[[CatalogEntry, str, str, Fetcher], Resolved]] = MappingProxyType(
    {
        "go": _resolve_go,
        "node": _resolve_node,
        "npm": _resolve_npm,
        "protoc": _resolve_protoc,
        "python": _resolve_python,
        "rust": _resolve_rust,
        "sums": _resolve_sums,
        "yq": _resolve_yq,
    }
)


# --- auto ---------------------------------------------------------------------


@dataclass(frozen=True)
class AutoVersion:
    """A version read from a repo file for ``auto``."""

    version: str
    source: str
    """The repo file it came from (a fixed name, safe to show)."""
    sha512: str | None = None
    """pnpm's ``packageManager`` hash, when it pins one."""


def _read_repo_file(workdir: Path, name: str, limit: int) -> str | None:
    """Read *name* at the workdir root as text, or None when it does not exist.

    A symlink is followed only while it stays inside the workdir, and only a
    regular file of at most *limit* bytes is read.
    """
    path = workdir / name
    if not os.path.lexists(path):
        return None
    root = os.path.realpath(workdir)
    real = os.path.realpath(path)
    if os.path.commonpath([root, real]) != root:
        raise ToolchainError(f"{name} is a symlink that leads out of the workdir")
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ToolchainError(f"{name} could not be read ({type(exc).__name__})") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ToolchainError(f"{name} is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ToolchainError(f"{name} is larger than {limit} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolchainError(f"{name} is not UTF-8") from exc


def _checked(version: str, name: str, source: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise ToolchainError(f"{name}: auto read a version from {source} that is not digits")
    return version


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            return line
    return ""


def _tool_versions(workdir: Path, tools: tuple[str, ...]) -> str | None:
    text = _read_repo_file(workdir, ".tool-versions", REPO_FILE_MAX_BYTES)
    if text is None:
        return None
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) >= 2 and fields[0] in tools:
            return fields[1]
    return None


def _auto_go(workdir: Path) -> AutoVersion:
    text = _read_repo_file(workdir, "go.mod", REPO_FILE_MAX_BYTES)
    if text is None:
        raise ToolchainError("go: auto needs a go.mod at the workdir root")
    go_line = toolchain = None
    for line in text.splitlines():
        fields = line.split("//", 1)[0].split()
        if len(fields) == 2 and fields[0] == "toolchain" and toolchain is None:
            toolchain = fields[1]
        elif len(fields) == 2 and fields[0] == "go" and go_line is None:
            go_line = fields[1]
    # The go command needs at least both lines' versions; `toolchain default`
    # names no version, so the go line applies.
    candidates = []
    if toolchain is not None and toolchain != "default":
        if not toolchain.startswith("go"):
            raise ToolchainError("go: auto read a toolchain line from go.mod that is not goX.Y.Z")
        candidates.append(_checked(toolchain[2:], "go", "go.mod"))
    if go_line is not None:
        candidates.append(_checked(go_line, "go", "go.mod"))
    if not candidates:
        raise ToolchainError("go: auto found no toolchain or go line in go.mod")
    # The higher one; on a tie the toolchain line (listed first) is more exact.
    return AutoVersion(max(candidates, key=_padded), "go.mod")


_ENGINE_RANGE = re.compile(r"(\^|~|>=)\s*([0-9]+)")


def _package_json(workdir: Path) -> dict | None:
    text = _read_repo_file(workdir, "package.json", PACKAGE_JSON_MAX_BYTES)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ToolchainError("package.json is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ToolchainError("package.json is not a JSON object")
    return data


def _auto_node(workdir: Path) -> AutoVersion:
    for name in (".nvmrc", ".node-version"):
        text = _read_repo_file(workdir, name, REPO_FILE_MAX_BYTES)
        if text is not None:
            value = _first_line(text)
            return AutoVersion(_checked(value.removeprefix("v"), "node", name), name)
    package = _package_json(workdir)
    engines = package.get("engines") if package is not None else None
    if isinstance(engines, dict) and "node" in engines:
        value = engines["node"]
        if isinstance(value, str):
            value = value.strip()
            if VERSION_RE.fullmatch(value):
                return AutoVersion(value, "package.json")
            match = _ENGINE_RANGE.fullmatch(value)
            if match:
                return AutoVersion(match.group(2), "package.json")
        raise ToolchainError(
            "node: package.json engines.node is not a simple version "
            "(exact, a major, or ^, ~ or >= a major)"
        )
    value = _tool_versions(workdir, ("nodejs", "node"))
    if value is not None:
        return AutoVersion(
            _checked(value.removeprefix("v"), "node", ".tool-versions"), ".tool-versions"
        )
    raise ToolchainError(
        "node: auto found no .nvmrc, .node-version, package.json engines.node or "
        ".tool-versions nodejs line"
    )


_PACKAGE_MANAGER = re.compile(r"pnpm@([0-9]+\.[0-9]+\.[0-9]+)(?:\+sha512\.([0-9a-f]{128}))?")


def _auto_pnpm(workdir: Path) -> AutoVersion:
    package = _package_json(workdir)
    value = package.get("packageManager") if package is not None else None
    if value is None:
        raise ToolchainError("pnpm: auto needs packageManager in package.json")
    match = _PACKAGE_MANAGER.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ToolchainError("pnpm: package.json packageManager is not pnpm@X.Y.Z[+sha512.<hex>]")
    return AutoVersion(match.group(1), "package.json", match.group(2))


def _auto_python(workdir: Path) -> AutoVersion:
    text = _read_repo_file(workdir, ".python-version", REPO_FILE_MAX_BYTES)
    if text is not None:
        return AutoVersion(
            _checked(_first_line(text), "python", ".python-version"), ".python-version"
        )
    value = _tool_versions(workdir, ("python",))
    if value is not None:
        return AutoVersion(_checked(value, "python", ".tool-versions"), ".tool-versions")
    raise ToolchainError("python: auto found no .python-version or .tool-versions python line")


_RUST_CHANNEL = re.compile(r"""channel\s*=\s*(["'])(.*)\1""")


def _rust_channel(text: str, name: str) -> str:
    """The ``[toolchain]`` ``channel`` of a rust-toolchain file (TOML or a bare line)."""
    if not any(line.split("#", 1)[0].strip() == "[toolchain]" for line in text.splitlines()):
        # The legacy rust-toolchain file may hold just the channel.
        return _checked(_first_line(text), "rust", name)
    section = ""
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("["):
            section = line
        elif section == "[toolchain]":
            match = _RUST_CHANNEL.fullmatch(line)
            if match:
                return _checked(match.group(2), "rust", name)
    raise ToolchainError(f"rust: auto found no [toolchain] channel in {name}")


def _auto_rust(workdir: Path) -> AutoVersion:
    # rustup reads rust-toolchain first when both files exist. A channel such
    # as stable or nightly, or a custom toolchain path, is not a version.
    for name in ("rust-toolchain", "rust-toolchain.toml"):
        text = _read_repo_file(workdir, name, REPO_FILE_MAX_BYTES)
        if text is not None:
            return AutoVersion(_rust_channel(text, name), name)
    value = _tool_versions(workdir, ("rust",))
    if value is not None:
        return AutoVersion(_checked(value, "rust", ".tool-versions"), ".tool-versions")
    raise ToolchainError(
        "rust: auto found no rust-toolchain, rust-toolchain.toml or .tool-versions rust line"
    )


_AUTO: Mapping[str, Callable[[Path], AutoVersion]] = MappingProxyType(
    {
        "go": _auto_go,
        "node": _auto_node,
        "pnpm": _auto_pnpm,
        "python": _auto_python,
        "rust": _auto_rust,
    }
)


def auto_version(name: str, workdir: Path) -> AutoVersion:
    """Read the version ``auto`` means for toolchain *name* from repo files in *workdir*."""
    entry = CATALOG[name]
    reader = _AUTO.get(entry.auto)
    if reader is None:
        raise ToolchainError(f"{name}: auto is not supported; pin a version")
    return reader(workdir)


def resolve(
    name: str, requested: str, *, workdir: Path, fetcher: Fetcher, arch: str | None = None
) -> tuple[Resolved, AutoVersion | None]:
    """Resolve *requested* (a version or ``auto``) for catalog entry *name*.

    Returns the release and, for ``auto``, what was read from the repo.
    Raises :class:`ToolchainError`.
    """
    entry = CATALOG.get(name)
    if entry is None:
        raise ToolchainError("unknown toolchain")
    machine = arch or host_machine()
    auto = None
    version = requested
    if requested == AUTO:
        auto = auto_version(name, workdir)
        version = auto.version
    if not VERSION_RE.fullmatch(version):
        raise ToolchainError(f"{name}: version must be auto or MAJOR[.MINOR[.PATCH]] digits")
    if len(_parts(version)) > entry.max_parts:
        form = ".".join(["MAJOR", "MINOR", "PATCH"][: entry.max_parts])
        raise ToolchainError(f"{name}: {version} has too many parts; use an exact {form} version")
    if len(_parts(version)) < entry.min_parts and not entry.partial:
        _require_exact(entry, version)
    resolved = _RESOLVERS[entry.source](entry, version, entry.arch[machine], fetcher)
    if auto is not None and auto.sha512 is not None and auto.sha512 != resolved.digest:
        raise ToolchainError(
            f"{name} {resolved.version}: the packageManager hash in package.json does not "
            "match the registry"
        )
    return resolved, auto


# --- Host cache -------------------------------------------------------------


def default_cache_dir() -> Path:
    """``$AGENTIC_CI_TOOLCHAIN_CACHE``, else ``~/.cache/agentic-ci/toolchains``."""
    override = os.environ.get(CACHE_ENV)
    if override:
        return Path(override)
    return Path.home() / ".cache" / "agentic-ci" / "toolchains"


def _hash_file(path: Path, algorithm: str) -> tuple[str, str]:
    sha256 = hashlib.sha256()
    other = hashlib.new(algorithm)
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            sha256.update(chunk)
            other.update(chunk)
    return sha256.hexdigest(), other.hexdigest()


def write_atomic(path: Path, data: bytes) -> None:
    """Write *data* to *path* atomically, through a fresh ``mkstemp`` name in its directory.

    Nothing left at another name in the directory (a symlink, say) is
    written through; *path* itself is replaced, never followed.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".partial-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _cached(resolved: Resolved, cache_dir: Path) -> tuple[Path, str] | None:
    """The cached, re-verified archive and its sha256, or None."""
    if resolved.algorithm == "sha256":
        sha256 = resolved.digest
    else:
        alias = cache_dir / resolved.algorithm / resolved.digest
        try:
            sha256 = alias.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return None
        if not _HEX64.fullmatch(sha256):
            return None
    blob = cache_dir / sha256
    if not blob.is_file() or blob.is_symlink():
        return None
    actual_sha256, actual = _hash_file(blob, resolved.algorithm)
    if actual_sha256 != sha256 or actual != resolved.digest:
        log.info(f"WARNING: cached {resolved.name} archive does not verify; downloading again")
        blob.unlink(missing_ok=True)
        return None
    return blob, sha256


def fetch_archive(resolved: Resolved, *, fetcher: Fetcher, cache_dir: Path) -> tuple[Path, str]:
    """Return the verified archive for *resolved* from the host cache, downloading it if needed.

    The archive is written to a temporary file and renamed to
    ``<cache_dir>/<sha256>`` only once its digest matches the official one;
    a mismatch deletes it and raises :class:`ToolchainError`. A digest other
    than sha256 (pnpm's sha512) gets an alias file ``<algorithm>/<hex>``
    holding the sha256. Returns the path and the archive's sha256.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    hit = _cached(resolved, cache_dir)
    if hit is not None:
        log.info(f"{resolved.name} {resolved.version}: host cache hit")
        return hit
    fd, tmp_name = tempfile.mkstemp(dir=cache_dir, prefix=".partial-")
    tmp = Path(tmp_name)
    try:
        sha256 = hashlib.sha256()
        other = hashlib.new(resolved.algorithm)
        size = 0
        with os.fdopen(fd, "wb") as fh:
            for chunk in fetcher.stream(
                resolved.url,
                hosts=CATALOG[resolved.name].hosts,
                limit=ARCHIVE_MAX_BYTES,
                timeout=ARCHIVE_TIMEOUT_SECONDS,
                path_prefix=CATALOG[resolved.name].path_prefix,
            ):
                fh.write(chunk)
                sha256.update(chunk)
                other.update(chunk)
                size += len(chunk)
        if other.hexdigest() != resolved.digest:
            raise ToolchainError(
                f"{resolved.name} {resolved.version}: checksum mismatch; the download was discarded"
            )
        if resolved.size is not None and size != resolved.size:
            raise ToolchainError(
                f"{resolved.name} {resolved.version}: size differs from the release index; "
                "the download was discarded"
            )
        digest = sha256.hexdigest()
        blob = cache_dir / digest
        os.replace(tmp, blob)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if resolved.algorithm != "sha256":
        (cache_dir / resolved.algorithm).mkdir(exist_ok=True)
        write_atomic(cache_dir / resolved.algorithm / resolved.digest, digest.encode("ascii"))
    log.info(f"{resolved.name} {resolved.version}: downloaded and verified ({size} bytes)")
    return blob, digest


# --- Sandbox installation -----------------------------------------------------


@dataclass(frozen=True)
class InstallPlan:
    """What a backend extracts where, for one verified archive."""

    name: str
    version: str
    sha256: str
    archive: str
    target: str
    """``<SANDBOX_TOOLCHAIN_ROOT>/<name>-<version>``."""
    binary: str = ""
    node_scripts: Mapping[str, str] = field(default_factory=dict)
    components: tuple[str, ...] = ()
    """Directories under ``component_root`` merged into ``target``; empty: the whole archive."""
    component_root: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "version": self.version,
                "sha256": self.sha256,
                "archive": self.archive,
                "target": self.target,
                "binary": self.binary,
                "wrapper_dir": WRAPPER_DIR,
                "node_scripts": dict(self.node_scripts),
                "components": list(self.components),
                "component_root": self.component_root,
            },
            sort_keys=True,
        )


class Installer(Protocol):
    """Puts verified archives into the sandbox (the OpenShell backend implements it)."""

    def is_installed(self, plan: InstallPlan) -> bool:
        """Whether *plan* is already extracted with the same sha256, so it can be skipped.

        This records what was installed; it does not verify the installed files.
        """
        ...

    def install(self, plan: InstallPlan, archive: Path) -> None:
        """Upload *archive* and extract it to ``plan.target``; raise :class:`ToolchainError`."""
        ...


def sandbox_dir(name: str, version: str) -> str:
    return posixpath.join(SANDBOX_TOOLCHAIN_ROOT, f"{name}-{version}")


def _plan(resolved: Resolved, sha256: str) -> InstallPlan:
    entry = CATALOG[resolved.name]
    arch = entry.arch[host_machine()] if entry.components else ""
    return InstallPlan(
        name=resolved.name,
        version=resolved.version,
        sha256=sha256,
        archive=entry.archive,
        target=sandbox_dir(resolved.name, resolved.version),
        binary=entry.binary,
        node_scripts=entry.node_scripts,
        components=tuple(c.format(arch=arch) for c in entry.components),
        component_root=entry.component_root.format(version=resolved.version, arch=arch),
    )


# --- Environment ----------------------------------------------------------------


@dataclass(frozen=True)
class ToolchainEnv:
    """Variables for the provisioned toolchains, for the env script and the setup shim.

    ``path`` is prepended to ``PATH`` in order; ``variables`` are exported
    as they are.
    """

    path: tuple[str, ...] = ()
    variables: Mapping[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.path or self.variables)

    def script_lines(self) -> list[str]:
        """``export`` lines for a bash script."""
        lines = []
        if self.path:
            lines.append(f'export PATH={shlex.quote(":".join(self.path))}:"$PATH"')
        for key, value in self.variables.items():
            lines.append(f"export {key}={shlex.quote(value)}")
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {"path": list(self.path), "variables": dict(self.variables)}


def toolchain_env(installed: Sequence[Resolved]) -> ToolchainEnv:
    """The :class:`ToolchainEnv` for *installed* toolchains (in the given order)."""
    path: list[str] = []
    variables: dict[str, str] = {}
    names = {r.name for r in installed}
    machine = host_machine() if installed else ""
    for resolved in installed:
        entry = CATALOG[resolved.name]
        base = sandbox_dir(resolved.name, resolved.version)
        for template in entry.bin_dirs:
            subdir = template.format(version=resolved.version, arch=entry.arch[machine])
            path.append(posixpath.normpath(posixpath.join(base, subdir)))
    if "go" in names:
        path.append(posixpath.join(SANDBOX_GOPATH, "bin"))
        variables.update(
            {
                "GOTOOLCHAIN": "local",
                "GOPATH": SANDBOX_GOPATH,
                "GOMODCACHE": SANDBOX_GOMODCACHE,
                "GOCACHE": SANDBOX_GOCACHE,
            }
        )
    if names & {"node", "pnpm"}:
        variables["npm_config_cache"] = SANDBOX_NPM_CACHE
    if "pnpm" in names:
        # pnpm 11 reads pnpm_config_*, earlier releases npm_config_*.
        variables["npm_config_store_dir"] = SANDBOX_PNPM_STORE
        variables["pnpm_config_store_dir"] = SANDBOX_PNPM_STORE
    if "rust" in names:
        path.append(posixpath.join(SANDBOX_CARGO_HOME, "bin"))
        # Sparse is the default from cargo 1.70; 1.68 and 1.69 would clone the
        # git index from github.com instead.
        variables.update(
            {"CARGO_HOME": SANDBOX_CARGO_HOME, "CARGO_REGISTRIES_CRATES_IO_PROTOCOL": "sparse"}
        )
    return ToolchainEnv(path=tuple(dict.fromkeys(path)), variables=variables)


# --- Provisioning ---------------------------------------------------------------


@dataclass(frozen=True)
class ToolchainResult:
    """One toolchain's outcome, as recorded in ``_run/toolchains.json``.

    ``status`` is ``installed``, ``present`` (a reused sandbox already had
    it) or ``failed``; ``reason`` says why it failed.
    """

    name: str
    requested: str
    resolved: str = ""
    sha256: str = ""
    status: str = "failed"
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "requested": self.requested,
            "resolved": self.resolved,
            "sha256": self.sha256,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Provisioned:
    """Result of :func:`provision`."""

    results: tuple[ToolchainResult, ...]
    env: ToolchainEnv


def _install_one(
    name: str,
    requested: str,
    *,
    workdir: Path,
    installer: Installer,
    fetcher: Fetcher,
    cache_dir: Path,
) -> tuple[ToolchainResult, Resolved | None]:
    resolved_version = ""
    sha256 = ""
    try:
        resolved, auto = resolve(name, requested, workdir=workdir, fetcher=fetcher)
        resolved_version = resolved.version
        if auto is not None:
            log.info(f"{name}: auto read {auto.version} from {auto.source}")
        if resolved.algorithm == "sha256" and installer.is_installed(
            _plan(resolved, resolved.digest)
        ):
            # Already in the sandbox: no download, so a cold host cache or a
            # flaky host cannot fail a toolchain that is there.
            sha256 = resolved.digest
            status = "present"
        else:
            # pnpm's digest is sha512, so its sha256 is known only after the fetch.
            archive, sha256 = fetch_archive(resolved, fetcher=fetcher, cache_dir=cache_dir)
            plan = _plan(resolved, sha256)
            if resolved.algorithm != "sha256" and installer.is_installed(plan):
                status = "present"
            else:
                installer.install(plan, archive)
                status = "installed"
    except ToolchainError as exc:
        return (
            ToolchainResult(name, requested, resolved_version, sha256, "failed", str(exc)),
            None,
        )
    except Exception as exc:  # one toolchain must not stop the run
        log.detail(f"toolchain {name} error", type(exc).__name__)
        return (
            ToolchainResult(
                name,
                requested,
                resolved_version,
                sha256,
                "failed",
                f"{name}: unexpected {type(exc).__name__}; see the job log",
            ),
            None,
        )
    return ToolchainResult(name, requested, resolved.version, sha256, status), resolved


def provision(
    toolchains: Mapping[str, str],
    *,
    workdir: Path,
    installer: Installer,
    fetcher: Fetcher | None = None,
    cache_dir: Path | None = None,
) -> Provisioned:
    """Resolve, fetch, verify and install every toolchain in *toolchains*.

    A toolchain that fails is recorded as ``failed`` and skipped; the run goes
    on. Names outside the catalog and versions that are neither ``auto`` nor
    digits are skipped the same way (``parse_profile`` never lets them
    through).
    """
    own_fetcher = None
    if fetcher is None:
        fetcher = own_fetcher = HttpFetcher()
    try:
        return _provision_all(
            toolchains,
            workdir=workdir,
            installer=installer,
            fetcher=fetcher,
            cache_dir=cache_dir or default_cache_dir(),
        )
    finally:
        if own_fetcher is not None:
            own_fetcher.close()


def _provision_all(
    toolchains: Mapping[str, str],
    *,
    workdir: Path,
    installer: Installer,
    fetcher: Fetcher,
    cache_dir: Path,
) -> Provisioned:
    results = []
    installed = []
    for name, requested in toolchains.items():
        if name not in CATALOG:
            log.info("WARNING: a toolchain outside the catalog was skipped")
            continue
        if requested != AUTO and not VERSION_RE.fullmatch(str(requested)):
            results.append(ToolchainResult(name, "invalid", reason=f"{name}: invalid version"))
            continue
        result, resolved = _install_one(
            name,
            requested,
            workdir=workdir,
            installer=installer,
            fetcher=fetcher,
            cache_dir=cache_dir,
        )
        results.append(result)
        if resolved is not None:
            installed.append(resolved)
        suffix = f" ({result.reason})" if result.reason else ""
        shown = result.resolved or "-"
        log.info(f"Toolchain {name}: {requested} -> {shown}: {result.status}{suffix}")
    return Provisioned(results=tuple(results), env=toolchain_env(installed))


def write_results(path: Path, results: Sequence[ToolchainResult]) -> None:
    """Write *results* as a JSON list to *path* (``_run/toolchains.json``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps([r.to_dict() for r in results], indent=2, sort_keys=True) + "\n"
    write_atomic(path, data.encode("utf-8"))
