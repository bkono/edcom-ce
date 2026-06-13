import base64
from email.parser import BytesParser
from email.policy import default
import json
import os
import tempfile
import unittest

try:
    import falcon
    from falcon import testing
    from api.shared.send import possible_backend_types
    from api.transactional import parse_multipart_send_request
except ModuleNotFoundError:
    falcon = None
    testing = None
    possible_backend_types = None
    parse_multipart_send_request = None

from api.shared.attachments import (
    AttachmentConfig,
    AttachmentDisabledError,
    AttachmentError,
    AttachmentUpload,
    LocalAttachmentStorage,
    build_raw_mime_message,
    decode_json_attachment,
    delete_attachments,
    store_attachments,
    validate_attachment_collection,
)


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


def config(**overrides):
    values = {
        "enabled": True,
        "storage_backend": "local",
        "s3_bucket": "",
        "s3_prefix": "attachments/txn/",
        "s3_region": "",
        "s3_access_key": "",
        "s3_secret_key": "",
        "s3_endpoint_url": "",
        "local_path": "/tmp/edcom-attachments-test",
        "manage_lifecycle": True,
        "ttl_hours": 24,
        "lifecycle_expiration_days": 2,
        "lifecycle_abort_multipart_days": 1,
        "max_count": 5,
        "max_file_bytes": 10 * 1024 * 1024,
        "max_total_bytes": 20 * 1024 * 1024,
        "max_mime_bytes": 30 * 1024 * 1024,
        "json_body_max_bytes": 32 * 1024 * 1024,
    }
    values.update(overrides)
    return AttachmentConfig(**values)


class TestAttachments(unittest.TestCase):
    def test_rejects_attachments_when_disabled(self):
        upload = AttachmentUpload("invoice.pdf", "application/pdf", b"%PDF-1.7\n")

        with self.assertRaises(AttachmentDisabledError):
            validate_attachment_collection([upload], config(enabled=False))

    def test_requires_extension_to_match_content_type(self):
        upload = AttachmentUpload("invoice.pdf", "image/png", b"%PDF-1.7\n")

        with self.assertRaises(AttachmentError):
            validate_attachment_collection([upload], config())

    def test_validates_binary_signature(self):
        upload = AttachmentUpload("invoice.pdf", "application/pdf", b"not a pdf")

        with self.assertRaises(AttachmentError):
            validate_attachment_collection([upload], config())

    def test_allows_utf8_markdown(self):
        upload = AttachmentUpload(
            "notes.md",
            "text/markdown",
            "# Invoice\n\nAttached payment notes.\n".encode("utf-8"),
        )

        validate_attachment_collection([upload], config())

    def test_rejects_unsupported_archive(self):
        upload = AttachmentUpload("payload.gz", "application/gzip", b"\x1f\x8b")

        with self.assertRaises(AttachmentError):
            validate_attachment_collection([upload], config())

    def test_enforces_total_size_limit(self):
        uploads = [
            AttachmentUpload("a.txt", "text/plain", b"a" * 6),
            AttachmentUpload("b.txt", "text/plain", b"b" * 6),
        ]

        with self.assertRaises(AttachmentError):
            validate_attachment_collection(uploads, config(max_total_bytes=10))

    def test_decodes_json_base64_attachment(self):
        upload = decode_json_attachment(
            {
                "filename": "invoice.pdf",
                "content_type": "application/pdf",
                "content": base64.b64encode(b"%PDF-1.7\n").decode("ascii"),
            },
            config(),
        )

        self.assertEqual(upload.filename, "invoice.pdf")
        self.assertEqual(upload.data, b"%PDF-1.7\n")

    def test_local_storage_manifest_and_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config(local_path=root)
            storage = LocalAttachmentStorage(root)
            upload = AttachmentUpload(
                "invoice.pdf", "application/pdf", b"%PDF-1.7\n"
            )

            manifests = store_attachments(
                storage, cfg, "companyid", "messageid", [upload]
            )

            self.assertEqual(len(manifests), 1)
            manifest = manifests[0]
            self.assertEqual(manifest["storage_backend"], "local")
            self.assertEqual(manifest["filename"], "invoice.pdf")
            self.assertEqual(manifest["content_type"], "application/pdf")
            self.assertTrue(storage.exists(manifest))
            with storage.open_read(manifest) as fp:
                self.assertEqual(fp.read(), b"%PDF-1.7\n")

            delete_attachments(storage, manifests)
            self.assertFalse(os.path.exists(os.path.join(root, manifest["key"])))

    def test_builds_raw_mime_with_attachment(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config(local_path=root)
            storage = LocalAttachmentStorage(root)
            manifests = store_attachments(
                storage,
                cfg,
                "companyid",
                "messageid",
                [AttachmentUpload("invoice.pdf", "application/pdf", b"%PDF-1.7\n")],
            )

            raw = build_raw_mime_message(
                "Billing <billing@example.com>",
                "support@example.com",
                "Customer <customer@example.com>",
                "Invoice",
                "<p>Attached.</p>",
                storage,
                manifests,
            )
            message = BytesParser(policy=default).parsebytes(raw)
            attachments = list(message.iter_attachments())

            self.assertEqual(message["Subject"], "Invoice")
            self.assertEqual(len(attachments), 1)
            self.assertEqual(attachments[0].get_filename(), "invoice.pdf")
            self.assertEqual(attachments[0].get_content_type(), "application/pdf")

    @unittest.skipIf(falcon is None, "falcon is not installed")
    def test_parses_falcon_streamed_multipart_payload_and_attachment(self):
        boundary = "edcom-test-boundary"
        payload = {
            "to": "customer@example.com",
            "fromemail": "billing@example.com",
            "subject": "Invoice",
            "body": "<p>Attached.</p>",
        }
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
                    b"%PDF-1.7\n",
                ),
                ("--%s--\r\n" % boundary).encode("utf-8"),
            ]
        )

        class Resource:
            def on_post(self, req, resp):
                doc, uploads = parse_multipart_send_request(req, config())
                resp.media = {
                    "subject": doc["subject"],
                    "filename": uploads[0].filename,
                    "content_type": uploads[0].content_type,
                    "size": len(uploads[0].data),
                }

        app = falcon.App()
        app.add_route("/send", Resource())
        client = testing.TestClient(app)
        resp = client.simulate_post(
            "/send",
            body=body,
            headers={"content-type": "multipart/form-data; boundary=%s" % boundary},
        )

        self.assertEqual(resp.status, "200 OK")
        self.assertEqual(
            resp.json,
            {
                "subject": "Invoice",
                "filename": "invoice.pdf",
                "content_type": "application/pdf",
                "size": 9,
            },
        )

    @unittest.skipIf(falcon is None, "falcon is not installed")
    def test_multipart_rejects_too_many_parts_before_buffering_extra_file(self):
        boundary = "edcom-test-boundary"
        payload = {
            "to": "customer@example.com",
            "fromemail": "billing@example.com",
            "subject": "Invoice",
            "body": "<p>Attached.</p>",
        }
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
                    "first.pdf",
                    "application/pdf",
                    b"%PDF-1.7\n",
                ),
                multipart_file(
                    boundary,
                    "attachment",
                    "second.pdf",
                    "application/pdf",
                    b"%PDF-1.7\n",
                ),
                ("--%s--\r\n" % boundary).encode("utf-8"),
            ]
        )

        resp = self.simulate_multipart_parse(body, boundary, config(max_count=1))

        self.assertEqual(resp.status, "400 Bad Request")
        self.assertIn("Too many attachments", resp.text)

    @unittest.skipIf(falcon is None, "falcon is not installed")
    def test_multipart_rejects_total_size_before_buffering_past_limit(self):
        boundary = "edcom-test-boundary"
        payload = {
            "to": "customer@example.com",
            "fromemail": "billing@example.com",
            "subject": "Invoice",
            "body": "<p>Attached.</p>",
        }
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
                    "first.txt",
                    "text/plain",
                    b"123456",
                ),
                multipart_file(
                    boundary,
                    "attachment",
                    "second.txt",
                    "text/plain",
                    b"123456",
                ),
                ("--%s--\r\n" % boundary).encode("utf-8"),
            ]
        )

        resp = self.simulate_multipart_parse(body, boundary, config(max_total_bytes=10))

        self.assertEqual(resp.status, "400 Bad Request")
        self.assertIn("Attachments exceed maximum total size", resp.text)

    @unittest.skipIf(falcon is None, "falcon is not installed")
    def test_route_backend_types_stop_after_first_routable_matching_rule(self):
        route = {
            "published": {
                "rules": [
                    {
                        "domaingroup": "ses-only",
                        "splits": [{"pct": 100, "policy": "ses-1"}],
                    },
                    {
                        "domaingroup": "",
                        "splits": [{"pct": 100, "policy": "mailgun-1"}],
                    },
                ]
            }
        }

        backend_types = possible_backend_types(
            route,
            "customer@example.com",
            {"ses-only": {"domains": "example.com"}},
            {},
            {},
            {"mailgun-1": {"id": "mailgun-1"}},
            {"ses-1": {"id": "ses-1"}},
            {},
            {},
            {},
        )

        self.assertEqual(backend_types, {"ses"})

    @unittest.skipIf(falcon is None, "falcon is not installed")
    def test_route_backend_types_keep_all_possibilities_in_first_routable_rule(self):
        route = {
            "published": {
                "rules": [
                    {
                        "domaingroup": "",
                        "splits": [
                            {"pct": 50, "policy": "ses-1"},
                            {"pct": 50, "policy": "mailgun-1"},
                        ],
                    },
                    {
                        "domaingroup": "",
                        "splits": [{"pct": 100, "policy": "ses-2"}],
                    },
                ]
            }
        }

        backend_types = possible_backend_types(
            route,
            "customer@example.com",
            {},
            {},
            {},
            {"mailgun-1": {"id": "mailgun-1"}},
            {"ses-1": {"id": "ses-1"}, "ses-2": {"id": "ses-2"}},
            {},
            {},
            {},
        )

        self.assertEqual(backend_types, {"ses", "mailgun"})

    def simulate_multipart_parse(self, body, boundary, cfg):
        class Resource:
            def on_post(self, req, resp):
                parse_multipart_send_request(req, cfg)
                resp.media = {"ok": True}

        app = falcon.App()
        app.add_route("/send", Resource())
        client = testing.TestClient(app)
        return client.simulate_post(
            "/send",
            body=body,
            headers={"content-type": "multipart/form-data; boundary=%s" % boundary},
        )


if __name__ == "__main__":
    unittest.main()
