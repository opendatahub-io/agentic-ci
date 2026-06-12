# Extending the Image

The base image is designed to be used as a `FROM` target for
workflow-specific images that need additional tools or skills.

## Adding tools

```dockerfile
FROM quay.io/aipcc/agentic-ci/claude-runner:latest

USER root
RUN microdnf install -y --nodocs my-tool && microdnf clean all

USER agent-ci
```

Common additions:

| Tool | Install command |
|------|----------------|
| OpenShift CLI | See example below |
| Atlassian CLI | See example below |

**OpenShift CLI:**

```bash
OCP_URL="https://mirror.openshift.com/pub/openshift-v4"
curl -LsSf "$OCP_URL/clients/ocp/stable/openshift-client-linux.tar.gz" \
  | tar xzf - -C /usr/local/bin/ oc
```

**Atlassian CLI:**

```bash
curl -fsSL \
  "https://acli.atlassian.com/linux/latest/acli_linux_amd64/acli" \
  -o /usr/local/bin/acli && chmod +x /usr/local/bin/acli
```

## Adding skills

### From a git repository

```dockerfile
FROM quay.io/aipcc/agentic-ci/claude-runner:latest

USER agent-ci
RUN install-plugin --repo https://github.com/opendatahub-io/knowledge-skills.git
```

### From the skills registry (by name)

If the plugin is listed in the registered
[skills-registry](https://github.com/opendatahub-io/skills-registry)
marketplace:

```dockerfile
FROM quay.io/aipcc/agentic-ci/claude-runner:latest

USER agent-ci
RUN install-plugin my-new-plugin
```

### From a different marketplace

Register an additional marketplace, then install from it:

```dockerfile
FROM quay.io/aipcc/agentic-ci/claude-runner:latest

USER agent-ci
RUN install-plugin --marketplace-repo my-org/my-skills-registry && \
    install-plugin --all
```

## Important notes

- Run `install-plugin` as `agent-ci` (not root) so plugins are
  written to the correct home directory
- The base image already has all 8 skills-registry plugins installed;
  downstream images only need to add extras
- Tools that require root (e.g. `microdnf install`) must switch to
  `USER root` and back to `USER agent-ci`
