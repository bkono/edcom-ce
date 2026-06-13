# AGENTS.md

## Repository Authority

This repository is the authoritative fork of EDCom CE for Bryan's production
server. Do not route install, upgrade, release, or documentation work back to
the upstream `emaildelivery/edcom-ce` repository unless explicitly asked.

Production server updates use GitHub Release artifacts from this fork:
`bkono/edcom-ce`. The legacy commercial/Keygen updater is deprecated and must
not be reintroduced.

## Production Update Flow

- Build release artifacts from this fork.
- Publish `edcom-install-<arch>.tgz`, `velocity-install-<arch>.tgz`, and
  `checksums.txt` to this fork's GitHub Releases.
- Server-side `upgrade.sh` checks this fork's latest release, downloads the
  matching architecture artifact, verifies checksums when available, loads the
  bundled Docker images, and restarts compose.
- Cron entries must `cd /root/edcom-install` before invoking `./upgrade.sh`.

## Commit Workflow

- Small, atomic commits are mandatory for all completed work.
- Committing completed work is mandatory. Do not leave completed changes only
  in the working tree.
- Each commit must contain one coherent behavior-scoped change. Split docs, CI,
  updater, build, server-handoff, and application changes into separate commits.
- Use conventional commit messages, for example:
  `feat(updater): ...`, `ci(release): ...`, `build(release): ...`,
  `docs(update): ...`.
- Do not create broad "misc" commits or combine unrelated concerns.

## Quality Gate

Before committing or pushing completed work, run the relevant checks for the
files touched. For release/update scripting, this includes:

- `git diff --check`
- `bash -n <changed shell scripts>`
- `shellcheck <changed shell scripts>`
- YAML parsing or workflow validation for GitHub Actions changes
- Any targeted tests or build checks required by the changed application code

Review GitHub Actions failures before pushing additional fixes.
