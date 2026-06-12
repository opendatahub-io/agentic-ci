#!/usr/bin/env python3
"""Bump pinned dependency versions in Containerfiles.

For each tool, fetches the latest version and SHA256 checksum, then
updates the corresponding ARG lines in the Containerfiles.

Usage:
    bump-versions.py                 # bump all tools
    bump-versions.py --check         # check for updates without modifying files
    bump-versions.py --tool uv gh    # bump specific tools only
    bump-versions.py --sync          # sync SHA256s for currently pinned versions
    bump-versions.py --renovate-json # generate Renovate custom datasource JSON
"""

import argparse
import datetime
import hashlib
import json
import re
import subprocess
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_CF = REPO_ROOT / "images" / "runner" / "shared" / "Containerfile.base"
CLAUDE_CF = REPO_ROOT / "images" / "runner" / "claude-code" / "Containerfile"
OPENCODE_CF = REPO_ROOT / "images" / "runner" / "opencode" / "Containerfile"
CI_CF = REPO_ROOT / "images" / "ci" / "Containerfile.podman"
OPENSHELL_BASE_CF = REPO_ROOT / "images" / "runner" / "shared" / "Containerfile.openshell-base"
OPENSHELL_CLAUDE_CF = REPO_ROOT / "images" / "runner" / "claude-code" / "Containerfile.openshell"
OPENSHELL_OPENCODE_CF = REPO_ROOT / "images" / "runner" / "opencode" / "Containerfile.openshell"
OPENSHELL_CI_CF = REPO_ROOT / "images" / "ci" / "Containerfile.openshell"
RENOVATE_OUT_DIR = REPO_ROOT / "public" / "renovate"


def _fetch_json(url):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "bump-versions/1.0")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _fetch_text(url):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "bump-versions/1.0")
    with urllib.request.urlopen(req) as resp:
        return resp.read().decode().strip()


def _sha256_of_url(url):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "bump-versions/1.0")
    h = hashlib.sha256()
    with urllib.request.urlopen(req) as resp:
        while chunk := resp.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _github_latest(owner_repo):
    data = _fetch_json(f"https://api.github.com/repos/{owner_repo}/releases/latest")
    return data["tag_name"].lstrip("v")


def _opencode_sha(assets, version):
    """Return SHA256 for opencode-linux-x64.tar.gz.

    Uses the ``digest`` field from the GitHub release asset when available
    (avoids downloading the tarball). Falls back to computing SHA256 from
    the download when the field is absent.
    """
    asset = next((a for a in assets if a["name"] == "opencode-linux-x64.tar.gz"), None)
    if asset is None:
        raise RuntimeError(f"opencode-linux-x64.tar.gz not found in release v{version}")
    digest = asset.get("digest")
    if digest:
        return digest.removeprefix("sha256:")
    url = (
        f"https://github.com/anomalyco/opencode/releases/download/"
        f"v{version}/opencode-linux-x64.tar.gz"
    )
    return _sha256_of_url(url)


def _update_arg(path, arg_name, new_value):
    text = path.read_text()
    pattern = re.compile(rf"^(ARG\s+{re.escape(arg_name)}\s*=\s*)(.+)$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return None
    old_value = match.group(2)
    if old_value == new_value:
        return None
    text = pattern.sub(rf"\g<1>{new_value}", text)
    path.write_text(text)
    return old_value


# ── Tool definitions ────────────────────────────────────────────────────


def bump_uv(check_only):
    version = _github_latest("astral-sh/uv")
    url = (
        f"https://github.com/astral-sh/uv/releases/download/"
        f"{version}/uv-x86_64-unknown-linux-musl.tar.gz"
    )
    sha_text = _fetch_text(url + ".sha256")
    sha = sha_text.split()[0]

    result = {"tool": "uv", "version": version, "sha256": sha}
    if not check_only:
        for cf in [BASE_CF, CI_CF, OPENSHELL_BASE_CF, OPENSHELL_CI_CF]:
            if cf.exists():
                _update_arg(cf, "UV_VERSION", version)
                _update_arg(cf, "UV_SHA256", sha)
    return result


def bump_shellcheck(check_only):
    version = _github_latest("koalaman/shellcheck")
    url = (
        f"https://github.com/koalaman/shellcheck/releases/download/"
        f"v{version}/shellcheck-v{version}.linux.x86_64.tar.xz"
    )
    sha = _sha256_of_url(url)

    result = {"tool": "shellcheck", "version": version, "sha256": sha}
    if not check_only:
        for cf in [BASE_CF, OPENSHELL_BASE_CF]:
            if cf.exists():
                _update_arg(cf, "SHELLCHECK_VERSION", version)
                _update_arg(cf, "SHELLCHECK_SHA256", sha)
    return result


def bump_gh(check_only):
    version = _github_latest("cli/cli")
    url = f"https://github.com/cli/cli/releases/download/v{version}/gh_{version}_linux_amd64.tar.gz"
    sha = _sha256_of_url(url)

    result = {"tool": "gh", "version": version, "sha256": sha}
    if not check_only:
        for cf in [BASE_CF, CI_CF, OPENSHELL_BASE_CF, OPENSHELL_CI_CF]:
            if cf.exists():
                _update_arg(cf, "GH_VERSION", version)
                _update_arg(cf, "GH_SHA256", sha)
    return result


def bump_glab(check_only):
    data = _fetch_json(
        "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/releases/permalink/latest"
    )
    version = data["tag_name"].lstrip("v")
    url = (
        f"https://gitlab.com/gitlab-org/cli/-/releases/"
        f"v{version}/downloads/glab_{version}_linux_amd64.tar.gz"
    )
    sha = _sha256_of_url(url)

    result = {"tool": "glab", "version": version, "sha256": sha}
    if not check_only:
        for cf in [BASE_CF, CI_CF, OPENSHELL_BASE_CF, OPENSHELL_CI_CF]:
            if cf.exists():
                _update_arg(cf, "GLAB_VERSION", version)
                _update_arg(cf, "GLAB_SHA256", sha)
    return result


def bump_claude(check_only):
    version = _fetch_text("https://downloads.claude.ai/claude-code-releases/latest")
    manifest = _fetch_json(
        f"https://downloads.claude.ai/claude-code-releases/{version}/manifest.json"
    )
    sha = manifest["platforms"]["linux-x64"]["checksum"]

    result = {"tool": "claude", "version": version, "sha256": sha}
    if not check_only:
        for cf in [CLAUDE_CF, OPENSHELL_CLAUDE_CF]:
            if cf.exists():
                _update_arg(cf, "CLAUDE_VERSION", version)
                _update_arg(cf, "CLAUDE_SHA256", sha)
    return result


def bump_acli(check_only):
    try:
        out = subprocess.run(
            [
                "dnf",
                "repoquery",
                "--quiet",
                "--latest-limit=1",
                "--repo=acli",
                "--queryformat=%{VERSION}-%{RELEASE}",
                "acli.x86_64",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        version = out.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        version = "(requires dnf with acli repo configured)"

    result = {"tool": "acli", "version": version}
    if not check_only and "~" in version:
        for cf in [CI_CF, OPENSHELL_CI_CF]:
            if cf.exists():
                _update_arg(cf, "ACLI_VERSION", version)
    return result


def bump_opencode(check_only):
    data = _fetch_json("https://api.github.com/repos/anomalyco/opencode/releases/latest")
    version = data["tag_name"].lstrip("v")
    sha = _opencode_sha(data["assets"], version)

    result = {"tool": "opencode", "version": version, "sha256": sha}
    if not check_only:
        for cf in [OPENCODE_CF, OPENSHELL_OPENCODE_CF]:
            if cf.exists():
                _update_arg(cf, "OPENCODE_VERSION", version)
                _update_arg(cf, "OPENCODE_SHA256", sha)
    return result


def bump_gitleaks(check_only):
    version = _github_latest("gitleaks/gitleaks")
    url = (
        f"https://github.com/gitleaks/gitleaks/releases/download/"
        f"v{version}/gitleaks_{version}_linux_x64.tar.gz"
    )
    sha = _sha256_of_url(url)

    result = {"tool": "gitleaks", "version": version, "sha256": sha}
    if not check_only:
        for cf in [CI_CF, OPENSHELL_CI_CF]:
            if cf.exists():
                _update_arg(cf, "GITLEAKS_VERSION", version)
                _update_arg(cf, "GITLEAKS_SHA256", sha)
    return result


def bump_ruff(check_only):
    data = _fetch_json("https://pypi.org/pypi/ruff/json")
    version = data["info"]["version"]

    result = {"tool": "ruff", "version": version}
    if not check_only:
        for cf in [BASE_CF, OPENSHELL_BASE_CF]:
            if cf.exists():
                text = cf.read_text()
                text = re.sub(r"ruff==[\d.]+", f"ruff=={version}", text)
                cf.write_text(text)
    return result


def bump_agentic_ci(check_only):
    data = _fetch_json("https://pypi.org/pypi/agentic-ci/json")
    version = data["info"]["version"]

    result = {"tool": "agentic-ci", "version": version}
    if not check_only:
        # CI images install from local source; only bump runner base images.
        for cf in [BASE_CF, OPENSHELL_BASE_CF]:
            if not cf.exists():
                continue
            text = cf.read_text()
            text = re.sub(r"agentic-ci==[\d.]+", f"agentic-ci=={version}", text)
            cf.write_text(text)
    return result


TOOLS = {
    "uv": bump_uv,
    "shellcheck": bump_shellcheck,
    "gh": bump_gh,
    "glab": bump_glab,
    "gitleaks": bump_gitleaks,
    "claude": bump_claude,
    "opencode": bump_opencode,
    "acli": bump_acli,
    "ruff": bump_ruff,
    "agentic-ci": bump_agentic_ci,
}


def _current_value(path, arg_name):
    if not path.exists():
        return None
    match = re.search(rf"^ARG\s+{re.escape(arg_name)}\s*=\s*(.+)$", path.read_text(), re.MULTILINE)
    return match.group(1) if match else None


# ── Sync functions (update SHA256 for currently pinned versions) ────────


def sync_uv():
    versions = {}
    for cf in [BASE_CF, CI_CF, OPENSHELL_BASE_CF, OPENSHELL_CI_CF]:
        if not cf.exists():
            continue
        version = _current_value(cf, "UV_VERSION")
        if version:
            versions.setdefault(version, []).append(cf)
    if not versions:
        return {"tool": "uv", "skipped": "UV_VERSION not found"}
    for version, files in versions.items():
        url = (
            f"https://github.com/astral-sh/uv/releases/download/"
            f"{version}/uv-x86_64-unknown-linux-musl.tar.gz"
        )
        sha_text = _fetch_text(url + ".sha256")
        sha = sha_text.split()[0]
        for cf in files:
            _update_arg(cf, "UV_SHA256", sha)
    return {"tool": "uv", "versions": list(versions)}


def sync_shellcheck():
    versions = {}
    for cf in [BASE_CF, OPENSHELL_BASE_CF]:
        if not cf.exists():
            continue
        version = _current_value(cf, "SHELLCHECK_VERSION")
        if version:
            versions.setdefault(version, []).append(cf)
    if not versions:
        return {"tool": "shellcheck", "skipped": "SHELLCHECK_VERSION not found"}
    for version, files in versions.items():
        url = (
            f"https://github.com/koalaman/shellcheck/releases/download/"
            f"v{version}/shellcheck-v{version}.linux.x86_64.tar.xz"
        )
        sha = _sha256_of_url(url)
        for cf in files:
            _update_arg(cf, "SHELLCHECK_SHA256", sha)
    return {"tool": "shellcheck", "versions": list(versions)}


def sync_gh():
    versions = {}
    for cf in [BASE_CF, CI_CF, OPENSHELL_BASE_CF, OPENSHELL_CI_CF]:
        if not cf.exists():
            continue
        version = _current_value(cf, "GH_VERSION")
        if version:
            versions.setdefault(version, []).append(cf)
    if not versions:
        return {"tool": "gh", "skipped": "GH_VERSION not found"}
    for version, files in versions.items():
        url = (
            f"https://github.com/cli/cli/releases/download/"
            f"v{version}/gh_{version}_linux_amd64.tar.gz"
        )
        sha = _sha256_of_url(url)
        for cf in files:
            _update_arg(cf, "GH_SHA256", sha)
    return {"tool": "gh", "versions": list(versions)}


def sync_glab():
    versions = {}
    for cf in [BASE_CF, CI_CF, OPENSHELL_BASE_CF, OPENSHELL_CI_CF]:
        if not cf.exists():
            continue
        version = _current_value(cf, "GLAB_VERSION")
        if version:
            versions.setdefault(version, []).append(cf)
    if not versions:
        return {"tool": "glab", "skipped": "GLAB_VERSION not found"}
    for version, files in versions.items():
        url = (
            f"https://gitlab.com/gitlab-org/cli/-/releases/"
            f"v{version}/downloads/glab_{version}_linux_amd64.tar.gz"
        )
        sha = _sha256_of_url(url)
        for cf in files:
            _update_arg(cf, "GLAB_SHA256", sha)
    return {"tool": "glab", "versions": list(versions)}


def sync_gitleaks():
    versions = {}
    for cf in [CI_CF, OPENSHELL_CI_CF]:
        if not cf.exists():
            continue
        version = _current_value(cf, "GITLEAKS_VERSION")
        if version:
            versions.setdefault(version, []).append(cf)
    if not versions:
        return {"tool": "gitleaks", "skipped": "GITLEAKS_VERSION not found"}
    for version, files in versions.items():
        url = (
            f"https://github.com/gitleaks/gitleaks/releases/download/"
            f"v{version}/gitleaks_{version}_linux_x64.tar.gz"
        )
        sha = _sha256_of_url(url)
        for cf in files:
            _update_arg(cf, "GITLEAKS_SHA256", sha)
    return {"tool": "gitleaks", "versions": list(versions)}


def sync_claude():
    versions = {}
    for cf in [CLAUDE_CF, OPENSHELL_CLAUDE_CF]:
        if not cf.exists():
            continue
        version = _current_value(cf, "CLAUDE_VERSION")
        if version:
            versions.setdefault(version, []).append(cf)
    if not versions:
        return {"tool": "claude", "skipped": "CLAUDE_VERSION not found"}
    for version, files in versions.items():
        manifest = _fetch_json(
            f"https://downloads.claude.ai/claude-code-releases/{version}/manifest.json"
        )
        sha = manifest["platforms"]["linux-x64"]["checksum"]
        for cf in files:
            _update_arg(cf, "CLAUDE_SHA256", sha)
    return {"tool": "claude", "versions": list(versions)}


def sync_opencode():
    versions = {}
    for cf in [OPENCODE_CF, OPENSHELL_OPENCODE_CF]:
        if not cf.exists():
            continue
        version = _current_value(cf, "OPENCODE_VERSION")
        if version:
            versions.setdefault(version, []).append(cf)
    if not versions:
        return {"tool": "opencode", "skipped": "OPENCODE_VERSION not found"}
    for version, files in versions.items():
        data = _fetch_json(
            f"https://api.github.com/repos/anomalyco/opencode/releases/tags/v{version}"
        )
        sha = _opencode_sha(data["assets"], version)
        for cf in files:
            _update_arg(cf, "OPENCODE_SHA256", sha)
    return {"tool": "opencode", "versions": list(versions)}


def sync_acli():
    return {"tool": "acli", "skipped": "rpm-based, no checksum to sync"}


def sync_ruff():
    return {"tool": "ruff", "skipped": "pip package, no checksum arg"}


def sync_agentic_ci():
    return {"tool": "agentic-ci", "skipped": "pip package, no checksum arg"}


SYNC_TOOLS = {
    "uv": sync_uv,
    "shellcheck": sync_shellcheck,
    "gh": sync_gh,
    "glab": sync_glab,
    "gitleaks": sync_gitleaks,
    "claude": sync_claude,
    "opencode": sync_opencode,
    "acli": sync_acli,
    "ruff": sync_ruff,
    "agentic-ci": sync_agentic_ci,
}


# ── Renovate custom datasource JSON generators ──────────────────────────


def renovate_json_claude():
    """Return Renovate releases JSON for the claude-code custom datasource."""
    version = _fetch_text("https://downloads.claude.ai/claude-code-releases/latest")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"releases": [{"version": version, "releaseTimestamp": now}]}


def renovate_json_opencode():
    """Return Renovate releases JSON for the opencode custom datasource."""
    version = _github_latest("anomalyco/opencode")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"releases": [{"version": version, "releaseTimestamp": now}]}


def renovate_json_acli():
    """Return Renovate releases JSON for the acli custom datasource."""
    out = subprocess.run(
        [
            "dnf",
            "repoquery",
            "--quiet",
            "--latest-limit=1",
            "--repo=acli",
            "--queryformat=%{VERSION}-%{RELEASE}",
            "acli.x86_64",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    version = out.stdout.strip()
    if not version:
        raise RuntimeError("dnf repoquery returned empty output; acli repo may not be configured")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"releases": [{"version": version, "releaseTimestamp": now}]}


RENOVATE_TOOLS = {
    "claude-code": renovate_json_claude,
    "opencode": renovate_json_opencode,
    "acli": renovate_json_acli,
}


def main():
    parser = argparse.ArgumentParser(description="Bump pinned versions in Containerfiles.")
    parser.add_argument(
        "--check", action="store_true", help="Check for updates without modifying files"
    )
    parser.add_argument(
        "--sync", action="store_true", help="Sync SHA256 checksums for currently pinned versions"
    )
    parser.add_argument(
        "--renovate-json",
        action="store_true",
        dest="renovate_json",
        help="Generate Renovate custom datasource JSON files",
    )
    parser.add_argument(
        "--tool", nargs="+", choices=list(TOOLS), help="Operate on specific tools only"
    )
    args = parser.parse_args()

    if args.renovate_json:
        tools = [t for t in (args.tool or list(RENOVATE_TOOLS)) if t in RENOVATE_TOOLS]
        RENOVATE_OUT_DIR.mkdir(parents=True, exist_ok=True)
        for name in tools:
            print(f"Generating renovate JSON for {name}...", end=" ", flush=True)
            try:
                data = RENOVATE_TOOLS[name]()
                out_file = RENOVATE_OUT_DIR / f"{name}.json"
                out_file.write_text(json.dumps(data, indent=2))
                print(data["releases"][0]["version"])
            except Exception as e:
                print(f"WARNING: {e}")
        return

    if args.sync:
        tools = args.tool or list(SYNC_TOOLS)
        for name in tools:
            fn = SYNC_TOOLS.get(name)
            if fn is None:
                continue
            print(f"Syncing {name}...", end=" ", flush=True)
            try:
                result = fn()
                if "skipped" in result:
                    print(f"skipped ({result['skipped']})")
                else:
                    versions = result.get("versions", [])
                    if len(versions) > 1:
                        print(
                            f"diverged ({', '.join(versions)}) — "
                            "hashes synced per file, bump versions to resolve"
                        )
                    else:
                        print(versions[0] if versions else "done")
            except Exception as e:
                print(f"ERROR: {e}")
        return

    tools = args.tool or list(TOOLS)
    updates = []

    for name in tools:
        print(f"Checking {name}...", end=" ", flush=True)
        try:
            result = TOOLS[name](check_only=args.check)
            print(result["version"])
            updates.append(result)
        except Exception as e:
            print(f"ERROR: {e}")

    if args.check:
        print("\nCurrent → Latest:")
        version_args = {
            "uv": "UV_VERSION",
            "shellcheck": "SHELLCHECK_VERSION",
            "gh": "GH_VERSION",
            "glab": "GLAB_VERSION",
            "gitleaks": "GITLEAKS_VERSION",
            "claude": "CLAUDE_VERSION",
            "opencode": "OPENCODE_VERSION",
            "acli": "ACLI_VERSION",
        }
        pip_packages = {"agentic-ci", "ruff"}
        for u in updates:
            name = u["tool"]
            if name in version_args:
                current = None
                for cf in [
                    BASE_CF,
                    CLAUDE_CF,
                    OPENCODE_CF,
                    CI_CF,
                    OPENSHELL_BASE_CF,
                    OPENSHELL_CLAUDE_CF,
                    OPENSHELL_OPENCODE_CF,
                    OPENSHELL_CI_CF,
                ]:
                    current = _current_value(cf, version_args[name])
                    if current is not None:
                        break
                latest = u["version"]
                changed = " ← UPDATE" if current != latest else ""
                print(f"  {name:15s} {current or '?':20s} → {latest}{changed}")
            elif name in pip_packages:
                current = "?"
                for cf in [BASE_CF, OPENSHELL_BASE_CF, CI_CF, OPENSHELL_CI_CF]:
                    if not cf.exists():
                        continue
                    match = re.search(rf"{re.escape(name)}==([\d.]+)", cf.read_text())
                    if match:
                        current = match.group(1)
                        break
                latest = u["version"]
                changed = " ← UPDATE" if current != latest else ""
                print(f"  {name:15s} {current:20s} → {latest}{changed}")
    else:
        print("\nContainerfiles updated.")


if __name__ == "__main__":
    main()
