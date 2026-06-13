# EmailDelivery.com Community Edition
[![MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/bkono/edcom-ce/blob/main/LICENSE)

**EmailDelivery.com Community Edition** is an open-source fork of the commercial email marketing platform [EmailDelivery.com](https://emaildelivery.com). 

This project provides the core functionality of the commercial platform, restructured for community-driven development and deployment from source.

> [!NOTE]
> For feature usage, workflows, and API references, please refer to the official [EmailDelivery.com Documentation](https://docs.emaildelivery.com). 

This document focuses on building, configuring, and running the Community Edition.

---

## Table of Contents

- [Overview](#overview)
- [Getting Started](#getting-started)
  - [System Requirements](#system-requirements)
  - [Building from Source](#building-from-source)
  - [Configuration](#configuration)
- [Running in Development](#running-in-development)
- [Project Structure](#project-structure)
- [Production Deployment](#production-deployment)
- [Call for Maintainers](#call-for-maintainers)
- [Modernization Roadmap](#modernization-roadmap)

---

## Overview

EmailDelivery.com Community Edition includes:

- An application backend built in Python
- A React-based frontend
- A custom MTA (Velocity) and SMTP relay services written in Go
- Docker-based development and production environments

This repository is intended for developers who want to contribute to the platform.

---

## Getting Started

### System Requirements

- Docker and Docker Compose
- Python 3.11+
- Node.js 18+
- Tested development environments: macOS (Apple Silicon/arm64), Ubuntu Linux (amd64), and WSL2 (Ubuntu)

### Building from Source

Clone the repository:
```bash
git clone https://github.com/bkono/edcom-ce.git
cd edcom-ce
```

Build the development environment:
```bash
dev/build_node_base.sh
dev/build_python_base.sh
docker compose build
```

To generate redistributable **amd64** artifacts for both the ESP platform and Velocity MTA:

```bash
dev/build_amd64.sh
```

To generate redistributable **arm64** artifacts for both the ESP platform and Velocity MTA:

```bash
dev/build_arm64.sh
```

The build artifacts will be saved in the `.build/` directory.

---

## Configuration

Before running the platform in development, copy the default config file and customize as needed:

```bash
cp config/edcom.defaults.json config/edcom.json
```

Set `"admin_url"` to `http://localhost:3000` in `config/edcom.json` for local development.

Create a `.env` file with the following content:

```bash
echo 'PLATFORM_IP=0.0.0.0' > .env
```

## Beefree is embedded as an optional licensed editor

- **Bring your own Beefree license:** Edit `config/edcom.json` to add your Beefree Client ID, Client Secret, and Content Services API key.
- **Enable Beefree with an EmailDelivery.com commercial license:** Create `config/commercial_license.key` containing your license key.

---

## Running in Development

### Apple Silicon / Mac 

Run with the default configuration:

- **Lite mode** (minimal services):
  ```bash
  docker compose --profile=lite up
  ```

- **Full mode** (all services):
  ```bash
  docker compose --profile=full up
  ```
### Intel/AMD 

You must specify the architecture override file:

- **Lite mode** (minimal services):
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.override.amd64.yml --profile=lite up
  ```

- **Full mode** (all services):
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.override.amd64.yml --profile=full up
  ```

### Create an admin account

After launching, create an admin user:

```bash
./create_admin.sh
```

### Port Information

- **Frontend (Node debug server)**: [http://localhost:3000](http://localhost:3000)
- **Nginx proxy**: Used only for proxying in development. In production, it serves the frontend on ports 80 and 443.

---

## Project Structure

```
api/           - Python backend: API server, background tasks, cron jobs
client/        - React SPA: user interface
config/        - Default configuration files
data/          - Ephemeral runtime data (e.g., database, uploads, logs)
dev/           - Developer utilities and setup scripts
schema/        - SQL schema for initial database setup
screenshot/    - Node.js service for email template screenshots
scripts/       - Python utilities for administrative tasks
services/      - Docker service definitions
smtprelay/     - Go service that receives mail via SMTP and relays it over HTTP
test/          - Unit tests (mostly for contact list features)
velocity/      - Velocity MTA (written in Go) for high-throughput mail delivery
```

---

## Production Deployment

In production:

- The frontend is statically built and served by Nginx
- Nginx listens on ports **80** and **443** (if SSL is configured)
- Nginx proxies API requests to the application backend

For production builds, see the files in `.build/` created by the scripts in `dev` and consult the project [README](https://github.com/bkono/edcom-ce/blob/main/README.md) for deploying Docker images.

---

## Call for Maintainers

EmailDelivery.com Community Edition is a transition from a mature commercial product to a community-led project and is currently operating under a "Product Owner" governance model.

This software does a staggering number of things and has accumulated technical debt during its multi-year evolution. 

This is already a complete ESP; what it really *needs* now is to refocus on the fundamentals. 

A Technical Steering Committee should be established to oversee architectural decisions, code reviews, and release management.

**Contact:** maintainers@emaildelivery.com

---

## Modernization Roadmap

The path from v1.0.0 to v2.0.0 prioritizes performance, scalability, and operator/developer experience over feature expansion.

### Intelligent Pruning for Orphaned List Files

High-volume sending without periodically clearing out `data/buckets/transfer/lists` can accumulate so many files that the filesystem will run out of inodes. 

A safe lifecycle model for `data/buckets/transfer/lists` is needed that:

- Deletes files when they are no longer needed
- Never touches files still in use (including throttled / warmup / scheduled sends)

### Database Performance and Scaling

Upgrade Postgres and revisit schema design with performance as a first-class constraint.

- Postgres is version 15.2 
- A scalable approach to log and event accumulation with archival/roll-up options for long-term analytics is needed
- High-volume sending causes rapid growth across multiple sources, such as SMTP Relay logs, ESP API webhooks, MTA logs
  - A retention strategy is needed, and schema patterns that keep the system fast over time
  - Postgres partitioning seems like it would be of benefit here
- UI responsiveness noticeably degrades as DB size grows
  
### Celery Task Queue Architecture

The current queue behavior serializes work and lacks prioritization, so long-running tasks (e.g., large broadcasts) can block interactive operations (e.g., building a segment). 

The backlog can also experience unbounded growth and become unstable.

Possible paths for improvement:

- Separate queues by workload class (interactive vs batch)
- Intelligent retry/backoff behavior
- Prioritization
- Better tooling to inspect and manage tasks with failure visibility

### Amazon SES API Modernization

Migrate the Amazon SES integration from the SESv1 API to SESv2.

### Public API Feature Parity with UI

Currently, authenticating via session cookies grants full administrative access to all platform features, while the public API is limited in scope. 

Most actions on the platform that aren't exposed by the public API can already be performed with a curl one-liner using user authentication. 

All capabilities should be documented, formalized, and made available via the public API for full programmatic control of the platform.

### UX Resilience

All transient modes of failure in the UI, such as a slow DB, overloaded Celery queue, or network issues, surface as an indefinite spinner and “blank screen” experience with no indication of where to look for the problem. 

### Logging

The default logging state is both noisy and minimal. 

Logging should provide clear answers to basic operational questions and be structured, readable, and actionable. 

- What's happening with this SMTP Relay broadcast right now? 
- Why did this automated email not trigger? 

### Operations Observability

Operators need a monitoring suite with a clear dashboard view of system health and activity, such as: 

- DB health/connection saturation, slow queries
- Celery queue depth, task latency/error rates
- Status of containers 
- Host resource utilization (CPU, memory, disk)
- API and webhook traffic 

### Release and Upgrade Experience

Ship an upgrade mechanism with release artifacts. 

The Community Edition should include a simple, reliable upgrade script, similar to the commercial edition, that can safely pull and apply the latest release.

### Archaic React

React needs to be upgraded from version 15.6.2.

### TinyMCE is EOL

MIT-licensed TinyMCE is EOL and needs to be replaced with another WYSIWYG editor.

### Internationalization

Add i18n support via `react-i18next`, with translation files exposed for user editing. 
