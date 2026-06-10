"""Verify Vertex AI authentication with a GCP service account or ADC."""

import json
import os
import sys
import urllib.error
import urllib.request

import google.auth
import google.auth.transport.requests
from google.oauth2 import service_account

key_path = os.environ.get("AIPCC_CICD_GCP_SERVICE_ACCOUNT_KEY")
project = os.environ.get("AIPCC_CICD_GCP_PROJECT_ID")

if key_path:
    creds = service_account.Credentials.from_service_account_file(
        key_path,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
else:
    creds, adc_project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    project = project or adc_project

if not project:
    sys.exit("Set AIPCC_CICD_GCP_PROJECT_ID or configure a default project")

creds.refresh(google.auth.transport.requests.Request())

model = "claude-sonnet-4@20250514"
url = (
    f"https://aiplatform.googleapis.com/v1/"
    f"projects/{project}/locations/global/"
    f"publishers/anthropic/models/{model}:rawPredict"
)
body = json.dumps({
    "anthropic_version": "vertex-2023-10-16",
    "messages": [{"role": "user", "content": "Say hello in exactly three words."}],
    "max_tokens": 32,
}).encode()

req = urllib.request.Request(url, data=body, method="POST")
req.add_header("Authorization", f"Bearer {creds.token}")
req.add_header("Content-Type", "application/json")

print(f"Project: {project}")
print(f"URL:     {url}")

try:
    resp = urllib.request.urlopen(req)
except urllib.error.HTTPError as e:
    sys.exit(f"HTTP {e.code}: {e.reason}\n{e.read().decode()}")

data = json.loads(resp.read())
reply = data["content"][0]["text"]
print("Vertex AI auth succeeded!")
print(f"Model reply: {reply}")
