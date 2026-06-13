# Transactional Attachment Support Proposal

## Context

Issue: <https://github.com/emaildelivery/edcom-ce/issues/1>

The production requirement is attachment support for transactional email, especially PDF invoice/payment documents submitted through both:

- The transactional SMTP relay.
- The transactional API.

SMTP relay support and API support are equal-priority entry points. The target production server has constrained local disk, so the feature must not depend on local attachment spooling as the normal production path.

The historical reason attachments were not implemented was operational risk: uncontrolled attachment volume could exhaust small VPS disks. This proposal treats that as a design constraint, not as a later cleanup item.

## Current Behavior

- `smtprelay` accepts MIME messages, extracts only the best HTML/plain text body, and drops attachment parts before forwarding to the API.
- `smtprelay` submits JSON to `/api/transactional/send`; the relay payload has no attachment field.
- `/api/transactional/send` accepts JSON, validates sender/recipient/template settings, writes raw non-template bodies to `/buckets/data/templates/txn/...`, and queues JSON in `txnqueue.data`.
- `send_txn` reads the queued body, deletes the temporary body file, generates final HTML, then calls `send_backend_mail`.
- Provider sends already converge on shared helpers in `api/shared/send.py`: `ses_send`, `mailgun_send`, `sparkpost_send`, and `smtprelay_send`.
- Despite helper names, `api/shared/s3.py` is local filesystem storage under mounted `/buckets/*`; there is no current real S3/R2 backend.
- SES currently uses `send_email`. Attachment-bearing SES sends need either SES API v2 attachment support or raw MIME. Given the current code uses boto3 SES v1-style `send_email`, the least disruptive implementation is raw MIME via `send_raw_email`.

## Ship Target

There are no intentionally deployable intermediate states for this feature. The branch should build, validate, and ship one complete transactional attachment implementation.

The complete ship target is:

- Transactional SMTP relay accepts attachment-bearing MIME messages.
- Transactional API accepts attachment-bearing requests.
- Attachment bytes are stored outside Postgres.
- S3-compatible storage is available on day 0.
- Cloudflare R2 is available on day 0 through the same S3-compatible backend.
- Local storage exists only as an explicit dev/test/small-install backend, not as the recommended production backend.
- SES postal routes support attachment-bearing transactional sends.
- Unsupported postal route types reject attachment-bearing sends clearly before queue acceptance.
- Cleanup is enforced in the app and by storage backend lifecycle policy where available.

## Goals

- Support transactional SMTP relay attachments.
- Support transactional API attachments.
- Support SES outbound routes as a first-class production path.
- Support S3 and R2 attachment storage at initial release.
- Avoid storing attachment bytes in Postgres.
- Avoid base64 relay-to-API transport for SMTP attachments.
- Avoid unbounded local disk, memory, request, or queue growth.
- Leave a provider/storage foundation that can be reused by campaigns and funnels later.

## Non-Goals

- Campaign/funnel attachment authoring or send behavior.
- Velocity/internal MTA attachment support.
- Persistent document library / asset manager semantics.
- Virus scanning, DLP, or deep attachment content policy beyond size/type/filename hygiene.
- Supporting every outbound provider before transactional SES is production complete.

## External Constraints

- SES supports multiple attachments up to a 40 MB total message size limit and documents unsupported file extensions. The implementation should set a lower platform default to leave MIME/base64 overhead and provider variance headroom.
- AWS S3 lifecycle rules can expire objects.
- Cloudflare R2 supports object lifecycle rules, including prefix-based expiration, and exposes lifecycle configuration through dashboard, Wrangler, and S3 API-compatible operations.
- R2 supports S3-compatible client configuration with an account-specific endpoint and `region = "auto"`.

Sources:
- <https://docs.aws.amazon.com/ses/latest/dg/attachments.html>
- <https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html>
- <https://developers.cloudflare.com/r2/buckets/object-lifecycles/>
- <https://developers.cloudflare.com/r2/api/s3/api/>

## Recommended Architecture

Introduce attachment handling as four separate concerns:

1. **Ingress parsing**
   - SMTP relay parses inbound MIME and extracts attachments.
   - API accepts attachment-bearing multipart requests.
   - API may also accept JSON base64 attachments for compatibility, but multipart is the primary large-payload path.

2. **Object storage**
   - Store bytes outside Postgres.
   - Queue only attachment manifests.
   - Production backend: S3-compatible object storage.
   - Supported production targets: AWS S3 and Cloudflare R2.
   - Local backend: explicit dev/test fallback.

3. **Transactional queue contract**
   - `txnqueue.data` stores sender/recipient/template data plus `attachments[]` manifests.
   - No queued base64 payloads.
   - No local temp filenames that are required for worker correctness.

4. **Provider rendering**
   - Build provider-specific outbound payloads from the same attachment manifest.
   - SES: MIME message sent with `send_raw_email`.
   - External SMTP backend: MIME message over `smtplib` if supported in this ship target.
   - Mailgun/SparkPost: either implemented from the same manifest or explicitly rejected for attachment-bearing transactional sends until added.
   - Velocity/internal MTA: explicitly rejected for attachment-bearing transactional sends.

The reusable boundary is the manifest + storage + provider rendering layer. Transactional code should populate the manifest, not own the storage abstraction or MIME assembly details.

## Attachment Manifest

Queued messages store metadata, not bytes:

```json
{
  "attachments": [
    {
      "id": "shortuuid",
      "storage_backend": "s3",
      "bucket": "edcom-attachments",
      "key": "attachments/txn/<cid>/<message-id>/<attachment-id>",
      "filename": "invoice.pdf",
      "content_type": "application/pdf",
      "disposition": "attachment",
      "content_id": null,
      "size": 184233,
      "sha256": "...",
      "created_at": "2026-06-13T00:00:00Z",
      "expires_at": "2026-06-14T00:00:00Z"
    }
  ]
}
```

Rules:
- The manifest is the only data structure passed through queues and provider helpers.
- Object keys are generated server-side.
- Object keys are not public URLs.
- Attachment objects are private.
- Attachment IDs and object keys are unique per queued transactional message.
- Cleanup must be safe to run repeatedly.

## Storage Backend Contract

Create a new attachment storage abstraction instead of extending the existing filesystem-only `api/shared/s3.py` semantics in place.

Required operations:
- `put_stream(key, stream, expected_size, content_type, metadata) -> manifest_fields`
- `open_read(manifest) -> binary stream`
- `delete(manifest) -> None`
- `exists(manifest) -> bool`
- `configure_lifecycle(prefix, ttl_days) -> validation/result`
- `healthcheck() -> validation/result`

Production backend:
- S3-compatible client using boto3.
- Configurable endpoint URL for R2.
- Configurable region, bucket, access key, secret key.
- R2 uses account endpoint and `region_name="auto"`.

Local backend:
- Uses a configured mounted path.
- Allowed for development and explicit small installs.
- Must enforce the same size/TTL/delete contract.

Do not store attachment objects under publicly served `/transfer` or image buckets.

## Ingress Contract

### API Multipart Request

Primary API path for attachments should be `multipart/form-data`:

- One JSON metadata field, for example `payload`.
- One or more file parts, for example `attachments`.
- File parts stream into the configured attachment storage backend.
- API stores only manifests in `txnqueue.data`.

This avoids bloating JSON requests and avoids the relay/API base64 gap.

### API JSON Request

Optional compatibility path:

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

If supported, JSON base64 must have stricter request limits than multipart. It is not the preferred path for large attachments.

### SMTP Relay

Relay parsing should:

- Continue extracting best HTML/plain body.
- Extract attachment parts from nested `multipart/mixed` messages.
- Preserve filename, content type, disposition, optional content ID.
- Decode transfer encoding correctly.
- Enforce relay-side max message and max attachment limits.
- Submit to `/api/transactional/send` as multipart, not JSON base64.

The relay will still temporarily hold decoded parts while parsing the SMTP message. That is acceptable only because the SMTP server already has a configured max message size and the relay must reject oversized messages before API submission. The important production constraint is that the relay does not write attachments to local disk and does not send inflated base64 JSON to the API.

## SES Path

SES is required for production.

Current code path:
- `do_ses_send` calls `sesclient.send_email(...)`.

Attachment path:
- If no attachments, keep current `send_email` path.
- If attachments exist, load attachment streams from object storage and build a MIME message.
- Send with `sesclient.send_raw_email(...)`.
- Preserve current `sesmessages` insert behavior using returned `MessageId`.
- Preserve current soft-error handling and stats behavior.
- Enforce the platform message-size cap before calling SES.

Optimization when storage is S3/R2:
- Do not download attachment objects until the message is actually selected for send.
- Stream object reads into MIME assembly where practical.
- Use bounded temp files only if the Python email/MIME builder requires materialization.
- Delete objects immediately after terminal success/error.
- Rely on bucket lifecycle as a second-line cleanup for orphaned objects.

## Provider Support Policy

For the first transactional shipment:

- SES must support attachments.
- SMTP ingress must support attachments.
- API ingress must support attachments.
- Mailgun/SparkPost/external SMTP can be included if completed against the same manifest contract, but they are not required for Bryan's SES production path.
- Any provider not implemented must reject attachment-bearing sends before queue acceptance or before route dispatch with a deterministic user-visible error.
- Velocity/internal MTA is out of scope and should reject attachment-bearing transactional sends.

This is not a partial deployment gap. It is an explicit provider support matrix.

## Lifecycle and Cleanup

Cleanup must be defense in depth:

1. **Immediate application cleanup**
   - Delete attachment objects after provider acceptance.
   - Delete attachment objects after terminal validation/send failure.
   - Make deletion idempotent.

2. **Queued orphan cleanup**
   - Scheduled cleanup scans attachment prefixes for expired objects.
   - Cleanup should not depend solely on `txnqueue`; it must tolerate partially failed queue writes.

3. **Bucket lifecycle**
   - Configure S3/R2 lifecycle expiration on the attachment prefix.
   - Use short TTL appropriate for transactional sends, for example one to three days.
   - Configure incomplete multipart upload cleanup where supported.

4. **Observability**
   - Log upload, manifest creation, send consumption, and cleanup failures.
   - Expose enough data to answer: queued attachment count, bytes pending, cleanup failures.

## Operational Limits

Suggested defaults:

- Attachments disabled unless storage backend is configured.
- Production storage backend: `s3`.
- Local backend allowed only when explicitly configured.
- Max attachments per message: 5.
- Max decoded attachment bytes each: 10 MiB.
- Max decoded attachment bytes total: 20 MiB.
- Max estimated final MIME bytes: 30 MiB.
- Attachment TTL: 24 hours.
- Bucket lifecycle expiration: one to three days on `attachments/txn/`.
- SMTP max message size should be aligned with the platform MIME cap.

These defaults intentionally sit below SES's 40 MB total message limit to leave room for MIME/base64 overhead and downstream provider variation.

## Dependency-Ordered Task Graph

1. **Lock configuration and limits**
   - Add config/env names for attachment enablement, backend, bucket, prefix, endpoint URL, region, credentials, TTL, and size limits.
   - Decide production default behavior when backend is missing: reject attachments.

2. **Build attachment storage abstraction**
   - Implement local backend.
   - Implement S3-compatible backend.
   - Validate AWS S3.
   - Validate Cloudflare R2.
   - Add healthcheck and lifecycle validation.

3. **Define manifest and validation helpers**
   - Filename sanitization.
   - Content type handling.
   - Extension denylist aligned with SES unsupported attachment types.
   - Size accounting and final MIME size estimation.
   - Idempotent cleanup helpers.

4. **Add API multipart ingestion**
   - Parse metadata + files.
   - Stream file parts to attachment storage.
   - Store manifests in `txnqueue.data`.
   - Preserve existing JSON body behavior for no-attachment sends.

5. **Add optional API JSON attachment ingestion**
   - Decode base64 under stricter caps.
   - Store decoded bytes through the same storage abstraction.
   - Queue only manifests.

6. **Add SMTP relay attachment extraction**
   - Parse nested MIME attachments.
   - Enforce relay-side limits.
   - Submit multipart request to the API.
   - Preserve current per-recipient behavior.

7. **Add MIME builder**
   - Generate HTML-only messages.
   - Generate multipart/mixed messages with HTML and attachments.
   - Preserve headers, tracking IDs, reply-to, return-path expectations.
   - Keep provider-specific quirks out of transactional handlers.

8. **Add SES attachment send path**
   - Use current `send_email` path for no-attachment sends.
   - Use raw MIME + `send_raw_email` for attachment sends.
   - Preserve `sesmessages`, stats, and error behavior.

9. **Add provider support matrix enforcement**
   - Reject attachment sends for unsupported route types.
   - Ensure unsupported route rejection is deterministic and tested.
   - Optionally implement Mailgun/SparkPost/external SMTP through the same manifest contract.

10. **Add cleanup workers**
    - Immediate cleanup after terminal send result.
    - Scheduled orphan cleanup.
    - Lifecycle setup/validation docs for S3/R2.

11. **Add observability**
    - Structured logs for upload, queue, send, cleanup.
    - Metrics or inspectable counters for pending bytes and cleanup failures.

12. **Validate end to end**
    - SMTP relay with PDF attachment through SES route.
    - API multipart with PDF attachment through SES route.
    - API JSON compatibility if enabled.
    - R2 storage backend.
    - S3 storage backend.
    - Local backend in dev/test.
    - Oversized attachment rejection.
    - Unsupported extension rejection.
    - Unsupported provider route rejection.
    - Cleanup after success and failure.

## Campaign / Funnel Level-Up

Campaign/funnel attachment support should be a separate feature after transactional attachments ship.

This proposal intentionally lays groundwork for it:

- Shared attachment manifest.
- Shared storage backend.
- Shared MIME builder.
- Shared provider renderers.
- Shared cleanup and limit enforcement.

But campaign/funnel support needs focused product and UI decisions:

- Where attachments live in message/template/stage data models.
- How authors upload, preview, replace, and delete attachments.
- Whether attachments can vary per recipient.
- How to reuse one stored object across many recipients.
- How campaign-level limits differ from transactional limits.
- How Velocity/internal MTA should support bulk attachments, if at all.

Do not implement campaign/funnel attachment behavior as part of the transactional shipment.

## Do Not

- Do not store base64 attachment bytes in `txnqueue.data`.
- Do not deploy a state where SMTP attachments are accepted but silently dropped.
- Do not deploy a state where SES routes cannot send accepted attachments.
- Do not rely on local VPS disk as the production attachment store.
- Do not expose attachment objects through public `/transfer` or image URLs.
- Do not claim Velocity/internal MTA support without extending its API and sender.
- Do not mix campaign/funnel UI mechanics into this transactional feature.

## Open Questions

- Should JSON base64 attachment ingestion be included for compatibility, or should the API require multipart for attachment-bearing sends?
- Should Mailgun/SparkPost/external SMTP be implemented now or explicitly rejected for attachments until after SES transactional support is shipped?
- Should the default MIME type policy be PDF-first for Bryan's workflow or broad with SES-style blocked extensions?
- Should lifecycle configuration be applied automatically when credentials allow it, or validated/documented but left as an operator step?

## Decision Ledger

### Decisions

- The ship target is one complete transactional attachment feature, not staged deployable intermediates.
- S3-compatible storage, including AWS S3 and Cloudflare R2, is required on day 0.
- SMTP relay will submit attachment-bearing requests as multipart, not base64 JSON.
- SES attachment support is mandatory and uses raw MIME with `send_raw_email` from the current SES helper path.
- Campaign/funnel attachments are deferred as a separate level-up feature.
- Velocity/internal MTA attachment support is out of scope for this transactional shipment.

### Rejected / Closed Doors

- Local-only production attachment storage is rejected for Bryan's current VPS constraints.
- Base64 attachment blobs in Postgres are rejected.
- Planned runtime gaps are rejected; incomplete states may exist during development but are not deployment targets.
- Campaign/funnel support is not part of this proposal's acceptance criteria.
- Velocity/internal MTA support is not part of this proposal's acceptance criteria.

### Invariants

- Attachment bytes must be bounded, private, temporary, and cleaned up.
- Provider support must be explicit: deliver through a supported route or reject clearly.
- SES must work with attachments before production shipment.
- S3/R2 storage must work before production shipment.
- The manifest shape must remain reusable for future campaign/funnel work.
