package main

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io/ioutil"
	"log"
	"math/big"
	"mime"
	"mime/multipart"
	"net/http"
	"net/mail"
	"net/textproto"
	"os"
	"strings"
	"time"

	"smtprelay/smtpd"
)

var trace *log.Logger = log.New(os.Stderr, "", log.Ldate|log.Lmicroseconds|log.Lshortfile)

type SendMailMsg struct {
	FromName   string      `json:"fromname"`
	FromEmail  string      `json:"fromemail"`
	To         string      `json:"to"`
	ToName     string      `json:"toname"`
	Body       string      `json:"body"`
	Subject    string      `json:"subject"`
	ReplyTo    string      `json:"replyto"`
	ReturnPath string      `json:"returnpath"`
	Tag        string      `json:"tag"`
	Route      string      `json:"route"`
	Template   string      `json:"template"`
	Variables  interface{} `json:"variables"`
}

type ReplyMsg struct {
	Description string `json:"description"`
}

var client http.Client

func authHandler(peer smtpd.Peer, username, password string) error {
	return nil
}

func mailHandler(peer smtpd.Peer, env smtpd.Envelope) error {
	trace.Printf("Message from %s (%s %s)", env.Sender, peer.HeloName, peer.Addr)

	email, err := mail.ReadMessage(bytes.NewBuffer(env.Data))
	if err != nil {
		trace.Printf("Error: %s", err)
		return &smtpd.Error{Code: 500, Message: "Could not parse message"}
	}

	apikey := email.Header.Get("X-Auth-APIKey")
	if apikey == "" {
		apikey = peer.Password
		if apikey == "" {
			trace.Printf("No API Key")
			return &smtpd.Error{Code: 503, Message: "AUTH LOGIN not used and no X-Auth-APIKey header, authentication failed"}
		}
	}

	dec := new(mime.WordDecoder)

	fromheader, _ := dec.DecodeHeader(email.Header.Get("From"))
	if fromheader == "" {
		trace.Printf("No From header")
		return &smtpd.Error{Code: 500, Message: "No From header found"}
	}
	fromaddr, err := mail.ParseAddress(fromheader)
	if err != nil {
		trace.Printf("Error: %s", err)
		return &smtpd.Error{Code: 500, Message: "Could not parse from address"}
	}

	toheader, _ := dec.DecodeHeader(email.Header.Get("To"))
	tonames := map[string]string{}
	if toheader != "" {
		addresses := strings.Split(toheader, ",")

		for _, address := range addresses {
			toaddr, err := mail.ParseAddress(address)
			if err != nil {
				trace.Printf("Error parsing to address: %s", err)
			} else {
				tonames[toaddr.Address] = toaddr.Name
			}
		}
	}

	body, err := ioutil.ReadAll(email.Body)
	if err != nil {
		trace.Printf("Error: %s", err)
		return &smtpd.Error{Code: 500, Message: "Could not parse body"}
	}

	extracted, err := extractMessage(body, email.Header.Get("Content-Type"), email.Header.Get("Content-Transfer-Encoding"))
	if err != nil {
		trace.Printf("Error: %s", err)
		return &smtpd.Error{Code: 500, Message: fmt.Sprintf("Could not parse body: %s", err)}
	}
	bodystr := extracted.Body

	subject, _ := dec.DecodeHeader(email.Header.Get("Subject"))
	if subject == "" {
		trace.Printf("No Subject header")
		return &smtpd.Error{Code: 500, Message: "No Subject header found"}
	}

	replyto, _ := dec.DecodeHeader(email.Header.Get("Reply-To"))

	tag := email.Header.Get("X-Transactional-Tag")
	route := email.Header.Get("X-Transactional-Route")
	template := email.Header.Get("X-Transactional-Template")

	variables, _ := dec.DecodeHeader(email.Header.Get("X-Transactional-Variables"))
	var vars interface{}

	if variables != "" {
		err = json.Unmarshal([]byte(variables), &vars)
		if err != nil {
			trace.Printf("Cannot parse JSON for variables: %s", err)
			return &smtpd.Error{Code: 500, Message: "Error parsing JSON in X-Transactional-Variables"}
		}
	}

	recipaddresses := make([]string, 0)
	for _, recip := range env.Recipients {
		recipaddr, err := mail.ParseAddress(recip)
		if err != nil {
			trace.Printf("Error: %s", err)
			return &smtpd.Error{Code: 500, Message: fmt.Sprintf("Could not parse recipient address: %s", recip)}
		}
		recipaddresses = append(recipaddresses, recipaddr.Address)
	}

	// if there is one address and one name then match them together
	if len(tonames) == 1 && len(recipaddresses) == 1 {
		for _, toname := range tonames {
			tonames[recipaddresses[0]] = toname
		}
	}

	for _, recipaddress := range recipaddresses {
		msg := SendMailMsg{
			FromName:   fromaddr.Name,
			FromEmail:  fromaddr.Address,
			To:         recipaddress,
			ToName:     tonames[recipaddress],
			Body:       bodystr,
			Subject:    subject,
			ReplyTo:    replyto,
			ReturnPath: env.Sender,
			Tag:        tag,
			Route:      route,
			Template:   template,
			Variables:  vars,
		}
		req, err := buildAPIRequest(apikey, msg, extracted.Attachments)
		if err != nil {
			trace.Printf("Error: %s", err)
			return &smtpd.Error{Code: 451, Message: "Local error in processing: 2"}
		}

		res, err := client.Do(req)
		if err != nil {
			trace.Printf("Error: %s", err)
			return &smtpd.Error{Code: 451, Message: "Local error in processing: 3"}
		}
		defer res.Body.Close()
		if res.StatusCode < 200 || res.StatusCode > 299 {
			trace.Printf("StatusCode: %d", res.StatusCode)
			if res.StatusCode == 401 {
				return &smtpd.Error{Code: 503, Message: "Authentication failure: invalid API key"}
			} else if res.StatusCode == 400 {
				var replymsg ReplyMsg
				decoder := json.NewDecoder(res.Body)
				err = decoder.Decode(&replymsg)
				if err != nil {
					trace.Printf("Error: %s", err)
					return &smtpd.Error{Code: 451, Message: "Local error in processing: 4"}
				}
				trace.Printf("Description: %s", replymsg.Description)
				return &smtpd.Error{Code: 501, Message: replymsg.Description}
			} else {
				return &smtpd.Error{Code: 451, Message: "Local error in processing: 5"}
			}
		}
	}
	trace.Printf("Success")
	return nil
}

func buildAPIRequest(apikey string, msg SendMailMsg, attachments []Attachment) (*http.Request, error) {
	url := fmt.Sprintf("http://%s/api/transactional/send", os.Getenv("edcomhost"))
	jsonval, err := json.Marshal(&msg)
	if err != nil {
		return nil, err
	}

	if len(attachments) == 0 {
		req, err := http.NewRequest("POST", url, bytes.NewBuffer(jsonval))
		if err != nil {
			return nil, err
		}
		req.Header.Add("Content-Type", "application/json")
		req.Header.Add("X-Auth-APIKey", apikey)
		return req, nil
	}

	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	if err := writer.WriteField("payload", string(jsonval)); err != nil {
		return nil, err
	}

	for _, attachment := range attachments {
		metadata, err := json.Marshal(attachment)
		if err != nil {
			return nil, err
		}
		if err := writer.WriteField("attachment_metadata", string(metadata)); err != nil {
			return nil, err
		}

		header := make(textproto.MIMEHeader)
		header.Set("Content-Disposition", mime.FormatMediaType("form-data", map[string]string{
			"name":     "attachment",
			"filename": attachment.Filename,
		}))
		header.Set("Content-Type", attachment.ContentType)
		part, err := writer.CreatePart(header)
		if err != nil {
			return nil, err
		}
		if _, err := part.Write(attachment.Data); err != nil {
			return nil, err
		}
	}

	if err := writer.Close(); err != nil {
		return nil, err
	}

	req, err := http.NewRequest("POST", url, &body)
	if err != nil {
		return nil, err
	}
	req.Header.Add("Content-Type", writer.FormDataContentType())
	req.Header.Add("X-Auth-APIKey", apikey)
	return req, nil
}

func fileExists(filename string) bool {
	info, err := os.Stat(filename)
	if os.IsNotExist(err) {
		return false
	}
	return !info.IsDir()
}

func publicKey(priv interface{}) interface{} {
	switch k := priv.(type) {
	case *rsa.PrivateKey:
		return &k.PublicKey
	case *ecdsa.PrivateKey:
		return &k.PublicKey
	default:
		return nil
	}
}

func pemBlockForKey(priv interface{}) *pem.Block {
	switch k := priv.(type) {
	case *rsa.PrivateKey:
		return &pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(k)}
	case *ecdsa.PrivateKey:
		b, err := x509.MarshalECPrivateKey(k)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Unable to marshal ECDSA private key: %v", err)
			os.Exit(2)
		}
		return &pem.Block{Type: "EC PRIVATE KEY", Bytes: b}
	default:
		return nil
	}
}

func generateCertificate(smtphost string) {
	priv, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		log.Fatal(err)
	}
	template := x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject: pkix.Name{
			Organization: []string{smtphost},
		},
		NotBefore: time.Now(),
		NotAfter:  time.Now().Add(time.Hour * 24 * 10000),

		KeyUsage:              x509.KeyUsageKeyEncipherment | x509.KeyUsageDigitalSignature,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
	}

	derBytes, err := x509.CreateCertificate(rand.Reader, &template, &template, publicKey(priv), priv)
	if err != nil {
		log.Fatalf("Failed to create certificate: %s", err)
	}
	out := &bytes.Buffer{}
	pem.Encode(out, &pem.Block{Type: "CERTIFICATE", Bytes: derBytes})

	ioutil.WriteFile("/cert/server.crt", out.Bytes(), 0600)

	out.Reset()
	pem.Encode(out, pemBlockForKey(priv))
	ioutil.WriteFile("/cert/server.key", out.Bytes(), 0600)
}

type SMTPRelayConfig struct {
	SMTPHost string `json:"smtphost"`
}

type Config struct {
	SMTPRelay SMTPRelayConfig `json:"smtprelay"`
}

func main() {
	// https://gist.github.com/samuel/8b500ddd3f6118d052b5e6bc16bc4c09

	CONFIGFILE := "/config/edcom.json"
	smtphost := "localhost"
	if fileExists(CONFIGFILE) {
		content, err := ioutil.ReadFile(CONFIGFILE)
		if err != nil {
			log.Fatalf("Error opening %s: %s", CONFIGFILE, err)
		}

		// Now let's unmarshall the data into `payload`
		var payload Config
		err = json.Unmarshal(content, &payload)
		if err != nil {
			log.Fatalf("Error parsing %s: %s", CONFIGFILE, err)
		}

		smtphost = payload.SMTPRelay.SMTPHost
	}

	if !fileExists("/cert/server.crt") {
		trace.Print("Generating new SSL certificate")
		generateCertificate(smtphost)
	}

	certBytes, err := ioutil.ReadFile("/cert/server.crt")
	if err != nil {
		panic(err)
	}
	keyBytes, err := ioutil.ReadFile("/cert/server.key")
	if err != nil {
		panic(err)
	}

	cer, err := tls.X509KeyPair(certBytes, keyBytes)
	if err != nil {
		panic(err)
	}

	smtp := &smtpd.Server{
		Hostname:       smtphost,
		ReadTimeout:    60 * time.Second,
		WriteTimeout:   60 * time.Second,
		DataTimeout:    60 * time.Second,
		MaxConnections: 500,
		MaxMessageSize: 30 * 1024 * 1024,
		Handler:        mailHandler,
		Authenticator:  authHandler,
		ForceTLS:       false,
		TLSConfig:      &tls.Config{Certificates: []tls.Certificate{cer}},
	}

	trace.Printf("Starting...")
	go func() {
		panic(smtp.ListenAndServe("0.0.0.0:587"))
	}()
	go func() {
		panic(smtp.ListenAndServe("0.0.0.0:2525"))
	}()
	go func() {
		panic(smtp.ListenAndServe("0.0.0.0:8025"))
	}()

	select {}
}
