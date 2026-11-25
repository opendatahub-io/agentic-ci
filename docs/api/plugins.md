# Plugins

`agentic_ci.plugins` handles plugin and skill installation at build time
and plugin filtering at runtime.

Build-time (called from Containerfiles via `agentic-ci install-plugins`):

- `install_claude_plugins()` — uses the native Claude Code CLI to install
  plugins from a marketplace seed directory.
- `install_opencode_skills()` — clones plugin repos and copies SKILL.md
  files into OpenCode's skills directory.
- `install_codex_plugins()` — installs native Codex plugins when the
  marketplace supports them, with a skills-only fallback for legacy
  marketplaces.

All of them generate a plugin-to-skill manifest at a well-known path.

Runtime (called from entrypoint scripts via `agentic-ci enable-plugins`):

- `enable_plugins()` — reads `AGENT_ENABLED_PLUGINS` and disables unwanted
  plugins via harness-specific mechanisms.
- `find_skill_dir()` — locates the directory of one installed skill, which
  the backends export as `CLAUDE_SKILL_DIR` for harnesses that do not set
  it themselves.

::: agentic_ci.plugins
