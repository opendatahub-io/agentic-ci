---
name: release-agentic-ci
description: Cut a new agentic-ci release by tagging and pushing. Version is derived from git tags automatically.
disable-model-invocation: true
argument-hint: "[version]"
---

# Release agentic-ci

Tag a new release. The version is embedded in the build via
`uv-dynamic-versioning` (reads from git tags), so no pyproject.toml
changes are needed.

## Inputs

The skill argument is an optional version string (e.g. `0.3.26`).
If omitted, bump the patch version of the latest git tag
(e.g. `0.3.25` becomes `0.3.26`).

## Steps

Follow these steps exactly. Stop and report if any step fails.

### 1. Determine the new version

```bash
git fetch origin --tags
git tag -l --sort=-v:refname | head -1
```

If the user supplied a version string as the skill argument, use it.
Otherwise, bump the patch component of the latest tag
(e.g. `0.3.25` -> `0.3.26`).

Confirm the new version with the user before proceeding.

### 2. Verify main is clean

```bash
git fetch origin main
git log --oneline origin/main | head -5
```

Show the user the recent commits that will be included in this release.
Ask for confirmation before tagging.

### 3. Tag and push

```bash
# Tag the latest commit on origin/main
git tag <VERSION> origin/main
git push origin <VERSION>
```

### 4. Verify the image build started

The tag push triggers the Container Images workflow which builds and
pushes version-tagged images to quay.io.

```bash
gh run list --repo opendatahub-io/agentic-ci --workflow "Container Images" --limit 3
```

### 5. Print summary

Print:
  - Version: `<VERSION>`
  - Tag: `<VERSION>`
  - Tagged commit: (short SHA from `git rev-parse --short origin/main`)
  - Image workflow: (link or status from step 4)
  - Images will be available at:
    - `quay.io/aipcc/agentic-ci/claude-runner:<VERSION>`
    - `quay.io/aipcc/agentic-ci/opencode-runner:<VERSION>`
    - `quay.io/aipcc/agentic-ci/claude-sandbox:<VERSION>`
    - `quay.io/aipcc/agentic-ci/opencode-sandbox:<VERSION>`
    - `quay.io/aipcc/agentic-ci/cursor-runner:<VERSION>`
    - `quay.io/aipcc/agentic-ci/cursor-sandbox:<VERSION>`
    - `quay.io/aipcc/agentic-ci/openshell:<VERSION>`
    - `quay.io/aipcc/agentic-ci/podman:<VERSION>`
