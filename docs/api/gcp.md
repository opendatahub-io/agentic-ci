# GCP Credentials

`agentic_ci.gcp` resolves GCP service account or user credentials for the
Podman and OpenShell backends. It locates credentials from the
`GCLOUD_CREDENTIALS` and `GCP_SERVICE_ACCOUNT_KEY` environment variables
(inline JSON, base64, or file path), the default gcloud ADC path, or a
`GOOGLE_APPLICATION_CREDENTIALS` file.

`find_credentials()` returns the credential JSON and a source label;
`ensure_adc()` guarantees the gcloud ADC file exists, writing it from the
environment when needed, and `read_credential_type()` reports the ADC
credential type for logging.

::: agentic_ci.gcp
