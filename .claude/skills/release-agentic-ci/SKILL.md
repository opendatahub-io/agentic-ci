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

### 4. Wait for approval and merge

Use `CronCreate` to schedule a recurring job that checks PR status every 5 minutes and completes the release automatically when ready.

The cron prompt must be **fully self-contained**.
Each run is an independent Claude invocation with no memory of prior turns.
Inline all necessary context: the version, PR number, and the exact commands to run.

```text
CronCreate:
  cron: "*/5 * * * *"
  recurring: true
  prompt: |
    You are completing a release of agentic-ci version <VERSION>.
    PR #<PR_NUMBER> is open at https://github.com/opendatahub-io/agentic-ci/pull/<PR_NUMBER>.
    The working directory is already the repo root.

    Step 1: Check if the PR is ready.

    Run:
      gh pr view <PR_NUMBER> --repo opendatahub-io/agentic-ci --json reviewDecision,mergedAt --jq '{reviewDecision, mergedAt}'

    - If mergedAt is non-empty, the PR was already merged. Skip to step 3.
    - If reviewDecision is not "APPROVED", print "PR #<PR_NUMBER>: waiting for approval" and stop.
    - If reviewDecision is "APPROVED", check CI:
        gh pr checks <PR_NUMBER> --repo opendatahub-io/agentic-ci
      If any required check is failing or pending, print the status and stop.
      If all checks pass, proceed to step 2.

    Step 2: Merge the PR.

    Run:
      gh pr merge <PR_NUMBER> --repo opendatahub-io/agentic-ci --merge --delete-branch

    Step 3: Tag and push.

    Run:
      git checkout main
      git pull origin main
      git log --oneline -1

    Verify the HEAD commit message contains "Merge pull request #<PR_NUMBER>".

    Then tag and push:
      git tag <VERSION>
      git push origin <VERSION>

    Step 4: Print a summary and clean up.

    Print:
      - Version: <VERSION>
      - PR: https://github.com/opendatahub-io/agentic-ci/pull/<PR_NUMBER>
      - Tag: <VERSION>
      - Merge commit: (short SHA from git log)

    Then delete this cron job using CronDelete with the job ID from CronList.
```

After creating the cron job, print the PR URL and tell the user the release will complete automatically once the PR is approved and CI passes.
Print the cron job ID so the user can cancel it manually if needed.
