# Skill Loading

The container image uses `install-plugin`, a pure-Python script that
populates Claude Code's plugin cache non-interactively at build time.

## How it works

1. A marketplace repo (e.g. `opendatahub-io/skills-registry`) is
   registered — the script clones it and records it in Claude Code's
   settings
2. For each plugin in the marketplace, the script:
    - Shallow-clones the plugin's source repository
    - Copies skills, agents, commands, and gems into the plugin cache
    - Writes an `.in_use` marker
    - Updates `installed_plugins.json` with version metadata
    - Adds the plugin to `enabledPlugins` in `settings.json`

## Plugin cache structure

```text
~/.claude/
  settings.json                              # enabledPlugins
  plugins/
    known_marketplaces.json                  # marketplace registry
    installed_plugins.json                   # plugin version tracking
    marketplaces/
      opendatahub-skills/
        marketplace.json                     # from skills-registry
    cache/
      opendatahub-skills/
        odh-ai-helpers/
          0.1.0/
            skills/
            agents/
            .in_use
        rfe-creator/
          0.1.0/
            skills/
            .in_use
        ...
```

## CLI reference

### Register a marketplace

```bash
install-plugin --marketplace-repo opendatahub-io/skills-registry
```

Clones the GitHub repo containing `.claude-plugin/marketplace.json` and
records it as a known marketplace.

### Install all plugins from a marketplace

```bash
install-plugin --all
```

Iterates every registered marketplace and installs all listed plugins.

### Install specific plugins by name

```bash
install-plugin odh-ai-helpers rfe-creator
```

Looks up the named plugins in registered marketplaces and installs them.

### Install from a git URL

```bash
install-plugin --repo https://github.com/org/my-plugin.git
```

Clones the repo directly (no marketplace needed) and installs any
skills, agents, commands, or gems found in standard locations
(`.claude/skills/`, `.claude/agents/`, etc.).

## Skill discovery

For plugins listed in a marketplace, the script uses the `skills` and
`agents` arrays from the marketplace entry to locate content in the
source repo. For example:

```json
{
  "name": "odh-ai-helpers",
  "skills": ["./helpers/skills"],
  "agents": ["./helpers/agents/python-packaging-investigator.md"]
}
```

If no arrays are provided, the script falls back to standard
directories: `.claude/skills/`, `.claude/agents/`, `.claude/commands/`,
`.claude/gems/`.

## Runtime skill injection

For ad-hoc skill additions without rebuilding the image, mount a
directory and set the seed environment variable:

```bash
podman run \
  -e CLAUDE_CODE_PLUGIN_SEED_DIR=/opt/extra-skills \
  -v ./my-skills:/opt/extra-skills:ro \
  quay.io/aipcc/agentic-ci/claude-runner:latest
```
