"""OpenShell credential provider setup."""

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from importlib.resources import as_file, files

import yaml

from agentic_ci import log
from agentic_ci.gcp import adc_path as _adc_path
from agentic_ci.gcp import ensure_adc
from agentic_ci.gcp import read_credential_type as _adc_credential_type

PROVIDER_NAME = "ci-gcp"

# Provider profiles agentic-ci registers with the gateway before creating an
# API key provider. OpenShell's builtin "openai" and "anthropic" profiles bind
# the API host to curl, so any sandbox process could spend the key; these
# profiles bind it to agent binaries instead. An agent binary wrapper can
# still spend the key; see the YAML files for details.
OPENAI_PROFILE_ID = "agentic-ci-openai"
ANTHROPIC_PROFILE_ID = "agentic-ci-anthropic"
_PROFILE_PACKAGE = "agentic_ci.backends.openshell"
_PROFILE_DIR = "profiles"

_SECRET_PREFIXES = ("private_key=", "GCP_SA_ACCESS_TOKEN=", "OPENAI_API_KEY=")
_PROVIDER_AUTH_MODES = {
    "google-cloud": "vertex",
    "google-vertex-ai": "vertex",
    ANTHROPIC_PROFILE_ID: "api-key",
    "claude": "api-key",
    OPENAI_PROFILE_ID: "openai",
    "codex": "openai",
    # Providers that earlier agentic-ci releases created from the builtin
    # profiles. Reporting a different auth mode makes OpenShellBackend.setup()
    # delete and recreate the sandbox and provider from agentic-ci's profile
    # instead of reusing one whose key curl can spend.
    "anthropic": "api-key-builtin-profile",
    "openai": "openai-builtin-profile",
}

# Auth modes whose credential reaches the sandbox only through the env
# script. OpenShell has no provider profile for a Claude subscription OAuth
# token, so no provider is created or attached for these modes.
_PROVIDERLESS_AUTH_MODES = frozenset({"oauth"})

# Credentials that reach the agent only through the provider, keyed by auth
# mode. A provider kept from an earlier run still holds the value captured
# when it was created, so these are stored again whenever an existing
# provider is reused, and their fingerprint is part of the sandbox identity
# so a rotated key recreates the sandbox. The Anthropic key is not listed:
# the sandbox env script still exports it on every run.
_PROVIDER_ONLY_CREDENTIALS = {"openai": "OPENAI_API_KEY"}


def requires_provider(auth_mode: str | None) -> bool:
    """Return whether *auth_mode* is backed by the CI provider."""
    return auth_mode not in _PROVIDERLESS_AUTH_MODES


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

    Creates a google-cloud provider that injects GCP credentials into the
    sandbox via the OpenShell supervisor proxy. The agent uses its native
    Vertex AI integration — no inference.local proxy is needed.

    For user OAuth credentials (from gcloud auth application-default login),
    --from-gcloud-adc handles everything. For service account keys (CI),
    the provider is created bare and refresh is configured separately with
    the service account's email and private key.

    For Anthropic or OpenAI API key auth, creates the corresponding provider.

    For a Claude subscription OAuth token, creates nothing: the token is
    exported by the sandbox env script instead.
    """
    if not requires_provider(auth_mode):
        print(f"  No provider needed for {auth_mode} auth", flush=True)
        return
    credential_env = env if env is not None else os.environ
    if provider_exists():
        # NOTE: switching auth modes (e.g. Vertex → API key) between runs
        # is not supported. The existing provider is reused regardless of
        # its type. To switch, tear down the environment and start fresh.
        print(f"  Provider '{PROVIDER_NAME}' already exists", flush=True)
        refresh_credentials(auth_mode, credential_env)
    elif auth_mode == "api-key":
        _create_anthropic_provider(credential_env)
    elif auth_mode == "openai":
        _create_openai_provider(credential_env)
    else:
        _create_gcp_provider(credential_env)


def credential_fingerprint(auth_mode, env: Mapping[str, str] | None = None):
    """Return a short digest of the provider-only credential for *auth_mode*.

    Returns None for auth modes whose credential also reaches the sandbox
    through the env script. The digest is stored in the local sandbox
    identity file, so it is truncated and never the key itself.
    """
    credential_key = _PROVIDER_ONLY_CREDENTIALS.get(auth_mode)
    if credential_key is None:
        return None
    credential_env = env if env is not None else os.environ
    value = credential_env.get(credential_key, "")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def refresh_credentials(auth_mode, env: Mapping[str, str] | None = None):
    """Store the current credential in the existing provider for *auth_mode*.

    Only auth modes whose credential the agent gets solely through the
    provider placeholder are refreshed; the others return without a call.
    Without this, a provider kept across runs on a running gateway keeps
    sending the key it was created with after the caller rotates it. A
    sandbox that already exists picks the update up only after a delay, so
    the caller recreates the sandbox when the key changed (see
    credential_fingerprint).
    """
    credential_key = _PROVIDER_ONLY_CREDENTIALS.get(auth_mode)
    if credential_key is None:
        return
    credential_env = env if env is not None else os.environ
    if not credential_env.get(credential_key):
        raise RuntimeError(f"OpenShell {auth_mode} runs require {credential_key}")
    print(f"  Refreshing {credential_key} in provider '{PROVIDER_NAME}'", flush=True)
    _run(
        ["openshell", "provider", "update", PROVIDER_NAME, "--credential", credential_key],
        check=True,
        env={**os.environ, **credential_env},
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


def _profile_matches(wanted, current):
    """Return whether every field of *wanted* has the same value in *current*.

    ``openshell provider profile export`` adds server-side fields
    (``resource_version``, ``source``, ``scope``) and defaults, so only the
    fields agentic-ci sets are compared.
    """
    if isinstance(wanted, dict):
        return isinstance(current, dict) and all(
            _profile_matches(value, current.get(key)) for key, value in wanted.items()
        )
    if isinstance(wanted, list):
        return (
            isinstance(current, list)
            and len(wanted) == len(current)
            and all(_profile_matches(w, c) for w, c in zip(wanted, current))
        )
    return wanted == current


def ensure_profile(profile_id):
    """Register agentic-ci's provider profile *profile_id* with the gateway.

    Profiles live in the gateway database, which ``gateway.start()`` creates
    fresh, so this runs before every provider creation. ``profile import``
    refuses an id that already exists, so an existing profile is exported
    first: left alone when it matches the packaged file, otherwise updated
    in place with the resource version the gateway expects.
    """
    resource = files(_PROFILE_PACKAGE).joinpath(_PROFILE_DIR, f"{profile_id}.yaml")
    wanted = yaml.safe_load(resource.read_text(encoding="utf-8"))

    current = _run(
        ["openshell", "provider", "profile", "export", profile_id, "-o", "yaml"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if current.returncode != 0:
        print(f"  Importing provider profile {profile_id}", flush=True)
        with as_file(resource) as profile_path:
            _run(
                ["openshell", "provider", "profile", "import", "-f", str(profile_path)],
                check=True,
            )
        return

    exported = yaml.safe_load(current.stdout)
    if _profile_matches(wanted, exported):
        return

    print(f"  Updating provider profile {profile_id}", flush=True)
    updated = {**wanted, "resource_version": exported["resource_version"]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(updated, f, sort_keys=False)
        profile_file = f.name
    try:
        _run(
            ["openshell", "provider", "profile", "update", profile_id, "-f", profile_file],
            check=True,
        )
    finally:
        os.unlink(profile_file)


def _create_anthropic_provider(env: Mapping[str, str] | None = None):
    ensure_profile(ANTHROPIC_PROFILE_ID)
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
            ANTHROPIC_PROFILE_ID,
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

    ensure_profile(OPENAI_PROFILE_ID)
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
            OPENAI_PROFILE_ID,
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
