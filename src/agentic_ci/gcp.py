"""GCP credential resolution for agentic-ci backends.

Locates GCP service account or user credentials from environment
variables, files, or the default gcloud ADC path. Used by both the
Podman and OpenShell backends.
"""

from __future__ import annotations

import base64
import json
import os

_ADC_PATH = "~/.config/gcloud/application_default_credentials.json"


def adc_path() -> str:
    """Return the expanded path to the gcloud ADC file."""
    return os.path.expanduser(_ADC_PATH)


def find_credentials() -> tuple[str, str]:
    """Locate GCP credentials. Returns (json_string, source_label).

    Search order:
    1. GCLOUD_CREDENTIALS env var (inline JSON or base64)
    2. GCP_SERVICE_ACCOUNT_KEY env var (file path, inline JSON, or base64)
    3. Default ADC file (~/.config/gcloud/application_default_credentials.json)
    4. GOOGLE_APPLICATION_CREDENTIALS file

    Raises RuntimeError if no valid credentials are found.
    """
    raw = os.environ.get("GCLOUD_CREDENTIALS", "")
    if raw:
        content, is_b64 = _resolve_json(raw)
        if content:
            suffix = " (base64)" if is_b64 else ""
            return content, f"GCLOUD_CREDENTIALS env var{suffix}"
        raise RuntimeError("GCLOUD_CREDENTIALS is not valid JSON or base64-encoded JSON")

    sa_key = os.environ.get("GCP_SERVICE_ACCOUNT_KEY", "")
    if sa_key:
        if os.path.isfile(sa_key):
            try:
                with open(sa_key) as f:
                    sa_key = f.read().strip()
            except OSError as exc:
                raise RuntimeError(
                    f"Failed to read GCP_SERVICE_ACCOUNT_KEY file {sa_key}: {exc}"
                ) from exc
            source_label = "GCP_SERVICE_ACCOUNT_KEY file"
        else:
            source_label = "GCP_SERVICE_ACCOUNT_KEY env var"
        content, _ = _resolve_json(sa_key)
        if content:
            return content, source_label
        raise RuntimeError("GCP_SERVICE_ACCOUNT_KEY is not valid JSON or base64-encoded JSON")

    adc = adc_path()
    ga_creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    for path, label in [
        (adc, "default ADC file"),
        (ga_creds, "GOOGLE_APPLICATION_CREDENTIALS file"),
    ]:
        if path and os.path.isfile(path):
            with open(path) as f:
                text = f.read()
            if _is_valid_json(text):
                return text, label

    raise RuntimeError(
        "No GCP credentials found. Set GCLOUD_CREDENTIALS, "
        "GCP_SERVICE_ACCOUNT_KEY, or configure gcloud ADC."
    )


def ensure_adc() -> str:
    """Ensure the gcloud ADC file exists, writing it from env vars if needed.

    When env vars are set they take precedence over an existing ADC file,
    matching the priority order in find_credentials(). When no env vars
    are set, an existing ADC file is left untouched (developer laptop).

    Returns the source label describing where the credentials came from.
    Raises RuntimeError if no credentials are found.
    """
    adc = adc_path()
    has_env_creds = bool(
        os.environ.get("GCLOUD_CREDENTIALS") or os.environ.get("GCP_SERVICE_ACCOUNT_KEY")
    )
    if os.path.isfile(adc) and not has_env_creds:
        cred_type = _read_credential_type(adc)
        return f"existing ADC file ({cred_type})"

    creds_json, source = find_credentials()
    os.makedirs(os.path.dirname(adc), exist_ok=True)
    with open(adc, "w") as f:
        f.write(creds_json)
    return source


def read_credential_type() -> str:
    """Read the credential type from the gcloud ADC file.

    Returns 'service_account', 'authorized_user', or 'unknown'.
    """
    return _read_credential_type(adc_path())


def _read_credential_type(path: str) -> str:
    if not os.path.isfile(path):
        return "unknown"
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("type", "unknown")
    except (json.JSONDecodeError, OSError):
        return "unknown"


def _resolve_json(val: str) -> tuple[str | None, bool]:
    """Try to extract valid JSON from a raw or base64-encoded string.

    Returns (json_string, was_base64). json_string is None if neither
    raw nor base64-decoded content is valid JSON.
    """
    if _is_valid_json(val):
        return val, False
    decoded = _try_base64_decode(val)
    if decoded and _is_valid_json(decoded):
        return decoded, True
    return None, False


def _is_valid_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _try_base64_decode(text: str) -> str | None:
    try:
        return base64.b64decode(text).decode("utf-8")
    except Exception:
        return None
