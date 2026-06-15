package main

import (
	"encoding/json"
	"io"
	"mime"
	"mime/multipart"
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

func TestExtractMessagePreservesInlineAttachmentMetadata(t *testing.T) {
	body := strings.ReplaceAll(`--boundary
Content-Type: text/html; charset=utf-8

<p><img src="cid:logo"></p>
--boundary
Content-Type: image/png; name="logo.png"
Content-Disposition: inline; filename="logo.png"
Content-ID: <logo>
Content-Transfer-Encoding: base64

iVBORw0KGgo=
--boundary--`, "\n", "\r\n")

	msg, err := extractMessage([]byte(body), `multipart/related; boundary="boundary"`, "")
	if err != nil {
		t.Fatalf("extractMessage returned error: %s", err)
	}
	if len(msg.Attachments) != 1 {
		t.Fatalf("expected one attachment, got %d", len(msg.Attachments))
	}
	attachment := msg.Attachments[0]
	if attachment.Disposition != "inline" {
		t.Fatalf("expected inline disposition, got %q", attachment.Disposition)
	}
	if attachment.ContentID != "logo" {
		t.Fatalf("expected logo content id, got %q", attachment.ContentID)
	}
}

func TestBuildAPIRequestForwardsAttachmentMetadata(t *testing.T) {
	t.Setenv("edcomhost", "127.0.0.1")

	req, err := buildAPIRequest(
		"apikey",
		SendMailMsg{To: "customer@example.com", FromEmail: "from@example.com", Subject: "Subject", Body: "<p>Body</p>"},
		[]Attachment{
			{
				Filename:    "logo.png",
				ContentType: "image/png",
				Data:        []byte("png"),
				Disposition: "inline",
				ContentID:   "logo",
			},
		},
	)
	if err != nil {
		t.Fatalf("buildAPIRequest returned error: %s", err)
	}

	_, params, err := mime.ParseMediaType(req.Header.Get("Content-Type"))
	if err != nil {
		t.Fatalf("could not parse content type: %s", err)
	}
	reader := multipart.NewReader(req.Body, params["boundary"])
	var metadata Attachment
	for {
		part, err := reader.NextPart()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatalf("could not read multipart part: %s", err)
		}
		if part.FormName() != "attachment_metadata" {
			continue
		}
		data, err := io.ReadAll(part)
		if err != nil {
			t.Fatalf("could not read metadata part: %s", err)
		}
		if err := json.Unmarshal(data, &metadata); err != nil {
			t.Fatalf("could not decode metadata: %s", err)
		}
	}
	if metadata.Disposition != "inline" {
		t.Fatalf("expected inline disposition, got %q", metadata.Disposition)
	}
	if metadata.ContentID != "logo" {
		t.Fatalf("expected logo content id, got %q", metadata.ContentID)
	}
}

func TestExtractMessagePreservesNestedMultipartBoundaryCase(t *testing.T) {
	body := strings.ReplaceAll(`--outer
Content-Type: multipart/alternative; boundary="AltBoundaryX"

--AltBoundaryX
Content-Type: text/plain; charset=utf-8

Plain body.
--AltBoundaryX
Content-Type: text/html; charset=utf-8

<p>HTML body.</p>
--AltBoundaryX--
--outer--`, "\n", "\r\n")

	msg, err := extractMessage([]byte(body), `multipart/mixed; boundary="outer"`, "")
	if err != nil {
		t.Fatalf("extractMessage returned error: %s", err)
	}
	if msg.Body != "<p>HTML body.</p>" {
		t.Fatalf("expected HTML body, got %q", msg.Body)
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
