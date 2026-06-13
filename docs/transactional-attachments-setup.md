# Transactional Attachments Setup

This runbook covers the production setup and validation path for transactional
email attachments.

## Scope

Attachments are supported for transactional sends only:

- Transactional API JSON requests with base64 attachments.
- Transactional API `multipart/form-data` requests.
- Transactional SMTP relay MIME messages.

SES is the only attachment-capable outbound provider in this release. Attachment
requests are rejected before queue acceptance when the selected postal route can
resolve to Mailgun, SparkPost, external SMTP relay, Easylink, Velocity/internal
MTA, or an unknown/drop route.

Campaign and funnel attachment support is intentionally out of scope.

## Storage Setup

Create a private AWS S3 bucket or Cloudflare R2 bucket for transactional
attachments. Do not use the legacy data, transfer, image, or block bucket paths.

The configured credentials must allow:

- Object write under `attachments/txn/`.
- Object read under `attachments/txn/`.
- Object delete under `attachments/txn/`.
- Bucket lifecycle configuration.

Objects are application-temporary. The app deletes objects after terminal send
success, suppression, or error. Bucket lifecycle is the backup cleanup layer.

## App Configuration

Add the attachment keys under the `app` object in `edcom-install/config/edcom.json`.

Cloudflare R2 example:

```json
{
  "app": {
    "attachment_enabled": "true",
    "attachment_storage_backend": "s3",
    "attachment_s3_bucket": "your-r2-bucket",
    "attachment_s3_prefix": "attachments/txn/",
    "attachment_s3_region": "auto",
    "attachment_s3_access_key": "R2_ACCESS_KEY",
    "attachment_s3_secret_key": "R2_SECRET_KEY",
    "attachment_s3_endpoint_url": "https://ACCOUNT_ID.r2.cloudflarestorage.com",
    "attachment_manage_lifecycle": "true",
    "attachment_ttl_hours": "24",
    "attachment_lifecycle_expiration_days": "2",
    "attachment_lifecycle_abort_multipart_days": "1",
    "attachment_max_count": "5",
    "attachment_max_file_bytes": "10485760",
    "attachment_max_total_bytes": "20971520",
    "attachment_max_mime_bytes": "31457280",
    "attachment_json_body_max_bytes": "33554432"
  }
}
```

AWS S3 example:

```json
{
  "app": {
    "attachment_enabled": "true",
    "attachment_storage_backend": "s3",
    "attachment_s3_bucket": "your-s3-bucket",
    "attachment_s3_prefix": "attachments/txn/",
    "attachment_s3_region": "us-east-1",
    "attachment_s3_access_key": "AWS_ACCESS_KEY",
    "attachment_s3_secret_key": "AWS_SECRET_KEY",
    "attachment_s3_endpoint_url": "",
    "attachment_manage_lifecycle": "true",
    "attachment_ttl_hours": "24",
    "attachment_lifecycle_expiration_days": "2",
    "attachment_lifecycle_abort_multipart_days": "1",
    "attachment_max_count": "5",
    "attachment_max_file_bytes": "10485760",
    "attachment_max_total_bytes": "20971520",
    "attachment_max_mime_bytes": "31457280",
    "attachment_json_body_max_bytes": "33554432"
  }
}
```

When `attachment_enabled=true`, API startup fails closed if the storage
healthcheck or lifecycle configuration fails.

Existing no-attachment transactional sends continue to work when
`attachment_enabled=false`.

## Postal Route Setup

Use an SES postal route for transactional attachment tests.

The route must resolve exclusively to SES for the recipient domain being tested.
Do not include weighted splits, policies, or fallback paths that can select:

- Mailgun.
- SparkPost.
- External SMTP relay.
- Easylink.
- Velocity/internal MTA sink.
- Drop/unknown backend.

If any possible route path is non-SES, the API rejects the attachment-bearing
send before queue insertion.

## Deploy After Merge

After the branch is merged to `main`:

1. Wait for GitHub Actions to build release artifacts.
2. On the server, run the fork-owned update flow from `edcom-install`:

   ```bash
   ./upgrade.sh
   ```

3. Confirm the containers restarted on the new release.
4. Confirm API startup succeeds with attachment storage enabled.
5. Confirm the storage bucket has a lifecycle rule for `attachments/txn/`.

## Manual API Multipart Validation

Create a small PDF test artifact, then send:

```bash
curl -X POST "https://YOUR_DOMAIN/api/transactional/send" \
  -H "X-Auth-APIKey: YOUR_API_KEY" \
  -F 'payload={
    "to":"recipient@example.com",
    "fromemail":"billing@yourdomain.com",
    "subject":"Invoice attachment test",
    "body":"<p>Attached invoice.</p>",
    "route":"SES_ROUTE_ID"
  };type=application/json' \
  -F "attachment=@./invoice.pdf;type=application/pdf"
```

Expected result:

- HTTP 2xx response.
- Received email includes `invoice.pdf`.
- Object storage entry is deleted after terminal send.
- Logs include transactional attachment acceptance and SES raw send entries.

## Manual API JSON Validation

```bash
PDF_BASE64="$(base64 < ./invoice.pdf | tr -d '\n')"

curl -X POST "https://YOUR_DOMAIN/api/transactional/send" \
  -H "Content-Type: application/json" \
  -H "X-Auth-APIKey: YOUR_API_KEY" \
  --data-binary @- <<JSON
{
  "to": "recipient@example.com",
  "fromemail": "billing@yourdomain.com",
  "subject": "Invoice JSON attachment test",
  "body": "<p>Attached invoice.</p>",
  "route": "SES_ROUTE_ID",
  "attachments": [
    {
      "filename": "invoice.pdf",
      "content_type": "application/pdf",
      "content": "$PDF_BASE64"
    }
  ]
}
JSON
```

Expected result is the same as the multipart path.

## SMTP Relay Validation

Send a MIME message through the transactional relay with:

- `X-Auth-APIKey: YOUR_API_KEY`, or SMTP auth password set to the API key.
- A normal HTML or text body.
- A PDF attachment.

Expected result:

- SMTP transaction succeeds.
- API receives a `multipart/form-data` relay request.
- Received email includes the PDF attachment.
- Attachment object is deleted after terminal send.

## Negative Validation

Run these before shipping to production:

- Attachment send through a non-SES route returns HTTP 400 before queue insertion.
- Route with mixed SES and non-SES possible paths returns HTTP 400.
- Unsupported extension returns HTTP 400.
- Mismatched extension/content type returns HTTP 400.
- Invalid binary signature returns HTTP 400.
- More than five attachments returns HTTP 400.
- File over 10 MiB returns HTTP 400.
- Total decoded attachment bytes over 20 MiB returns HTTP 400.
- Estimated final MIME size over 30 MiB returns HTTP 400.

## Production Validation Artifact

The first end-to-end production artifact should be a PDF invoice-style attachment
sent through SES by each supported ingress path:

1. API multipart.
2. API JSON base64.
3. SMTP relay MIME.

## Live Integration Runner

Use the integration runner before opening the PR and again after deployment:

```bash
export EDCOM_BASE_URL="https://esp.yourdomain.com"
export EDCOM_API_KEY="customer-api-key"
export EDCOM_FROM_EMAIL="billing@yourdomain.com"
export EDCOM_TO_EMAIL="recipient@example.com"
export EDCOM_ROUTE_ID="ses-only-route-id"
export EDCOM_SMTP_HOST="esp.yourdomain.com"
export EDCOM_SMTP_PORT="587"
export EDCOM_SMTP_STARTTLS="true"

python3 scripts/transactional_attachments_integration.py
```

The runner sends:

- API JSON base64 attachment request.
- API multipart attachment request.
- SMTP relay MIME attachment request.

It uses a generated invoice-style PDF attachment and expects the route to resolve
exclusively to SES.

To also inspect the lifecycle rule with the already configured `aws` CLI:

```bash
export EDCOM_ATTACHMENT_BUCKET="your-s3-or-r2-bucket"
export EDCOM_ATTACHMENT_PREFIX="attachments/txn/"
export EDCOM_ATTACHMENT_ENDPOINT_URL="https://ACCOUNT_ID.r2.cloudflarestorage.com"

python3 scripts/transactional_attachments_integration.py \
  --check-lifecycle \
  --skip-json \
  --skip-multipart \
  --skip-smtp
```

For AWS S3, omit `EDCOM_ATTACHMENT_ENDPOINT_URL`.

The runner verifies request acceptance and lifecycle configuration visibility. It
does not log into the recipient mailbox; manually confirm the inbox received the
PDF attachment for each enabled ingress path.
