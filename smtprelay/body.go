package main

import (
	"bytes"
	"encoding/base64"
	"errors"
	"io"
	"io/ioutil"
	"mime"
	"mime/multipart"
	"mime/quotedprintable"
	"strings"
	"unicode/utf8"

	"golang.org/x/net/html/charset"
)

type Attachment struct {
	Filename    string `json:"filename"`
	ContentType string `json:"content_type"`
	Data        []byte `json:"-"`
	Disposition string `json:"disposition,omitempty"`
	ContentID   string `json:"content_id,omitempty"`
}

type extractedMessage struct {
	HTML        string
	Text        string
	HasHTML     bool
	HasText     bool
	Attachments []Attachment
}

func extractBody(body []byte, contentType, contentTransferEncoding string) (string, error) {
	msg, err := extractMessage(body, contentType, contentTransferEncoding)
	if err != nil {
		return "", err
	}
	return msg.Body, nil
}

func extractMessage(body []byte, contentType, contentTransferEncoding string) (struct {
	Body        string
	Attachments []Attachment
}, error) {
	msg, err := extractMessageParts(body, contentType, contentTransferEncoding)
	if err != nil {
		return struct {
			Body        string
			Attachments []Attachment
		}{}, err
	}
	if msg.HasHTML {
		return struct {
			Body        string
			Attachments []Attachment
		}{Body: msg.HTML, Attachments: msg.Attachments}, nil
	}
	if msg.HasText {
		return struct {
			Body        string
			Attachments []Attachment
		}{Body: msg.Text, Attachments: msg.Attachments}, nil
	}
	return struct {
		Body        string
		Attachments []Attachment
	}{}, errors.New("no valid HTML or plain text body found")
}

func extractMessageParts(body []byte, contentType, contentTransferEncoding string) (extractedMessage, error) {
	mediaType := "text/html"
	var params map[string]string
	var err error

	if contentType != "" {
		mediaType, params, err = mime.ParseMediaType(contentType)
		if err != nil {
			return extractedMessage{}, err
		}
	}

	mediaType = strings.ToLower(mediaType)
	contentTransferEncoding = strings.ToLower(contentTransferEncoding)

	if (mediaType == "multipart/alternative" || mediaType == "multipart/mixed" || mediaType == "multipart/related") &&
		(contentTransferEncoding == "quoted-printable" || contentTransferEncoding == "base64") {
		return extractedMessage{}, errors.New("Cannot use a content transfer encoding with a multipart body")
	}

	body, err = decodeContentTransfer(body, contentTransferEncoding)
	if err != nil {
		return extractedMessage{}, err
	}

	charset, ok := params["charset"]
	if !ok {
		charset = "utf-8"
	}

	switch mediaType {
	case "text/plain", "text/html":
		decoded, err := decodeCharset(body, charset)
		if err != nil {
			return extractedMessage{}, err
		}
		if mediaType == "text/html" {
			return extractedMessage{HTML: decoded, HasHTML: true}, nil
		}
		return extractedMessage{Text: decoded, HasText: true}, nil
	case "multipart/alternative", "multipart/mixed", "multipart/related":
		return parseMultipartMessage(body, params["boundary"])
	default:
		return extractedMessage{}, errors.New("unsupported content type")
	}
}

func decodeContentTransfer(body []byte, contentTransferEncoding string) ([]byte, error) {
	switch contentTransferEncoding {
	case "base64":
		return ioutil.ReadAll(base64.NewDecoder(base64.StdEncoding, bytes.NewReader(body)))
	case "quoted-printable":
		return ioutil.ReadAll(quotedprintable.NewReader(bytes.NewReader(body)))
	default:
		return body, nil
	}
}

func decodeCharset(body []byte, label string) (string, error) {
	switch strings.ToLower(label) {
	case "utf-8":
		if utf8.Valid(body) {
			s := string(body)
			return s, nil
		}
		return "", errors.New("invalid utf-8 encoded body")
	default:
		r, err := charset.NewReaderLabel(label, strings.NewReader(string(body)))
		if err != nil {
			return "", err
		}

		newBody, err := io.ReadAll(r)
		if err != nil {
			return "", err
		}

		s := string(newBody)
		return s, nil
	}
}

func parseMultipartBody(body []byte, boundary string) (string, error) {
	msg, err := parseMultipartMessage(body, boundary)
	if err != nil {
		return "", err
	}
	if msg.HasHTML {
		return msg.HTML, nil
	}
	if msg.HasText {
		return msg.Text, nil
	}
	return "", errors.New("no valid HTML or plain text part found in multipart body")
}

func parseMultipartMessage(body []byte, boundary string) (extractedMessage, error) {
	r := multipart.NewReader(bytes.NewReader(body), boundary)

	var msg extractedMessage
	for {
		p, err := r.NextPart()
		if err == io.EOF {
			break
		}
		if err != nil {
			return extractedMessage{}, err
		}
		partBody, err := ioutil.ReadAll(p)
		if err != nil {
			return extractedMessage{}, err
		}

		contentType := p.Header.Get("Content-Type")
		contentTransferEncoding := p.Header.Get("Content-Transfer-Encoding")
		filename := p.FileName()

		mediaType := "text/plain"
		var params map[string]string

		if contentType != "" {
			mediaType, params, err = mime.ParseMediaType(contentType)
			if err != nil {
				return extractedMessage{}, err
			}
		}

		mediaType = strings.ToLower(mediaType)
		if contentType == "" {
			contentType = "application/octet-stream"
		}

		if filename == "" && params != nil {
			filename = params["name"]
		}
		if filename != "" {
			decodedBody, err := decodeContentTransfer(partBody, strings.ToLower(contentTransferEncoding))
			if err != nil {
				return extractedMessage{}, err
			}
			disposition := "attachment"
			if dispositionHeader := p.Header.Get("Content-Disposition"); dispositionHeader != "" {
				if dispositionType, _, err := mime.ParseMediaType(dispositionHeader); err == nil && strings.ToLower(dispositionType) == "inline" {
					disposition = "inline"
				}
			}
			contentID := strings.Trim(strings.TrimSpace(p.Header.Get("Content-ID")), "<>")
			msg.Attachments = append(msg.Attachments, Attachment{
				Filename:    filename,
				ContentType: mediaType,
				Data:        decodedBody,
				Disposition: disposition,
				ContentID:   contentID,
			})
			continue
		}

		switch strings.ToLower(mediaType) {
		case "text/plain":
			decodedBody, err := extractMessageParts(partBody, contentType, contentTransferEncoding)
			if err != nil {
				trace.Printf("Error decoding text: %s", err)
			} else if decodedBody.HasText && !msg.HasText {
				msg.Text = decodedBody.Text
				msg.HasText = true
			}
		case "text/html":
			decodedBody, err := extractMessageParts(partBody, contentType, contentTransferEncoding)
			if err != nil {
				trace.Printf("Error decoding HTML: %s", err)
			} else {
				if decodedBody.HasHTML {
					msg.HTML = decodedBody.HTML
					msg.HasHTML = true
				}
			}
		case "multipart/alternative", "multipart/mixed", "multipart/related":
			decodedBody, err := extractMessageParts(partBody, contentType, contentTransferEncoding)
			if err == nil {
				if decodedBody.HasHTML {
					msg.HTML = decodedBody.HTML
					msg.HasHTML = true
				}
				if decodedBody.HasText && !msg.HasText {
					msg.Text = decodedBody.Text
					msg.HasText = true
				}
				msg.Attachments = append(msg.Attachments, decodedBody.Attachments...)
			}
		}
	}

	if msg.HasHTML || msg.HasText || len(msg.Attachments) > 0 {
		return msg, nil
	}
	return extractedMessage{}, errors.New("no valid HTML or plain text part found in multipart body")
}
