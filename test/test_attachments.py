import base64
from email.parser import BytesParser
from email.policy import default
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
