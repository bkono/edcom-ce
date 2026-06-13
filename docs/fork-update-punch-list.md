# Fork-Owned Update Punch List

This repo is the deployment authority for Bryan's EDCom CE server. The legacy
commercial Keygen updater is deprecated and must not be used after the first
fork migration.

## Repo Changes

- [x] Add a fork-owned `upgrade.sh` that checks GitHub Releases for
  `bkono/edcom-ce`.
- [x] Compare the installed `config/VERSION` to the latest release tag.
- [x] Download `edcom-install-<arch>.tgz` from the latest fork release.
- [x] Verify the archive against `checksums.txt` when available.
- [x] Extract the archive over the install directory, load bundled Docker
  images, restart compose, and prune old Docker images.
- [x] Package `upgrade.sh` into future `edcom-install` archives.
- [x] Make build scripts accept a caller-supplied `BUILD` value so all release
  artifacts share one version.
- [x] Name artifacts consistently with the README:
  `edcom-install-amd64.tgz`, `edcom-install-arm64.tgz`,
  `velocity-install-amd64.tgz`, and `velocity-install-arm64.tgz`.
- [x] Add a GitHub Actions release workflow that builds amd64 and arm64
  archives on merges to `main`, publishes a versioned release, and attaches
  `checksums.txt`.

## Server Snapshot

- [x] Create a timestamped backup directory under `/root`.
- [x] Save root crontab.
- [x] Save Docker image inventory and compose service state.
- [x] Save `/root/edcom-install` scripts, config, compose files, schema, setup
  assets, buckets, and current image tarballs.
- [x] Save a logical PostgreSQL dump from `edcom-database`.
- [x] Record installed version and architecture.

Snapshot location: `/root/edcom-pre-fork-20260613T174413Z`

## First Fork Migration

- [x] Confirm the first fork GitHub Release exists and has
  `edcom-install-amd64.tgz` plus `checksums.txt`.
- [x] Replace `/root/edcom-install/upgrade.sh` with the fork-owned updater.
- [x] Disable the legacy Keygen cron entry until a fork release is available.
- [x] Run `cd /root/edcom-install && ./upgrade.sh --check-only`.
- [x] Run `cd /root/edcom-install && ./upgrade.sh --no-prompt` once the
  latest fork release is ready.
- [x] Verify `config/VERSION`, Docker image creation times, service health, and
  login path after restart.
- [x] Install daily cron:
  `17 3 * * * cd /root/edcom-install && ./upgrade.sh --no-prompt >> /var/log/edcom-upgrade.log 2>&1`

Migration completed against release `v1.20260613.1754`. The first restart hit a
one-time compose project-name conflict because the fork compose declares
`name: edcom` while the commercial install used the directory-derived project
name. Recovery used `/root/edcom-install/docker-compose.pre-fork-migration.yml`
to stop/remove the old project containers, then `docker compose up -d` with the
fork compose.

Production-specific compose behavior is preserved in
`/root/edcom-install/docker-compose.override.yml`: machine-id mounts,
`edcom-tasks` container name, and the `segments` worker.

## Decision Ledger

### Decisions

- GitHub Releases from `bkono/edcom-ce` are the update source of truth.
- Release tags are deployment versions. The archive's `config/VERSION` must
  match the release tag without the leading `v`.
- The installed server keeps the existing local Docker image tarball model.

### Rejected / Closed Doors

- Do not keep using Keygen artifacts for server upgrades.
- Do not switch the VPS to a live `git pull` deployment.
- Do not run the first fork upgrade before the fork has published release
  artifacts.

### Invariants

- Preserve `/root/edcom-install/config/edcom.json`, `config/edcom.env`,
  certificate files, `data/database`, `data/buckets`, and runtime logs.
- The updater must not reference `api.keygen.sh` or `emaildelivery/edcom-ce`.
- Server cron must `cd /root/edcom-install` before invoking `upgrade.sh`.
