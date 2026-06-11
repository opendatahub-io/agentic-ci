---
name: release-agentic-ci
description: Cut a new agentic-ci release: bump version, open PR, wait for approval and checks, merge, tag, and push.
disable-model-invocation: true
argument-hint: "[version]"
---

# Release agentic-ci

Orchestrates the full release flow: version bump, PR, approval wait, merge, tag, push.

## Inputs

The skill argument is an optional version string (e.g. `0.3.0`).
If omitted, bump the patch version of the current version in `pyproject.toml` (e.g. `0.2.24` becomes `0.2.25`).

## Steps

Follow these steps exactly. Stop and report if any step fails.

### 1. Determine the new version

```bash
# Read current version
grep '^version' pyproject.toml
```

If the user supplied a version string as the skill argument, use it.
Otherwise, bump the patch component (e.g. `0.2.24` -> `0.2.25`).

Confirm the new version with the user before proceeding.

### 2. Create the release branch and commit

```bash
git checkout -b agentic-ci-<VERSION> main
```

Edit `pyproject.toml` to set `version = "<VERSION>"`.

Commit:
```bash
git commit -s -am "Bump agentic-ci to <VERSION>"
```

### 3. Push and open PR

```bash
git push -u origin agentic-ci-<VERSION>
gh pr create --title "Bump agentic-ci to <VERSION>" --body ""
```

Print the PR URL.

### 4. Wait for approval and checks

Use `ScheduleWakeup` to set a 300-second (5 min) recurring check. On each wake-up:

```bash
# Check review approval (need at least 1 APPROVED)
gh pr view <PR_NUMBER> --json reviewDecision --jq '.reviewDecision'

# Check CI status
gh pr checks <PR_NUMBER>
```

The PR is ready to merge when:
- `reviewDecision` is `APPROVED`
- All required checks pass (no failing checks)

If not ready, log the current state and schedule the next wake-up.
If ready, proceed to step 5.

### 5. Merge the PR

```bash
gh pr merge <PR_NUMBER> --merge --delete-branch
```

### 6. Tag and push

```bash
git checkout main
git pull origin main
```

Verify that `HEAD` is the merge commit for this PR:
```bash
git log --oneline -1
```

The commit message should contain `Merge pull request #<PR_NUMBER>`.

Tag and push:
```bash
git tag <VERSION>
git push origin <VERSION>
```

### 7. Done

Print a summary:
- Version: `<VERSION>`
- PR: link
- Tag: `<VERSION>`
- Merge commit: short SHA
