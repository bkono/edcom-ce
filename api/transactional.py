import falcon
import os
import re
import json
import shortuuid
import dateutil.parser
import email.utils
import traceback
import csv
import zipfile
from typing import Any, List, Tuple
from datetime import datetime, timedelta
from dateutil.tz import tzutc
from email.utils import formataddr, parseaddr
from jinja2 import Template

from .shared import config as config_module_side_effects  # noqa: F401
from .shared.crud import check_noadmin, CRUDCollection, CRUDSingle, get_orig
from .shared.utils import (
    gen_screenshot,
    generate_html,
    remove_newlines,
    create_txnid,
    run_task,
    parse_txnid,
    get_webroot,
)
from .shared.send import (
    send_backend_mail,
    check_send_limit,
    check_test_limit,
    load_domain_throttles,
    route_backend_types_for_recipient,
)
from .shared.attachments import (
    AttachmentError,
    AttachmentUpload,
    build_attachment_storage,
    decode_json_attachment,
    delete_attachments,
    get_attachment_config,
    read_limited_stream,
    store_attachments,
    validate_attachment_collection,
)
from .shared.db import json_iter, open_db, JsonObj
from .shared.tasks import tasks, LOW_PRIORITY, HIGH_PRIORITY
from .shared.s3 import s3_write, s3_read, s3_write_stream, s3_delete
from .shared.log import get_logger

log = get_logger()

Template("").environment.autoescape = True


class Log(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req)

        page = int(req.get_param("page", default=1))

        PAGE_SIZE = 10

        db = req.context["db"]

        ret = []
        for id, ts, data in db.execute(
            """
                select id, ts, data from txnsends
                where cid = %s
                order by ts desc
                limit %s
                offset %s
        """,
            db.get_cid(),
            PAGE_SIZE,
            PAGE_SIZE * (page - 1),
        ):
            data["ts"] = ts.isoformat() + "Z"
            data["id"] = id
            ret.append(data)

        total = db.single("select count(id) from txnsends where cid = %s", db.get_cid())

        req.context["result"] = {
            "records": ret,
            "page_size": PAGE_SIZE,
            "total": total,
        }


class LogExport(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req)

        db = req.context["db"]

        uuid = shortuuid.uuid()

        ts = datetime.utcnow()

        path = "exports/%s/transactional-log-%s.zip" % (
            uuid,
            ts.strftime("%Y%m%d-%H%M%SZ"),
        )

        exportid = db.exports.add(
            {
                "transactional_log": True,
                "started_at": ts.isoformat() + "Z",
                "name": "Transactional Log",
                "url": f"{get_webroot()}/transfer/{path}",
            }
        )

        run_task(export_transactional_log, db.get_cid(), exportid, path)


@tasks.task(priority=HIGH_PRIORITY)
def export_transactional_log(cid: str, exportid: str, path: str) -> None:
    with open_db() as db:
        try:
            cnt = 0

            file = "/tmp/transactional-log-%s.csv" % exportid
            fp = open(file, "w")
            writer = csv.DictWriter(
                fp,
                [
                    "Event",
                    "From Name",
                    "From Email",
                    "To Name",
                    "To Email",
                    "Subject",
                    "Tag",
                    "Date",
                    "Status",
                    "Error",
                    "Opened",
                    "Clicked",
                    "Unsubscribed",
                    "Complained",
                ],
            )
            writer.writeheader()
            for ts, data in db.execute(
                """
                    select ts, data from txnsends
                    where cid = %s
                    order by ts desc
            """,
                cid,
            ):
                formatted_ts = ts.isoformat() + "Z"
                writer.writerow(
                    {
                        "Event": data.get("event") or "Delivery",
                        "From Name": data.get("fromname", ""),
                        "From Email": data.get("fromemail", ""),
                        "To Name": data.get("toname", ""),
                        "To Email": data.get("to", ""),
                        "Subject": data.get("subject", ""),
                        "Tag": data.get("tag") or "untagged",
                        "Date": formatted_ts,
                        "Status": data.get("status", ""),
                        "Error": data.get("error", ""),
                        "Opened": "true" if data.get("open") else "",
                        "Clicked": "true" if data.get("click") else "",
                        "Unsubscribed": "true" if data.get("unsub") else "",
                        "Complained": "true" if data.get("complaint") else "",
                    }
                )
                cnt += 1

            zipname = "/tmp/%s.zip" % exportid
            outzip = zipfile.ZipFile(zipname, "w", zipfile.ZIP_DEFLATED)
            fp.close()
            outzip.write(file, "transactional-log.csv")
            outzip.close()

            size = os.path.getsize(zipname)
            outfp = open(zipname, "rb")
            s3_write_stream(os.environ["s3_transferbucket"], path, outfp)
            outfp.close()

            db.exports.patch(exportid, {"complete": True, "count": cnt, "size": size})
        except Exception as e:
            log.exception("error")
            db.exports.patch(exportid, {"error": str(e)})


class Stats(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req)

        db = req.context["db"]

        start = req.get_param("start", required=True)
        end = req.get_param("end", required=True)
        search = (
            ".*" + re.escape(req.get_param("search", default="").strip().lower()) + ".*"
        )

        try:
            start = (
                dateutil.parser.parse(start).astimezone(tzutc()).replace(tzinfo=None)
            )
            end = dateutil.parser.parse(end).astimezone(tzutc()).replace(tzinfo=None)
        except:
            raise falcon.HTTPBadRequest(
                title="Invalid parameter", description="invalid date parameter"
            )

        now = datetime.utcnow().replace(minute=59, second=59, microsecond=0)
        if end > now:
            end = now

        diff = end - start

        if diff.total_seconds() < 24 * 60 * 60:
            start = end - timedelta(hours=24)
            diff = end - start

        interval = diff / 12

        hourarray = []
        for i in range(12):
            hourarray.append(end - (interval * (11 - i)))

        stats = {}
        for row in db.execute(
            """select width_bucket(ts, %s) hourbucket,
                                 sum(send), sum(soft), sum(hard), sum(open)
                                 from txnstats
                                 where cid = %s and ts >= %s
                                 and tag ~ %s
                                 group by hourbucket
                                 order by hourbucket""",
            hourarray,
            db.get_cid(),
            start,
            search,
        ):
            if row[0] >= len(hourarray):
                break
            ts = hourarray[row[0]].isoformat() + "Z"
            stats[ts] = {
                "ts": ts,
                "send": row[1],
                "soft": row[2],
                "hard": row[3],
                "open": row[4],
            }
        for ts in hourarray:
            t = ts.isoformat() + "Z"
            if t not in stats:
                stats[t] = {
                    "ts": t,
                    "send": 0,
                    "soft": 0,
                    "hard": 0,
                    "err": 0,
                    "open": 0,
                    "defercnt": 0,
                }
        req.context["result"] = sorted(
            iter(stats.values()), key=lambda s: s["ts"], reverse=True
        )


class Tags(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req)

        db = req.context["db"]

        start = req.get_param("start", required=True)
        end = req.get_param("end", required=True)
        search = (
            ".*" + re.escape(req.get_param("search", default="").strip().lower()) + ".*"
        )

        try:
            start = (
                dateutil.parser.parse(start).astimezone(tzutc()).replace(tzinfo=None)
            )
            end = dateutil.parser.parse(end).astimezone(tzutc()).replace(tzinfo=None)
        except:
            raise falcon.HTTPBadRequest(
                title="Invalid parameter", description="invalid date parameter"
            )

        ret = []
        for tag, complaint, unsub, count, send, open, click in db.execute(
            """select tag, sum(complaint), sum(unsub),
                                                                             sum(hard+soft+send), sum(send), sum(open), sum(click)
                                                                             from txnstats
                                                                             where cid = %s and ts >= %s and ts <= %s
                                                                             and tag ~ %s
                                                                             group by tag""",
            db.get_cid(),
            start,
            end,
            search,
        ):
            ret.append(
                {
                    "tag": tag,
                    "complaint": complaint,
                    "unsub": unsub,
                    "count": count,
                    "send": send,
                    "open": open,
                    "click": click,
                }
            )

        req.context["result"] = ret


class Tag(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response, tag: str) -> None:
        check_noadmin(req)

        db = req.context["db"]

        start = req.get_param("start", required=True)
        end = req.get_param("end", required=True)

        try:
            start = (
                dateutil.parser.parse(start).astimezone(tzutc()).replace(tzinfo=None)
            )
            end = dateutil.parser.parse(end).astimezone(tzutc()).replace(tzinfo=None)
        except:
            raise falcon.HTTPBadRequest(
                title="Invalid parameter", description="invalid date parameter"
            )

        row = db.row(
            """select sum(complaint), sum(unsub), sum(hard+soft+send), sum(send), sum(open), sum(click),
                        sum(hard), sum(soft), sum(open_all), sum(click_all)
                        from txnstats
                        where tag = %s and cid = %s and ts >= %s and ts <= %s""",
            tag,
            db.get_cid(),
            start,
            end,
        )

        complaint, unsub, count, send, open, click, hard, soft, open_all, click_all = (
            row
        )

        req.context["result"] = {
            "tag": tag,
            "complaint": complaint,
            "unsub": unsub,
            "count": count,
            "send": send,
            "open": open,
            "click": click,
            "hard": hard,
            "soft": soft,
            "open_all": open_all,
            "click_all": click_all,
        }


class TagDomainStats(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response, tag: str) -> None:
        check_noadmin(req)

        db = req.context["db"]

        start = req.get_param("start", required=True)
        end = req.get_param("end", required=True)

        try:
            start = (
                dateutil.parser.parse(start).astimezone(tzutc()).replace(tzinfo=None)
            )
            end = dateutil.parser.parse(end).astimezone(tzutc()).replace(tzinfo=None)
        except:
            raise falcon.HTTPBadRequest(
                title="Invalid parameter", description="invalid date parameter"
            )

        ret = []
        for row in db.execute(
            """select domain, sum(complaint), sum(unsub),
                                 sum(hard+soft+send), sum(send), sum(hard), sum(soft),
                                 sum(open), sum(click)
                                 from txnstats
                                 where cid = %s and ts >= %s and ts <= %s and tag = %s
                                 group by domain""",
            db.get_cid(),
            start,
            end,
            tag,
        ):
            domain, complaint, unsub, count, send, hard, soft, open, click = row
            ret.append(
                {
                    "domain": domain,
                    "complaint": complaint,
                    "unsub": unsub,
                    "count": count,
                    "send": send,
                    "hard": hard,
                    "soft": soft,
                    "open": open,
                    "click": click,
                }
            )

        req.context["result"] = ret


class TagMessages(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response, tag: str) -> None:
        check_noadmin(req, True)

        db = req.context["db"]

        start = req.get_param("start", required=True)
        end = req.get_param("end", required=True)

        try:
            start = (
                dateutil.parser.parse(start).astimezone(tzutc()).replace(tzinfo=None)
            )
            end = dateutil.parser.parse(end).astimezone(tzutc()).replace(tzinfo=None)
        except:
            raise falcon.HTTPBadRequest(
                title="Invalid parameter", description="invalid date parameter"
            )

        req.context["result"] = [
            {
                "msg": row[0],
                "count": row[1],
            }
            for row in db.execute(
                """select message, sum(count) cnt from txnstatmsgs
               where tag = %s and cid = %s
               and domain = %s and msgtype = %s
               and ts >= %s and ts <= %s
               group by message""",
                tag,
                db.get_cid(),
                req.get_param("domain", required=True),
                req.get_param("type", required=True),
                start,
                end,
            )
        ]


def parse_send_request(
    req: falcon.Request,
) -> Tuple[JsonObj, List[AttachmentUpload]]:
    content_type = req.content_type or ""
    config = get_attachment_config()
    if content_type.startswith("multipart/form-data"):
        return parse_multipart_send_request(req, config)
    return parse_json_send_request(req, config)


def parse_json_send_request(
    req: falcon.Request,
    config: Any,
) -> Tuple[JsonObj, List[AttachmentUpload]]:
    doc = req.context.get("doc")
    if not doc:
        raise falcon.HTTPBadRequest(
            title="Not JSON", description="A valid JSON document is required."
        )
    raw_body = req.context.get("body_raw", b"")
    raw_attachments = doc.get("attachments", [])
    if raw_attachments is None:
        raw_attachments = []
    if raw_attachments and len(raw_body) > config.json_body_max_bytes:
        raise falcon.HTTPBadRequest(
            title="Request too large",
            description="Attachment JSON request body exceeds the maximum size",
        )
    if not isinstance(raw_attachments, list):
        raise falcon.HTTPBadRequest(
            title="Invalid attachments",
            description="attachments must be an array",
        )
    try:
        uploads = [
            decode_json_attachment(attachment, config)
            for attachment in raw_attachments
        ]
    except AttachmentError as e:
        raise falcon.HTTPBadRequest(
            title="Invalid attachment", description=str(e)
        )
    doc.pop("attachments", None)
    return doc, uploads


def read_multipart_part_bytes(
    part: Any, limit: int, limit_message: str = "Attachment exceeds maximum file size"
) -> bytes:
    stream = getattr(part, "stream", None)
    try:
        if stream is not None:
            return read_limited_stream(stream, limit)
        data = getattr(part, "data", None)
        if data is None:
            data = getattr(part, "text", "")
        if isinstance(data, str):
            data = data.encode("utf-8")
        if len(data) > limit:
            raise AttachmentError()
        return data
    except AttachmentError:
        raise AttachmentError(limit_message)


def read_multipart_payload(part: Any, config: Any) -> str:
    try:
        data = read_multipart_part_bytes(part, config.json_body_max_bytes)
    except AttachmentError:
        raise falcon.HTTPBadRequest(
            title="Request too large",
            description="Attachment JSON request body exceeds the maximum size",
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise falcon.HTTPBadRequest(
            title="Malformed payload",
            description="payload must be a valid UTF-8 JSON document",
        )


def parse_multipart_attachment_metadata(part: Any, config: Any) -> JsonObj:
    payload = read_multipart_payload(part, config)
    try:
        metadata = json.loads(payload)
    except (TypeError, ValueError, UnicodeDecodeError):
        raise falcon.HTTPBadRequest(
            title="Malformed attachment metadata",
            description="attachment_metadata must be a valid UTF-8 JSON document",
        )
    if not isinstance(metadata, dict):
        raise falcon.HTTPBadRequest(
            title="Malformed attachment metadata",
            description="attachment_metadata must be a JSON object",
        )
    disposition = metadata.get("disposition", "attachment")
    if disposition == "":
        disposition = "attachment"
    content_id = metadata.get("content_id")
    if not isinstance(disposition, str):
        raise falcon.HTTPBadRequest(
            title="Malformed attachment metadata",
            description="attachment_metadata disposition must be a string",
        )
    if content_id is not None and not isinstance(content_id, str):
        raise falcon.HTTPBadRequest(
            title="Malformed attachment metadata",
            description="attachment_metadata content_id must be a string",
        )
    return {
        "disposition": disposition,
        "content_id": content_id.strip().strip("<>") if content_id else None,
    }


def parse_multipart_send_request(
    req: falcon.Request,
    config: Any,
) -> Tuple[JsonObj, List[AttachmentUpload]]:
    payload = None
    uploads = []
    total_attachment_bytes = 0
    pending_attachment_metadata = None
    try:
        media = req.get_media()
        for part in media:
            if part.name == "payload":
                if payload is not None:
                    raise falcon.HTTPBadRequest(
                        title="Invalid multipart request",
                        description="Only one payload field is allowed",
                    )
                payload = read_multipart_payload(part, config)
                continue
            if part.name == "attachment_metadata":
                if pending_attachment_metadata is not None:
                    raise falcon.HTTPBadRequest(
                        title="Invalid multipart request",
                        description="attachment_metadata must be followed by an attachment",
                    )
                pending_attachment_metadata = parse_multipart_attachment_metadata(
                    part, config
                )
                continue
            if part.name != "attachment":
                raise falcon.HTTPBadRequest(
                    title="Invalid multipart request",
                    description=(
                        "Only payload, attachment_metadata, and attachment fields "
                        "are allowed"
                    ),
                )

            if len(uploads) >= config.max_count:
                raise AttachmentError("Too many attachments")
            remaining_total = config.max_total_bytes - total_attachment_bytes
            if remaining_total <= 0:
                raise AttachmentError("Attachments exceed maximum total size")
            filename = getattr(part, "filename", "") or ""
            content_type = getattr(part, "content_type", "") or ""
            data = read_multipart_part_bytes(
                part,
                min(config.max_file_bytes, remaining_total),
                "Attachments exceed maximum total size",
            )
            total_attachment_bytes += len(data)
            uploads.append(
                AttachmentUpload(
                    filename=filename,
                    content_type=content_type,
                    data=data,
                    disposition=(
                        pending_attachment_metadata or {}
                    ).get("disposition", "attachment"),
                    content_id=(pending_attachment_metadata or {}).get("content_id"),
                )
            )
            pending_attachment_metadata = None
    except falcon.HTTPError:
        raise
    except AttachmentError as e:
        raise falcon.HTTPBadRequest(
            title="Invalid attachment", description=str(e)
        )
    except Exception as e:
        raise falcon.HTTPBadRequest(
            title="Invalid multipart request", description=str(e)
        )

    if payload is None:
        raise falcon.HTTPBadRequest(
            title="Missing payload",
            description="Multipart transactional sends require a payload field",
        )
    if pending_attachment_metadata is not None:
        raise falcon.HTTPBadRequest(
            title="Invalid multipart request",
            description="attachment_metadata must be followed by an attachment",
        )
    try:
        doc = json.loads(payload)
    except (TypeError, ValueError, UnicodeDecodeError):
        raise falcon.HTTPBadRequest(
            title="Malformed payload",
            description="payload must be a valid UTF-8 JSON document",
        )
    if not isinstance(doc, dict):
        raise falcon.HTTPBadRequest(
            title="Malformed payload", description="payload must be a JSON object"
        )
    return doc, uploads


def attachment_manifests_expired(
    attachments: List[JsonObj],
    now: datetime = None,
) -> bool:
    if now is None:
        now = datetime.utcnow()
    for attachment in attachments:
        expires_at = attachment.get("expires_at")
        if not expires_at:
            return True
        try:
            expires_dt = (
                dateutil.parser.parse(expires_at)
                .astimezone(tzutc())
                .replace(tzinfo=None)
            )
        except:
            return True
        if expires_dt <= now:
            return True
    return False


class Send(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req, True)

        db = req.context["db"]

        doc, attachment_uploads = parse_send_request(req)

        template = None
        if doc.get("template", ""):
            template = db.txntemplates.get(doc["template"])
            if template is None:
                raise falcon.HTTPForbidden()
            if template.get("fromname", ""):
                doc["fromname"] = template["fromname"]
            if template.get("fromemail", ""):
                doc["fromemail"] = template["fromemail"]
            if template.get("returnpath", ""):
                doc["returnpath"] = template["returnpath"]
            if template.get("subject", ""):
                doc["subject"] = template["subject"]
            if template.get("replyto", ""):
                doc["replyto"] = template["replyto"]
            if template.get("tag", ""):
                doc["tag"] = template["tag"]

        if not doc.get("to", ""):
            raise falcon.HTTPBadRequest(
                title="Missing parameter", description="No to address specified."
            )
        if not doc.get("body", "") and template is None:
            raise falcon.HTTPBadRequest(
                title="Missing parameter", description="No body specified."
            )
        if not doc.get("fromemail", "") and not doc.get("returnpath", ""):
            raise falcon.HTTPBadRequest(
                title="Missing parameter", description="No from email specified."
            )
        if not doc.get("subject", ""):
            raise falcon.HTTPBadRequest(
                title="Missing parameter", description="No subject specified."
            )

        txnsettings = db.txnsettings.get_singleton()

        mycid = db.get_cid()

        db.set_cid(None)

        company = db.companies.get(mycid)
        if company is None:
            raise falcon.HTTPForbidden()
        availroutes = company["routes"]

        if doc.get("route", ""):
            if doc["route"] not in availroutes:
                raise falcon.HTTPForbidden()
            route = db.routes.get(doc["route"])
            if route is None or "published" not in route:
                raise falcon.HTTPForbidden()
        elif txnsettings.get("route", ""):
            if txnsettings["route"] in availroutes:
                route = db.routes.get(txnsettings["route"])
                if route is not None and "published" in route:
                    doc["route"] = txnsettings["route"]

        # if no route specified or the default route is not available, pick the first route in alphabetical order (same as the dropdown in the settings UI)
        if not doc.get("route", ""):
            routeobjs = []
            for routeid in availroutes:
                routeobj = db.routes.get(routeid)
                if routeobj is None or "published" not in routeobj:
                    continue
                routeobjs.append(routeobj)

            if len(routeobjs) == 0:
                raise falcon.HTTPForbidden()

            routeobjs.sort(key=lambda r: r.get("name", "").lower())
            doc["route"] = routeobjs[0]["id"]

        tag = "untagged"
        if doc.get("tag", ""):
            tag = doc["tag"].lower().strip()
            if re.search(r"[^a-z0-9\.\-_]", tag) or len(tag) > 18:
                raise falcon.HTTPBadRequest(
                    title="Missing parameter", description="Invalid tag."
                )

        if db.single(
            "select count(id) from txntags where cid = %s", mycid
        ) > 400 and not db.single(
            "select id from txntags where cid = %s and tag  = %s", mycid, tag
        ):
            raise falcon.HTTPBadRequest(
                title="Too many transactional tags",
                description="You have reached the limit of distinct transactional tags in the system; please contact your administrator if you need to add more",
            )
        tagid = db.single(
            "insert into txntags (id, cid, tag) values (%s, %s, %s) on conflict (cid, tag) do update set tag = excluded.tag returning id",
            shortuuid.uuid(),
            mycid,
            tag,
        )

        campid = create_txnid(tagid)
        txnmsgid, _ = parse_txnid(campid)

        bodykey = ""
        _, addr = parseaddr(doc["to"])
        if "@" not in addr:
            raise falcon.HTTPBadRequest(
                title="Invalid to address",
                description="The 'to' address specified is invalid",
            )
        domain = addr.split("@")[1]

        if attachment_uploads:
            route = db.routes.get(doc["route"])
            if route is None or "published" not in route:
                raise falcon.HTTPForbidden()
            backend_types = route_backend_types_for_recipient(db, route, addr.lower())
            if not backend_types or backend_types != {"ses"}:
                raise falcon.HTTPBadRequest(
                    title="Unsupported postal route",
                    description=(
                        "Transactional attachments are only supported for routes "
                        "that resolve exclusively to SES"
                    ),
                )
            attachment_config = get_attachment_config()
            try:
                validate_attachment_collection(
                    attachment_uploads,
                    attachment_config,
                    doc.get("body", ""),
                )
            except AttachmentError as e:
                raise falcon.HTTPBadRequest(
                    title="Invalid attachment", description=str(e)
                )
        else:
            attachment_config = None

        attachment_storage = None
        attachment_manifests = []
        try:
            if template is None:
                bodyutf8 = doc["body"].encode("utf-8")
                bodykey = "templates/txn/%s/%s.html" % (mycid, shortuuid.uuid())
                s3_write(os.environ["s3_databucket"], bodykey, bodyutf8)

            if attachment_uploads:
                attachment_storage = build_attachment_storage(attachment_config)
                try:
                    attachment_manifests = store_attachments(
                        attachment_storage,
                        attachment_config,
                        mycid,
                        txnmsgid,
                        attachment_uploads,
                        doc.get("body", ""),
                    )
                except AttachmentError as e:
                    raise falcon.HTTPBadRequest(
                        title="Invalid attachment", description=str(e)
                    )
                log.info(
                    "accepted %s transactional attachments for cid=%s msgid=%s",
                    len(attachment_manifests),
                    mycid,
                    txnmsgid,
                )

            db.execute(
                "insert into txnqueue (cid, route, domain, data) values (%s, %s, %s, %s)",
                mycid,
                doc["route"],
                domain,
                {
                    "tag": tag,
                    "template": doc.get("template", ""),
                    "body": bodykey,
                    "variables": doc.get("variables", None),
                    "to": doc["to"],
                    "fromname": doc.get("fromname", ""),
                    "fromemail": doc.get("fromemail", ""),
                    "returnpath": doc.get("returnpath", ""),
                    "replyto": doc.get("replyto", ""),
                    "subject": doc["subject"],
                    "toname": doc.get("toname"),
                    "route": doc["route"],
                    "campid": campid,
                    "disableopens": txnsettings.get("disableopens", False),
                    "attachments": attachment_manifests,
                },
            )
            db.execute(
                "insert into txnsends (id, cid, ts, msgid, data) values (%s, %s, %s, %s, %s)",
                shortuuid.uuid(),
                mycid,
                datetime.utcnow(),
                txnmsgid,
                {
                    "event": "Injection",
                    "status": "Accepted",
                    "subject": doc["subject"],
                    "tag": tag,
                    "fromname": doc.get("fromname", ""),
                    "fromemail": doc.get("fromemail", "")
                    or doc.get("returnpath", ""),
                    "toname": doc.get("toname"),
                    "to": doc["to"],
                },
            )
        except Exception:
            if attachment_storage is not None and attachment_manifests:
                delete_attachments(attachment_storage, attachment_manifests)
            raise

        req.context["result"] = {"tag": tag, "to": doc["to"], "result": "ok"}


@tasks.task(priority=LOW_PRIORITY)
def send_txn(company: JsonObj, data: JsonObj) -> None:
    with open_db() as db:
        mycid = None
        txnmsgid = None
        attachment_manifests = data.get("attachments") or []

        try:
            campid = data.get("campid")
            tag = data["tag"]
            if campid is None:
                tagid = db.single(
                    "select id from txntags where cid = %s and tag = %s", mycid, tag
                )
                campid = create_txnid(tagid)

            txnmsgid, _ = parse_txnid(campid)

            mycid = company["id"]
            if data["template"]:
                bodytemplate = db.txntemplates.get(data["template"])
                if bodytemplate is None:
                    raise Exception("Template not found")
            else:
                bodytxt = s3_read(os.environ["s3_databucket"], data["body"]).decode(
                    "utf-8"
                )
                bodytemplate = {"type": "raw", "rawText": bodytxt, "cid": mycid}
                s3_delete(os.environ["s3_databucket"], data["body"])
            variables = data["variables"]

            imagebucket = os.environ["s3_imagebucket"]
            parentcompany = db.companies.get(company["cid"])
            if parentcompany is not None:
                imagebucket = parentcompany.get("s3_imagebucket", imagebucket)

            route = db.routes.get(data["route"])
            if route is None or "published" not in route:
                raise Exception("Route not found")

            if attachment_manifests and attachment_manifests_expired(
                attachment_manifests
            ):
                log.warning(
                    "rejecting transactional message with expired attachments cid=%s campid=%s",
                    mycid,
                    campid,
                )
                raise Exception("Transactional attachments expired before send")

            if variables is not None and bodytemplate.get("type") == "raw":
                try:
                    bodytemplate["rawText"] = Template(bodytemplate["rawText"]).render(
                        **variables
                    )
                except Exception as e:
                    data["error"] = "Template error: %s" % e
                    data["event"] = "Error"
                    db.execute(
                        "insert into txnsends (id, cid, ts, msgid, data) values (%s, %s, %s, %s, %s)",
                        shortuuid.uuid(),
                        mycid,
                        datetime.utcnow(),
                        txnmsgid,
                        data,
                    )
                    return

            html, _ = generate_html(
                db,
                bodytemplate,
                campid,
                imagebucket,
                noopens=data.get("disableopens", False),
            )

            _, addr = parseaddr(data["to"])
            if not addr:
                addr = remove_newlines(data["to"])

            email = addr.strip().lower()
            d = email.split("@")[1]
            if db.single(
                "select email from unsublogs where cid = %s and email = %s and (unsubscribed or complained or bounced)",
                mycid,
                email,
            ):
                log.info(
                    "Suppressing transactional message to %s for %s due to unsub",
                    email,
                    mycid,
                )
                return
            if db.single(
                "select item from exclusions where cid = %s and item in (%s, %s)",
                mycid,
                email,
                d,
            ):
                log.info(
                    "Suppressing transactional message to %s for %s due to exclusion",
                    email,
                    mycid,
                )
                return

            fromname = remove_newlines(data["fromname"])
            fromemail = remove_newlines(data["fromemail"])
            returnpath = remove_newlines(data["returnpath"])
            if variables is not None:
                fromname = replace_vars(fromname, variables)
                fromemail = replace_vars(fromemail, variables)
                returnpath = replace_vars(returnpath, variables)

            fromdomain = ""
            if "@" in returnpath:
                fromdomain = returnpath.split("@")[-1].strip().lower()
            elif "@" in fromemail:
                fromdomain = fromemail.split("@")[-1].strip().lower()

            if not fromemail:
                fromemail = returnpath
            if not returnpath:
                returnpath = fromemail

            if fromname:
                frm = formataddr((fromname, fromemail))
            else:
                frm = fromemail

            if data["replyto"]:
                replyto = remove_newlines(data["replyto"])
            else:
                replyto = remove_newlines(data["fromemail"] or data["returnpath"])

            subject = remove_newlines(data["subject"])

            if variables is not None:
                subject = replace_vars(subject, variables)
                replyto = replace_vars(replyto, variables)

                if bodytemplate.get("type") != "raw":
                    html = replace_vars(html, variables)

            tofull = addr
            if data["toname"]:
                tofull = formataddr((data["toname"], addr))

            delivered = send_backend_mail(
                db,
                mycid,
                route,
                html,
                frm,
                returnpath,
                fromdomain,
                replyto,
                tofull,
                addr,
                subject,
                campid=campid,
                toname=data["toname"],
                attachments=attachment_manifests,
                raise_err=True,
            )

            if delivered:
                db.execute(
                    "insert into txnsends (id, cid, ts, msgid, data) values (%s, %s, %s, %s, %s)",
                    shortuuid.uuid(),
                    mycid,
                    datetime.utcnow(),
                    txnmsgid,
                    {
                        "event": "Delivery",
                        "status": "OK",
                        "subject": subject,
                        "tag": tag,
                        "fromname": fromname,
                        "fromemail": fromemail or returnpath,
                        "toname": data["toname"],
                        "to": addr,
                    },
                )
        except Exception as e:
            log.exception("error")
            if mycid is not None and txnmsgid is not None:
                try:
                    data["event"] = "Error"
                    data["error"] = str(e)
                    db.execute(
                        "insert into txnsends (id, cid, ts, msgid, data) values (%s, %s, %s, %s, %s)",
                        shortuuid.uuid(),
                        mycid,
                        datetime.utcnow(),
                        txnmsgid,
                        data,
                    )
                except:
                    log.exception("error")
        finally:
            if attachment_manifests:
                try:
                    delete_attachments(
                        build_attachment_storage(get_attachment_config()),
                        attachment_manifests,
                    )
                except:
                    log.exception("error cleaning up transactional attachments")


@tasks.task(priority=HIGH_PRIORITY)
def get_txn_screenshot(id: str) -> None:
    try:
        with open_db() as db:
            gen_screenshot(db, id, "txntemplates", True)
    except:
        log.exception("error")


class TxnTemplates(CRUDCollection):

    def __init__(self) -> None:
        self.domain = "txntemplates"
        self.useronly = True
        self.api = True

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        db = req.context["db"]

        req.context["result"] = list(
            json_iter(
                db.execute(
                    "select id, cid, data - 'parts' - 'rawText' from txntemplates where cid = %s",
                    db.get_cid(),
                )
            )
        )

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        CRUDCollection.on_post(self, req, resp)

        run_task(get_txn_screenshot, req.context["result"]["id"])


class TxnTemplate(CRUDSingle):

    def __init__(self) -> None:
        self.domain = "txntemplates"
        self.useronly = True

    def on_patch(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        db = req.context["db"]

        t = db.txntemplates.get(id)

        CRUDSingle.on_patch(self, req, resp, id)

        if (
            t is not None
            and not t.get("example")
            and "parts" in doc
            and len(doc["parts"]) > 0
            and doc["parts"][-1].get("footer", False)
        ):
            mycid = db.get_cid()
            db.set_cid(None)
            doc["parts"][-1].pop("html", None)
            db.companies.patch(mycid, {"lastFooter": doc["parts"][-1]})

        run_task(get_txn_screenshot, id)


class TxnTemplateDuplicate(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        check_noadmin(req)

        db = req.context["db"]

        t = db.txntemplates.get(id)

        if t is None:
            raise falcon.HTTPForbidden()

        t.pop("example", None)

        orig, i = get_orig(t["name"])
        while True:
            t["name"] = "%s (%s)" % (orig, i)
            if db.txntemplates.find_one({"name": t["name"]}) is None:
                break
            i += 1

        req.context["result"] = db.txntemplates.add(t)


def replace_vars(html: str, variables: JsonObj) -> str:
    for var, val in variables.items():
        html = html.replace("{{%s}}" % var, str(val))
    return html


class TxnTemplateTest(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        check_noadmin(req)

        db = req.context["db"]

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )
        if "to" not in doc:
            raise falcon.HTTPBadRequest(
                title="Missing parameter", description="No to address specified."
            )
        if "json" not in doc:
            raise falcon.HTTPBadRequest(
                title="Missing parameter", description="No JSON variables specified."
            )

        jsonvars = None
        if doc["json"]:
            jsonvars = json.loads(doc["json"])
            if not isinstance(jsonvars, dict):
                raise falcon.HTTPBadRequest(
                    title="Invalid JSON", description="JSON input must be an object"
                )

        t = db.txntemplates.get(id)

        if t is None:
            raise falcon.HTTPForbidden()

        db.set_cid(None)

        db.users.patch(
            req.context["uid"],
            {"lasttest": {"to": doc["to"], "route": doc["route"], "json": doc["json"]}},
        )

        company = db.companies.get(t["cid"])
        if company is None:
            raise falcon.HTTPForbidden()

        check_test_limit(db, company, doc["to"].strip().lower())

        availroutes = company["routes"]
        if doc.get("route", ""):
            if doc["route"] not in availroutes:
                raise falcon.HTTPForbidden()
        elif len(availroutes) == 1:
            doc["route"] = availroutes[0]
        else:
            raise falcon.HTTPBadRequest(
                title="Missing parameter",
                description="No route specified and multiple are available.",
            )

        route = db.routes.get(doc["route"])
        if route is None or "published" not in route:
            raise falcon.HTTPForbidden()

        imagebucket = os.environ["s3_imagebucket"]
        parentcompany = db.companies.get(company["cid"])
        if parentcompany is not None:
            imagebucket = parentcompany.get("s3_imagebucket", imagebucket)

        if jsonvars and t.get("type") == "raw":
            try:
                t["rawText"] = Template(t["rawText"]).render(**jsonvars)
            except Exception as e:
                raise falcon.HTTPBadRequest(
                    title="Template error", description="Template error: %s" % e
                )

        html, _ = generate_html(db, t, "test", imagebucket)

        _, addr = email.utils.parseaddr(doc["to"])
        if not addr:
            addr = remove_newlines(doc["to"])

        fromdomain = ""

        fromemail = t.get("fromemail", "")
        returnpath = t.get("returnpath", "")

        if not fromemail:
            fromemail = returnpath
        if not returnpath:
            returnpath = fromemail

        if "@" in returnpath:
            fromdomain = returnpath.split("@")[-1].strip().lower()
        elif "@" in fromemail:
            fromdomain = fromemail.split("@")[-1].strip().lower()
        frm = formataddr((remove_newlines(t["fromname"]), remove_newlines(fromemail)))

        if t.get("replyto", ""):
            replyto = remove_newlines(t["replyto"])
        else:
            replyto = remove_newlines(fromemail)

        subject = remove_newlines(t["subject"])

        if jsonvars:
            subject = replace_vars(subject, jsonvars)
            frm = replace_vars(frm, jsonvars)
            replyto = replace_vars(replyto, jsonvars)

            if t.get("type") != "raw":
                html = replace_vars(html, jsonvars)

        try:
            send_backend_mail(
                db,
                t["cid"],
                route,
                html,
                frm,
                returnpath,
                fromdomain,
                replyto,
                remove_newlines(doc["to"]),
                addr,
                subject,
            )
        except Exception as e:
            traceback.print_exc()
            raise falcon.HTTPBadRequest(
                title="Error sending test", description="Error sending test: %s" % e
            )


class RecentTags(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req)

        db = req.context["db"]

        req.context["result"] = [
            tag
            for tag, in db.execute(
                "select tag from txntags where cid = %s and tag != 'untagged' order by tag limit 500",
                db.get_cid(),
            )
        ]


CHECK_TXNS_LOCK = 38660663
TXN_ATTACHMENT_CLEANUP_LOCK = 38660664


def check_txns() -> None:
    with open_db() as db:
        try:
            with db.transaction():
                if not db.single(
                    f"select pg_try_advisory_xact_lock({CHECK_TXNS_LOCK})"
                ):
                    return
                for company in list(
                    json_iter(
                        db.execute(
                            """
                    select id, cid, data from companies where data @> %s and id in (
                        select distinct cid from txnqueue
                    )
                """,
                            {"admin": False},
                        )
                    )
                ):
                    try:
                        cid = company["id"]

                        domainthrottles = load_domain_throttles(db, company)

                        for cnt, route, domain in list(
                            db.execute(
                                "select count(id), route, domain from txnqueue where cid = %s group by route, domain having count(id) > 0",
                                cid,
                            )
                        ):
                            requesting = min(cnt, 1000)
                            cnt = check_send_limit(
                                company, route, domain, domainthrottles, requesting
                            )
                            if cnt > 0:
                                log.debug(
                                    "%s clear to send %s transactional, route: %s, domain: %s (requested %s)",
                                    cid,
                                    cnt,
                                    route,
                                    domain,
                                    requesting,
                                )
                                for rowid, data in [
                                    (rowid, data)
                                    for rowid, data in db.execute(
                                        "select id, data from txnqueue where cid = %s and route = %s and domain = %s order by id limit %s",
                                        cid,
                                        route,
                                        domain,
                                        cnt,
                                    )
                                ]:
                                    db.execute(
                                        "delete from txnqueue where cid = %s and id = %s",
                                        cid,
                                        rowid,
                                    )
                                    run_task(send_txn, company, data)
                    except:
                        log.exception("error")
        except:
            log.exception("error")


def cleanup_expired_txn_attachments() -> None:
    with open_db() as db:
        try:
            with db.transaction():
                if not db.single(
                    f"select pg_try_advisory_xact_lock({TXN_ATTACHMENT_CLEANUP_LOCK})"
                ):
                    return

                now = datetime.utcnow()
                for rowid, cid, data in list(
                    db.execute(
                        """
                    select id, cid, data from txnqueue
                    where data->'attachments' is not null
                """
                    )
                ):
                    attachments = data.get("attachments") or []
                    if not attachments:
                        continue

                    if not attachment_manifests_expired(attachments, now):
                        continue

                    delete_attachments(
                        build_attachment_storage(get_attachment_config()),
                        attachments,
                    )
                    bodykey = data.get("body")
                    if bodykey:
                        try:
                            s3_delete(os.environ["s3_databucket"], bodykey)
                        except FileNotFoundError:
                            pass
                        except Exception:
                            log.exception(
                                "error deleting expired transactional body %s",
                                bodykey,
                            )
                    db.execute(
                        "delete from txnqueue where cid = %s and id = %s",
                        cid,
                        rowid,
                    )
                    campid = data.get("campid")
                    txnmsgid = None
                    if campid is not None:
                        txnmsgid, _ = parse_txnid(campid)
                    data["event"] = "Error"
                    data["error"] = "Transactional attachments expired before send"
                    db.execute(
                        "insert into txnsends (id, cid, ts, msgid, data) values (%s, %s, %s, %s, %s)",
                        shortuuid.uuid(),
                        cid,
                        datetime.utcnow(),
                        txnmsgid,
                        data,
                    )
                    log.info(
                        "expired transactional attachments for cid=%s queue_id=%s",
                        cid,
                        rowid,
                    )
        except:
            log.exception("error")


class TxnSettings:

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req)

        db = req.context["db"]

        req.context["result"] = db.txnsettings.get_singleton()

    def on_patch(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req)

        db = req.context["db"]

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        db.txnsettings.patch_singleton(doc)
