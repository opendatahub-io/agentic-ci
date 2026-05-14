"""Policy resolution for OpenShell sandbox."""

import os
import tempfile

DEFAULT_POLICY = """\
version: 1

filesystem_policy:
  include_workdir: true
  read_only: [/usr, /lib, /lib64, /proc, /dev/urandom, /app, /etc, /var/log, /opt]
  read_write: [/sandbox, /tmp, /dev/null]

landlock:
  compatibility: best_effort

network_policies:
  gcp_auth:
    name: gcp-oauth
    endpoints:
      - host: oauth2.googleapis.com
        port: 443
        protocol: rest
        enforcement: enforce
        access: full
      - host: accounts.google.com
        port: 443
        protocol: rest
        enforcement: enforce
        access: full
    binaries:
      - path: /usr/local/bin/claude
      - path: /usr/local/bin/node

  vertex_ai:
    name: vertex-ai-inference
    endpoints:
      - host: "*.googleapis.com"
        port: 443
        protocol: rest
        enforcement: enforce
        access: full
    binaries:
      - path: /usr/local/bin/claude
      - path: /usr/local/bin/node

  github_api:
    name: github-api
    endpoints:
      - host: "*.github.com"
        port: 443
        protocol: rest
        enforcement: enforce
        access: full
    binaries:
      - path: "*"

  gitlab_api:
    name: gitlab-api
    endpoints:
      - host: gitlab.com
        port: 443
        protocol: rest
        enforcement: enforce
        access: full
    binaries:
      - path: "*"

  pypi:
    name: pypi-registry
    endpoints:
      - host: pypi.org
        port: 443
        protocol: rest
        enforcement: enforce
        access: read-only
      - host: files.pythonhosted.org
        port: 443
        protocol: rest
        enforcement: enforce
        access: read-only
    binaries:
      - path: "*"
"""

REPO_POLICY_PATH = ".agentic-ci/openshell-policy.yml"


def resolve(flag_path=None, workdir="."):
    """Resolve the policy file to use, following priority order.

    1. Explicit --policy flag
    2. .agentic-ci/openshell-policy.yml in the workdir
    3. Built-in default (written to a temp file)

    Returns the absolute path to the policy file.
    """
    if flag_path:
        return os.path.abspath(flag_path)

    repo_policy = os.path.join(workdir, REPO_POLICY_PATH)
    if os.path.isfile(repo_policy):
        return os.path.abspath(repo_policy)

    fd, path = tempfile.mkstemp(suffix=".yml", prefix="agentic-ci-policy-")
    with os.fdopen(fd, "w") as f:
        f.write(DEFAULT_POLICY)
    return path
