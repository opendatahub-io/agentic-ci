"""OpenShell credential provider setup."""

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from agentic_ci import log
from agentic_ci.gcp import adc_path as _adc_path
from agentic_ci.gcp import ensure_adc
from agentic_ci.gcp import read_credential_type as _adc_credential_type

PROVIDER_NAME = "ci-gcp"

# Provider profiles imported into the gateway before a provider is created.
# OpenShell v0.0.116-rhaiv.8+ ships no profiles in the gateway binary, so
# ``openshell provider create --type <id>`` fails with "provider profile
# '<id>' not found" until a matching profile is imported. The YAML files are
# vendored copies of the upstream ``providers/`` examples with ``binaries``
# adjusted for the agentic-ci sandbox images.
PROFILES_DIR = Path(__file__).parent / "profiles"
PROFILE_IDS = ("google-vertex-ai", "openai", "anthropic")

# Credential key the gateway refreshes for service-account Vertex auth. The
# sandbox receives it as an opaque placeholder that the supervisor proxy
# resolves on requests to the aiplatform endpoints declared by the profile.
VERTEX_SA_TOKEN_KEY = "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN"

_SECRET_PREFIXES = ("private_key=", f"{VERTEX_SA_TOKEN_KEY}=", "OPENAI_API_KEY=")
_PROVIDER_AUTH_MODES = {
    "google-cloud": "vertex",
    "google-vertex-ai": "vertex",
    "anthropic": "api-key",
    "claude": "api-key",
    "openai": "openai",
    "codex": "openai",
}


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


def setup(auth_mode, env: Mapping[str, str] | None = None):
    """Configure the OpenShell provider.

    Creates a google-vertex-ai provider for Vertex AI auth. The gateway
    keeps the refresh material and mints short-lived access tokens; the
    sandbox only sees a placeholder env var that the supervisor proxy
    resolves on requests to the aiplatform endpoints. (OpenShell
    v0.0.116-rhaiv.8+ removed the GCE metadata emulator the older
    google-cloud provider relied on, so SDK-side ADC discovery no longer
    works inside the sandbox.)

    For user OAuth credentials (from gcloud auth application-default login),
    --from-gcloud-adc handles everything. For service account keys (CI),
    the provider is created bare and refresh is configured separately with
    the service account's email and private key.

    For Anthropic or OpenAI API key auth, creates the corresponding provider.
    """
    credential_env = env if env is not None else os.environ
    if provider_exists():
        # NOTE: switching auth modes (e.g. Vertex → API key) between runs
        # is not supported. The existing provider is reused regardless of
        # its type. To switch, tear down the environment and start fresh.
        print(f"  Provider '{PROVIDER_NAME}' already exists", flush=True)
        return

    ensure_profiles()
    if auth_mode == "api-key":
        _create_anthropic_provider(credential_env)
    elif auth_mode == "openai":
        _create_openai_provider(credential_env)
    else:
        _create_gcp_provider(credential_env)


def _collect_profile_ids(node, ids):
    """Collect every ``id``/``name`` string value from a nested JSON document.

    ``openshell provider list-profiles -o json`` wraps profiles differently
    across versions, so walk the whole document instead of assuming a shape.
    """
    if isinstance(node, dict):
        for key in ("id", "name"):
            value = node.get(key)
            if isinstance(value, str):
                ids.add(value)
        for value in node.values():
            _collect_profile_ids(value, ids)
    elif isinstance(node, list):
        for value in node:
            _collect_profile_ids(value, ids)


def installed_profile_ids():
    """Return the IDs of platform-scoped provider profiles the gateway serves."""
    result = _run(
        ["openshell", "provider", "list-profiles", "--global", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return set()
    try:
        data = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return set()
    ids: set[str] = set()
    _collect_profile_ids(data, ids)
    return ids


def ensure_profiles():
    """Import the vendored provider profiles the gateway does not have yet.

    Import is create-only on the gateway side, so profiles that are already
    present are skipped rather than re-imported.
    """
    installed = installed_profile_ids()
    for profile_id in PROFILE_IDS:
        if profile_id in installed:
            continue
        path = PROFILES_DIR / f"{profile_id}.yaml"
        print(f"  Importing provider profile '{profile_id}'", flush=True)
        _run(
            ["openshell", "provider", "profile", "import", "-f", str(path), "--global"],
            check=True,
        )


def validate_credentials(auth_mode, env: Mapping[str, str] | None = None):
    """Validate credentials supported by the OpenShell provider."""
    credential_env = env if env is not None else os.environ
    if auth_mode == "openai" and not credential_env.get("OPENAI_API_KEY"):
        raise RuntimeError("OpenShell Codex runs require OPENAI_API_KEY")


def provider_exists():
    """Check if the CI provider already exists."""
    result = _run(
        ["openshell", "provider", "get", PROVIDER_NAME],
        capture_output=True,
        timeout=15,
    )
    return result.returncode == 0


def auth_mode():
    """Return the auth mode represented by the persisted provider, if known."""
    result = _run(
        ["openshell", "provider", "list", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return None

    try:
        provider_data = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return None

    if isinstance(provider_data, dict):
        providers = provider_data.get("providers", provider_data.get("items", []))
    else:
        providers = provider_data
    if not isinstance(providers, list):
        return None

    for persisted_provider in providers:
        if not isinstance(persisted_provider, dict):
            continue
        if persisted_provider.get("name") != PROVIDER_NAME:
            continue
        provider_type = persisted_provider.get("type") or persisted_provider.get("provider_type")
        return _PROVIDER_AUTH_MODES.get(provider_type)
    return None


def delete():
    """Delete the persistent OpenShell provider."""
    _run(["openshell", "provider", "delete", PROVIDER_NAME], check=True)


def _create_anthropic_provider(env: Mapping[str, str] | None = None):
    print("  Creating Anthropic API key provider", flush=True)
    kwargs: dict[str, object] = {"check": True}
    if env is not None:
        kwargs["env"] = {**os.environ, **env}
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
        **kwargs,
    )


def _create_openai_provider(env: Mapping[str, str] | None = None):
    credential_env = env if env is not None else os.environ
    api_key = credential_env.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OpenShell Codex runs require OPENAI_API_KEY")

    print("  Creating OpenAI API key provider", flush=True)
    process_env = {**os.environ, **credential_env}
    _run(
        [
            "openshell",
            "provider",
            "create",
            "--name",
            PROVIDER_NAME,
            "--type",
            "openai",
            "--credential",
            "OPENAI_API_KEY",
        ],
        check=True,
        env=process_env,
    )


def _create_gcp_provider(env: Mapping[str, str] | None = None):
    credential_env = env if env is not None else os.environ
    project = credential_env.get(
        "GOOGLE_CLOUD_PROJECT",
        credential_env.get("ANTHROPIC_VERTEX_PROJECT_ID", credential_env.get("GCP_PROJECT_ID", "")),
    )
    region = credential_env.get(
        "VERTEX_LOCATION",
        credential_env.get("CLOUD_ML_REGION", "global"),
    )

    source = ensure_adc(credential_env)
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


def _vertex_config_args(project, region):
    args = []
    if project:
        args.extend(["--config", f"VERTEX_AI_PROJECT_ID={project}"])
    args.extend(["--config", f"VERTEX_AI_REGION={region}"])
    return args


def _create_gcp_provider_adc(project, region):
    """Create a Vertex AI provider from gcloud ADC user credentials."""
    args = [
        "openshell",
        "provider",
        "create",
        "--name",
        PROVIDER_NAME,
        "--type",
        "google-vertex-ai",
        "--from-gcloud-adc",
        *_vertex_config_args(project, region),
    ]
    _run(args, check=True)


def _create_gcp_provider_sa(project, region):
    """Create a Vertex AI provider from a service account key.

    --from-gcloud-adc only accepts user OAuth credentials. For service
    accounts we create the provider with a placeholder token, then
    configure the JWT refresh strategy with the service account's email
    and private key so the gateway can mint access tokens.
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
        "google-vertex-ai",
        "--credential",
        f"{VERTEX_SA_TOKEN_KEY}=placeholder",
        *_vertex_config_args(project, region),
    ]
    _run(args, check=True)

    _run(
        [
            "openshell",
            "provider",
            "refresh",
            "configure",
            "--credential-key",
            VERTEX_SA_TOKEN_KEY,
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
            VERTEX_SA_TOKEN_KEY,
            PROVIDER_NAME,
        ],
        check=True,
    )
