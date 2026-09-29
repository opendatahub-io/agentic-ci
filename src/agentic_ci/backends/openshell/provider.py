"""OpenShell credential provider setup."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from importlib.resources import as_file, files

import yaml

from agentic_ci import log
from agentic_ci.gcp import adc_path as _adc_path
from agentic_ci.gcp import ensure_adc
from agentic_ci.gcp import read_credential_type as _adc_credential_type

if sys.version_info >= (3, 11):
    from importlib.resources.abc import Traversable
else:
    from importlib.abc import Traversable

PROVIDER_NAME = "ci-gcp"

# Provider profiles agentic-ci registers with the gateway before creating a
# provider (the gateway ships none). OpenShell's example "openai" and
# "anthropic" profiles bind the API host to curl, so any sandbox process could
# spend the key, and its "google-vertex-ai" example binds no binary at all;
# these profiles bind the hosts to agent binaries instead. An agent binary
# wrapper can still spend the credential; see the YAML files for details.
OPENAI_PROFILE_ID = "agentic-ci-openai"
ANTHROPIC_PROFILE_ID = "agentic-ci-anthropic"
VERTEX_PROFILE_ID = "agentic-ci-google-vertex-ai"
_PROFILE_PACKAGE = "agentic_ci.backends.openshell"
_PROFILE_DIR = "profiles"

# Credential key the gateway refreshes for service-account Vertex auth. The
# sandbox receives it as an opaque placeholder that the supervisor proxy
# resolves on requests to the aiplatform endpoints the Vertex profile declares.
VERTEX_SA_TOKEN_KEY = "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN"
# The same for gcloud ADC (user OAuth) Vertex auth.
VERTEX_ADC_TOKEN_KEY = "GOOGLE_VERTEX_AI_TOKEN"

_SECRET_PREFIXES = ("private_key=", f"{VERTEX_SA_TOKEN_KEY}=", "OPENAI_API_KEY=")
_PROVIDER_AUTH_MODES = {
    VERTEX_PROFILE_ID: "vertex",
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
    # The same for Vertex. A google-cloud provider relied on the GCE metadata
    # emulator, which OpenShell removed: it still injects GCE_METADATA_HOST,
    # pointing SDKs at a port nothing serves. A google-vertex-ai provider
    # comes from a profile agentic-ci does not control.
    "google-cloud": "vertex-google-cloud-provider",
    "google-vertex-ai": "vertex-builtin-profile",
}

# The provider profile agentic-ci creates each provider-backed auth mode from.
_AUTH_MODE_PROFILES = {
    "api-key": ANTHROPIC_PROFILE_ID,
    "openai": OPENAI_PROFILE_ID,
    "vertex": VERTEX_PROFILE_ID,
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

# OpenShell refuses to inject a secret that contains CR, LF or NUL (header
# injection guard) and answers the request with HTTP 500
# "credential_unavailable", which Codex reports as "high demand". A key
# pasted into a CI secret often ends with a newline, and `codex login`
# strips it, so a real key in auth.json hides the problem. Provider-only
# credentials are therefore stripped before they reach the provider.
_PROHIBITED_CREDENTIAL_CHARS = frozenset("\r\n\0")


def _provider_credential(credential_key, env: Mapping[str, str]) -> str:
    """Return *credential_key* from *env* as the provider should store it.

    Surrounding whitespace is removed, as ``codex login --with-api-key``
    does. A value that still contains CR, LF or NUL is rejected here with a
    clear error, because OpenShell would refuse to inject it on every request.
    Returns an empty string when the variable is unset or blank.
    """
    value = env.get(credential_key, "").strip()
    if any(char in _PROHIBITED_CREDENTIAL_CHARS for char in value):
        raise RuntimeError(
            f"{credential_key} contains a line break or NUL character; "
            "OpenShell cannot inject it into requests"
        )
    return value


def _provider_process_env(credential_key, env: Mapping[str, str]) -> dict[str, str]:
    """Return the environment for an ``openshell provider`` call.

    The credential is passed by name (``--credential KEY``) so it never
    appears in the process arguments; this sets it to the stripped value.
    When stripping changed the value, one line says so (never the value),
    so a run log shows whether the secret had surrounding whitespace.
    """
    value = _provider_credential(credential_key, env)
    raw = env.get(credential_key, "")
    if value != raw:
        kind = "a line break" if any(c in raw for c in "\r\n") else "spaces or tabs"
        print(
            f"  Stripped surrounding whitespace ({kind}) from {credential_key}",
            flush=True,
        )
    return {**os.environ, **env, credential_key: value}


# The variables that carry the provider placeholder for each auth mode whose
# provider injects a credential (the env_vars of the profiles in profiles/
# that agentic-ci stores a credential under). These providers are detached
# while the setup shim's egress is open: the rule OpenShell composes from the
# profile lets agent binaries, and so anything a setup or validate step runs
# under an agent binary, spend the credential. A Vertex provider holds one of
# two tokens, depending on whether it was created from a service account key
# or from gcloud ADC, so both are listed. The oauth mode has no provider at
# all.
_PROVIDER_ENV_VARS = {
    "openai": ("OPENAI_API_KEY",),
    "api-key": ("ANTHROPIC_API_KEY",),
    "vertex": (VERTEX_SA_TOKEN_KEY, VERTEX_ADC_TOKEN_KEY),
}


def profile_endpoint_hosts(directory: Traversable | None = None) -> frozenset[str]:
    """Return the endpoint hosts every vendored provider profile declares, lower-cased.

    Reads each ``*.yaml`` file in *directory* (default: the packaged
    ``profiles/`` directory). Wildcard hosts such as
    ``*-aiplatform.googleapis.com`` are returned as written. A profile with no
    ``endpoints`` key (an endpointless profile) contributes nothing.

    Raises ``ValueError`` naming the file when a profile is not valid YAML, is
    not a mapping, has an ``id`` other than its file name, or has an
    ``endpoints`` value that is not a list of mappings with a non-empty
    ``host``. Only the file name and the failed rule are in the message.
    """
    root = files(_PROFILE_PACKAGE).joinpath(_PROFILE_DIR) if directory is None else directory
    hosts: set[str] = set()
    for resource in sorted(root.iterdir(), key=lambda r: r.name):
        if not resource.name.endswith(".yaml"):
            continue
        name = resource.name
        try:
            data = yaml.safe_load(resource.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            raise ValueError(f"provider profile {name} is not valid YAML") from None
        if not isinstance(data, dict):
            raise ValueError(f"provider profile {name} is not a mapping")
        if data.get("id") != name.removesuffix(".yaml"):
            raise ValueError(f"provider profile {name} must have its file name as its id")
        endpoints = data.get("endpoints", [])
        if not isinstance(endpoints, list):
            raise ValueError(f"provider profile {name}: endpoints must be a list")
        for index, endpoint in enumerate(endpoints):
            host = endpoint.get("host") if isinstance(endpoint, dict) else None
            if not isinstance(host, str) or not host.strip():
                raise ValueError(f"provider profile {name}: endpoint {index} has no host")
            hosts.add(host.strip().lower())
    return frozenset(hosts)


def requires_provider(auth_mode: str | None) -> bool:
    """Return whether *auth_mode* is backed by the CI provider."""
    return auth_mode not in _PROVIDERLESS_AUTH_MODES


def provider_env_vars(auth_mode: str | None) -> tuple[str, ...]:
    """Return the variables that can hold *auth_mode*'s provider placeholder.

    Empty for oauth and anything else that has no provider credential to
    detach (see :data:`_PROVIDER_ENV_VARS`).
    """
    return _PROVIDER_ENV_VARS.get(auth_mode, ()) if auth_mode is not None else ()


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

    For Vertex AI, creates a provider from agentic-ci's Vertex profile. The
    gateway keeps the refresh material and mints short-lived access tokens;
    the sandbox only sees a placeholder variable that the supervisor proxy
    resolves on requests to the aiplatform endpoints. (OpenShell removed the
    GCE metadata emulator the older google-cloud provider relied on, so
    SDK-side ADC discovery no longer works inside the sandbox.)

    For user OAuth credentials (from gcloud auth application-default login),
    --from-gcloud-adc handles everything. For service account keys (CI),
    the provider is created with a placeholder token and refresh is
    configured separately with the service account's email and private key.

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
        # OpenShellBackend.setup() has already made the gateway's copy of the
        # profile match agentic-ci's (ensure_auth_mode_profile), before it
        # decided whether the sandbox can be reused.
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
    value = _provider_credential(credential_key, credential_env)
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
    if not _provider_credential(credential_key, credential_env):
        raise RuntimeError(f"OpenShell {auth_mode} runs require {credential_key}")
    print(f"  Refreshing {credential_key} in provider '{PROVIDER_NAME}'", flush=True)
    _run(
        ["openshell", "provider", "update", PROVIDER_NAME, "--credential", credential_key],
        check=True,
        env=_provider_process_env(credential_key, credential_env),
    )


def validate_credentials(auth_mode, env: Mapping[str, str] | None = None):
    """Validate credentials supported by the OpenShell provider."""
    credential_env = env if env is not None else os.environ
    if auth_mode == "openai" and not _provider_credential("OPENAI_API_KEY", credential_env):
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


def ensure_profile(profile_id) -> bool:
    """Register agentic-ci's provider profile *profile_id* with the gateway.

    Profiles live in the gateway database, which ``gateway.start()`` creates
    fresh, so this runs before every provider creation. ``profile import``
    refuses an id that already exists, so an existing profile is exported
    first: left alone when it matches the packaged file, otherwise updated
    in place with the resource version the gateway expects.

    Returns True when the gateway's copy was imported or updated, False when
    it already matched.
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
        return True

    exported = yaml.safe_load(current.stdout)
    if _profile_matches(wanted, exported):
        return False

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
    return True


def ensure_auth_mode_profile(auth_mode) -> bool:
    """Make the gateway's copy of *auth_mode*'s provider profile match the packaged one.

    A provider reused across runs composes its sandbox rule from whatever
    profile the gateway holds under its type id, which may come from an older
    agentic-ci or have been changed by hand, so it could bind binaries other
    than the agent's. Returns True when the profile had to be imported or
    updated; the caller then recreates the sandbox rather than reuse one that
    ran under the drifted rule. Returns False for auth modes without a
    profile.
    """
    profile_id = _AUTH_MODE_PROFILES.get(auth_mode)
    if profile_id is None:
        return False
    return ensure_profile(profile_id)


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
    if not _provider_credential("OPENAI_API_KEY", credential_env):
        raise RuntimeError("OpenShell Codex runs require OPENAI_API_KEY")

    ensure_profile(OPENAI_PROFILE_ID)
    print("  Creating OpenAI API key provider", flush=True)
    process_env = _provider_process_env("OPENAI_API_KEY", credential_env)
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

    ensure_profile(VERTEX_PROFILE_ID)
    print(
        f"  Creating Vertex AI provider "
        f"(project={project}, region={region}, creds={cred_type}, source={source})",
        flush=True,
    )

    if cred_type == "service_account":
        _create_gcp_provider_sa(project, region)
    else:
        _create_gcp_provider_adc(project, region)


def _vertex_config_args(project, region):
    """``--config`` arguments for the settings the Vertex profile projects into the sandbox."""
    args = []
    if project:
        args.extend(["--config", f"VERTEX_AI_PROJECT_ID={project}"])
    args.extend(["--config", f"VERTEX_AI_REGION={region}"])
    return args


def _create_gcp_provider_adc(project, region):
    """Create a Vertex AI provider from gcloud ADC user credentials."""
    _run(
        [
            "openshell",
            "provider",
            "create",
            "--name",
            PROVIDER_NAME,
            "--type",
            VERTEX_PROFILE_ID,
            "--from-gcloud-adc",
            *_vertex_config_args(project, region),
        ],
        check=True,
    )


def _create_gcp_provider_sa(project, region):
    """Create a Vertex AI provider from a service account key.

    --from-gcloud-adc only accepts user OAuth credentials. For service
    accounts we create the provider with a placeholder token, then configure
    the JWT refresh strategy with the service account's email and private
    key so the gateway can mint access tokens.
    """
    adc = _adc_path()
    with open(adc) as f:
        sa = json.load(f)

    client_email = sa["client_email"]
    private_key = sa["private_key"]

    _run(
        [
            "openshell",
            "provider",
            "create",
            "--name",
            PROVIDER_NAME,
            "--type",
            VERTEX_PROFILE_ID,
            "--credential",
            f"{VERTEX_SA_TOKEN_KEY}=placeholder",
            *_vertex_config_args(project, region),
        ],
        check=True,
    )

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

    The provider holds its token under the key it was created with:
    :data:`VERTEX_ADC_TOKEN_KEY` for gcloud ADC user credentials,
    :data:`VERTEX_SA_TOKEN_KEY` otherwise.

    Raises subprocess.CalledProcessError on failure.
    """
    if _adc_credential_type() == "authorized_user":
        credential_key = VERTEX_ADC_TOKEN_KEY
    else:
        credential_key = VERTEX_SA_TOKEN_KEY
    _run(
        [
            "openshell",
            "provider",
            "refresh",
            "rotate",
            "--credential-key",
            credential_key,
            PROVIDER_NAME,
        ],
        check=True,
    )
