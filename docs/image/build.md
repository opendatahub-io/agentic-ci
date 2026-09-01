# Image Build Pipeline

All container images are built by GitHub Actions and pushed to
`quay.io/aipcc/agentic-ci/`.

## Tag strategy

Every push to main (when `images/**` changes), tag push, and daily
schedule triggers builds. Images are tagged with:

- `latest` and an epoch timestamp on every build
- The git tag name on tag pushes (semver pattern)

## Images

| Image | Containerfile | Description |
|-------|---------------|-------------|
| `podman` | `images/ci/Containerfile.podman` | CI env with podman |
| `openshell` | `images/ci/Containerfile.openshell` | CI env with OpenShell gateway |
| `claude-runner` | `images/runner/claude-code/Containerfile` | Claude Code runner |
| `opencode-runner` | `images/runner/opencode/Containerfile` | OpenCode runner |
| `codex-runner` | `images/runner/codex/Containerfile` | Codex runner |
| `claude-sandbox` | `images/runner/claude-code/Containerfile.openshell` | Hummingbird sandbox extending the hardened Claude Code image |
| `opencode-sandbox` | `images/runner/opencode/Containerfile.openshell` | Hummingbird sandbox extending the hardened OpenCode image |
| `codex-sandbox` | `images/runner/codex/Containerfile.openshell` | Hummingbird sandbox extending the hardened Codex image |

## Local builds

The sandbox images pull private base images from `quay.io/aipcc/base-images/`.
Log in before building them:

```bash
podman login quay.io
```

```bash
make ci-build                  # podman CI image
make openshell-ci-build        # openshell CI image
make base-build                # runner base
make claude-build              # claude-runner (depends on base-build)
make opencode-build            # opencode-runner (depends on base-build)
make openshell-claude-build    # claude-sandbox (extends the hardened harness image)
make openshell-opencode-build  # opencode-sandbox (extends the hardened harness image)
make openshell-codex-build     # codex-sandbox (extends the hardened harness image)
```
