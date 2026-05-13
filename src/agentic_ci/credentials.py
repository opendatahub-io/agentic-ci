"""GCP credential management for CI environments.

Handles decoding, validating, and staging Google Cloud credentials for
use inside containers or directly on the host. Supports raw JSON and
base64-encoded formats from multiple environment variable sources.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_ADC_REL = ".config/gcloud/application_default_credentials.json"
_DEFAULT_CFG_REL = ".config/gcloud/configurations/config_default"


def _decode_credentials(raw: str) -> str:
    """Decode raw JSON or base64-encoded JSON. Returns valid JSON string.

    Raises ValueError if the input is neither valid JSON nor valid base64-encoded JSON.
    """
    try:
        json.loads(raw)
        return raw
    except (json.JSONDecodeError, ValueError):
        pass

    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
        json.loads(decoded)
        return decoded
    except Exception:
        pass

    raise ValueError("Credential value is neither valid JSON nor base64-encoded JSON")


def resolve_credentials(
    env_var: str = "GCLOUD_CREDENTIALS",
    fallback: str = "GCP_SERVICE_ACCOUNT_KEY",
) -> str:
    """Resolve and validate GCP credentials from environment variables.

    Checks ``env_var`` first, then ``fallback``. Both may contain raw JSON
    or base64-encoded JSON. Returns the decoded JSON string.

    Raises ``RuntimeError`` if no valid credentials are found.
    """
    for var in (env_var, fallback):
        raw = os.environ.get(var)
        if not raw:
            continue
        try:
            return _decode_credentials(raw)
        except ValueError:
            log.warning("Env var %s is set but not valid JSON or base64 JSON", var)

    raise RuntimeError(
        f"No GCP credentials found. Set {env_var} (JSON) or {fallback} (base64)"
    )


def setup_gcp_credentials(
    *,
    env_var: str = "GCLOUD_CREDENTIALS",
    fallback: str = "GCP_SERVICE_ACCOUNT_KEY",
    home: str | Path | None = None,
) -> Path:
    """Decode credentials and write ADC file under ``home``.

    Also sets ``GOOGLE_APPLICATION_CREDENTIALS`` in the current process
    environment and normalizes ``GCP_PROJECT_ID`` to
    ``ANTHROPIC_VERTEX_PROJECT_ID`` when applicable.

    Returns the path to the written ADC file.
    """
    creds_json = resolve_credentials(env_var, fallback)

    if os.environ.get("GCP_PROJECT_ID") and not os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"):
        os.environ["ANTHROPIC_VERTEX_PROJECT_ID"] = os.environ["GCP_PROJECT_ID"]

    if home is None:
        home = Path.home()
    home = Path(home)

    adc_path = home / _DEFAULT_ADC_REL
    adc_path.parent.mkdir(parents=True, exist_ok=True)
    adc_path.write_text(creds_json, encoding="utf-8")

    cfg_path = home / _DEFAULT_CFG_REL
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    project = os.environ.get(
        "ANTHROPIC_VERTEX_PROJECT_ID",
        os.environ.get("GCP_PROJECT_ID", ""),
    )
    cfg_path.write_text(
        f"[core]\nproject = {project}\ndisable_prompts = true\n",
        encoding="utf-8",
    )

    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(adc_path)
    log.info("GCP credentials written to %s", adc_path)
    return adc_path


def stage_credentials_for_mount(
    staging_dir: str | Path,
    *,
    env_var: str = "GCLOUD_CREDENTIALS",
    fallback: str = "GCP_SERVICE_ACCOUNT_KEY",
) -> Path:
    """Write credentials to a staging directory for Podman volume mount.

    Creates the gcloud directory structure under ``staging_dir`` so it can
    be bind-mounted into a container at ``/home/<user>/.config/gcloud/``.

    Returns the staging directory path.
    """
    staging = Path(staging_dir)
    return setup_gcp_credentials(env_var=env_var, fallback=fallback, home=staging)
