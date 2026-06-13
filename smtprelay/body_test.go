package main

import (
	"strings"
	"testing"
)

func TestExtractMessageWithAttachment(t *testing.T) {
	body := strings.ReplaceAll(`--boundary
Content-Type: multipart/alternative; boundary="alt"

--alt
Content-Type: text/plain; charset=utf-8

Plain body.
--alt
Content-Type: text/html; charset=utf-8

<p>HTML body.</p>
--alt--
--boundary
Content-Type: application/pdf; name="invoice.pdf"
Content-Disposition: attachment; filename="invoice.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjcK
--boundary--`, "\n", "\r\n")

	msg, err := extractMessage([]byte(body), `multipart/mixed; boundary="boundary"`, "")
	if err != nil {
		t.Fatalf("extractMessage returned error: %s", err)
	}
	if msg.Body != "<p>HTML body.</p>" {
		t.Fatalf("expected HTML body, got %q", msg.Body)
	}
	if len(msg.Attachments) != 1 {
		t.Fatalf("expected one attachment, got %d", len(msg.Attachments))
	}
	attachment := msg.Attachments[0]
	if attachment.Filename != "invoice.pdf" {
		t.Fatalf("expected invoice.pdf filename, got %q", attachment.Filename)
	}
	if attachment.ContentType != "application/pdf" {
		t.Fatalf("expected application/pdf content type, got %q", attachment.ContentType)
	}
	if string(attachment.Data) != "%PDF-1.7\n" {
		t.Fatalf("unexpected attachment data: %q", string(attachment.Data))
	}
}

func TestExtractMessageWithoutAttachmentPreservesPlainTextFallback(t *testing.T) {
	body := strings.ReplaceAll(`--boundary
Content-Type: text/plain; charset=utf-8

Plain body.
--boundary--`, "\n", "\r\n")

	msg, err := extractMessage([]byte(body), `multipart/mixed; boundary="boundary"`, "")
	if err != nil {
		t.Fatalf("extractMessage returned error: %s", err)
	}
	if msg.Body != "Plain body." {
		t.Fatalf("expected plain text body, got %q", msg.Body)
	}
	if len(msg.Attachments) != 0 {
		t.Fatalf("expected no attachments, got %d", len(msg.Attachments))
	}
}
