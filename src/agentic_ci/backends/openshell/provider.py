"""OpenShell provider setup for GCP credential injection."""

import json
import os
import re
import subprocess

from agentic_ci import log
from agentic_ci.gcp import adc_path as _adc_path
from agentic_ci.gcp import ensure_adc
from agentic_ci.gcp import read_credential_type as _adc_credential_type

PROVIDER_NAME = "ci-gcp"

_SECRET_PREFIXES = ("private_key=", "GCP_SA_ACCESS_TOKEN=")
_CURSOR_HARNESS_NAMES = frozenset({"cursor", "Cursor"})


def _is_cursor_harness(harness_name: str | None) -> bool:
    """True when harness_name is the Cursor slug or legacy display name."""
    return harness_name in _CURSOR_HARNESS_NAMES


def _run(args, **kwargs):
    """Run an openshell command with logging. Redacts secret values."""
    safe = []
    for a in args:
        if any(a.startswith(p) for p in _SECRET_PREFIXES):
            key = a.split("=", 1)[0]
            safe.append(f"{key}=<redacted>")
        else:
            safe.append(a)
    log.detail("exec", " ".join(safe))
    return subprocess.run(args, **kwargs)


def setup(auth_mode, harness_name=None):
    """Configure the OpenShell provider.

    Creates a google-cloud provider that injects GCP credentials into the
    sandbox via the OpenShell supervisor proxy. The agent uses its native
    Vertex AI integration — no inference.local proxy is needed.

    For user OAuth credentials (from gcloud auth application-default login),
    --from-gcloud-adc handles everything. For service account keys (CI),
    the provider is created bare and refresh is configured separately with
    the service account's email and private key.

    For API key auth, creates an anthropic provider (Claude/OpenCode) or
    a generic provider with CURSOR_API_KEY (Cursor).
    """
    details = _get_provider_details()
    if details is not None:
        if _provider_matches(details, auth_mode=auth_mode, harness_name=harness_name):
            print(f"  Provider '{PROVIDER_NAME}' already exists", flush=True)
            return
        print(
            f"  Provider '{PROVIDER_NAME}' exists with mismatched auth settings; recreating",
            flush=True,
        )
        _delete_provider()

    if auth_mode == "api-key" and _is_cursor_harness(harness_name):
        _create_cursor_provider()
    elif auth_mode == "api-key":
        _create_anthropic_provider()
    else:
        _create_gcp_provider()


def provider_exists():
    """Check if the CI provider already exists."""
    return _get_provider_details() is not None


def _get_provider_details():
    """Fetch provider details, preferring JSON output when supported."""
    result = _run(
        ["openshell", "provider", "get", PROVIDER_NAME, "--output", "json"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode == 0:
        parsed = _parse_provider_output(result.stdout)
        if parsed is not None:
            return parsed

    result = _run(
        ["openshell", "provider", "get", PROVIDER_NAME],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return None

    parsed = _parse_provider_output(result.stdout)
    if parsed is not None:
        return parsed
    return {"raw": result.stdout or ""}


def _parse_provider_output(stdout):
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


def _desired_provider_type(auth_mode, harness_name=None):
    if auth_mode == "api-key" and _is_cursor_harness(harness_name):
        return "generic"
    if auth_mode == "api-key":
        return "anthropic"
    return "google-cloud"


def _extract_provider_type(provider_details):
    if isinstance(provider_details, dict):
        for key in ("type", "provider_type", "providerType"):
            val = provider_details.get(key)
            if isinstance(val, str) and val:
                return val
        for val in provider_details.values():
            found = _extract_provider_type(val)
            if found:
                return found
        return None
    if isinstance(provider_details, list):
        for item in provider_details:
            found = _extract_provider_type(item)
            if found:
                return found
        return None
    if isinstance(provider_details, str):
        match = re.search(r'"type"\s*:\s*"([^"]+)"', provider_details)
        if match:
            return match.group(1)
        match = re.search(r"(?im)^\s*type\s*[:=]\s*['\"]?([A-Za-z0-9_-]+)", provider_details)
        if match:
            return match.group(1)
    return None


def _provider_has_credential(provider_details, credential_key):
    if isinstance(provider_details, dict) and "raw" in provider_details:
        return False
    return credential_key in json.dumps(provider_details, sort_keys=True)


def _provider_matches(provider_details, auth_mode, harness_name=None):
    desired_type = _desired_provider_type(auth_mode, harness_name=harness_name)
    existing_type = _extract_provider_type(provider_details)
    if existing_type != desired_type:
        return False
    if desired_type == "generic":
        return _provider_has_credential(provider_details, "CURSOR_API_KEY")
    if desired_type == "anthropic":
        return _provider_has_credential(provider_details, "ANTHROPIC_API_KEY")
    return True


def _delete_provider():
    print(f"  Deleting provider '{PROVIDER_NAME}'", flush=True)
    _run(["openshell", "provider", "delete", PROVIDER_NAME], check=True)


def _create_cursor_provider():
    print("  Creating Cursor API key provider (generic)", flush=True)
    _run(
        [
            "openshell",
            "provider",
            "create",
            "--name",
            PROVIDER_NAME,
            "--type",
            "generic",
            "--credential",
            "CURSOR_API_KEY",
        ],
        check=True,
    )


def _create_anthropic_provider():
    print("  Creating Anthropic API key provider", flush=True)
    _run(
        [
            "openshell",
            "provider",
            "create",
            "--name",
            PROVIDER_NAME,
            "--type",
            "anthropic",
            "--credential",
            "ANTHROPIC_API_KEY",
        ],
        check=True,
    )


def _create_gcp_provider():
    project = os.environ.get(
        "GOOGLE_CLOUD_PROJECT",
        os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", os.environ.get("GCP_PROJECT_ID", "")),
    )
    region = os.environ.get(
        "VERTEX_LOCATION",
        os.environ.get("CLOUD_ML_REGION", "global"),
    )

    source = ensure_adc()
    cred_type = _adc_credential_type()

    print(
        f"  Creating GCP provider "
        f"(project={project}, region={region}, creds={cred_type}, source={source})",
        flush=True,
    )

    if cred_type == "service_account":
        _create_gcp_provider_sa(project, region)
    else:
        _create_gcp_provider_adc(project, region)


def _create_gcp_provider_adc(project, region):
    """Create a GCP provider from gcloud ADC user credentials."""
    args = [
        "openshell",
        "provider",
        "create",
        "--name",
        PROVIDER_NAME,
        "--type",
        "google-cloud",
        "--from-gcloud-adc",
    ]
    if project:
        args.extend(["--config", f"project_id={project}"])
    args.extend(["--config", f"region={region}"])
    _run(args, check=True)


def _create_gcp_provider_sa(project, region):
    """Create a GCP provider from a service account key.

    --from-gcloud-adc only accepts user OAuth credentials. For service
    accounts we create the provider bare, then configure the JWT refresh
    strategy with the service account's email and private key so the
    gateway can mint access tokens.
    """
    adc = _adc_path()
    with open(adc) as f:
        sa = json.load(f)

    client_email = sa["client_email"]
    private_key = sa["private_key"]

    args = [
        "openshell",
        "provider",
        "create",
        "--name",
        PROVIDER_NAME,
        "--type",
        "google-cloud",
        "--credential",
        "GCP_SA_ACCESS_TOKEN=placeholder",
    ]
    if project:
        args.extend(["--config", f"project_id={project}"])
    args.extend(["--config", f"region={region}"])
    args.extend(["--config", f"service_account_email={client_email}"])
    _run(args, check=True)

    _run(
        [
            "openshell",
            "provider",
            "refresh",
            "configure",
            "--credential-key",
            "GCP_SA_ACCESS_TOKEN",
            "--strategy",
            "google-service-account-jwt",
            "--material",
            f"client_email={client_email}",
            "--material",
            f"private_key={private_key}",
            "--secret-material-key",
            "private_key",
            PROVIDER_NAME,
        ],
        check=True,
    )

    # The refresh worker runs on a 60s interval. Request an immediate
    # rotation so the initial access token is minted before the agent starts.
    rotate_token()


def rotate_token():
    """Force-rotate the gateway's GCP access token.

    The OpenShell gateway refresh worker mints tokens on a 60s interval,
    but can let a token lapse around the hourly expiry boundary when a
    transient mint failure is only retried after 60s while the old token
    keeps aging. Calling this proactively keeps a fresh token in play.

    Raises subprocess.CalledProcessError on failure.
    """
    _run(
        [
            "openshell",
            "provider",
            "refresh",
            "rotate",
            "--credential-key",
            "GCP_SA_ACCESS_TOKEN",
            PROVIDER_NAME,
        ],
        check=True,
    )
