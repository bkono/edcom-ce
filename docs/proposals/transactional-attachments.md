# Transactional Attachment Support Proposal

## Context

Issue: <https://github.com/emaildelivery/edcom-ce/issues/1>

The immediate production need is attachment support for transactional email, especially PDF invoice/payment documents submitted through the transactional SMTP relay and API. The historical reason attachments were not implemented was operational risk: uncontrolled attachment volume could exhaust small VPS disks.

This fork should solve the immediate transactional use case without baking in a design that blocks later campaign and funnel attachment support.

## Current Behavior

- `smtprelay` accepts MIME messages, extracts only the best HTML/plain text body, and drops attachment parts before forwarding to the API.
- `smtprelay` submits a JSON payload to `/api/transactional/send`; the relay payload has no attachment field.
- `/api/transactional/send` accepts JSON, validates sender/recipient/template settings, writes raw non-template bodies to `/buckets/data/templates/txn/...`, and queues a JSON payload in `txnqueue.data`.
- `send_txn` reads the queued body, deletes the temporary body file, generates final HTML, then calls `send_backend_mail`.
- Provider sends already converge on shared helpers in `api/shared/send.py`: `ses_send`, `mailgun_send`, `sparkpost_send`, and `smtprelay_send`.
- Despite helper names, `api/shared/s3.py` is local filesystem storage under mounted `/buckets/*`; there is no current real S3/R2 backend.
- SES currently uses `send_email`, which cannot support attachments in this code path. SES attachment sends require raw MIME via `send_raw_email`.

## Goals

- Support transactional SMTP relay attachments.
- Support transactional API attachments.
- Support SES outbound routes as a first-class path.
- Avoid storing attachment bytes in Postgres.
- Prevent VPS disk exhaustion through limits, lifecycle cleanup, and optional object storage.
- Create a storage/provider foundation that can be reused by campaigns and funnels later.

## Non-Goals For The First Build

- Campaign or funnel attachment UI.
- Campaign or funnel send behavior changes.
- Velocity/internal MTA attachment support.
- Persistent document library / asset manager semantics.
- Virus scanning, DLP, or attachment content policy beyond size/type/filename hygiene.

## Recommended Architecture

Introduce attachment handling as three separate concerns:

1. **Attachment ingestion**
   - API JSON base64 attachments.
   - SMTP relay MIME attachment extraction.
   - Later: multipart API upload or presigned upload.

2. **Attachment storage**
   - Store bytes outside Postgres.
   - Queue only an attachment manifest.
   - Default to local filesystem storage to match existing deployments.
   - Add an S3-compatible backend for AWS S3, Cloudflare R2, and MinIO.

3. **Provider rendering**
   - Build provider-specific outbound payloads from the same attachment manifest.
   - SES: raw MIME with `send_raw_email`.
   - SMTP relay backend: raw multipart MIME over `smtplib`.
   - Mailgun: multipart `attachment` fields.
   - SparkPost: `content.attachments[]`.
   - Velocity/internal MTA: unsupported until its JSON API and sender can carry attachments.

This keeps the first implementation transactional-focused while putting the reusable boundary in the provider/storage layer rather than inside transactional-only code.

## Attachment Manifest

Queued messages should store metadata, not bytes:

```json
{
  "attachments": [
    {
      "id": "shortuuid",
      "storage": "local",
      "bucket": "attachments",
      "key": "txn/<cid>/<message-id>/<attachment-id>",
      "filename": "invoice.pdf",
      "content_type": "application/pdf",
      "disposition": "attachment",
      "content_id": null,
      "size": 184233,
      "sha256": "..."
    }
  ]
}
```

The manifest shape should be shared by transactional, campaign, and funnel send paths even if only transactional paths populate it in the first phase.

## Storage Options

### Option A: Local Spool Only

Store attachment bytes under a local mounted path, for example `/buckets/data/attachments/...`, then delete after successful provider acceptance or final error.

Pros:
- Smallest implementation.
- Fits current deployment model.
- Reuses existing temporary-body lifecycle pattern.

Cons:
- Still VPS disk-backed.
- Requires strict caps and cleanup to address the original product concern.
- Harder to scale if transactional volume or attachment size grows.

Use this only as a first phase if the storage interface is still designed for S3-compatible backends.

### Option B: Local + S3-Compatible Backend

Add a storage abstraction with local filesystem as default and S3-compatible object storage as a production option. AWS S3, Cloudflare R2, and MinIO are all viable for the needed object operations.

Pros:
- Best production answer for disk exhaustion.
- Cloudflare R2 avoids egress surprise for many workflows.
- Lifecycle policies can clean up orphaned objects defensively.
- Keeps large bytes outside Postgres and VPS storage.

Cons:
- More config and operational validation.
- More error modes: credentials, bucket policy, endpoint, regional latency.

Recommended first durable target.

### Option C: Two-Step Upload / Presigned API

Add an API surface for attachment upload first, then send references attachment IDs.

Pros:
- Better for large API clients.
- Avoids base64 request bloat.
- Natural fit for R2/S3 direct upload later.

Cons:
- More API surface and lifecycle semantics.
- Not required for SMTP relay support.

Best as a later phase after the manifest/storage/provider model exists.

### Rejected: Store Base64 In `txnqueue.data`

This is easy but wrong for production. It bloats Postgres, increases queue pressure, complicates retries, and recreates the storage exhaustion problem in a worse layer.

## API Contract

First phase JSON support:

```json
{
  "to": "customer@example.com",
  "fromemail": "billing@example.com",
  "subject": "Invoice",
  "body": "<p>Attached.</p>",
  "attachments": [
    {
      "filename": "invoice.pdf",
      "content_type": "application/pdf",
      "content": "base64..."
    }
  ]
}
```

Validation:
- `attachments` optional.
- Reject if attachments disabled.
- Reject invalid base64.
- Reject empty filename after sanitization.
- Reject path separators/control characters in filename.
- Reject unsupported disposition values.
- Enforce per-attachment decoded byte cap.
- Enforce total decoded attachment cap.
- Enforce final estimated MIME size cap.

Later API extension:
- `multipart/form-data` for clients that should not base64 large payloads.
- Presigned/direct upload if using S3/R2 and clients can support it.

## SMTP Relay Contract

Relay parsing should:
- Continue extracting best HTML/plain body.
- Extract `Content-Disposition: attachment` parts from nested `multipart/mixed`.
- Preserve filename, content type, disposition, optional content ID.
- Decode content transfer encoding.
- Enforce relay-side max message and attachment limits before posting to API.
- Forward attachments to `/api/transactional/send` using the same JSON attachment contract.

Gap after this phase:
- SMTP relay still buffers decoded attachment bytes before posting to the API. This is acceptable only with strict limits. A later S3/R2-aware relay could upload objects directly and submit manifests instead.

## SES Path

SES is required for production.

Current code path:
- `do_ses_send` calls `sesclient.send_email(...)`.

Attachment path:
- If no attachments, keep current `send_email` path.
- If attachments exist, build a raw MIME message and call `send_raw_email`.
- Preserve current `sesmessages` insert behavior using the returned `MessageId`.
- Preserve current soft-error handling and stats behavior.

This should be implemented through a reusable MIME builder, not as ad hoc SES-only string assembly.

## Campaign / Funnel Reuse

The proposed foundation is reusable if the attachment manifest and provider rendering live below transactional-specific code.

Campaigns and funnels already converge on the same provider helpers:
- Campaigns call `mailgun_send`, `ses_send`, `sparkpost_send`, `smtprelay_send`, or Velocity `/send-lists`.
- Funnels call the same helpers for provider routes.
- Tests/direct paths can use `send_backend_mail`.

Reusable foundation:
- Attachment manifest schema.
- Storage backend interface.
- MIME builder.
- Provider attachment renderers.
- Limit/cleanup services.

Not automatically reusable yet:
- Campaign/funnel data models need attachment fields.
- Campaign/funnel UI needs authoring semantics.
- Bulk sends need scaling rules so one attachment is referenced once and reused across many recipients instead of copied per recipient.
- Velocity/internal MTA must get a new attachment contract before it can support attachments.

Recommendation:
- Architect the core for `EmailAttachmentManifest[]` everywhere.
- Implement only transactional population/use in phase 1.
- Add campaign/funnel support as a later feature using the same manifest, but with campaign/funnel-specific product decisions.

## Velocity / Internal MTA Gap

Velocity currently accepts `/send-addr` and `/send-lists` JSON commands with template/list fields but no attachment field. Its MIME generation/DKIM path assumes an HTML-only message shape.

Phase 1 behavior:
- If a transactional message has attachments and the selected route resolves to Velocity/internal MTA, reject with a clear error before send.

Later Velocity support requires:
- Extend `APICmd` / `SendAddrCmd` / `SendListsCmd` with attachment manifests or fetch URLs.
- Fetch attachment bytes in the sender process.
- Generate multipart MIME.
- Update DKIM signing header choices as needed.
- Add message size enforcement in Velocity, not only API.

## Phased Plan

### Phase 0: Lock Contracts

Deliverables:
- Document attachment manifest.
- Add config names/defaults.
- Decide max sizes.
- Decide local path and S3/R2 env shape.

Gaps after phase:
- No runtime behavior changes.

### Phase 1: Transactional Local Storage + SES

Deliverables:
- Local attachment storage backend.
- Attachment validation and manifest creation in `/api/transactional/send`.
- SMTP relay MIME extraction.
- SES raw MIME path for attachment-bearing sends.
- Clear rejection for unsupported attachment routes.
- Cleanup on success/error and orphan cleanup cron.
- Tests for SMTP extraction, API validation, SES raw MIME generation, cleanup.

Gaps after phase:
- API base64 only; no multipart/presigned upload.
- Local disk still used.
- Mailgun/SparkPost/external SMTP attachment routes may be rejected unless implemented in this same phase.
- Campaigns/funnels unsupported.
- Velocity unsupported.

### Phase 2: S3-Compatible Storage Backend

Deliverables:
- Storage abstraction implementation for S3-compatible endpoints.
- AWS S3/R2/MinIO config.
- Optional lifecycle policy documentation.
- Startup/config validation.
- Tests using mocked S3 client or MinIO in integration.

Gaps after phase:
- SMTP relay still posts base64 JSON to API unless direct-upload relay support is added.
- Campaign/funnel authoring still unsupported.

### Phase 3: Provider Completion

Deliverables:
- Mailgun attachments.
- SparkPost attachments.
- External SMTP relay attachments.
- Provider-specific test coverage.

Gaps after phase:
- Velocity/internal MTA still unsupported unless explicitly added.
- Campaign/funnel authoring still unsupported.

### Phase 4: Campaign / Funnel Attachment Support

Deliverables:
- Add attachment fields to message/template data models.
- UI for campaign/funnel stage attachments.
- Reuse one stored object across all recipients.
- Enforce campaign-level total attachment caps.
- Update send paths to pass attachment manifests to provider helpers.

Gaps after phase:
- Velocity may still be unsupported unless phase 5 is included.

### Phase 5: Velocity / Internal MTA Attachment Support

Deliverables:
- Extend Velocity JSON API.
- Fetch attachment bytes or signed URLs.
- Generate multipart MIME.
- Preserve DKIM/tracking behavior.
- Add Velocity message-size enforcement.

Gaps after phase:
- None known for supported providers, subject to provider-specific limits.

## Operational Limits

Suggested initial defaults:
- Attachments disabled by default for existing installs, enabled explicitly.
- Max attachments per message: 5.
- Max decoded attachment bytes each: 10 MiB.
- Max decoded attachment bytes total: 15 MiB.
- Max estimated final MIME bytes: 25 MiB.
- Attachment TTL: 24 hours.
- Cleanup cadence: hourly or daily cron.

The final MIME cap matters because base64 transfer encoding adds roughly one third overhead before provider-specific limits apply.

## Open Questions

- Should local storage be acceptable for the first production deploy if limits are strict, or should R2/S3 be part of the first release?
- Should Mailgun/SparkPost/external SMTP be supported in phase 1 or explicitly rejected until phase 3?
- Should transactional API support only JSON base64 initially, or should `multipart/form-data` be included immediately?
- Should attachments be enabled by default for new installs once S3/R2 is configured?
- Should MIME type allowlist default to broad or PDF-first?

## Decision Ledger

### Decisions

- Use an attachment manifest and store bytes outside Postgres.
- Make SES raw MIME a first-class requirement.
- Put reusable behavior in storage and provider rendering layers, not only in transactional handlers.
- Keep transactional support as phase 1 while designing the manifest for campaign/funnel reuse.

### Rejected / Closed Doors

- Do not store base64 attachment bytes in `txnqueue.data`.
- Do not silently drop attachments on unsupported routes.
- Do not claim campaign/funnel support until their data model, UI, and bulk-send semantics are implemented.
- Do not treat Velocity/internal MTA as automatically supported; it needs its own command and MIME changes.

### Invariants

- Attachment bytes must be bounded, temporary, and cleaned up.
- Provider support must be explicit: deliver or reject clearly.
- SES must support attachments before this feature is production-complete for Bryan's current route setup.
- The manifest shape must remain reusable for future campaigns/funnels.
