# Critical Development Rules — Homebound

Homebound is a Django blog generator: it ingests your social media archives (Google+, Facebook, Twitter/X), serves a personal-archive blog, exposes a semantic-search-backed MCP server for LLM agents, and (optionally) hosts a "talk to the author" public bot widget. AGPL-3.0.

This repo contains the application code. Site-specific deployment configs, operational runbooks, and personal data live in your own private overlay repo (the maintainer's lives at `homebound-platform`). See `docs/HIGH_LEVEL_DESIGN.md`.

Cursor rules: `.cursor/rules/` (symlinks to `~/.cursor/shared_rules/`). See `rules_management.mdc` for the layout.

---

## Rule: Only Commit When Explicitly Asked (with Deploy Exception)

**NEVER auto-commit. Wait for words like "commit", "push", "save to git".**

Phrases like "create a file", "looks good", or "task complete" do NOT mean commit.

**Deploy exception:** when the user asks to "deploy", commit and push without a separate ask. Deploying implies the code must be in the repo.

---

## Rule: Never Use git commit --no-verify

If the pre-commit hook fails, fix the underlying issue and try again.

---

## Rule: Ask Before Installing Tools or Using Workarounds

When a tool is missing, present options with tradeoffs. Never silently `brew install` or `apt install`.

---

## Rule: Never Run Long-Running Processes in Foreground

Background long jobs (`nohup ... > /tmp/log 2>&1 &`); foreground is fine for short tests.

---

## Rule: Do Not Pipe Long-Running Commands Through `head` / `tail -N`

Pipe to a file, then `tail -f` or `tail -N` the **file**.

---

## Rule: Audit Docker Topology Before Touching Containers on Production

`docker ps -a --format '...'` before any `down` / `stop` / `rm`. Site-specific deployment rules live in your overlay repo, not here.

---

## Rule: Use SSH Config Aliases for Remote Servers

Define aliases in `~/.ssh/config`; never hardcode IPs or key paths in scripts.

---

## Python / Django

Follow `docs/DJANGO_BLOG_DESIGN.md` and `docs/HIGH_LEVEL_DESIGN.md`. Use a venv for local work (`python3 -m venv .venv`). Keep `requirements.txt` accurate.

---

## Build System

Bazel-based; see `.cursor/rules/bazel.mdc` for conventions. Package layout: `blog/`, `django_config/`, `extractors/`, `tests/`, `tools/`, `mcp_server/`.

- Run tests: `bazel test //tests:...`
- Run server: `bazel run //:runserver` (port 8080)
- Manage commands: use the venv pattern documented in `docs/HIGH_LEVEL_DESIGN.md`
