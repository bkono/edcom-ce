import base64
import binascii
import hashlib
import logging
import os
import posixpath
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from io import BytesIO
from typing import BinaryIO, Dict, Iterator, List, Optional, Protocol, TypedDict

log = logging.getLogger(__name__)
ATTACHMENT_LIFECYCLE_RULE_ID = "edcom-transactional-attachment-expiration"


class AttachmentError(ValueError):
    pass


class AttachmentDisabledError(AttachmentError):
    pass


class AttachmentConfigError(RuntimeError):
    pass


class AttachmentManifest(TypedDict):
    id: str
    storage_backend: str
    bucket: str
    key: str
    filename: str
    content_type: str
    disposition: str
    content_id: str | None
    size: int
    sha256: str
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class AttachmentConfig:
    enabled: bool
    storage_backend: str
    s3_bucket: str
    s3_prefix: str
    s3_region: str
    s3_access_key: str
    s3_secret_key: str
    s3_endpoint_url: str
    local_path: str
    manage_lifecycle: bool
    ttl_hours: int
    lifecycle_expiration_days: int
    lifecycle_abort_multipart_days: int
    max_count: int
    max_file_bytes: int
    max_total_bytes: int
    max_mime_bytes: int
    json_body_max_bytes: int


@dataclass(frozen=True)
class AttachmentUpload:
    filename: str
    content_type: str
    data: bytes
    disposition: str = "attachment"
    content_id: str | None = None


class AttachmentStorage(Protocol):
    storage_backend: str
    bucket: str

    def put_stream(
        self,
        key: str,
        stream: BinaryIO,
        expected_size: int,
        content_type: str,
        metadata: Dict[str, str],
    ) -> Dict[str, str]:
        ...

    @contextmanager
    def open_read(self, manifest: AttachmentManifest) -> Iterator[BinaryIO]:
        ...

    def delete(self, manifest: AttachmentManifest) -> None:
        ...

    def exists(self, manifest: AttachmentManifest) -> bool:
        ...

    def configure_lifecycle(
        self, prefix: str, expiration_days: int, abort_multipart_days: int
    ) -> None:
        ...

    def healthcheck(self, prefix: str) -> None:
        ...


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return int(value)


def _prefix_env(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if not value:
        value = default
    if not value.endswith("/"):
        value += "/"
    while value.startswith("/"):
        value = value[1:]
    return value


def get_attachment_config() -> AttachmentConfig:
    return AttachmentConfig(
        enabled=_bool_env("attachment_enabled", False),
        storage_backend=os.environ.get("attachment_storage_backend", "s3")
        .strip()
        .lower(),
        s3_bucket=os.environ.get("attachment_s3_bucket", "").strip(),
        s3_prefix=_prefix_env("attachment_s3_prefix", "attachments/txn/"),
        s3_region=os.environ.get("attachment_s3_region", "").strip(),
        s3_access_key=os.environ.get("attachment_s3_access_key", "").strip(),
        s3_secret_key=os.environ.get("attachment_s3_secret_key", "").strip(),
        s3_endpoint_url=os.environ.get("attachment_s3_endpoint_url", "").strip(),
        local_path=os.environ.get("attachment_local_path", "/buckets/attachments"),
        manage_lifecycle=_bool_env("attachment_manage_lifecycle", True),
        ttl_hours=_int_env("attachment_ttl_hours", 24),
        lifecycle_expiration_days=_int_env("attachment_lifecycle_expiration_days", 2),
        lifecycle_abort_multipart_days=_int_env(
            "attachment_lifecycle_abort_multipart_days", 1
        ),
        max_count=_int_env("attachment_max_count", 5),
        max_file_bytes=_int_env("attachment_max_file_bytes", 10 * 1024 * 1024),
        max_total_bytes=_int_env("attachment_max_total_bytes", 20 * 1024 * 1024),
        max_mime_bytes=_int_env("attachment_max_mime_bytes", 30 * 1024 * 1024),
        json_body_max_bytes=_int_env("attachment_json_body_max_bytes", 32 * 1024 * 1024),
    )


SES_BLOCKED_EXTENSIONS = {
    ".ade",
    ".adp",
    ".app",
    ".asp",
    ".bas",
    ".bat",
    ".cer",
    ".chm",
    ".cmd",
    ".com",
    ".cpl",
    ".crt",
    ".csh",
    ".der",
    ".exe",
    ".fxp",
    ".gadget",
    ".hlp",
    ".hta",
    ".inf",
    ".ins",
    ".isp",
    ".its",
    ".jar",
    ".js",
    ".jse",
    ".ksh",
    ".lib",
    ".lnk",
    ".mad",
    ".maf",
    ".mag",
    ".mam",
    ".maq",
    ".mar",
    ".mas",
    ".mat",
    ".mau",
    ".mav",
    ".maw",
    ".mda",
    ".mdb",
    ".mde",
    ".mdt",
    ".mdw",
    ".mdz",
    ".msc",
    ".msh",
    ".msh1",
    ".msh2",
    ".mshxml",
    ".msh1xml",
    ".msh2xml",
    ".msi",
    ".msp",
    ".mst",
    ".ops",
    ".pcd",
    ".pif",
    ".plg",
    ".prf",
    ".prg",
    ".ps1",
    ".ps1xml",
    ".ps2",
    ".ps2xml",
    ".psc1",
    ".psc2",
    ".reg",
    ".scf",
    ".scr",
    ".sct",
    ".shb",
    ".shs",
    ".sys",
    ".ps1",
    ".vb",
    ".vbe",
    ".vbs",
    ".vps",
    ".vsmacros",
    ".vss",
    ".vst",
    ".vsw",
    ".vxd",
    ".ws",
    ".wsc",
    ".wsf",
    ".wsh",
    ".xnk",
}

REJECTED_EXTENSIONS = {
    ".exe",
    ".dll",
    ".bat",
    ".cmd",
    ".com",
    ".scr",
    ".ps1",
    ".js",
    ".vbs",
    ".jar",
    ".msi",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
}

ALLOWED_CONTENT_TYPES = {
    ".pdf": {"application/pdf"},
    ".txt": {"text/plain"},
    ".md": {"text/markdown", "text/plain"},
    ".markdown": {"text/markdown", "text/plain"},
    ".csv": {"text/csv", "application/csv"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".gif": {"image/gif"},
    ".webp": {"image/webp"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    },
    ".xlsx": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    },
    ".pptx": {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    },
    ".zip": {"application/zip", "application/x-zip-compressed"},
    ".mp4": {"video/mp4"},
    ".mov": {"video/quicktime"},
    ".webm": {"video/webm"},
}


def _normalize_content_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _safe_filename(filename: str) -> str:
    filename = filename.strip().replace("\\", "/")
    filename = posixpath.basename(filename)
    if not filename or filename in (".", ".."):
        raise AttachmentError("Attachment filename is required")
    if "/" in filename or "\x00" in filename:
        raise AttachmentError("Attachment filename is invalid")
    return filename


def attachment_extension(filename: str) -> str:
    safe = _safe_filename(filename)
    _, ext = os.path.splitext(safe)
    ext = ext.lower()
    if not ext:
        raise AttachmentError("Attachment filename must include an extension")
    return ext


def _validate_signature(ext: str, data: bytes) -> None:
    if ext == ".pdf" and not data.startswith(b"%PDF-"):
        raise AttachmentError("Attachment content is not a valid PDF")
    if ext == ".png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AttachmentError("Attachment content is not a valid PNG")
    if ext in (".jpg", ".jpeg") and not data.startswith(b"\xff\xd8\xff"):
        raise AttachmentError("Attachment content is not a valid JPEG")
    if ext == ".gif" and not (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
        raise AttachmentError("Attachment content is not a valid GIF")
    if ext == ".webp" and not (
        len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    ):
        raise AttachmentError("Attachment content is not a valid WebP")
    if ext in (".zip", ".docx", ".xlsx", ".pptx") and not (
        data.startswith(b"PK\x03\x04")
        or data.startswith(b"PK\x05\x06")
        or data.startswith(b"PK\x07\x08")
    ):
        raise AttachmentError("Attachment content is not a valid ZIP container")
    if ext == ".mp4" and not _has_ftyp_brand(
        data,
        {
            b"avc1",
            b"dash",
            b"iso2",
            b"iso6",
            b"isom",
            b"m4v ",
            b"M4V ",
            b"mp41",
            b"mp42",
            b"mp71",
            b"msdh",
            b"msix",
        },
    ):
        raise AttachmentError("Attachment content is not a valid MP4")
    if ext == ".mov" and not _has_ftyp_brand(data, {b"qt  "}):
        raise AttachmentError("Attachment content is not a valid MOV")
    if ext == ".webm" and not data.startswith(b"\x1a\x45\xdf\xa3"):
        raise AttachmentError("Attachment content is not a valid WebM")


def _has_ftyp_brand(data: bytes, allowed_brands: set[bytes]) -> bool:
    if len(data) < 12:
        return False
    # ISO BMFF files start with a 32-bit box size followed by "ftyp".
    if data[4:8] != b"ftyp":
        return False
    box_size = int.from_bytes(data[0:4], "big")
    if box_size < 12:
        return False
    brand_bytes = data[8 : min(len(data), box_size)]
    for offset in range(0, len(brand_bytes) - 3, 4):
        if brand_bytes[offset : offset + 4] in allowed_brands:
            return True
    return False


def _normalize_content_id(content_id: Optional[str]) -> Optional[str]:
    if content_id is None:
        return None
    normalized = content_id.strip().strip("<>")
    if not normalized:
        return None
    if any(char in normalized for char in ("\r", "\n", "<", ">")):
        raise AttachmentError("Attachment content_id is invalid")
    return normalized


def _mime_content_id(content_id: str) -> str:
    normalized = _normalize_content_id(content_id)
    if normalized is None:
        raise AttachmentError("Attachment content_id is invalid")
    return "<%s>" % normalized


def validate_attachment_upload(upload: AttachmentUpload, config: AttachmentConfig) -> None:
    filename = _safe_filename(upload.filename)
    ext = attachment_extension(filename)
    content_type = _normalize_content_type(upload.content_type)

    if ext in REJECTED_EXTENSIONS or ext in SES_BLOCKED_EXTENSIONS:
        raise AttachmentError("Attachment file type is not allowed")
    if ext not in ALLOWED_CONTENT_TYPES:
        raise AttachmentError("Attachment file type is not allowed")
    if content_type not in ALLOWED_CONTENT_TYPES[ext]:
        raise AttachmentError("Attachment content type does not match its extension")
    if upload.disposition not in ("attachment", "inline"):
        raise AttachmentError("Attachment disposition must be attachment or inline")
    _normalize_content_id(upload.content_id)
    if not upload.data:
        raise AttachmentError("Attachment must not be empty")
    if len(upload.data) > config.max_file_bytes:
        raise AttachmentError("Attachment exceeds maximum file size")
    if ext in (".txt", ".md", ".markdown", ".csv"):
        try:
            upload.data.decode("utf-8")
        except UnicodeDecodeError:
            raise AttachmentError("Text attachments must be UTF-8")
    else:
        _validate_signature(ext, upload.data)


def estimate_mime_size(decoded_total_bytes: int, attachment_count: int, html: str = "") -> int:
    # Base64 expands by roughly 4/3. Add deterministic headroom for MIME headers,
    # boundaries, and transfer encoding line wrapping.
    return (
        len(html.encode("utf-8"))
        + ((decoded_total_bytes + 2) // 3) * 4
        + attachment_count * 2048
        + 8192
    )


def validate_attachment_collection(
    uploads: List[AttachmentUpload],
    config: AttachmentConfig,
    html: str = "",
) -> None:
    if not uploads:
        return
    if not config.enabled:
        raise AttachmentDisabledError("Transactional attachments are not enabled")
    if len(uploads) > config.max_count:
        raise AttachmentError("Too many attachments")

    total = 0
    for upload in uploads:
        validate_attachment_upload(upload, config)
        total += len(upload.data)

    if total > config.max_total_bytes:
        raise AttachmentError("Attachments exceed maximum total size")
    if estimate_mime_size(total, len(uploads), html) > config.max_mime_bytes:
        raise AttachmentError("Attachments exceed maximum MIME message size")


def read_limited_stream(stream: BinaryIO, limit: int) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise AttachmentError("Attachment exceeds maximum file size")
        chunks.append(chunk)
    return b"".join(chunks)


class LocalAttachmentStorage:
    storage_backend = "local"

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.bucket = self.root

    def _path(self, key: str) -> str:
        path = os.path.abspath(os.path.join(self.root, key))
        if not path.startswith(self.root + os.sep):
            raise AttachmentConfigError("Attachment key escapes local storage root")
        return path

    def put_stream(
        self,
        key: str,
        stream: BinaryIO,
        expected_size: int,
        content_type: str,
        metadata: Dict[str, str],
    ) -> Dict[str, str]:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=os.path.dirname(path), delete=False
        ) as tmp:
            tmp_path = tmp.name
            size = shutil.copyfileobj(stream, tmp)
        size = os.path.getsize(tmp_path)
        if size != expected_size:
            os.unlink(tmp_path)
            raise AttachmentError("Attachment storage size mismatch")
        os.replace(tmp_path, path)
        return {
            "storage_backend": self.storage_backend,
            "bucket": self.bucket,
            "key": key,
        }

    @contextmanager
    def open_read(self, manifest: AttachmentManifest) -> Iterator[BinaryIO]:
        with open(self._path(manifest["key"]), "rb") as fp:
            yield fp

    def delete(self, manifest: AttachmentManifest) -> None:
        try:
            os.unlink(self._path(manifest["key"]))
        except FileNotFoundError:
            pass

    def exists(self, manifest: AttachmentManifest) -> bool:
        return os.path.exists(self._path(manifest["key"]))

    def configure_lifecycle(
        self, prefix: str, expiration_days: int, abort_multipart_days: int
    ) -> None:
        os.makedirs(self.root, exist_ok=True)

    def healthcheck(self, prefix: str) -> None:
        os.makedirs(self.root, exist_ok=True)
        key = posixpath.join(prefix, ".healthcheck-%s" % uuid.uuid4().hex)
        manifest = AttachmentManifest(
            id="healthcheck",
            storage_backend=self.storage_backend,
            bucket=self.bucket,
            key=key,
            filename="healthcheck.txt",
            content_type="text/plain",
            disposition="attachment",
            content_id=None,
            size=2,
            sha256=hashlib.sha256(b"ok").hexdigest(),
            created_at=_iso_now(),
            expires_at=_iso_now(),
        )
        self.put_stream(key, BytesIO(b"ok"), 2, "text/plain", {})
        if not self.exists(manifest):
            raise AttachmentConfigError("Attachment storage healthcheck write failed")
        with self.open_read(manifest) as fp:
            if fp.read() != b"ok":
                raise AttachmentConfigError("Attachment storage healthcheck read failed")
        self.delete(manifest)


class S3AttachmentStorage:
    storage_backend = "s3"

    def __init__(self, config: AttachmentConfig):
        import boto3

        if not config.s3_bucket:
            raise AttachmentConfigError("attachment_s3_bucket is required")
        if not config.s3_region:
            raise AttachmentConfigError("attachment_s3_region is required")
        if not config.s3_access_key:
            raise AttachmentConfigError("attachment_s3_access_key is required")
        if not config.s3_secret_key:
            raise AttachmentConfigError("attachment_s3_secret_key is required")

        self.bucket = config.s3_bucket
        client_args = {
            "region_name": config.s3_region,
            "aws_access_key_id": config.s3_access_key,
            "aws_secret_access_key": config.s3_secret_key,
        }
        if config.s3_endpoint_url:
            client_args["endpoint_url"] = config.s3_endpoint_url
        self.client = boto3.client("s3", **client_args)

    def put_stream(
        self,
        key: str,
        stream: BinaryIO,
        expected_size: int,
        content_type: str,
        metadata: Dict[str, str],
    ) -> Dict[str, str]:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=stream,
            ContentType=content_type,
            Metadata=metadata,
        )
        return {
            "storage_backend": self.storage_backend,
            "bucket": self.bucket,
            "key": key,
        }

    @contextmanager
    def open_read(self, manifest: AttachmentManifest) -> Iterator[BinaryIO]:
        obj = self.client.get_object(Bucket=manifest["bucket"], Key=manifest["key"])
        body = obj["Body"]
        try:
            yield body
        finally:
            body.close()

    def delete(self, manifest: AttachmentManifest) -> None:
        self.client.delete_object(Bucket=manifest["bucket"], Key=manifest["key"])

    def exists(self, manifest: AttachmentManifest) -> bool:
        try:
            self.client.head_object(Bucket=manifest["bucket"], Key=manifest["key"])
            return True
        except Exception:
            return False

    def configure_lifecycle(
        self, prefix: str, expiration_days: int, abort_multipart_days: int
    ) -> None:
        rules = []
        try:
            lifecycle = self.client.get_bucket_lifecycle_configuration(
                Bucket=self.bucket
            )
            rules = lifecycle.get("Rules", [])
        except Exception as e:
            error = getattr(e, "response", {}).get("Error", {})
            if error.get("Code") != "NoSuchLifecycleConfiguration":
                raise

        rules = [
            rule
            for rule in rules
            if rule.get("ID") != ATTACHMENT_LIFECYCLE_RULE_ID
        ]
        rules.append(
            {
                "ID": ATTACHMENT_LIFECYCLE_RULE_ID,
                "Status": "Enabled",
                "Filter": {"Prefix": prefix},
                "Expiration": {"Days": expiration_days},
                "AbortIncompleteMultipartUpload": {
                    "DaysAfterInitiation": abort_multipart_days
                },
            }
        )
        self.client.put_bucket_lifecycle_configuration(
            Bucket=self.bucket,
            LifecycleConfiguration={"Rules": rules},
        )

    def healthcheck(self, prefix: str) -> None:
        key = posixpath.join(prefix, ".healthcheck-%s" % uuid.uuid4().hex)
        manifest = AttachmentManifest(
            id="healthcheck",
            storage_backend=self.storage_backend,
            bucket=self.bucket,
            key=key,
            filename="healthcheck.txt",
            content_type="text/plain",
            disposition="attachment",
            content_id=None,
            size=2,
            sha256=hashlib.sha256(b"ok").hexdigest(),
            created_at=_iso_now(),
            expires_at=_iso_now(),
        )
        self.put_stream(key, BytesIO(b"ok"), 2, "text/plain", {})
        if not self.exists(manifest):
            raise AttachmentConfigError("Attachment storage healthcheck write failed")
        with self.open_read(manifest) as fp:
            if fp.read() != b"ok":
                raise AttachmentConfigError("Attachment storage healthcheck read failed")
        self.delete(manifest)


def build_attachment_storage(config: AttachmentConfig) -> AttachmentStorage:
    if config.storage_backend == "s3":
        return S3AttachmentStorage(config)
    if config.storage_backend == "local":
        return LocalAttachmentStorage(config.local_path)
    raise AttachmentConfigError("Unsupported attachment_storage_backend")


def validate_attachment_startup() -> None:
    config = get_attachment_config()
    if not config.enabled:
        return
    storage = build_attachment_storage(config)
    storage.healthcheck(config.s3_prefix)
    if config.manage_lifecycle:
        storage.configure_lifecycle(
            config.s3_prefix,
            config.lifecycle_expiration_days,
            config.lifecycle_abort_multipart_days,
        )
    log.info(
        "Transactional attachment storage ready: backend=%s bucket=%s prefix=%s",
        storage.storage_backend,
        storage.bucket,
        config.s3_prefix,
    )


def _iso_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def store_attachments(
    storage: AttachmentStorage,
    config: AttachmentConfig,
    cid: str,
    message_id: str,
    uploads: List[AttachmentUpload],
    html: str = "",
) -> List[AttachmentManifest]:
    validate_attachment_collection(uploads, config, html)
    manifests: List[AttachmentManifest] = []
    created_at = datetime.utcnow().replace(microsecond=0)
    expires_at = created_at + timedelta(hours=config.ttl_hours)

    try:
        for upload in uploads:
            attachment_id = uuid.uuid4().hex
            filename = _safe_filename(upload.filename)
            content_type = _normalize_content_type(upload.content_type)
            sha256 = hashlib.sha256(upload.data).hexdigest()
            key = posixpath.join(
                config.s3_prefix,
                cid,
                message_id,
                attachment_id,
                filename,
            )
            storage_fields = storage.put_stream(
                key,
                BytesIO(upload.data),
                len(upload.data),
                content_type,
                {
                    "attachment-id": attachment_id,
                    "sha256": sha256,
                    "expires-at": expires_at.isoformat() + "Z",
                },
            )
            manifests.append(
                AttachmentManifest(
                    id=attachment_id,
                    storage_backend=storage_fields["storage_backend"],
                    bucket=storage_fields["bucket"],
                    key=storage_fields["key"],
                    filename=filename,
                    content_type=content_type,
                    disposition=upload.disposition,
                    content_id=_normalize_content_id(upload.content_id),
                    size=len(upload.data),
                    sha256=sha256,
                    created_at=created_at.isoformat() + "Z",
                    expires_at=expires_at.isoformat() + "Z",
                )
            )
    except Exception:
        delete_attachments(storage, manifests)
        raise

    return manifests


def delete_attachments(
    storage: AttachmentStorage,
    manifests: List[AttachmentManifest],
) -> None:
    for manifest in manifests:
        try:
            storage.delete(manifest)
        except Exception:
            log.exception("error deleting transactional attachment %s", manifest["id"])


def read_attachment_bytes(
    storage: AttachmentStorage, manifest: AttachmentManifest
) -> bytes:
    with storage.open_read(manifest) as fp:
        data = fp.read()
    if len(data) != manifest["size"]:
        raise AttachmentError("Attachment size changed in storage")
    if hashlib.sha256(data).hexdigest() != manifest["sha256"]:
        raise AttachmentError("Attachment checksum changed in storage")
    return data


def build_raw_mime_message(
    frm: str,
    replyto: str,
    to: str,
    subject: str,
    html: str,
    storage: AttachmentStorage,
    attachments: List[AttachmentManifest],
) -> bytes:
    msg = EmailMessage(policy=SMTP)
    msg["From"] = frm
    msg["To"] = to
    if replyto:
        msg["Reply-To"] = replyto
    msg["Subject"] = subject
    msg.set_content("This message contains an HTML body and attachments.")
    msg.add_alternative(html, subtype="html")

    for manifest in attachments:
        data = read_attachment_bytes(storage, manifest)
        maintype, subtype = manifest["content_type"].split("/", 1)
        kwargs = {
            "maintype": maintype,
            "subtype": subtype,
            "filename": manifest["filename"],
            "disposition": manifest["disposition"],
        }
        if manifest.get("content_id"):
            kwargs["cid"] = _mime_content_id(manifest["content_id"])
        msg.add_attachment(data, **kwargs)

    return msg.as_bytes()


def json_attachment_string(
    item: Dict[str, str],
    field: str,
    default: Optional[str] = "",
    allow_null: bool = False,
) -> Optional[str]:
    if field not in item:
        return default
    value = item[field]
    if value is None and allow_null:
        return None
    if not isinstance(value, str):
        raise AttachmentError("Attachment %s must be a string" % field)
    return value


def decode_json_attachment(item: Dict[str, str], config: AttachmentConfig) -> AttachmentUpload:
    if not isinstance(item, dict):
        raise AttachmentError("Attachment must be a JSON object")
    content = item.get("content", "")
    if not isinstance(content, str):
        raise AttachmentError("Attachment content must be valid base64")
    try:
        data = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise AttachmentError("Attachment content must be valid base64")
    if len(data) > config.max_file_bytes:
        raise AttachmentError("Attachment exceeds maximum file size")
    return AttachmentUpload(
        filename=json_attachment_string(item, "filename", ""),
        content_type=json_attachment_string(item, "content_type", ""),
        data=data,
        disposition=(
            json_attachment_string(item, "disposition", "attachment")
            or "attachment"
        ),
        content_id=json_attachment_string(item, "content_id", None, allow_null=True),
    )


def new_message_id() -> str:
    # Keep object keys unique even if the transactional campid is regenerated later.
    return uuid.uuid4().hex
