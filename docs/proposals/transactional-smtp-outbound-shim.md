# Transactional SMTP Outbound Shim Proposal

## Context

The transactional SMTP relay is already usable as a pragmatic outbound SMTP
option for normal personal mail clients such as Fastmail. In that deployment
shape, the mail client submits to `smtprelay`, EDCom authenticates the request,
queues the message through `/api/transactional/send`, and the selected Postal
Route delivers through a backend such as SES.

That existing use is valid and useful. It provides Postal Route selection,
provider delivery, link/open/click tracking, transactional webhooks, rate limits,
and suppression behavior without introducing a separate outbound SMTP product
surface.

The current gaps are not basic delivery. The gaps are semantic fidelity:
attachments, multiple visible recipients, `Cc` / `Bcc`, and message-vs-recipient
identity.

## Current Behavior

- `smtprelay` accepts SMTP messages, parses the inbound MIME message, extracts
  one body, and submits JSON to `/api/transactional/send`.
- The relay accepts multiple SMTP envelope recipients, but fans them out into
  one transactional API call per recipient.
- The relay parses only the `To` header for display-name lookup. `Cc` and `Bcc`
  are not first-class fields.
- `/api/transactional/send` requires a single `to` value, derives one recipient
  domain, and inserts one `txnqueue` row.
- `txnqueue` stores one `route`, one `domain`, and one JSON message payload.
- The transactional worker groups queue work by `(cid, route, domain)`, applies
  send limits, and dispatches one queued item through `send_backend_mail`.
- Provider sends already converge through `api/shared/send.py`.
- Tracking already works through the transactional model: generated `campid`,
  per-recipient tracking IDs, rewritten links, open pixels, provider webhook
  metadata, `txnsends`, `txnlogs`, and `txnstats`.

## Problem

Normal outbound email is message-oriented. The current transactional relay path
is recipient-oriented.

This is acceptable for simple personal outbound use, but it causes mismatches:

- A single normal email with multiple recipients becomes multiple independent
  transactional sends.
- Visible `To` / `Cc` semantics are not preserved.
- `Bcc` cannot be represented correctly.
- Attachments are currently dropped by the relay path.
- Tracking, logs, and webhooks are reported as transactional activity, not as a
  distinct outbound SMTP source.
- Original MIME identity and headers are not preserved as the source of truth.

The goal is to harden the existing useful shim without pretending that this is a
complete general outbound SMTP product.

## Recommended V1

Implement a hardened transactional SMTP outbound shim.

V1 should keep using the existing transactional queue and Postal Route machinery,
but add enough structure to support normal email-client behavior:

- Attachment-bearing SMTP messages are accepted and delivered through the
  transactional attachment implementation.
- `To`, `Cc`, and `Bcc` recipients are parsed into structured recipient records.
- Every actual SMTP envelope recipient gets a per-recipient delivery/tracking
  identity.
- Visible `To` and `Cc` headers are preserved for recipients.
- `Bcc` recipients receive the message but are never exposed in visible headers.
- Postal Route dispatch remains per recipient/domain.
- Existing transactional logs, stats, and webhooks remain the reporting surface
  for this V1.

This is intentionally not a new outbound SMTP subsystem.

## Structured Recipient Contract

Introduce a structured recipient model at the relay/API boundary even if the
internal queue continues to fan out by recipient/domain.

Example shape:

```json
{
  "recipients": [
    {"email": "alice@example.com", "name": "Alice", "kind": "to"},
    {"email": "bob@example.com", "name": "Bob", "kind": "cc"},
    {"email": "carol@example.com", "name": "Carol", "kind": "bcc"}
  ]
}
```

Rules:

- `kind` is one of `to`, `cc`, or `bcc`.
- Envelope recipients remain authoritative for who receives mail.
- Header recipients provide visible display semantics.
- If an envelope recipient is absent from visible `To` / `Cc`, treat it as
  `bcc` unless the client supplied a stronger explicit mapping.
- If a visible `To` / `Cc` recipient is not present in the envelope recipient
  list, reject or ignore it deterministically. Do not silently deliver to header
  recipients that were not accepted by SMTP `RCPT TO`.
- Preserve display names with proper address parsing, not comma-splitting.
- Dedupe by normalized email address while preserving first-seen recipient kind
  order: `to` before `cc` before `bcc`.

## Proposed Flow

1. SMTP client submits one normal email to `smtprelay`.
2. `smtprelay` parses envelope recipients and MIME headers.
3. `smtprelay` extracts body and attachment manifests through the transactional
   attachment path.
4. `smtprelay` submits a structured transactional request containing sender,
   body/template data, structured recipients, visible header intent, route, tag,
   variables, and attachment manifests.
5. The API validates the sender, route, and every recipient.
6. The API fans out internally by routable recipient/domain while preserving the
   original visible `To` / `Cc` header set.
7. The worker applies existing send limits and suppression checks per recipient.
8. Provider send helpers receive one recipient at a time for V1, plus the
   preserved visible header data needed to render the outgoing MIME correctly.
9. Events continue to flow through the transactional tracking and webhook path.

## Why This Fits The Repo

The repo already has strong primitives for the hard parts:

- SMTP ingress in `smtprelay`.
- Transactional auth and API acceptance in `api/transactional.py`.
- Per-domain queueing and throttling in `txnqueue` and `check_txns`.
- Postal Route backend selection in `send_backend_mail`.
- Provider-specific delivery helpers in `api/shared/send.py`.
- Link rewriting and open-pixel injection in `generate_html`.
- Provider webhook correlation through tracking metadata.
- Transactional stats, logs, and webhooks for downstream visibility.

The smallest durable improvement is to add missing message semantics around the
existing pipeline rather than introduce a parallel outbound system immediately.

## Deferred Full Outbound SMTP Mode

A separate outbound SMTP subsystem may be warranted later if normal mailbox
outbound becomes a product surface instead of a personal/operational shim.

That later subsystem would likely add:

- `outbound_messages` and `outbound_recipients` persistence.
- A separate outbound message identity distinct from transactional tags.
- Separate outbound logs/API/UI.
- Webhook source objects such as `{"outbound": "<id>"}` instead of
  `{"tag": "<tag>"}`.
- Raw MIME preservation as the canonical message source.
- Provider event handling that understands outbound SMTP as a first-class source.

That is not required for the current Fastmail-style use case.

## Likely Files Affected

- `smtprelay/main.go`
  - Parse `To`, `Cc`, and `Bcc` using address-list parsing.
  - Build structured recipient data from headers plus SMTP envelope recipients.
  - Submit the expanded transactional payload.

- `smtprelay/body.go`
  - Coordinate with transactional attachment extraction so body parsing and
    attachment preservation do not fight each other.

- `api/transactional.py`
  - Accept either legacy single `to` or new `recipients[]`.
  - Validate all recipient records.
  - Preserve visible `To` / `Cc` header intent.
  - Fan out into existing queue rows while retaining per-message visible header
    data.

- `api/shared/send.py`
  - Extend `send_backend_mail` or its inputs to include visible recipient header
    data.
  - Ensure SES raw-MIME attachment path preserves visible `To` / `Cc` and hides
    `Bcc`.
  - Keep provider-specific quirks out of transactional request parsing.

- `api/shared/utils.py`
  - Reuse existing link/open tracking HTML generation.

- `schema/edcom.sql` and migrations
  - Avoid schema changes for V1 if the structured recipient and visible header
    data can live inside `txnqueue.data`.
  - Add schema only if query/reporting needs require it.

- tests under `test/` and `smtprelay/`
  - Add SMTP relay parsing tests for `To`, `Cc`, `Bcc`, multiple envelope
    recipients, display names, and dedupe.
  - Add API validation tests for structured recipients.
  - Add provider rendering tests for visible headers and hidden `Bcc`.

## Compatibility

- Legacy `/api/transactional/send` callers using single `to` must keep working.
- Existing no-attachment single-recipient SMTP relay behavior must keep working.
- Existing transactional logs and webhooks remain the V1 observability surface.
- Existing Postal Route routing and throttling remain recipient/domain scoped.

## Security And Abuse Boundaries

- The SMTP relay must not become an open relay.
- SMTP AUTH / API-key behavior remains required.
- Envelope recipients remain the delivery authority.
- Header-only recipients must not be delivered unless accepted by SMTP `RCPT TO`
  or an authenticated API request explicitly names them as recipients.
- Suppression and exclusion checks continue per recipient.
- Rate limits continue per customer, route, and domain.
- `Bcc` must never appear in visible message headers.

## Error Handling

- Invalid recipient syntax should fail before queue acceptance.
- Unsupported recipient kind should fail before queue acceptance.
- Header/envelope mismatch should produce deterministic behavior:
  reject for strict mode, or classify envelope-only recipients as `bcc` for
  compatibility. V1 should choose one behavior and test it.
- Attachment-bearing sends through unsupported provider routes should fail before
  queue acceptance, consistent with the transactional attachment proposal.
- Partial multi-recipient acceptance should be avoided at the API boundary; once
  accepted, per-recipient provider failures are tracked through existing event
  paths.

## Testing Strategy

Required regression coverage:

- Single-recipient transactional SMTP still works.
- Multiple `RCPT TO` recipients produce separate per-recipient queue/delivery
  units.
- `To` and `Cc` display headers are preserved.
- `Bcc` recipients receive the message but are not visible.
- Header-only recipients are not silently delivered.
- Attachments plus multiple recipients work through the SES path.
- Link/open tracking still produces one tracking identity per actual recipient.
- Provider webhook events still update transactional logs/stats.

Suggested focused tests:

- Go unit tests for relay recipient/header parsing.
- Python API tests for legacy `to` and new `recipients[]` request shapes.
- SES raw-MIME construction tests that inspect rendered headers and MIME parts.
- Integration test extending the transactional attachment harness with multiple
  recipients and `Cc`.

## Decision Ledger

### Decisions

- Harden the existing transactional SMTP relay path as a supported outbound shim
  for normal SMTP-client usage.
- Keep V1 reporting in transactional logs/stats/webhooks.
- Use structured recipients at the relay/API boundary.
- Preserve Postal Route routing and throttling as per-recipient/domain behavior.

### Rejected / Closed Doors

- Do not build a full outbound SMTP subsystem for this V1.
- Do not model `Cc` as an untyped string bolted onto the current single `to`
  contract.
- Do not deliver to visible header recipients that were not accepted as envelope
  recipients.
- Do not expose `Bcc` in rendered headers.

### Invariants

- Envelope recipients are authoritative for delivery.
- Postal Route selection remains recipient-domain scoped.
- Each delivered recipient gets its own tracking identity.
- Existing single-recipient transactional SMTP/API behavior remains compatible.

## Do Not

- Do not reinterpret this proposal as a replacement for the transactional
  attachment proposal.
- Do not reintroduce a provider path that silently drops attachments after queue
  acceptance.
- Do not preserve broken comma-splitting for address parsing when adding
  structured recipients.
- Do not create a new outbound SMTP product surface unless that becomes an
  explicit later project.

## Open Questions

- Should header/envelope mismatch be strict reject, or should envelope-only
  recipients become `bcc` for compatibility with common SMTP clients?
- Should personal outbound messages use a reserved transactional tag by default,
  or should callers continue to supply tags explicitly?
- Should `Cc`/`Bcc` support ship only with SES raw-MIME support first, or should
  Mailgun be included in the same release if attachment work remains SES-first?
- How much original header preservation is needed beyond visible recipient
  headers, `Message-ID`, `In-Reply-To`, `References`, `Reply-To`, and
  `List-Unsubscribe`?

## Evidence Pointers

- `smtprelay/main.go`
  - Defines the current relay JSON payload with a single `to`.
  - Parses `To` but not `Cc` or `Bcc`.
  - Loops over `env.Recipients` and POSTs one transactional request per
    recipient.
- `api/transactional.py`
  - `/api/transactional/send` requires single `doc["to"]`.
  - Derives one queue `domain` from that address.
  - Inserts one `txnqueue` row and one accepted `txnsends` event.
  - Worker groups queue rows by `(cid, route, domain)`.
- `api/shared/send.py`
  - `send_backend_mail` takes one display recipient and one email recipient.
  - SES/Mailgun helpers already know how to generate tracking metadata per
    recipient.
- `api/shared/utils.py`
  - `generate_html` / `raw_to_html` provide existing link rewriting and open
    pixel insertion.
- `api/events.py`
  - Provider events already branch into transactional handling through
    transactional IDs and tracking metadata.
