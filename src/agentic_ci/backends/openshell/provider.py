"""OpenShell provider setup for GCP credential injection."""

import json
import os
import subprocess

from agentic_ci import log
from agentic_ci.gcp import adc_path as _adc_path
from agentic_ci.gcp import ensure_adc
from agentic_ci.gcp import read_credential_type as _adc_credential_type

PROVIDER_NAME = "ci-gcp"

_SECRET_PREFIXES = ("private_key=", "GCP_SA_ACCESS_TOKEN=")


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


def setup(auth_mode):
    """Configure the OpenShell provider.

    Creates a google-cloud provider that injects GCP credentials into the
    sandbox via the OpenShell supervisor proxy. The agent uses its native
    Vertex AI integration — no inference.local proxy is needed.

    For user OAuth credentials (from gcloud auth application-default login),
    --from-gcloud-adc handles everything. For service account keys (CI),
    the provider is created bare and refresh is configured separately with
    the service account's email and private key.

    For API key auth, creates an anthropic provider instead.
    """
    if provider_exists():
        # NOTE: switching auth modes (e.g. Vertex → API key) between runs
        # is not supported. The existing provider is reused regardless of
        # its type. To switch, tear down the environment and start fresh.
        print(f"  Provider '{PROVIDER_NAME}' already exists", flush=True)
    elif auth_mode == "api-key":
        _create_anthropic_provider()
    else:
        _create_gcp_provider()


def provider_exists():
    """Check if the CI provider already exists."""
    result = _run(
        ["openshell", "provider", "get", PROVIDER_NAME],
        capture_output=True,
        timeout=15,
    )
    return result.returncode == 0


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
