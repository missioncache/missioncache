# Contributing to Orbit

Thanks for your interest in contributing. Orbit is a small personal project that grew into a public plugin, and outside eyes are very welcome - bug reports, feature ideas, and pull requests all help.

This document covers the mechanics. For the "why" behind orbit's architecture, read [`docs/architecture.md`](docs/architecture.md) first - it's the anchor for every other doc.

## Ways to contribute

- **Report a bug** - open an issue with steps to reproduce, what you expected, and what happened instead.
- **Suggest a feature** - open an issue and describe the use case. "Orbit should do X" is less useful than "when I'm doing Y I wish orbit would do X because Z."
- **Improve the docs** - typos, clarifications, and new sections are welcome. The contributor docs under `docs/` are especially open to extension.
- **Fix a bug or build a feature** - see the development workflow below.

## Development setup

```bash
git clone https://github.com/tomerbr1/claude-orbit.git
cd claude-orbit
./setup.sh
```

`setup.sh` installs everything: the plugin itself via a local marketplace, `orbit-db` and `orbit-auto` as editable Python packages, the dashboard launchd service, the statusline symlink, and the rule files under `~/.claude/rules/`. It is interactive and will ask before making changes you might not want.

Prerequisites:

- Python 3.11 or newer (orbit uses modern syntax like `str | None` and the walrus operator).
- `git filter-repo` if you plan to work on history-rewrite tooling (optional).
- macOS is the primary test platform. Linux should work for the plugin core; the dashboard launchd integration is macOS-specific.

## Running tests

The top-level `Makefile` is the canonical test runner:

```bash
make test        # full suite, verbose
make test-fast   # stop at first failure, quiet
```

This runs all six component test suites: `mcp-server`, `orbit-db`, `orbit-auto`, `orbit-dashboard`, `hooks`, `statusline`. Expect 284 passing tests on a clean checkout. `main` should be fully green - if a test fails locally that isn't caused by your changes, please open an issue.

## Pull request standards

- **One logical change per PR.** A PR that fixes a bug and also refactors an unrelated module is hard to review. Split it.
- **Commit messages use conventional prefixes**: `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`, `test:`. Keep the subject line under 50 characters and imperative ("Add retry logic", not "Added retry logic"). Body is optional but welcome when the "why" isn't obvious from the diff.
- **Don't reformat files you didn't meaningfully change.** Whitespace-only changes in unrelated files make review noisy.
- **Update tests when you change behavior.** If you can't add a test, say so in the PR description and explain why.
- **Update docs when you change behavior.** Especially if you touch extension points covered in `docs/architecture.md` or any component doc.
- **Keep the working tree deletable.** Don't commit temporary scratch files, `.DS_Store`, backup copies, or editor state.

## Code style

- **Python**: targeted at 3.11+. Prefer `str | None` over `Optional[str]`, `list[dict]` over `List[Dict]`. Type hints on new function signatures. Match the existing module's style rather than imposing a new one.
- **JavaScript / HTML in the dashboard**: `orbit-dashboard/index.html` is a single-file SPA with embedded CSS and JS. No build tools, no bundler. CSS variables for theming. Keep it self-contained.
- **Bash**: `set -euo pipefail` at the top of any new script. `shellcheck` clean.
- **Comments**: explain *why*, not *what*. If a comment only restates the code, delete it. If a comment encodes a non-obvious constraint or a past-bug lesson, keep it.

## Reporting security issues

**Do not open a public GitHub issue for security vulnerabilities.** See [`SECURITY.md`](SECURITY.md) for the private disclosure channel.

## Code of conduct

Orbit follows the [Contributor Covenant](CODE_OF_CONDUCT.md). In short: treat everyone with respect, assume good faith, and keep discussions focused on the code and the project.

## Questions?

Open a GitHub issue with the `question` label. For longer discussions or design debates, the issue is usually the right place too - Orbit doesn't have a separate forum.

Thanks again for being here.
