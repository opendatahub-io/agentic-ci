# Skill Installation

Skills from the
[opendatahub-io/skills-registry](https://github.com/opendatahub-io/skills-registry)
are pre-installed into every runner and sandbox image at build time. Each
harness (Claude Code, OpenCode, Cursor) uses a different installation mechanism
that matches the tool's native plugin system.

## Claude Code: plugin seed

Claude Code images use the official
[plugin seed mechanism](https://code.claude.com/docs/en/plugin-marketplaces#pre-populate-plugins-for-containers).
At build time, native CLI commands populate a seed directory that Claude
Code reads at startup without re-cloning anything.

### Build-time flow

The Containerfile runs two steps as the non-root user:

```dockerfile
RUN git config --global url."https://github.com/".insteadOf "git@github.com:" \
    && export CLAUDE_CODE_PLUGIN_CACHE_DIR=/home/agent-ci/.claude-seed \
    && export DISABLE_AUTOUPDATER=1 \
    && claude plugin marketplace add opendatahub-io/skills-registry \
    && agentic-ci install-plugins \
    && git config --global --unset-all url."https://github.com/".insteadOf
```

1. `claude plugin marketplace add` — registers the skills-registry
   marketplace and clones it into the seed directory.
2. `agentic-ci install-plugins` — reads the marketplace.json, calls
   `claude plugin install <name>@<marketplace>` for each plugin, and
   generates the [plugin-skills manifest](#plugin-skills-manifest).

The `git config` override forces HTTPS cloning since the container has no
SSH keys. It is removed at the end of the same `RUN` layer so the final
image has clean git config.

### Runtime

Two environment variables control plugin loading:

| Variable | Purpose |
|----------|---------|
| `CLAUDE_CODE_PLUGIN_SEED_DIR` | Points to the pre-built seed directory. Claude Code reads it on startup (read-only, no auto-updates). |
| `CLAUDE_CODE_SYNC_PLUGIN_INSTALL` | Set to `1`. Required for skills to load synchronously in non-interactive (`-p`) mode. |

### Seed directory structure

```text
~/.claude-seed/
  known_marketplaces.json
  installed_plugins.json
  marketplaces/
    opendatahub-skills/           # full git clone of skills-registry
      .claude-plugin/
        marketplace.json
  cache/
    opendatahub-skills/
      odh-ai-helpers/0.1.0/       # full git clone of plugin repo
        helpers/skills/
        helpers/agents/
      code-review-skills/0.1.0/
        skills/gitlab-code-review/
          SKILL.md
          scripts/review.py
      ...
```

## OpenCode: file-based skill discovery

OpenCode has no native marketplace or seed mechanism. It discovers skills
by scanning directories for `SKILL.md` files. The build copies skill
directories from each plugin repo into OpenCode's global skills path.

### Build-time flow

```dockerfile
RUN git clone --depth 1 --quiet https://github.com/opendatahub-io/skills-registry.git /tmp/skills-registry \
    && agentic-ci install-plugins --harness opencode \
       --marketplace-json /tmp/skills-registry/.claude-plugin/marketplace.json \
    && rm -rf /tmp/skills-registry
```

`agentic-ci install-plugins --harness opencode` reads the marketplace.json, then for each plugin:

1. Clones the plugin's source repo.
2. Copies skill directories to `~/.config/opencode/skills/`.
   Uses explicit `skills` paths from the marketplace entry when present
   (e.g. `"skills": ["./helpers/skills"]` for `odh-ai-helpers`), otherwise
   falls back to standard paths (`.claude/skills/`, `.opencode/skills/`,
   `skills/`).
3. Records the plugin→skill mapping in the
   [plugin-skills manifest](#plugin-skills-manifest).

### Skills directory structure

```text
~/.config/opencode/skills/
  git-shallow-clone/
    SKILL.md
  code-review/
    SKILL.md
  gitlab-code-review/
    SKILL.md
    scripts/review.py
  ...
```

All skills are flat in one directory — no plugin namespacing. OpenCode
discovers them by scanning for `SKILL.md` files.

## Cursor: no native plugin system

Cursor Agent does not have a native plugin marketplace or skill loading
mechanism at this time. The Cursor runner and sandbox images include the
Cursor Agent CLI binary but do not pre-install skills from the
skills-registry.

Plugin installation and filtering (`install-plugins`, `enable-plugins`)
gracefully skip for Cursor with an INFO message. When Cursor adds a
plugin/extension system in the future, the corresponding installation
and filtering logic will be implemented.

## Plugin-skills manifest

Both install scripts generate a manifest at
`/usr/local/share/agentic-ci/plugin-skills.manifest.json` that maps
plugin names to the skills they contain:

```json
{
  "odh-ai-helpers": ["git-shallow-clone", "code-review", "cve-scan", ...],
  "code-review-skills": ["gitlab-code-review"],
  "rfe-creator": ["rfe.create", "rfe.review", ...]
}
```

For Claude Code, the manifest is informational (debugging, auditing).
For OpenCode, the manifest is functional — `enable-plugins` uses it to
apply per-skill filtering at runtime (see below).

## Filtering plugins at runtime

The `AGENT_ENABLED_PLUGINS` environment variable controls which plugins
are active. If unset, all plugins are enabled. If set to a
comma-separated list of plugin names, only those plugins are active.

```bash
# Enable only odh-ai-helpers — all other plugins are disabled
AGENT_ENABLED_PLUGINS=odh-ai-helpers agentic-ci run "do something" ...

# Enable two plugins
AGENT_ENABLED_PLUGINS=odh-ai-helpers,code-review-skills agentic-ci run ...
```

### How filtering works

The `enable-plugins` script runs at container startup (called by
`entrypoint.sh` for Podman, or by the env script for OpenShell). It
reads `AGENT_ENABLED_PLUGINS` and `AGENT_TOOL` to decide which mechanism
to use:

**Claude Code** (`AGENT_TOOL=claude`): Sets `enabledPlugins` entries to
`false` in `~/.claude/settings.json` for unwanted plugins. Claude Code
reads this at startup and skips disabled plugins.

**OpenCode** (`AGENT_TOOL=opencode`): Reads the plugin-skills manifest
to find which skills belong to unwanted plugins, then writes
`"skill-name": "deny"` entries to `opencode.json`'s `permission.skill`
section. OpenCode hides denied skills from the agent.

### Error handling

If a requested plugin name doesn't match any installed plugin, the script
exits with an error and reports which names were unmatched:

```text
Matched: odh-ai-helpers
ERROR: unknown plugin(s) in AGENT_ENABLED_PLUGINS: nonexistent-plugin
```

## CLI subcommands

Plugin installation and filtering are `agentic-ci` subcommands
(implemented in `src/agentic_ci/plugins.py`):

| Command | When | Purpose |
|---------|------|---------|
| `agentic-ci install-plugins` | Build time | Install plugins for Claude Code (reads seed dir) or OpenCode (`--harness opencode --marketplace-json <path>`) |
| `agentic-ci enable-plugins` | Runtime | Filter active plugins based on `AGENT_ENABLED_PLUGINS` |

The container entrypoint (`images/runner/shared/entrypoint.sh`) calls
`agentic-ci enable-plugins` at startup. For OpenShell sandboxes, the env
script calls it before the agent runs.

## Adding skills without rebuilding

Mount a directory containing SKILL.md files and point Claude Code at it:

```bash
podman run --rm \
  -v ./my-skills:/opt/extra-skills:ro \
  -e CLAUDE_CODE_PLUGIN_SEED_DIR=/opt/extra-skills \
  quay.io/aipcc/agentic-ci/claude-runner:latest
```

For OpenCode, mount into the skills directory directly:

```bash
podman run --rm \
  -v ./my-skills:/home/agent-ci/.config/opencode/skills:ro \
  quay.io/aipcc/agentic-ci/opencode-runner:latest
```
