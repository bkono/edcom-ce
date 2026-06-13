#!/usr/bin/env python3

import argparse
import base64
import json
import os
import smtplib
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from email.message import EmailMessage
from email.utils import formataddr


PDF_BYTES = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length 59 >>
stream
BT /F1 18 Tf 36 90 Td (EmailDelivery invoice attachment test) Tj ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000241 00000 n
0000000350 00000 n
trailer
<< /Root 1 0 R /Size 6 >>
startxref
420
%%EOF
"""


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit("Missing required environment variable: %s" % name)
    return value


def optional_env(name, default=""):
    return os.environ.get(name, default).strip()


def build_payload(subject):
    payload = {
        "to": required_env("EDCOM_TO_EMAIL"),
        "fromemail": required_env("EDCOM_FROM_EMAIL"),
        "subject": subject,
        "body": "<p>Attached invoice integration test.</p>",
    }
    route = optional_env("EDCOM_ROUTE_ID")
    if route:
        payload["route"] = route
    fromname = optional_env("EDCOM_FROM_NAME")
    if fromname:
        payload["fromname"] = fromname
    return payload


def post(path, body, content_type):
    base_url = required_env("EDCOM_BASE_URL").rstrip("/")
    api_key = required_env("EDCOM_API_KEY")
    req = urllib.request.Request(
        base_url + path,
        data=body,
        method="POST",
        headers={
            "Content-Type": content_type,
            "X-Auth-APIKey": api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            text = response.read().decode("utf-8", errors="replace")
            print("%s %s" % (response.status, text))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        raise SystemExit("HTTP %s: %s" % (e.code, text))


def send_json():
    payload = build_payload("Transactional JSON attachment integration test")
    payload["attachments"] = [
        {
            "filename": "invoice.pdf",
            "content_type": "application/pdf",
            "content": base64.b64encode(PDF_BYTES).decode("ascii"),
        }
    ]
    post(
        "/api/transactional/send",
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )
    print("JSON attachment request accepted")


def multipart_field(boundary, name, value, content_type=None):
    headers = [
        "--%s" % boundary,
        'Content-Disposition: form-data; name="%s"' % name,
    ]
    if content_type:
        headers.append("Content-Type: %s" % content_type)
    return ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + value + b"\r\n"


def multipart_file(boundary, name, filename, content_type, value):
    headers = [
        "--%s" % boundary,
        'Content-Disposition: form-data; name="%s"; filename="%s"' % (name, filename),
        "Content-Type: %s" % content_type,
    ]
    return ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + value + b"\r\n"


def send_multipart():
    boundary = "edcom-%s" % uuid.uuid4().hex
    payload = build_payload("Transactional multipart attachment integration test")
    body = b"".join(
        [
            multipart_field(
                boundary,
                "payload",
                json.dumps(payload).encode("utf-8"),
                "application/json",
            ),
            multipart_file(
                boundary,
                "attachment",
                "invoice.pdf",
                "application/pdf",
                PDF_BYTES,
            ),
            ("--%s--\r\n" % boundary).encode("utf-8"),
        ]
    )
    post(
        "/api/transactional/send",
        body,
        "multipart/form-data; boundary=%s" % boundary,
    )
    print("Multipart attachment request accepted")


def send_smtp():
    host = required_env("EDCOM_SMTP_HOST")
    port = int(optional_env("EDCOM_SMTP_PORT", "587"))
    use_starttls = optional_env("EDCOM_SMTP_STARTTLS", "true").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    msg = EmailMessage()
    from_name = optional_env("EDCOM_FROM_NAME", "")
    from_email = required_env("EDCOM_FROM_EMAIL")
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
    msg["To"] = required_env("EDCOM_TO_EMAIL")
    msg["Subject"] = "Transactional SMTP attachment integration test"
    msg["X-Auth-APIKey"] = required_env("EDCOM_API_KEY")
    route = optional_env("EDCOM_ROUTE_ID")
    if route:
        msg["X-Transactional-Route"] = route
    msg.set_content("Attached invoice integration test.")
    msg.add_alternative("<p>Attached invoice integration test.</p>", subtype="html")
    msg.add_attachment(
        PDF_BYTES,
        maintype="application",
        subtype="pdf",
        filename="invoice.pdf",
    )

    with smtplib.SMTP(host, port, timeout=60) as smtp:
        smtp.ehlo()
        if use_starttls:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        smtp.send_message(msg)
    print("SMTP attachment message accepted")


def check_lifecycle():
    bucket = required_env("EDCOM_ATTACHMENT_BUCKET")
    prefix = optional_env("EDCOM_ATTACHMENT_PREFIX", "attachments/txn/")
    endpoint = optional_env("EDCOM_ATTACHMENT_ENDPOINT_URL")
    cmd = [
        "aws",
        "s3api",
        "get-bucket-lifecycle-configuration",
        "--bucket",
        bucket,
    ]
    if endpoint:
        cmd.extend(["--endpoint-url", endpoint])
    result = subprocess.run(cmd, check=False, text=True, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.stdout.strip())
    print(result.stdout.strip())
    if prefix not in result.stdout:
        raise SystemExit("Lifecycle configuration did not mention prefix %s" % prefix)
    print("Lifecycle configuration includes %s" % prefix)


def main():
    parser = argparse.ArgumentParser(
        description="Run live transactional attachment integration checks"
    )
    parser.add_argument("--skip-json", action="store_true")
    parser.add_argument("--skip-multipart", action="store_true")
    parser.add_argument("--skip-smtp", action="store_true")
    parser.add_argument("--check-lifecycle", action="store_true")
    args = parser.parse_args()

    if args.check_lifecycle:
        check_lifecycle()
    if not args.skip_json:
        send_json()
    if not args.skip_multipart:
        send_multipart()
    if not args.skip_smtp:
        send_smtp()

    print("Confirm the recipient inbox received all requested PDF attachments.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
