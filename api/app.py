import os
import falcon
import json
import bcrypt
import email
import re
import shortuuid
import hashlib
import requests
import urllib
from typing import Any, Dict, List, cast
from datetime import datetime, timedelta
import dateutil.parser
from dateutil.tz import tzutc, tzoffset
from jsonschema import validate
import email.utils
from netaddr import IPAddress
from Crypto.Random import random as cryptrandom
import boto3

from .falcon_swagger_ui import register_swaggerui_app  # type: ignore

from .shared import config

from .shared.db import open_db, json_obj, DB, JsonObj
from .shared.utils import (
    user_log,
    redis_connect,
    PERIOD_CREDITS,
    REFILL_CREDITS,
    TRIAL_DAYS,
    run_task,
    open_ticket,
    get_webhost,
    get_webroot,
    find_user,
    fix_empty_limit,
    handle_mg_error,
    handle_sp_error,
)
from .shared.crud import CRUDCollection, CRUDSingle, check_noadmin, compare_patch
from .shared.send import send_rate, sparkpost_domain, mg_domain
from .shared.tasks import tasks, HIGH_PRIORITY
from .shared.s3 import s3_write, s3_size, s3_read, s3_copy, s3_delete, s3_write_stream
from .shared import contacts
from .shared.attachments import validate_attachment_startup
from .shared.log import get_logger, get_root_logger
from .shared.version import VERSION

from . import frontends
from . import backends
from . import lists
from . import campaigns
from . import funnels
from . import events
from . import transactional

import logging

logging.getLogger("boto3").setLevel(logging.INFO)
logging.getLogger("botocore").setLevel(logging.INFO)
logging.getLogger("nose").setLevel(logging.INFO)

log = get_logger()

validate_attachment_startup()

# nothing returned by the web app should be cached by a proxy
class NoCache(object):

    def process_response(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        resource: Any,
        req_succeeded: bool,
    ) -> None:
        resp.cache_control = ["no-store", "no-cache"]


class JSONTranslator(object):

    def process_request(self, req: falcon.Request, resp: falcon.Response) -> None:
        if req.method in ("GET", "HEAD", "DELETE"):
            return

        if req.content_type is None or not req.content_type.startswith(
            "application/json"
        ):
            return

        body = req.bounded_stream.read()
        if not body:
            raise falcon.HTTPBadRequest(
                title="Empty request body",
                description="A valid JSON document is required.",
            )
        req.context["body_raw"] = body
        if body:
            log.debug("request body: %s", body)
        try:
            req.context["doc"] = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            log.info("Invalid body:")
            log.info(repr(body))
            raise falcon.HTTPBadRequest(
                title="Malformed JSON",
                description="Could not decode the request body. The "
                "JSON was incorrect or not encoded as "
                "UTF-8.",
            )

    def process_response(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        resource: Any,
        req_succeeded: bool,
    ) -> None:
        if "result" not in req.context:
            if (
                resp.status.startswith("2")
                and not resp.data
                and not resp.text
                and not resp.stream
                and not resp.content_type
            ):
                # default response type is application/json but some clients dont like an empty
                # body
                resp.data = b"{}"
            return

        resp.data = json.dumps(req.context["result"]).encode("utf-8")


USER_LIMIT = 5000


class RateLimit(object):

    def process_request(self, req: falcon.Request, resp: falcon.Response) -> None:
        if req.path in (
            "/api/mgwebhook",
            "/api/seswebhook",
            "/api/spwebhook",
            "/api/transactional/send",
        ):
            return

        rdb = redis_connect()

        if "db" in req.context:
            uid = req.context["db"].get_cid()
        else:
            if len(req.access_route) > 0:
                uid = req.access_route[0]
            else:
                uid = "unknown"

        key = "rate-%s:%s" % (uid, datetime.now().minute)

        cnt = rdb.get(key)
        if cnt is not None and int(cnt) >= USER_LIMIT:
            raise falcon.HTTPTooManyRequests(
                title="Request limit exceeded",
                description="This client has exceeded the number of allowed requests. Please wait and try again later.",
            )

        rdb.pipeline().incr(key).expire(key, 59).execute()


allowedpaths = [
    re.compile(r"^/api/links/"),
    re.compile(r"^/api/events/"),
    re.compile(r"^/api/stats/"),
    re.compile(r"^/api/queue/"),
    re.compile(r"^/api/limits/"),
    re.compile(r"^/api/sendlogs/"),
    re.compile(r"^/api/testlogs/"),
    re.compile(r"^/api/login$"),
    re.compile(r"^/api/register$"),
    re.compile(r"^/api/resendcode$"),
    re.compile(r"^/api/invite$"),
    re.compile(r"^/api/track$"),
    re.compile(r"^/l$"),
    re.compile(r"^/api/mgwebhook$"),
    re.compile(r"^/api/seswebhook$"),
    re.compile(r"^/api/spwebhook$"),
    re.compile(r"^/api/cbwebhook$"),
    re.compile(r"^/api/sendwebhook$"),
    re.compile(r"^/api/reset/sendemail$"),
    re.compile(r"^/api/reset/passemail$"),
    re.compile(r"^/api/doc"),
    re.compile(r"^/api/healthy$"),
    re.compile(r"^/api/showform/"),
    re.compile(r"^/api/trackform/"),
    re.compile(r"^/api/postform/"),
    re.compile(r"^/api/loginfrontend"),
    re.compile(r"^/signup/"),
    re.compile(r"^/api/signupaction/"),
]


class AuthMiddleware(object):

    def process_request(self, req: falcon.Request, resp: falcon.Response) -> None:
        for allowed in allowedpaths:
            if allowed.search(req.path):
                return
        if req.path == "/api/uploadlogfile" and req.method == "POST":
            return

        uid = req.get_header("X-Auth-UID")
        cookieid = req.get_header("X-Auth-Cookie")
        apikey = req.get_header("X-Auth-APIKey")

        if not (uid and cookieid) and not apikey:
            raise falcon.HTTPUnauthorized(
                title="Invalid login", description="Please log in again"
            )

        db = DB()

        try:
            if apikey:
                user = db.users.find_one({"apikey": apikey})
                if user is None:
                    raise falcon.HTTPUnauthorized(
                        title="Invalid login", description="Please log in again"
                    )

                company = db.companies.get(user["cid"])
                if company is None:
                    raise falcon.HTTPUnauthorized(
                        title="Invalid login", description="Please log in again"
                    )

                uid = user["id"]
                admin = company.get("admin", False)
                if admin:
                    raise falcon.HTTPUnauthorized(
                        title="Invalid login", description="Please log in again"
                    )
                cid = company["id"]
            else:
                cookie = db.cookies.get(cookieid)
                if cookie is None:
                    raise falcon.HTTPUnauthorized(
                        title="Invalid login", description="Please log in again"
                    )

                if cookie["uid"] != uid:
                    raise falcon.HTTPUnauthorized(
                        title="Invalid login", description="Please log in again"
                    )

                if req.method != "GET" or req.path not in (
                    "/api/gallerytemplates",
                    "/api/formtemplates",
                    "/api/broadcasts",
                    "/api/lists",
                    "/api/exports",
                    "/api/segments",
                    "/api/supplists",
                    "/api/users/%s" % (uid,),
                ):
                    db.cookies.patch(
                        cookieid, {"lastused": datetime.utcnow().isoformat() + "Z"}
                    )

                admin = cookie["admin"]
                cid = cookie["cid"]

                impersonateid = req.get_header("X-Auth-Impersonate")

                if impersonateid:
                    if not admin:
                        raise falcon.HTTPUnauthorized(
                            title="Invalid login", description="Please log in again"
                        )

                    customer = db.companies.get(impersonateid)
                    if customer is None or customer["admin"] or customer["cid"] != cid:
                        raise falcon.HTTPUnauthorized(
                            title="Invalid login", description="User not found"
                        )

                    cid = impersonateid

                    admin = False
        except:
            db.close()
            raise

        db.set_cid(cid)
        req.context["db"] = db
        req.context["uid"] = uid
        req.context["admin"] = admin
        req.context["api"] = bool(apikey)

    def process_response(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        resource: Any,
        req_succeeded: bool,
    ) -> None:
        if "db" in req.context:
            req.context["db"].close()
            req.context["db"] = None


class Healthy(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        pass


class Ping(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req, True)

        db = req.context["db"]

        mycid = db.get_cid()

        db.set_cid(None)

        company = db.companies.get(mycid)

        if company is None:
            raise falcon.HTTPForbidden()

        req.context["result"] = [
            {
                "name": company["name"],
            }
        ]

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        self.on_post(req, resp)


def send_signup_email(
    db: DB,
    frontend: JsonObj,
    firstname: str,
    lastname: str,
    username: str,
    subject: str,
    code: str,
) -> None:
    try:
        send_internal_txn(
            db,
            frontend,
            email.utils.formataddr(
                (("%s %s" % (firstname, lastname)).strip(), username)
            ),
            subject,
            """<html><body>
<p>Hello %s!</p><p>This email is in response to a request to join %s. To complete your registration, enter the following code in the <a href="%s/activate?username=%s">activation page</a>:</p>
<p style="text-align: center"><b>%s</b></p>
<p>If you did not request this message, please disregard it.</p>
</body></html>"""
            % (
                firstname,
                get_webhost(),
                get_webroot(),
                urllib.parse.quote(username),
                code,
            ),
        )
    except Exception as e:
        log.exception("Error sending email")
        raise falcon.HTTPBadRequest(
            title="Error sending email", description="Error sending email: %s" % str(e)
        )


class ResendCode(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        with open_db() as db:
            tu = json_obj(
                db.row(
                    "select id, cid, data from tempusers where data->>'username' = %s",
                    doc["username"].strip().lower(),
                )
            )
            if tu is None:
                raise falcon.HTTPBadRequest(
                    title="User not found",
                    description="No registration found for that email",
                )

            frontend = db.frontends.get(tu["frontend"])
            if frontend is None:
                raise falcon.HTTPForbidden()

            send_signup_email(
                db,
                frontend,
                tu["firstname"],
                tu["lastname"],
                tu["username"],
                tu["subject"],
                tu["code"],
            )

            log.info("[NOTIFY] Signup Email Re-Sent:Email: %s", tu["username"])


class Invite(object):

    def on_options(self, req: falcon.Request, resp: falcon.Response) -> None:
        resp.set_header("Access-Control-Allow-Origin", "*")
        resp.set_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        resp.set_header(
            "Access-Control-Allow-Headers",
            req.get_header("Access-Control-Request-Headers") or "*",
        )
        resp.set_header("Access-Control-Max-Age", 86400)

        resp.set_header("Allow", "POST, OPTIONS")
        resp.content_type = "text/plain"

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        resp.set_header("Access-Control-Allow-Origin", "*")
        resp.set_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        resp.set_header(
            "Access-Control-Allow-Headers",
            req.get_header("Access-Control-Request-Headers") or "*",
        )
        resp.set_header("Access-Control-Max-Age", 86400)

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )
        try:
            validate(
                doc,
                {
                    "type": "object",
                    "properties": {
                        "email": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "firstname": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "lastname": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "companyname": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "signup": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "params": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": [
                        "email",
                        "firstname",
                        "lastname",
                        "companyname",
                        "signup",
                        "params",
                    ],
                },
            )
        except Exception as e:
            raise falcon.HTTPBadRequest(
                title="Input validation error", description=str(e)
            )

        with open_db() as db:
            signupsettings = db.signupsettings.get(doc["signup"])
            if signupsettings is None:
                raise falcon.HTTPForbidden()

            frontend = db.frontends.get(signupsettings["frontend"])
            if frontend is None:
                raise falcon.HTTPForbidden()

            doc["email"] = doc["email"].strip().lower()
            doc["firstname"] = doc["firstname"].strip()
            doc["lastname"] = doc["lastname"].strip()
            doc["companyname"] = doc["companyname"].strip()

            u = find_user(db, doc["email"])
            if u is not None:
                raise falcon.HTTPBadRequest(
                    title="User already exists",
                    description="Sorry, a user already exists with this email address.",
                )

            tu = json_obj(
                db.row(
                    "select id, cid, data from tempusers where data->>'username' = %s",
                    doc["email"],
                )
            )
            if tu is not None:
                db.tempusers.remove(tu["id"])

            code = "%06d" % cryptrandom.randint(0, 999999)

            db.set_cid(frontend["cid"])
            db.tempusers.add(
                {
                    "username": doc["email"],
                    "firstname": doc["firstname"],
                    "lastname": doc["lastname"],
                    "companyname": doc["companyname"],
                    "invited_at": datetime.utcnow().isoformat() + "Z",
                    "frontend": frontend["id"],
                    "code": code,
                    "params": doc["params"],
                    "tries": 0,
                    "requireconfirm": signupsettings["requireconfirm"],
                }
            )
            db.set_cid(None)

            if signupsettings["requireconfirm"]:
                send_signup_email(
                    db,
                    frontend,
                    doc["firstname"],
                    doc["lastname"],
                    doc["email"],
                    signupsettings["subject"].strip(),
                    code,
                )

                country, region = "Unknown", "Unknown"
                try:
                    if len(req.access_route) > 0:
                        clientip = req.access_route[0]
                        ipnum = 0
                        try:
                            ipnum = int(IPAddress(clientip).ipv4())
                        except Exception:
                            pass
                        if ipnum != 0:
                            row = db.row(
                                "select country, region from iplocations where iprange @> (%s)::bigint limit 1",
                                ipnum,
                            )
                            if row is not None:
                                country, region = row
                except:
                    log.exception("error")

                log.info(
                    "[NOTIFY] Signup Email Sent:Email: %s, First Name: %s, Last Name: %s, Company: %s, Params: %s, Country: %s, Region: %s",
                    doc["email"],
                    doc["firstname"],
                    doc["lastname"],
                    doc["companyname"],
                    doc["params"],
                    country,
                    region,
                )


class SendResetEmail(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        email = doc["email"].strip().lower()

        with open_db() as db:
            user = find_user(db, email)
            if user is None:
                raise falcon.HTTPBadRequest(
                    title="Invalid email",
                    description="No user found for that email address",
                )

            company = db.companies.get(user["cid"])

            if company is None:
                raise falcon.HTTPBadRequest(
                    title="Invalid company",
                    description="No company found for that email address",
                )

            if user.get("banned") or company.get("banned"):
                raise falcon.HTTPBadRequest(
                    title="Invalid email",
                    description="No user found for that email address",
                )

            if company.get("admin"):
                raise falcon.HTTPBadRequest(
                    title="Can't Reset",
                    description="Please contact support to reset your password",
                )
            else:
                frontend = db.frontends.get(company.get("frontend", ""))
                if frontend is None:
                    raise falcon.HTTPBadRequest(
                        title="Invalid configuration",
                        description="Invalid company configuration",
                    )
                frontendname = frontend["name"]

            tempid = shortuuid.uuid()

            db.users.patch(
                user["id"],
                {"resetid": tempid, "resettime": datetime.utcnow().isoformat() + "Z"},
            )

            try:
                send_internal_txn(
                    db,
                    frontend,
                    email,
                    "Reset password request",
                    """<html><body>
<p>This email is in response to a password reset request for %s.</p>
<p><a href="%s/emailreset?key=%s">Complete your password reset now.</a></p>
<p>If you did not request this message, please disregard it. Thank you.</p>
</body></html>"""
                    % (frontendname, get_webroot(), tempid),
                )
            except Exception as e:
                log.exception("Error sending email")
                raise falcon.HTTPBadRequest(
                    title="Error sending email",
                    description="Error sending email: %s" % str(e),
                )


def send_internal_txn(
    db: DB, frontend: JsonObj, toaddr: str, subj: str, body: str
) -> None:
    acct = frontend.get("txnaccount", "")
    if not acct:
        raise Exception("No API connection configured for internal mail")

    mg = db.mailgun.get(acct)
    if mg is not None:
        data: JsonObj = {
            "from": email.utils.formataddr(
                (frontend["invitename"], frontend["inviteemail"])
            ),
            "to": [toaddr],
            "subject": subj,
            "html": body,
        }

        r = requests.post(
            f'{mg_domain(mg)}/v3/{mg["domain"]}/messages',
            auth=("api", mg["apikey"]),
            data=data,
        )
        handle_mg_error(r)
        return
    s = db.ses.get(acct)
    if s is not None:
        ses = boto3.client(
            "ses",
            region_name=s["region"],
            aws_access_key_id=s["access"],
            aws_secret_access_key=s["secret"],
        )
        ses.send_email(
            Source=email.utils.formataddr(
                (frontend["invitename"], frontend["inviteemail"])
            ),
            Destination={
                "ToAddresses": [toaddr],
            },
            Message={
                "Subject": {
                    "Data": subj,
                },
                "Body": {
                    "Html": {
                        "Data": body,
                    }
                },
            },
        )
        return
    sp = db.sparkpost.get(acct)
    if sp is not None:
        name, addr = email.utils.parseaddr(toaddr)
        data = {
            "content": {
                "from": {
                    "name": frontend["invitename"],
                    "email": frontend["inviteemail"],
                },
                "subject": subj,
                "html": body,
            },
            "recipients": [
                {
                    "address": {"email": addr, "name": name},
                },
            ],
            "return_path": frontend["inviteemail"],
            "options": {
                "click_tracking": False,
                "open_tracking": False,
            },
        }

        r = requests.post(
            f"{sparkpost_domain(sp)}/transmissions",
            headers={"Authorization": sp["apikey"], "Content-Type": "appliation/json"},
            json=data,
        )
        handle_sp_error(r)
        return

    raise Exception(
        "API connection for internal mail not found, it may have been deleted"
    )


user_re = re.compile(r"^[A-Za-z0-9._%+-@]+$")


def copy_example(db: DB, newcompany: JsonObj) -> None:
    if newcompany.get("exampletemplate", False):
        return

    oldcid = db.get_cid()
    try:
        db.set_cid(newcompany["cid"])

        templ = db.companies.find_one({"exampletemplate": True})
        if templ is None:
            return

        oldcid = templ["id"]
        db.set_cid(oldcid)

        camps = [
            c
            for c in db.campaigns.get_all()
            if not c.get("started_at") and not c.get("scheduled_for")
        ]
        lists = db.lists.get_all()
        supplists = db.supplists.get_all()
        segments = db.segments.get_all()
        funnels = db.funnels.get_all()
        messages = db.messages.get_all()
        templates = db.txntemplates.get_all()
        forms = db.forms.get_all()

        listmap = {}
        supplistmap = {}
        segmentmap = {}
        messagemap = {}
        funnelmap = {}

        newcid = newcompany["id"]
        db.set_cid(newcid)
        for l in lists:
            l["example"] = True
            oldid = l["id"]

            newid = db.lists.add(l)
            listmap[oldid] = newid
            for contact_email, added, props, tags in list(
                db.execute(
                    f"""
                            select c.email, c.added, c.props, array_agg(t.value)
                            from contacts."contacts_{oldcid}" c
                            join contacts."contact_lists_{oldcid}" l on c.contact_id = l.contact_id
                            left join contacts."contact_values_{oldcid}" t on t.contact_id = c.contact_id and t.type = 'tag'
                            where l.list_id = %s
                            group by c.email, c.added, c.props""",
                    oldid,
                )
            ):
                contact_id = db.single(
                    f"""insert into contacts."contacts_{newcid}" (email, added, props)
                                            values (%s, %s, %s) on conflict (email) do update set email = excluded.email returning contact_id""",
                    contact_email,
                    added,
                    props,
                )
                db.execute(
                    f"""insert into contacts."contact_lists_{newcid}" (contact_id, list_id) values (%s, %s) on conflict (contact_id, list_id) do nothing""",
                    contact_id,
                    oldid,
                )
                for tag in tags:
                    if tag:
                        db.execute(
                            f"""insert into contacts."contact_values_{newcid}" (contact_id, type, value) values (%s, 'tag', %s) on conflict (contact_id, type, value) do nothing""",
                            contact_id,
                            tag,
                        )

            db.execute(
                "insert into list_domains (list_id, domain, count) select %s, domain, count from list_domains where list_id = %s",
                newid,
                oldid,
            )
        for s in supplists:
            oldid = s["id"]
            newid = db.supplists.add(s)
            supplistmap[oldid] = newid

            for contact_email, added, props in list(
                db.execute(
                    f"""
                            select c.email, c.added, c.props
                            from contacts."contacts_{oldcid}" c
                            join contacts."contact_supplists_{oldcid}" l on c.contact_id = l.contact_id
                            where l.supplist_id = %s""",
                    oldid,
                )
            ):
                contact_id = db.single(
                    f"""insert into contacts."contacts_{newcid}" (email, added, props)
                                                values (%s, %s, %s) on conflict (email) do update set email = excluded.email returning contact_id""",
                    contact_email,
                    added,
                    props,
                )
                db.execute(
                    f"""insert into contacts."contact_supplists_{newcid}" (contact_id, supplist_id) values (%s, %s) on conflict (contact_id, supplist_id) do nothing""",
                    contact_id,
                    oldid,
                )
        for s in segments:

            def change_lists(part: JsonObj, lm: Dict[str, str]) -> None:
                if part["type"] == "Lists" and part["operator"] in ("in", "notin"):
                    if part["list"] in lm:
                        part["list"] = lm[part["list"]]
                elif part["type"] == "Group":
                    for p in part["parts"]:
                        change_lists(p, lm)

            for p in s["parts"]:
                change_lists(p, listmap)
            oldid = s["id"]
            segmentmap[oldid] = db.segments.add(s)
        for m in messages:
            oldid = m["id"]
            m["example"] = True
            m["suppsegs"] = [segmentmap.get(s, s) for s in m["suppsegs"]]
            m["supplists"] = [supplistmap.get(s, s) for s in m["supplists"]]
            newid = db.messages.add(m)
            messagemap[oldid] = newid

            db.execute(
                "insert into message_browsers (message_id, os, browser, count) select %s, os, browser, count from message_browsers where message_id = %s",
                newid,
                oldid,
            )
            db.execute(
                "insert into message_devices (message_id, device, count) select %s, device, count from message_devices where message_id = %s",
                newid,
                oldid,
            )
            db.execute(
                "insert into message_domains (message_id, domain, count) select %s, domain, count from message_domains where message_id = %s",
                newid,
                oldid,
            )
            db.execute(
                "insert into message_locations (message_id, country_code, country, region, count) select %s, country_code, country, region, count from message_locations where message_id = %s",
                newid,
                oldid,
            )
        for f in funnels:
            for m in f["messages"]:
                if m["id"] in messagemap:
                    m["id"] = messagemap[m["id"]]
            oldid = f["id"]
            funnelmap[oldid] = db.funnels.add(f)
        for m in db.messages.get_all():
            if m["funnel"] in funnelmap:
                db.messages.patch(m["id"], {"funnel": funnelmap[m["funnel"]]})
        for c in camps:
            oldid = c["id"]
            c["example"] = True
            c["segments"] = [segmentmap.get(s, s) for s in c["segments"]]
            c["lists"] = [listmap.get(l, l) for l in c["lists"]]
            c["suppsegs"] = [segmentmap.get(s, s) for s in c["suppsegs"]]
            c["supplists"] = [supplistmap.get(s, s) for s in c["supplists"]]
            newid = db.campaigns.add(c)

            db.execute(
                "insert into campaign_browsers (campaign_id, os, browser, count) select %s, os, browser, count from campaign_browsers where campaign_id = %s",
                newid,
                oldid,
            )
            db.execute(
                "insert into campaign_devices (campaign_id, device, count) select %s, device, count from campaign_devices where campaign_id = %s",
                newid,
                oldid,
            )
            db.execute(
                "insert into campaign_domains (campaign_id, domain, count) select %s, domain, count from campaign_domains where campaign_id = %s",
                newid,
                oldid,
            )
            db.execute(
                "insert into campaign_locations (campaign_id, country_code, country, region, count) select %s, country_code, country, region, count from campaign_locations where campaign_id = %s",
                newid,
                oldid,
            )
        for t in templates:
            t["example"] = True
            db.txntemplates.add(t)
        for f in forms:
            f["example"] = True
            if f.get("list"):
                f["list"] = listmap.get(f["list"], f["list"])
            if f.get("funnel"):
                f["funnel"] = listmap.get(f["funnel"], f["funnel"])
            db.forms.add(f)

    finally:
        db.set_cid(oldcid)


class Register(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if "doc" not in req.context:
            raise falcon.HTTPUnauthorized(
                title="Info required", description="Registration information missing"
            )

        doc = req.context["doc"]

        username = doc.get("username", None)
        code = doc.get("code", "").strip()

        if not username:
            raise falcon.HTTPUnauthorized(
                title="Info required", description="Registration information missing"
            )

        username = username.strip().lower()

        if not user_re.search(username):
            raise falcon.HTTPUnauthorized(
                title="Invalid login", description="Invalid username"
            )

        with open_db() as db:
            u = find_user(db, username)
            if u is None:
                u = json_obj(
                    db.row(
                        "select id, cid, data from tempusers where data->>'username' = %s",
                        username,
                    )
                )
                if u is not None:
                    if u["requireconfirm"]:
                        if not code:
                            raise falcon.HTTPUnauthorized(
                                title="Invalid registration",
                                description="Missing activation code",
                            )
                        if u["code"] != code:
                            if u["tries"] > 9:
                                raise falcon.HTTPUnauthorized(
                                    title="Invalid registration",
                                    description="Too many invalid activation codes, registration denied",
                                )
                            db.execute(
                                "update tempusers set data = data || jsonb_build_object('tries', (data->>'tries')::integer + 1) where id = %s",
                                u["id"],
                            )
                            raise falcon.HTTPUnauthorized(
                                title="Invalid registration",
                                description="Invalid activation code",
                            )

                    db.tempusers.remove(u["id"])

                    frontend = db.frontends.get(u["frontend"])
                    if frontend is not None:
                        offset = doc.get("offset", 0)

                        fullname = ("%s %s" % (u["firstname"], u["lastname"])).strip()

                        n = datetime.utcnow().isoformat() + "Z"

                        inreview = False
                        trialend = None
                        approvedat = None
                        if frontend.get("useapprove", False):
                            inreview = True
                        else:
                            if frontend.get("usetrial", False):
                                approvedat = n
                                trialend = (
                                    datetime.utcnow()
                                    + timedelta(
                                        days=frontend.get("trialdays", TRIAL_DAYS)
                                    )
                                ).isoformat() + "Z"

                        db.set_cid(u["cid"])
                        c = db.companies.add(
                            {
                                "name": u["companyname"],
                                "admin": False,
                                "routes": [
                                    id
                                    for id, in db.execute(
                                        "select id from routes where cid = %s and (data->'published'->>'usedefault')::boolean",
                                        u["cid"],
                                    )
                                ],
                                "frontend": u["frontend"],
                                "monthlimit": frontend.get("monthlimit"),
                                "daylimit": frontend.get("daylimit"),
                                "hourlimit": frontend.get("hourlimit"),
                                "minlimit": frontend.get("minlimit"),
                                "defaultmonthlimit": frontend.get("monthlimit"),
                                "defaultdaylimit": frontend.get("daylimit"),
                                "defaulthourlimit": frontend.get("hourlimit"),
                                "defaultminlimit": frontend.get("minlimit"),
                                "tzoffset": offset,
                                "created": n,
                                "inreview": inreview,
                                "trialend": trialend,
                                "approved_at": approvedat,
                                "params": u["params"],
                            }
                        )
                        with db.transaction():
                            contacts.initialize_cid(db, c)
                        copyto = db.companies.get(c)
                        assert copyto is not None
                        copy_example(db, copyto)
                        db.set_cid(c)
                        uid = db.users.add(
                            {
                                "hash": bcrypt.hashpw(
                                    u["id"].encode("utf-8"), bcrypt.gensalt()
                                ).decode("ascii"),
                                "apikey": shortuuid.uuid(),
                                "newuser": True,
                                "admin": False,
                                "fullname": fullname,
                                "firstname": u["firstname"],
                                "lastname": u["lastname"],
                                "username": username,
                                "companyname": u["companyname"],
                            }
                        )
                        db.set_cid(None)

                        u = db.users.get(uid)

                        assert u is not None

                        log.info(
                            "[NOTIFY] A New User Signed Up:Email: %s, Company: %s",
                            username,
                            u["companyname"],
                        )

                        db.set_cid(u["cid"])

                        req.context["result"] = {
                            "cookie": db.cookies.add(
                                {
                                    "lastused": datetime.utcnow().isoformat() + "Z",
                                    "uid": u["id"],
                                    "admin": False,
                                }
                            ),
                            "uid": u["id"],
                            "changepass": True,
                            "admin": False,
                        }
                    else:
                        raise falcon.HTTPUnauthorized(
                            title="Invalid signup",
                            description="This registration is invalid, sorry",
                        )
                else:
                    raise falcon.HTTPUnauthorized(
                        title="Invalid signup",
                        description="No registration found for this email address",
                    )
            else:
                raise falcon.HTTPUnauthorized(
                    title="Invalid registration",
                    description="This registration is no longer valid, sorry",
                )


class Login(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if "doc" not in req.context:
            raise falcon.HTTPUnauthorized(
                title="Login required", description="Login information missing"
            )

        doc = req.context["doc"]

        username = doc.get("username", None)
        password = doc.get("password", "").encode("utf-8")

        if not username or not password:
            raise falcon.HTTPUnauthorized(
                title="Login required", description="Login information missing"
            )

        username = username.strip()

        if not user_re.search(username):
            bcrypt.hashpw(password, bcrypt.gensalt())
            raise falcon.HTTPUnauthorized(
                title="Invalid login", description="Invalid username/password"
            )

        with open_db() as db:
            u = find_user(db, username)
            if u is None or u.get("disabled", False) or u.get("banned"):
                h = bcrypt.hashpw(password, bcrypt.gensalt())
                raise falcon.HTTPUnauthorized(
                    title="Invalid login", description="Invalid username/password"
                )
            else:
                h = u.get("hash", "").encode("utf-8")
                if bcrypt.hashpw(password, h) != h:
                    raise falcon.HTTPUnauthorized(
                        title="Invalid login", description="Invalid username/password"
                    )

            db.set_cid(u["cid"])

            req.context["result"] = {
                "cookie": db.cookies.add(
                    {
                        "lastused": datetime.utcnow().isoformat() + "Z",
                        "uid": u["id"],
                        "admin": u["admin"],
                    }
                ),
                "uid": u["id"],
                "changepass": False,
                "admin": u["admin"],
            }


class Logout(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        db = req.context["db"]

        db.set_cid(None)

        db.cookies.delete({"uid": req.context["uid"]})


class APIKeyReset(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req)

        db = req.context["db"]

        db.set_cid(None)

        db.users.patch(req.context["uid"], {"apikey": shortuuid.uuid()})


class PasswordEmailReset(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        with open_db() as db:
            doc = req.context.get("doc")
            if not doc:
                raise falcon.HTTPBadRequest(
                    title="Not JSON", description="A valid JSON document is required."
                )
            if not doc.get("pass", None):
                raise falcon.HTTPBadRequest(
                    title="Parameter missing", description="Invalid request."
                )
            if not doc.get("key", None):
                raise falcon.HTTPBadRequest(
                    title="Parameter missing", description="Invalid request."
                )

            user = db.users.find_one({"resetid": doc["key"]})
            if user is None:
                raise falcon.HTTPBadRequest(
                    title="Invalid key",
                    description="Invalid password reset key, please request another",
                )

            resettime = (
                dateutil.parser.parse(user["resettime"])
                .astimezone(tzutc())
                .replace(tzinfo=None)
            )

            if datetime.utcnow() - resettime > timedelta(hours=4):
                raise falcon.HTTPBadRequest(
                    title="Expired",
                    description="Password reset link has expired, please request another",
                )

            db.users.patch(
                user["id"],
                {
                    "newuser": False,
                    "hash": bcrypt.hashpw(
                        doc["pass"].encode("utf-8"), bcrypt.gensalt()
                    ).decode("ascii"),
                },
            )


class RemoveLimitAlert(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if req.context["api"]:
            raise falcon.HTTPBadRequest(title="Invalid method")

        db = req.context["db"]

        db.set_cid(None)

        db.users.patch(req.context["uid"], {"nolimitalert": True})


class RemoveProbationAlert(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if req.context["api"]:
            raise falcon.HTTPBadRequest(title="Invalid method")

        db = req.context["db"]

        db.set_cid(None)

        db.users.patch(req.context["uid"], {"noprobationalert": True})


class RemoveMobileAlert(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if req.context["api"]:
            raise falcon.HTTPBadRequest(title="Invalid method")

        db = req.context["db"]

        db.set_cid(None)

        db.users.patch(req.context["uid"], {"nomobilealert": True})


class PasswordReset(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if req.context["api"]:
            raise falcon.HTTPBadRequest(title="Invalid method")

        db = req.context["db"]

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )
        if not doc.get("pass", None):
            raise falcon.HTTPBadRequest(
                title="Parameter missing", description="Invalid request."
            )

        db.set_cid(None)

        user = db.users.get(req.context["uid"])
        if user is None:
            raise falcon.HTTPForbidden()

        db.users.patch(
            req.context["uid"],
            {
                "newuser": False,
                "hash": bcrypt.hashpw(
                    doc["pass"].encode("utf-8"), bcrypt.gensalt()
                ).decode("ascii"),
            },
        )


class UserLogs(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        req.context["result"] = db.userlogs.get_all()


COMPANY_SCHEMA = {
    "type": "object",
    "required": ["name", "routes", "frontend"],
    "properties": {
        "name": {
            "type": "string",
            "maxLength": 1024,
            "minLength": 1,
        },
        "routes": {
            "type": "array",
            "items": {
                "$ref": "#/definitions/id",
            },
        },
        "frontend": {
            "$ref": "#/definitions/id",
        },
        "apikey": {},
        "admin": {},
    },
    "additionalProperties": False,
    "definitions": {
        "id": {
            "type": "string",
            "pattern": "^[0-9a-zA-Z]{1,22}$",
        }
    },
}


class PollRestHook(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response, event: str) -> None:
        check_noadmin(req, True)

        rdb = redis_connect()

        db = req.context["db"]

        ev = rdb.get("lastevent-%s-%s" % (db.get_cid(), event))

        if ev is None:
            req.context["result"] = []
        else:
            req.context["result"] = [json.loads(ev)]


RESTHOOK_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
        },
        "target_url": {
            "type": "string",
        },
        "event": {
            "type": "string",
        },
    },
    "required": ["target_url", "event"],
}


class RestHook(object):

    def on_delete(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        check_noadmin(req, True)

        db = req.context["db"]

        db.resthooks.remove(id)
        resp.status = falcon.HTTP_200

    def on_get(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        check_noadmin(req, True)

        db = req.context["db"]
        res = db.resthooks.get(id)
        if not res:
            raise falcon.HTTPForbidden()

        req.context["result"] = res

    def on_patch(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        check_noadmin(req, True)

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        try:
            validate(doc, RESTHOOK_SCHEMA)
        except Exception as e:
            raise falcon.HTTPBadRequest(
                title="Input validation error", description=str(e)
            )

        db = req.context["db"]

        old = db.resthooks.find_one(
            {"target_url": doc["target_url"], "event": doc["event"]}
        )
        if old is not None and old["id"] != id:
            raise falcon.HTTPConflict(
                title="Duplicate",
                description="A webhook already exists with this target URL and event",
            )

        doc["updated"] = datetime.utcnow().isoformat() + "Z"

        db.resthooks.patch(id, doc)

        req.context["result"] = doc


class RestHookTest(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req, True)

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        url = doc.get("webhook", {}).get("target_url", "")

        if not url:
            req.context["result"] = {
                "response_text": "No webhook URL specified",
                "error": True,
            }
            return

        payload = doc.get("payload", {})

        try:
            with requests.Session() as session:
                session.max_redirects = 2
                r = session.post(
                    url,
                    json=payload,
                    headers={"User-Agent": f"{get_webhost()} webhook"},
                    timeout=10,
                )

                text = f"Response received:\n\nHTTP {r.status_code} {r.reason}"

                if r.text:
                    body = r.text
                    if len(body) > 203:
                        body = body[:200] + "..."
                    text += f"\n\n{body}"

                req.context["result"] = {
                    "response_text": text,
                    "error": r.status_code < 200 or r.status_code >= 300,
                }
            return
        except requests.exceptions.ConnectionError as ece:
            msg = str(ece)
            if msg.split(":")[-1].strip(" \t\n'\",)"):
                msg = msg.split(":")[-1].strip(" \t\n'\",)")
            err = f"could not connect to target host ({msg})"
        except requests.exceptions.Timeout:
            err = "request timed out"
        except requests.exceptions.HTTPError as e:
            msg = str(e)
            if msg.split(":")[-1].strip(" \t\n'\",)"):
                msg = msg.split(":")[-1].strip(" \t\n'\",)")
            err = f"invalid HTTP response: {msg}"
        except requests.exceptions.TooManyRedirects:
            err = "HTTP request returned too many redirects"
        except Exception as e:
            err = str(e)

        req.context["result"] = {"response_text": f"Error: {err}", "error": True}


class RestHooks(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req, True)

        db = req.context["db"]

        req.context["result"] = db.resthooks.get_all()

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req, True)

        db = req.context["db"]

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        try:
            validate(doc, RESTHOOK_SCHEMA)
        except Exception as e:
            raise falcon.HTTPBadRequest(
                title="Input validation error", description=str(e)
            )

        exist = db.resthooks.find_one(
            {"target_url": doc["target_url"], "event": doc["event"]}
        )
        if exist is not None:
            raise falcon.HTTPConflict(
                title="Duplicate",
                description="A webhook already exists with this target URL and event",
            )

        now = datetime.utcnow().isoformat() + "Z"
        doc["created"] = now
        doc["updated"] = now

        newid = db.resthooks.add(doc)

        req.context["result"] = {"id": newid}
        resp.status = falcon.HTTP_201


class CompanyLimits(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        req.context["result"] = db.companylimits.get_singleton()

    def on_patch(self, req: falcon.Request, resp: falcon.Response) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        db.companylimits.patch_singleton(doc)


class LimitCompanies(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        try:
            validate(
                doc,
                {
                    "type": "object",
                    "properties": {
                        "ids": {
                            "type": "array",
                            "items": {
                                "type": "string",
                            },
                        },
                        "minlimit": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "hourlimit": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "daylimit": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "monthlimit": {
                            "type": "integer",
                            "minimum": 0,
                        },
                        "setdefault": {
                            "type": "boolean",
                        },
                    },
                    "additionalProperties": False,
                    "required": ["ids"],
                },
            )
        except Exception as e:
            raise falcon.HTTPBadRequest(
                title="Input validation error", description=str(e)
            )

        if "setdefault" in doc and doc["setdefault"]:
            db.execute(
                """update companies set data = data || jsonb_build_object('minlimit', data->'defaultminlimit', 'hourlimit', data->'defaulthourlimit', 'daylimit', data->'defaultdaylimit', 'monthlimit', data->'defaultmonthlimit') where id = any(%s)""",
                doc["ids"],
            )
        else:
            if "hourlimit" in doc:
                db.execute(
                    """update companies set data = data || jsonb_build_object('hourlimit', %s) where id = any(%s)""",
                    doc["hourlimit"],
                    doc["ids"],
                )
            if "daylimit" in doc:
                db.execute(
                    """update companies set data = data || jsonb_build_object('daylimit', %s) where id = any(%s)""",
                    doc["daylimit"],
                    doc["ids"],
                )
            if "monthlimit" in doc:
                db.execute(
                    """update companies set data = data || jsonb_build_object('monthlimit', %s) where id = any(%s)""",
                    doc["monthlimit"],
                    doc["ids"],
                )
        for cid in doc["ids"]:
            user_log(
                req, "pencil", "set send limits for customer ", "companies", cid, "."
            )


class PurgeQueues(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response, t: str) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        try:
            validate(
                doc,
                {
                    "type": "array",
                    "items": {
                        "type": "string",
                    },
                },
            )
        except Exception as e:
            raise falcon.HTTPBadRequest(
                title="Input validation error", description=str(e)
            )

        if t == "f":
            db.execute("""delete from funnelqueue where cid = any(%s)""", doc)
            for cid in doc:
                user_log(
                    req, "pencil", "purged funnels for customer ", "companies", cid, "."
                )
        elif t == "t":
            db.execute("""delete from txnqueue where cid = any(%s)""", doc)
            for cid in doc:
                user_log(
                    req,
                    "pencil",
                    "purged transactional for customer ",
                    "companies",
                    cid,
                    ".",
                )
        elif t == "c":
            db.execute("""delete from campqueue where cid = any(%s)""", doc)
            for cid in doc:
                user_log(
                    req,
                    "pencil",
                    "purged broadcasts for customer ",
                    "companies",
                    cid,
                    ".",
                )


class PauseCompanies(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        try:
            validate(
                doc,
                {
                    "type": "array",
                    "items": {
                        "type": "string",
                    },
                },
            )
        except Exception as e:
            raise falcon.HTTPBadRequest(
                title="Input validation error", description=str(e)
            )

        db.execute(
            """update companies set data = data || '{"paused": true}' where id = any(%s)""",
            doc,
        )

        for cid in doc:
            user_log(req, "pencil", "paused customer ", "companies", cid, ".")


class UnpauseCompanies(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        try:
            validate(
                doc,
                {
                    "type": "array",
                    "items": {
                        "type": "string",
                    },
                },
            )
        except Exception as e:
            raise falcon.HTTPBadRequest(
                title="Input validation error", description=str(e)
            )

        db.execute(
            """update companies set data = data || '{"paused": false}' where id = any(%s)""",
            doc,
        )

        for cid in doc:
            user_log(req, "pencil", "unpaused customer ", "companies", cid, ".")


class ApproveCompanies(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        for cid in doc["ids"]:
            company = db.companies.get(cid)
            if company is None:
                raise falcon.HTTPForbidden()
            frontend = db.frontends.get(company["frontend"])
            if frontend is None:
                raise falcon.HTTPForbidden()

            trialend = None
            if frontend.get("usetrial", False):
                trialend = (
                    datetime.utcnow()
                    + timedelta(days=frontend.get("trialdays", TRIAL_DAYS))
                ).isoformat() + "Z"

            db.companies.patch(
                cid,
                {
                    "inreview": False,
                    "approved_at": datetime.utcnow().isoformat() + "Z",
                    "trialend": trialend,
                },
            )

            if company.get("moderation_ticket"):
                run_task(close_ticket, company["moderation_ticket"], doc["comment"])

            user_log(req, "pencil", "approved customer ", "companies", cid, ".")


class BanCompanies(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        try:
            validate(
                doc,
                {
                    "type": "array",
                    "items": {
                        "type": "string",
                    },
                },
            )
        except Exception as e:
            raise falcon.HTTPBadRequest(
                title="Input validation error", description=str(e)
            )

        db.execute(
            """update companies set data = data || '{"banned": true}' where id = any(%s)""",
            doc,
        )
        db.execute(
            """update users set data = data || '{"banned": true}' where cid = any(%s)""",
            doc,
        )
        db.execute("""delete from cookies where cid = any(%s)""", doc)
        db.execute("""delete from funnelqueue where cid = any(%s)""", doc)
        db.execute("""delete from txnqueue where cid = any(%s)""", doc)
        db.execute("""delete from campqueue where cid = any(%s)""", doc)

        for cid in doc:
            user_log(req, "pencil", "banned customer ", "companies", cid, ".")


class UnbanCompanies(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        try:
            validate(
                doc,
                {
                    "type": "array",
                    "items": {
                        "type": "string",
                    },
                },
            )
        except Exception as e:
            raise falcon.HTTPBadRequest(
                title="Input validation error", description=str(e)
            )

        db.execute(
            """update companies set data = data - 'banned' where id = any(%s)""", doc
        )
        db.execute(
            """update users set data = data - 'banned' where cid = any(%s)""", doc
        )

        for cid in doc:
            user_log(req, "pencil", "unbanned customer ", "companies", cid, ".")


class Companies(CRUDCollection):

    def __init__(self) -> None:
        self.domain = "companies"
        self.adminonly = True
        self.userlog = "customer"
        self.hide = "apikey"
        # self.schema = COMPANY_SCHEMA

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        ret = db.companies.get_all()

        if req.get_param("start"):
            rdict = {}
            for c in ret:
                rdict[c["id"]] = c
                c["contacts"] = 0
                c["complaint"] = 0
                c["open"] = 0
                c["send"] = 0
                c["hard"] = 0
                c["cqueue"] = 0
                c["fqueue"] = 0
                c["tqueue"] = 0
                c["uid"] = None
                c["email"] = "None"
                c["lasttime"] = None

            mycid = db.get_cid()

            start = req.get_param("start", required=True)
            end = req.get_param("end", required=True)

            try:
                start = (
                    dateutil.parser.parse(start)
                    .astimezone(tzutc())
                    .replace(tzinfo=None)
                )
                end = (
                    dateutil.parser.parse(end).astimezone(tzutc()).replace(tzinfo=None)
                )
            except:
                raise falcon.HTTPBadRequest(
                    title="Invalid parameter", description="invalid date parameter"
                )

            allids = [c["id"] for c in ret]

            for cid, cnt in db.execute(
                """select cid, sum((data->>'count')::bigint)
                                          from lists where data->>'count' ~ '^[0-9]+$'
                                          and cid = any(%s) group by cid""",
                allids,
            ):
                if cid not in rdict:
                    continue
                c = rdict[cid]
                c["contacts"] = cnt

            for cid, uid, user_email in db.execute(
                """select users.cid, users.id, users.data->>'username' from
                                            users inner join
                                            (select id, min(users.data->>'created') from users
                                             where users.cid = any(%s) group by id) minusers on minusers.id = users.id""",
                allids,
            ):
                if cid not in rdict:
                    continue
                c = rdict[cid]
                c["uid"] = uid
                c["email"] = user_email

            for cid, lasttime in db.execute(
                """select cookies.cid, cookies.data->>'lastused' from
                                               cookies inner join
                                               (select id, max(cookies.data->>'lastused') from cookies
                                                where cookies.data->>'uid' = any(%s) group by id) maxcookies on maxcookies.id = cookies.id""",
                [c["uid"] for c in ret if c["uid"]],
            ):
                if cid not in rdict:
                    continue
                c = rdict[cid]
                c["lasttime"] = lasttime

            for cid, cnt in db.execute(
                """select cid, sum(remaining) from campqueue where cid = any(%s) group by cid""",
                allids,
            ):
                if cid not in rdict:
                    continue
                c = rdict[cid]
                c["cqueue"] = cnt
            for cid, cnt in db.execute(
                """select cid, count(id) from funnelqueue where cid = any(%s) and not sent and ts < %s group by cid""",
                allids,
                datetime.utcnow(),
            ):
                if cid not in rdict:
                    continue
                c = rdict[cid]
                c["fqueue"] = cnt
            for cid, cnt in db.execute(
                """select cid, count(id) from txnqueue where cid = any(%s) group by cid""",
                allids,
            ):
                if cid not in rdict:
                    continue
                c = rdict[cid]
                c["tqueue"] = cnt

            for cid, complaint, opens, send, hard, soft in db.execute(
                """select campcid, sum(complaint) as complaint, sum(open) as open, sum(send) as send, sum(hard) as hard, sum(soft) as soft
                   from hourstats
                   where hourstats.cid = %s
                   and send+soft+hard+defercnt > 0
                   and ts >= %s and ts <= %s
                   group by campcid""",
                mycid,
                start,
                end,
            ):
                if cid not in rdict:
                    continue

                c = rdict[cid]

                if send > 0:
                    c["complaint"] = float(complaint) / float(send)
                    c["open"] = float(opens) / float(send)
                if float(hard + soft + send) > 0:
                    c["hard"] = float(hard) / float(hard + soft + send)
                c["send"] = send

        req.context["result"] = ret

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        db = req.context["db"]

        frontend = db.frontends.get(doc["frontend"])
        if frontend is None:
            raise falcon.HTTPForbidden()

        doc["admin"] = False
        doc["created"] = datetime.utcnow().isoformat() + "Z"
        if frontend.get("useapprove", False):
            doc["inreview"] = True
        else:
            if frontend.get("usetrial", False):
                doc["approved_at"] = doc["created"]
                doc["trialend"] = (
                    datetime.utcnow()
                    + timedelta(days=frontend.get("trialdays", TRIAL_DAYS))
                ).isoformat() + "Z"

        if doc.get("exampletemplate", False):
            db.execute(
                """update companies set data = data || '{"exampletemplate": false}' where cid = %s""",
                db.get_cid(),
            )

        CRUDCollection.on_post(self, req, resp)
        with db.transaction():
            contacts.initialize_cid(db, req.context["result"]["id"])
        copy_example(db, req.context["result"])


class Company(CRUDSingle):

    def __init__(self) -> None:
        self.domain = "companies"
        self.adminonly = True
        self.userlog = "customer"
        self.hide = "apikey"
        # self.schema = patch_schema(COMPANY_SCHEMA)

    def on_patch(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        db = req.context["db"]
        if doc.get("exampletemplate", False):
            db.execute(
                """update companies set data = data || '{"exampletemplate": false}' where cid = %s""",
                db.get_cid(),
            )

        CRUDSingle.on_patch(self, req, resp, id)

        if "name" in doc:
            db.execute(
                """update users set data = data || jsonb_build_object('companyname', %s) where cid = %s""",
                doc["name"],
                id,
            )

    def on_delete(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        CRUDSingle.on_delete(self, req, resp, id)

        db = req.context["db"]

        db.execute(
            """update users set data = data || jsonb_build_object('username', data->>'username' || 'xxxx') where cid = %s""",
            id,
        )


class CompanyCampaign(object):

    def on_get(
        self, req: falcon.Request, resp: falcon.Response, id: str, campid: str
    ) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        company = db.companies.get(id)
        if company is None:
            raise falcon.HTTPForbidden()

        c = json_obj(
            db.row(
                "select id, cid, data - 'parts' - 'rawText' from campaigns where cid = %s and id = %s",
                id,
                campid,
            )
        )
        if c is None:
            c = json_obj(
                db.row(
                    "select id, cid, data - 'parts' - 'rawText' from messages where cid = %s and id = %s",
                    id,
                    campid,
                )
            )
            if c is None:
                raise falcon.HTTPForbidden()

        req.context["result"] = c


class ListDomainStats(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        check_noadmin(req, True)

        db = req.context["db"]

        req.context["result"] = [
            {
                "domain": row[0],
                "count": row[1],
            }
            for row in db.execute(
                """select domain, count
               from lists
               inner join list_domains on list_domains.list_id = lists.id
               where lists.id = %s and lists.cid = %s""",
                id,
                db.get_cid(),
            )
        ]


class MessageMessages(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        check_noadmin(req, True)

        db = req.context["db"]

        cid = db.get_cid()
        db.set_cid(None)

        req.context["result"] = [
            {
                "msg": row[0],
                "count": row[1],
            }
            for row in db.execute(
                """select message, sum(count) cnt from statmsgs
               inner join messages on statmsgs.campid = messages.id
               where messages.id = %s and messages.cid = %s
               and domaingroupid = %s and msgtype = %s
               group by message""",
                id,
                cid,
                req.get_param("domain", required=True),
                req.get_param("type", required=True),
            )
        ]


class CampaignMessages(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        check_noadmin(req, True)

        db = req.context["db"]

        cid = db.get_cid()
        db.set_cid(None)

        req.context["result"] = [
            {
                "msg": row[0],
                "count": row[1],
            }
            for row in db.execute(
                """select message, sum(count) cnt from statmsgs
               inner join campaigns on statmsgs.campid = campaigns.id
               where campaigns.id = %s and campaigns.cid = %s
               and domaingroupid = %s and msgtype = %s
               group by message""",
                id,
                cid,
                req.get_param("domain", required=True),
                req.get_param("type", required=True),
            )
        ]


class MessageDomainStats(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        check_noadmin(req, True)

        db = req.context["db"]

        cid = db.get_cid()
        db.set_cid(None)

        req.context["result"] = [
            {
                "domain": row[0],
                "send": row[1],
                "open": row[2],
                "complaint": row[3],
                "soft": row[4],
                "hard": row[5],
                "click": row[6],
                "unsub": row[7],
                "count": row[8],
            }
            for row in db.execute(
                """select domaingroupid, sum(send), sum(open),
                        sum(complaint), sum(soft), sum(hard), sum(click), sum(unsub), message_domains.count, campid
                from messages
                inner join hourstats on messages.id = hourstats.campid
                inner join message_domains on message_domains.message_id = hourstats.campid and message_domains.domain = hourstats.domaingroupid
                where hourstats.campid = %s and hourstats.campcid = %s
                group by campid, domaingroupid, message_domains.count""",
                id,
                cid,
            )
        ]


class CampaignDomainStats(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        check_noadmin(req, True)

        db = req.context["db"]

        cid = db.get_cid()
        db.set_cid(None)

        company = db.companies.get(cid)
        frontend = json_obj(
            db.row(
                "select id, cid, data - 'image' from frontends where id = %s",
                company["frontend"],
            )
        )

        domainrates = {}
        for dr in frontend["domainrates"]:
            if dr["domain"].strip():
                domainrates[dr["domain"].strip().lower()] = dr

        req.context["result"] = [
            {
                "domain": row[0],
                "send": row[1],
                "open": row[2],
                "complaint": row[3],
                "soft": row[4],
                "hard": row[5],
                "click": row[6],
                "unsub": row[7],
                "count": row[8],
                "overdomainbounce": False,
                "overdomaincomplaint": False,
            }
            for row in db.execute(
                """select domaingroupid, sum(send), sum(open),
                        sum(complaint), sum(soft), sum(hard), sum(click), sum(unsub), campaign_domains.count, campid
                from campaigns
                inner join hourstats on campaigns.id = hourstats.campid
                inner join campaign_domains on campaign_domains.campaign_id = hourstats.campid and campaign_domains.domain = hourstats.domaingroupid
                where hourstats.campid = %s and hourstats.campcid = %s
                group by campid, domaingroupid, campaign_domains.count""",
                id,
                cid,
            )
        ]

        for s in req.context["result"]:
            dr = domainrates.get(s["domain"])
            if dr is not None and s["send"] > 0:
                s["overdomainbounce"] = (
                    float(s["soft"]) / float(s["send"])
                ) * 100 > dr["bouncerate"] or (
                    float(s["hard"]) / float(s["send"])
                ) * 100 > dr[
                    "bouncerate"
                ]
                s["overdomaincomplaint"] = (
                    float(s["complaint"]) / float(s["send"])
                ) * 100 > dr["complaintrate"]


class CustomerDashboard(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req)

        db = req.context["db"]

        end = req.get_param("end", required=True)

        try:
            end = dateutil.parser.parse(end).astimezone(tzutc()).replace(tzinfo=None)
        except:
            raise falcon.HTTPBadRequest(
                title="Invalid parameter", description="invalid date parameter"
            )

        now = datetime.utcnow()
        if end > now:
            end = now

        dayarray = []
        for i in range(20):
            dayarray.append(end - (timedelta(days=1) * (19 - i)))

        stats = {}
        for row in db.execute(
            """select width_bucket(ts, %s) hourbucket, sum(open),
                                 sum(
                                    case when substring(campid from 1 for 3) = 'tx-' then send
                                    else 0
                                    end
                                 ),
                                 sum(
                                    case when campaigns.id is not null then send
                                    else 0
                                    end
                                 ),
                                 sum(
                                    case when messages.id is not null then send
                                    else 0
                                    end
                                 )
                                 from hourstats
                                 left join campaigns on campaigns.id = hourstats.campid
                                 left join messages on messages.id = hourstats.campid
                                 where campcid = %s and ts >= %s
                                 group by hourbucket
                                 order by hourbucket""",
            dayarray,
            db.get_cid(),
            dayarray[0] - timedelta(days=1),
        ):
            if row[0] >= len(dayarray):
                break
            ts = dayarray[row[0]].isoformat() + "Z"
            stats[ts] = {
                "ts": ts,
                "open": row[1],
                "txn": row[2],
                "bc": row[3],
                "funnel": row[4],
            }
        for ts in dayarray:
            t = ts.isoformat() + "Z"
            if t not in stats:
                stats[t] = {
                    "ts": t,
                    "txn": 0,
                    "bc": 0,
                    "funnel": 0,
                }
        req.context["result"] = {
            "stats": sorted(iter(stats.values()), key=lambda s: s["ts"], reverse=True),
            "contacts": db.single(
                "select sum((data->>'count')::integer) from lists where cid = %s",
                db.get_cid(),
            )
            or 0,
        }


class CompanyCampaigns(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        mycid = db.get_cid()

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
                "cid": row[0],
                "name": row[1],
                "open": row[2],
                "complaint": row[3],
                "hard": row[4],
                "soft": row[5],
                "campid": row[6],
            }
            for row in db.execute(
                """select
               case when substring(hourstats.campid from 1 for 3) = 'tx-' then substring(hourstats.campid from 4)
               else
               coalesce(campaigns.cid, funnels.cid)
               end ccid,
               case when substring(hourstats.campid from 1 for 3) = 'tx-' then 'Transactional'
               else
               coalesce(campaigns.data->>'name', (funnels.data->>'name')::text || ' (' || (messages.data->>'subject')::text || ')')
               end campname,
               sum(open::decimal)/nullif(sum(send), 0),
               sum(complaint::decimal)/nullif(sum(send), 0),
               sum(hard::decimal)/nullif(sum(send), 0),
               sum(soft::decimal)/nullif(sum(send), 0),
               hourstats.campid
               from hourstats
               left join campaigns on campaigns.id = hourstats.campid
               left join messages on messages.id = hourstats.campid
               left join funnels on funnels.id = messages.data->>'funnel'
               where hourstats.cid = %s
               and (campaigns.data is null or (campaigns.data->>'sent_at' >= %s and campaigns.data->>'sent_at' <= %s))
               and send+soft+hard+defercnt > 0
               and ts >= %s and ts <= %s
               group by hourstats.campid, ccid, campname
               """,
                mycid,
                start.isoformat() + "Z",
                end.isoformat() + "Z",
                start,
                end,
            )
        ]


class CompanyCampaignStats(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        mycid = db.get_cid()

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
                "cid": row[0],
                "open": row[1],
                "complaint": row[2],
                "complaint_max": row[3],
                "hard_max": row[4],
                "soft_max": row[5],
            }
            for row in db.execute(
                """select ccid as cid,
               sum(open::decimal)/nullif(sum(send), 0),
               sum(complaint::decimal)/nullif(sum(send), 0),
               max(complaint::decimal/nullif(send, 0)),
               max(hard::decimal/nullif(send, 0)),
               max(soft::decimal/nullif(send, 0))
               from (
                 select
                 case when substring(hourstats.campid from 1 for 3) = 'tx-' then substring(hourstats.campid from 4)
                 else
                 coalesce(campaigns.cid, messages.cid)
                 end as ccid,
                 hourstats.campid id, sum(complaint) as complaint,
                 sum(open) as open, sum(send) as send, sum(hard) as hard, sum(soft) as soft
                 from hourstats
                 left join campaigns on campaigns.id = hourstats.campid
                 left join messages on messages.id = hourstats.campid
                 where hourstats.cid = %s
                 and (campaigns.data is null or (campaigns.data->>'sent_at' >= %s and campaigns.data->>'sent_at' <= %s))
                 and send+soft+hard+defercnt > 0
                 and ts >= %s and ts <= %s
                 group by ccid, hourstats.campid
               ) tbl
               group by cid""",
                mycid,
                start.isoformat() + "Z",
                end.isoformat() + "Z",
                start,
                end,
            )
        ]


class CompanyCredits(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        company = db.companies.get(id)
        if company is None:
            raise falcon.HTTPForbidden()

        rdb = redis_connect()

        req.context["result"] = {
            "unlimited": int(rdb.get("credits-%s" % id) or 0),
            "expire": int(rdb.get("credits_expire-%s" % id) or 0),
        }

    def on_patch(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        db = req.context["db"]

        company = db.companies.get(id)
        if company is None:
            raise falcon.HTTPForbidden()

        rdb = redis_connect()

        if "unlimited" in doc:
            rdb.set("credits-%s" % id, int(doc["unlimited"]))
        if "expire" in doc:
            rdb.set("credits_expire-%s" % id, int(doc["expire"]))


@tasks.task(priority=HIGH_PRIORITY)
def close_ticket(ticketid: str, comment: str) -> None:
    try:
        r = requests.put(
            "https://%s/api/v2/tickets/%s.json"
            % (os.environ["zendesk_host"], ticketid),
            json={
                "ticket": {
                    "comment": {"body": comment},
                    "status": "solved",
                },
            },
            auth=("%s/token" % os.environ["zendesk_user"], os.environ["zendesk_key"]),
        )
        r.raise_for_status()
    except:
        log.exception("error")


@tasks.task(priority=HIGH_PRIORITY)
def open_moderation_ticket(uid: str, info: str) -> None:
    try:
        with open_db() as db:
            user = db.users.get(uid)
            if user is None:
                raise Exception("User not found to open moderation ticket for")

            ticketid = open_ticket("Account Activation Request", info, user)

            db.companies.patch(user["cid"], {"moderation_ticket": ticketid})
    except:
        log.exception("error")


class CompanyModerationInfo(object):

    def on_patch(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req)

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        db = req.context["db"]

        mycid = db.get_cid()

        db.set_cid(None)

        db.companies.patch(mycid, {"moderation": doc["info"]})

        if os.environ.get("zendesk_host"):
            run_task(open_moderation_ticket, req.context["uid"], doc["info"])
        else:
            c = db.companies.get(mycid)
            log.info(
                "[NOTIFY] Moderation info submitted for company %s: %s", c["name"], doc
            )


class CompanyOnboarding(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req)

        db = req.context["db"]

        mycid = db.get_cid()

        db.set_cid(None)

        company = db.companies.get(mycid)
        if company is None:
            raise falcon.HTTPForbidden()

        req.context["result"] = company.get("onboarding", {})


class CompanyUsers(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        company = db.companies.get(id)
        if company is None:
            raise falcon.HTTPForbidden()

        db.set_cid(id)

        res = db.users.get_all()
        for r in res:
            r.pop("hash", None)
        req.context["result"] = res


class CompanyPendingLists(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        company = db.companies.get(id)
        if company is None:
            raise falcon.HTTPForbidden()

        db.set_cid(id)

        req.context["result"] = {
            "lists": list(db.lists.find({"unapproved": True})),
            "zendesk_host": os.environ.get("zendesk_host"),
        }


class CompanyPendingListApprove(object):

    def on_post(
        self, req: falcon.Request, resp: falcon.Response, id: str, listid: str
    ) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        company = db.companies.get(id)
        if company is None:
            raise falcon.HTTPForbidden()

        db.set_cid(id)

        lst = db.lists.get(listid)
        if lst is None:
            raise falcon.HTTPForbidden()

        db.lists.patch(listid, {"unapproved": False})

        if lst.get("approval_ticket"):
            run_task(close_ticket, lst["approval_ticket"], doc["comment"])


class Users(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        company = db.companies.get(doc["cid"])
        if company is None:
            raise falcon.HTTPForbidden()

        mycid = db.get_cid()
        if company["admin"] or company["cid"] != mycid:
            raise falcon.HTTPUnauthorized()

        db.set_cid(None)
        if find_user(db, doc["username"]) is not None:
            raise falcon.HTTPBadRequest(
                title="This e-mail address is already in use, please choose another"
            )
        db.set_cid(mycid)

        pw = doc.pop("password1", None)
        doc.pop("password2")
        doc["companyname"] = company["name"]
        doc["admin"] = False
        doc["apikey"] = shortuuid.uuid()

        doc["hash"] = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode(
            "ascii"
        )

        mycid = db.get_cid()

        db.set_cid(doc["cid"])

        r = db.users.get(db.users.add(doc))

        r.pop("hash", None)
        r.pop("apikey", None)

        db.set_cid(None)

        db.set_cid(mycid)
        user_log(req, "plus-circle", "created user ", "users", r["id"], ".")

        req.context["req"] = r


class UserRoutes(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req, True)

        db = req.context["db"]

        mycid = db.get_cid()

        db.set_cid(None)

        pc = db.companies.get(mycid)
        if pc is None:
            raise falcon.HTTPForbidden()

        ret = []

        if "routes" in pc and pc["routes"] is not None:
            for r in pc["routes"]:
                rt = db.routes.get(r)
                if rt is not None and "published" in rt:
                    ret.append({"name": rt["name"], "id": rt["id"]})

        req.context["result"] = ret


class User(object):

    def on_patch(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        mycid = db.get_cid()
        db.set_cid(None)

        existing = db.users.get(id)
        if existing is None:
            raise falcon.HTTPForbidden()

        company = db.companies.get(existing["cid"])
        if company is None:
            raise falcon.HTTPForbidden()

        if company["admin"] or company["cid"] != mycid:
            raise falcon.HTTPUnauthorized()

        if "username" in doc:
            exist = find_user(db, doc["username"])
            if exist is not None and exist["id"] != id:
                raise falcon.HTTPBadRequest(
                    title="This e-mail address is already in use, please choose another"
                )

        pw = doc.pop("password1", None)
        doc.pop("password2", None)

        doc["companyname"] = company["name"]
        doc["admin"] = False

        if pw:
            doc["hash"] = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode(
                "ascii"
            )

        db.users.patch(id, doc)

        if compare_patch(doc, existing):
            db.set_cid(mycid)
            user_log(req, "pencil", "edited user ", "users", id, ".")
            db.set_cid(None)

        if doc.get("disabled", False):
            db.cookies.delete({"uid": id})

    def on_delete(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        if not req.context["admin"]:
            raise falcon.HTTPUnauthorized()

        db = req.context["db"]

        myuid = req.context["uid"]

        db.set_cid(None)

        u = db.users.get(myuid)

        r = db.users.get(id)
        if r is None:
            raise falcon.HTTPForbidden()

        if r.get("admin", False):
            raise falcon.HTTPUnauthorized()

        pc = db.companies.get(r["cid"])
        if pc is None:
            raise falcon.HTTPForbidden()

        if pc["cid"] != u["cid"]:
            raise falcon.HTTPUnauthorized()

        db.set_cid(u["cid"])

        user_log(req, "remove", "deleted user %s" % r["username"])

        db.set_cid(r["cid"])

        db.users.remove(id)

        db.cookies.delete({"uid": id})

    def on_get(self, req: falcon.Request, resp: falcon.Response, id: str) -> None:
        db = req.context["db"]

        myuid = req.context["uid"]

        if req.context["api"]:
            raise falcon.HTTPUnauthorized()

        mycid = db.get_cid()

        db.set_cid(None)

        u = db.users.get(myuid)

        if not u or (not u.get("admin", False) and id != myuid):
            raise falcon.HTTPUnauthorized()

        r = db.users.get(id)
        if r is None:
            raise falcon.HTTPForbidden()

        pc = None
        if id != myuid:
            if r.get("admin", False):
                raise falcon.HTTPUnauthorized()

            pc = db.companies.get(r["cid"])
            if pc is None:
                raise falcon.HTTPForbidden()

            if pc["cid"] != u["cid"]:
                raise falcon.HTTPUnauthorized()

        if pc is None:
            pc = db.companies.get(mycid)

        if pc is not None and id == myuid:
            rdb = redis_connect()

            r["smtphost"] = os.environ.get("smtphost")
            r["software_version"] = VERSION
            r["webroot"] = get_webroot()
            # Only expose BeeFree when credentials are present (ID/secret required; CS API key optional).
            bf_id = os.environ.get("beefree_client_id") or ""
            bf_secret = os.environ.get("beefree_client_secret") or ""
            bf_direct = bool(bf_id.strip() and bf_secret.strip())
            bf_proxy = bool(get_beefree_proxy_url() and get_commercial_license_key())
            r["hasbeefree"] = bool(bf_direct or bf_proxy)
            r["lastFooter"] = pc.get("lastFooter")
            if "frontend" in pc and pc["frontend"] is not None:
                fe = db.frontends.get(pc["frontend"])
                if fe is not None:
                    fe.pop("headers", None)
                    fe.pop("txnaccount", None)
                    fe.pop("fromencoding", None)
                    fe.pop("subjectencoding", None)
                    fe.pop("usedkim", None)
                    fe.pop("invitename", None)
                    fe.pop("inviteemail", None)
                    fe.pop("bodydomain", None)
                    r["frontend"] = fe
            r["paid"] = pc.get("paid")
            r["hasmoderation"] = pc.get("moderation") is not None
            r["limit"] = fix_empty_limit(pc.get("daylimit"))
            r["inreview"] = pc.get("inreview")
            r["trialend"] = pc.get("trialend")
            if r["limit"] is not None:
                r["limitresettime"] = (
                    datetime.now(tzoffset("", timedelta(minutes=pc.get("tzoffset", 0))))
                    .replace(hour=7, minute=0, second=0, microsecond=0)
                    .astimezone(tzutc())
                    .replace(tzinfo=None)
                    .isoformat()
                    + "Z"
                )
                r["sent"], _ = send_rate(pc)
            if r["paid"]:
                credits = int(rdb.get("credits-%s" % pc["id"]) or 0)
                creditsexpire = int(rdb.get("credits_expire-%s" % pc["id"]) or 0)
                r["credits"] = credits + creditsexpire
                period = pc.get("credits", None)
                if period is None:
                    period = PERIOD_CREDITS
                r["creditsmax"] = period
                if credits:
                    refill = pc.get("overagecredits", None)
                    if refill is None:
                        refill = REFILL_CREDITS
                    r["creditsmax"] += refill
                r["probation"] = (
                    pc.get("daylimit") is not None
                    and isinstance(pc["daylimit"], int)
                    and pc.get("hourlimit") is not None
                    and isinstance(pc["hourlimit"], int)
                    and pc.get("defaultdaylimit") is not None
                    and isinstance(pc["defaultdaylimit"], int)
                    and pc.get("defaulthourlimit") is not None
                    and isinstance(pc["defaulthourlimit"], int)
                    and pc["daylimit"] <= pc["defaultdaylimit"]
                    and pc["hourlimit"] <= pc["defaulthourlimit"]
                )
        else:
            r.pop("apikey", None)

        r.pop("hash", None)

        req.context["result"] = r


class LastTest(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        db = req.context["db"]
        myuid = req.context["uid"]

        db.set_cid(None)

        u = db.users.get(myuid)
        if u is None:
            raise falcon.HTTPForbidden()
        req.context["result"] = u.get("lasttest", {})


class Countries(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req)

        db = req.context["db"]

        req.context["result"] = [
            row[0]
            for row in db.execute(
                "select country from countries where country != '-' order by country"
            )
        ]


class Regions(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req)

        db = req.context["db"]

        ret: Dict[str, List[JsonObj]] = {}
        for country, region in db.execute(
            "select country, region from regions where region != '-'"
        ):
            if country not in ret:
                ret[country] = []
            ret[country].append(region)

        req.context["result"] = ret


class TableConfig(object):

    def on_patch(self, req: falcon.Request, resp: falcon.Response, name: str) -> None:
        if req.context["api"]:
            raise falcon.HTTPBadRequest(title="Invalid method")

        db = req.context["db"]
        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        doc["name"] = name

        exist = db.tableconfigs.find_one({"name": name})
        if exist is None:
            db.tableconfigs.add(doc)
        else:
            db.tableconfigs.patch(exist["id"], doc)

    def on_get(self, req: falcon.Request, resp: falcon.Response, name: str) -> None:
        if req.context["api"]:
            raise falcon.HTTPBadRequest(title="Invalid method")

        db = req.context["db"]
        exist = db.tableconfigs.find_one({"name": name})
        if exist is None:
            exist = {}
        req.context["result"] = exist


filenamere = re.compile(r'filename="([^"]+)"')


class ImageUpload(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req, True)

        images = os.environ["s3_imagebucket"]

        s = bytearray(
            "".join(
                "%s: %s\r\n" % (str(k), str(v)) for k, v in req.headers.items()
            ).encode("utf-8")
        )
        s.extend(b"\r\n")
        s.extend(req.bounded_stream.read())
        msg = email.message_from_bytes(s)

        filename: str | None = None
        filedata: bytes | None = None

        key = "Content-Disposition"
        for item in msg.get_payload():
            itemmsg = cast(email.message.Message, item)
            if key not in itemmsg:
                continue
            m = filenamere.search(cast(str, itemmsg.get(key)))
            if m:
                filename = m.group(1)
                filedata = cast(bytes, itemmsg.get_payload(decode=True))

        if filename is None or filedata is None:
            raise falcon.HTTPBadRequest(
                title="Missing file", description="No file parameter found"
            )

        _, ext = os.path.splitext(filename)

        if not re.search(r"^\.[a-zA-Z]{3,4}$", ext):
            raise falcon.HTTPBadRequest(
                title="Invalid filename",
                description="File extensions cannot contain special characters",
            )

        md5 = hashlib.md5(filedata).hexdigest()

        newkey = "%s%s" % (md5, ext)

        try:
            s3_size(images, newkey)
        except:
            s3_write(images, newkey, filedata)

        req.context["result"] = {
            "link": "%s/i/%s" % (get_webroot(), newkey),
        }


class ImageImport(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        uploads = os.environ["s3_transferbucket"]
        images = os.environ["s3_imagebucket"]

        md5 = hashlib.md5(s3_read(uploads, doc["key"])).hexdigest()

        ext = os.path.splitext(doc["key"])[1]

        newkey = "%s%s" % (md5, ext)

        try:
            s3_size(images, newkey)
        except:
            s3_copy(uploads, doc["key"], images, newkey)

        s3_delete(uploads, doc["key"])

        req.context["result"] = {
            "url": "%s/i/%s" % (get_webroot(), newkey),
        }


class OpenTicket(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        check_noadmin(req)

        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        subject = doc["subject"]
        message = doc["message"]

        db = req.context["db"]

        myuid = req.context["uid"]

        db.set_cid(None)

        user = db.users.get(myuid)
        if user is None:
            raise falcon.HTTPForbidden()

        if not os.environ.get("zendesk_host"):
            raise falcon.HTTPBadRequest(
                title="Zendesk Not Configured",
                description="You must configure the zendesk_host, zendesk_user and zendesk_pass settings to enable this feature",
            )

        try:
            open_ticket(subject, message, user)
        except Exception as e:
            raise falcon.HTTPInternalServerError(
                title="Unable to open ticket",
                description="Unable to open ticket: %s" % e,
            )


class StockSearch(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        if not os.environ.get("pixabay_key"):
            raise falcon.HTTPBadRequest(
                title="No API Key",
                description="You must configure the pixabay_key setting to use the stock art feature",
            )

        rdb = redis_connect()

        rediskey = "pixabay:%s" % (
            hashlib.md5(
                ("%s:%s:%s" % (doc["image_type"], doc["q"], doc["page"])).encode(
                    "utf-8"
                )
            ).hexdigest()
        )

        cached = rdb.get(rediskey)
        if cached:
            req.context["result"] = json.loads(cached)
            return

        r = requests.get(
            "https://pixabay.com/api/",
            params={
                "key": os.environ["pixabay_key"],
                "image_type": doc["image_type"],
                "q": doc["q"],
                "safesearch": "true",
                "page": doc["page"],
                "per_page": 15,
            },
        )
        r.raise_for_status()

        req.context["result"] = r.json()

        rdb.pipeline().set(rediskey, json.dumps(req.context["result"])).expire(
            rediskey, 60 * 60 * 24
        ).execute()


class SupportContact(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:

        req.context["result"] = {"email": os.environ["support_email"]}


class DomainThrottles(CRUDCollection):

    def __init__(self) -> None:
        self.domain = "domainthrottles"
        self.useronly = True


class DomainThrottle(CRUDSingle):

    def __init__(self) -> None:
        self.domain = "domainthrottles"
        self.useronly = True


class UploadFile(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if req.context["api"]:
            raise falcon.HTTPBadRequest(title="Invalid method")

        t = req.get_param("type")
        ext = req.get_param("ext")

        if t == "img":
            if ext is None or not re.search(r"^[a-zA-Z]{3,4}$", ext):
                raise falcon.HTTPBadRequest()
            key = "images/%s.%s" % (shortuuid.uuid(), ext)
        else:
            key = "lists/%s.txt" % shortuuid.uuid()

        s3_write_stream(os.environ["s3_transferbucket"], key, req.bounded_stream)

        req.context["result"] = {
            "key": key,
        }


class UploadLogFile(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        with open_db() as db:
            sinkid = req.get_param("sinkid")
            accesskey = req.get_param("accesskey")

            sink = db.sinks.get(sinkid)
            if sink is None:
                raise falcon.HTTPUnauthorized()

            if sink["accesskey"] != accesskey:
                raise falcon.HTTPUnauthorized()

            key = "sendlogs/%s.txt" % shortuuid.uuid()

            s3_write_stream(os.environ["s3_transferbucket"], key, req.bounded_stream)

            req.context["result"] = {
                "key": key,
            }


COMMERCIAL_LICENSE_PATH = "/config/commercial_license.key"


def get_commercial_license_key() -> str:
    key = (os.environ.get("commercial_license_key") or "").strip()
    if key:
        return key
    try:
        with open(COMMERCIAL_LICENSE_PATH) as handle:
            return handle.read().strip()
    except FileNotFoundError:
        return ""
    except Exception:
        logging.exception("Error reading commercial license key")
        return ""


def get_beefree_proxy_url() -> str:
    url = (os.environ.get("beefree_proxy_url") or "").strip()
    if url.endswith("/"):
        url = url[:-1]
    return url


class BeeFreeAuth(object):

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        logging.info("Getting BeeFree auth token (v2)...")
        client_id = (os.environ.get("beefree_client_id") or "").strip()
        client_secret = (os.environ.get("beefree_client_secret") or "").strip()
        uid = str(req.context.get("uid") or "edcom-user")

        proxy_url = get_beefree_proxy_url()
        license_key = get_commercial_license_key()

        if client_id and client_secret:
            res = requests.post(
                "https://auth.getbee.io/loginV2",
                json={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "uid": uid,
                },
                headers={
                    "Accept": "application/json",
                },
            )
        elif proxy_url and license_key:
            res = requests.post(
                f"{proxy_url}/auth",
                json={
                    "uid": uid,
                },
                headers={
                    "Accept": "application/json",
                    "X-Edcom-License": license_key,
                },
            )
        else:
            raise falcon.HTTPForbidden()

        if res.status_code < 200 or res.status_code >= 300:
            logging.error("Error getting auth token: %s", res.text)
            raise falcon.InternalServerError()

        resp.text = res.text
        resp.content_type = res.headers['Content-Type']
        resp.status = f"{res.status_code} {res.reason}"
        logging.info("Got auth token")


class BeeFreeMerge(object):

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        logging.info("Merging template...")
        doc = req.context.get("doc")
        if not doc:
            raise falcon.HTTPBadRequest(
                title="Not JSON", description="A valid JSON document is required."
            )

        cs_api_key = (os.environ.get("beefree_cs_api_key") or "").strip()
        proxy_url = get_beefree_proxy_url()
        license_key = get_commercial_license_key()

        if cs_api_key:
            res = requests.post('https://api.getbee.io/v1/message/merge', json=doc, headers={
                "Authorization": f"Bearer {cs_api_key}"
            })
        elif proxy_url and license_key:
            res = requests.post(
                f"{proxy_url}/merge",
                json=doc,
                headers={
                    "X-Edcom-License": license_key,
                },
            )
        else:
            raise falcon.HTTPForbidden()

        if res.status_code < 200 or res.status_code >= 300:
            logging.error("Error merging template: %s", res.text)
            resp.text = "{}"
            resp.content_type = "application/json"
            resp.status_code = 202
            return

        resp.text = res.text
        resp.content_type = res.headers["Content-Type"]
        resp.status = f"{res.status_code} {res.reason}"
        logging.info("Merged template")

class DocCSSFixer(object):

    def process_response(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        resource: Any,
        req_succeeded: bool,
    ) -> None:
        if req.path != "/api/doc/swagger-ui.css":
            return

        body = resp.stream.read().decode("utf-8")
        resp.stream = None
        resp.content_length = None
        resp.text = (
            body
            + '\n\n.swagger-ui .opblock-description-wrapper p { margin-bottom: 10px; } .swagger-ui .opblock-description-wrapper p br { display: block; margin-bottom: 10px; content: " "; } .swagger-ui .topbar { display: none; }'
        )


app = application = falcon.App(
    middleware=[
        AuthMiddleware(),
        RateLimit(),
        JSONTranslator(),
        DocCSSFixer(),
        NoCache(),
    ]
)


def http_error_handler(
    ex: Exception, req: falcon.Request, resp: falcon.Response, params: Any
) -> None:
    raise ex


def falcon_error_handler(
    ex: Exception, req: falcon.Request, resp: falcon.Response, params: Any
) -> None:
    path = req.path
    if path.startswith("/"):
        path = path[1:]
    log.exception(
        "%s %s",
        path.replace("/", "-"),
        {
            "params": req.params,
            "headers": req.headers,
        },
    )

    raise falcon.HTTPInternalServerError()


app.add_error_handler(Exception, falcon_error_handler)
app.add_error_handler(falcon.HTTPError, http_error_handler)
app.add_error_handler(falcon.HTTPStatus, http_error_handler)

app.req_options.strip_url_path_trailing_slash = True

app.add_route("/api/ping", Ping())
app.add_route("/api/stats/{sinkid}", events.Stats())
app.add_route("/api/events/{sinkid}", events.Events())
app.add_route("/api/queue/{sinkid}", events.Queue())
app.add_route("/api/limits/{sinkid}", events.Limits())
app.add_route("/api/sendlogs/{sinkid}", events.SendLogs())
app.add_route("/api/testlogs/{sinkid}", events.TestLogs())
app.add_route("/api/mgwebhook", events.MGWebHook())
app.add_route("/api/seswebhook", events.SESWebHook())
app.add_route("/api/spwebhook", events.SPWebHook())

register_swaggerui_app(
    app,
    "/api/doc",
    get_webroot() + "/openapi.yaml",
    page_title=get_webhost(),
    favicon_url=get_webroot() + "/favicon-ed.ico",
)  # type: ignore

app.add_route("/api/stock/search", StockSearch())
app.add_route("/api/healthy", Healthy())
app.add_route("/api/invite", Invite())
app.add_route("/api/resendcode", ResendCode())
app.add_route("/api/login", Login())
app.add_route("/api/register", Register())
app.add_route("/api/logout", Logout())
app.add_route("/api/reset/sendemail", SendResetEmail())
app.add_route("/api/reset/password", PasswordReset())
app.add_route("/api/reset/passemail", PasswordEmailReset())
app.add_route("/api/reset/apikey", APIKeyReset())
app.add_route("/api/reset/limitalert", RemoveLimitAlert())
app.add_route("/api/reset/probationalert", RemoveProbationAlert())
app.add_route("/api/reset/mobilealert", RemoveMobileAlert())
app.add_route("/api/users/{id}", User())
app.add_route("/api/users", Users())
app.add_route("/api/userroutes", UserRoutes())
app.add_route("/api/tableconfigs/{name}", TableConfig())
app.add_route("/api/lasttest", LastTest())
app.add_route("/api/countries", Countries())
app.add_route("/api/regions", Regions())
app.add_route("/api/frontends", frontends.Frontends())
app.add_route("/api/frontends/{id}", frontends.Frontend())
app.add_route("/api/loginfrontend", frontends.LoginFrontend())
app.add_route("/api/gallerytemplates", frontends.GalleryTemplates())
app.add_route("/api/gallerytemplates/{id}", frontends.GalleryTemplate())
app.add_route(
    "/api/gallerytemplates/{id}/duplicate", frontends.GalleryTemplateDuplicate()
)
app.add_route("/api/formtemplates", frontends.FormTemplates())
app.add_route("/api/formtemplates/{id}", frontends.FormTemplate())
app.add_route("/api/formtemplates/{id}/duplicate", frontends.FormTemplateDuplicate())
app.add_route("/api/allformtemplates", frontends.AllFormTemplates())
app.add_route("/api/allformtemplates/{id}", frontends.AllFormTemplatesSingle())
app.add_route("/api/alltemplates", frontends.AllTemplates())
app.add_route("/api/alltemplates/{id}", frontends.AllTemplatesSingle())
app.add_route("/api/allbeefreetemplates", frontends.AllBeefreeTemplates())
app.add_route("/api/allbeefreetemplates/{id}", frontends.AllBeefreeTemplatesSingle())
app.add_route("/api/beefreetemplates", frontends.BeefreeTemplates())
app.add_route("/api/beefreetemplates/{id}", frontends.BeefreeTemplate())
app.add_route(
    "/api/beefreetemplates/{id}/duplicate", frontends.BeefreeTemplateDuplicate()
)
app.add_route("/api/signupsettings", frontends.SignupSettings())
app.add_route("/api/signupaction/{id}", frontends.SignupAction())
app.add_route("/signup/{id}", frontends.Signup())
app.add_route("/api/policies", backends.Policies())
app.add_route("/api/policies/{id}", backends.Policy())
app.add_route("/api/policies/{id}/publish", backends.PolicyPublish())
app.add_route("/api/policies/{id}/revert", backends.PolicyRevert())
app.add_route("/api/policies/{id}/duplicate", backends.PolicyDuplicate())
app.add_route("/api/routepolicies", backends.RoutePolicies())
app.add_route("/api/routes", backends.Routes())
app.add_route("/api/routes/{id}", backends.Route())
app.add_route("/api/routes/{id}/publish", backends.RoutePublish())
app.add_route("/api/routes/{id}/revert", backends.RouteRevert())
app.add_route("/api/routes/{id}/duplicate", backends.RouteDuplicate())
app.add_route("/api/domaingroups", backends.DomainGroups())
app.add_route("/api/domaingroups/{id}", backends.DomainGroup())
app.add_route("/api/sinks", backends.Sinks())
app.add_route("/api/sinks/{id}", backends.Sink())
app.add_route("/api/sinks/{id}/stats", backends.SinkStats())
app.add_route("/api/sinks/{id}/sumstats", backends.SinkSumStats())
app.add_route("/api/sinks/{id}/domainoptions", backends.SinkDomainOptions())
app.add_route("/api/clientdkim", backends.ClientDKIMEntries())
app.add_route("/api/clientdkim/{id}", backends.ClientDKIMEntry())
app.add_route("/api/clientdkim/{id}/verify", backends.ClientDKIMVerify())
app.add_route("/api/dkimentries", backends.DKIMEntries())
app.add_route("/api/allstats", backends.AllStats())
app.add_route("/api/allsettings", backends.AllSettings())
app.add_route("/api/settingsstats/{id}", backends.SettingsSumStats())
app.add_route("/api/warmups", backends.Warmups())
app.add_route("/api/warmups/{id}", backends.Warmup())
app.add_route("/api/warmups/{id}/publish", backends.WarmupPublish())
app.add_route("/api/warmups/{id}/revert", backends.WarmupRevert())
app.add_route("/api/warmups/{id}/duplicate", backends.WarmupDuplicate())
app.add_route("/api/warmups/{id}/enable", backends.WarmupEnable())
app.add_route("/api/warmups/{id}/disable", backends.WarmupDisable())
app.add_route("/api/ipstats", backends.IPStats())
app.add_route("/api/ipmsgs", backends.IPMsgs())
app.add_route("/api/ippauses", backends.IPPauses())
app.add_route("/api/mailgun", backends.Mailguns())
app.add_route("/api/mailgun/{id}", backends.Mailgun())
app.add_route("/api/ses", backends.SESs())
app.add_route("/api/ses/{id}", backends.SES())
app.add_route("/api/sparkpost", backends.Sparkposts())
app.add_route("/api/sparkpost/{id}", backends.Sparkpost())
app.add_route("/api/easylink", backends.Easylinks())
app.add_route("/api/easylink/{id}", backends.Easylink())
app.add_route("/api/smtprelays", backends.SMTPRelays())
app.add_route("/api/smtprelays/{id}", backends.SMTPRelay())
app.add_route("/api/userlogs", UserLogs())
app.add_route("/api/approvecompanies", ApproveCompanies())
app.add_route("/api/bancompanies", BanCompanies())
app.add_route("/api/unbancompanies", UnbanCompanies())
app.add_route("/api/pausecompanies", PauseCompanies())
app.add_route("/api/unpausecompanies", UnpauseCompanies())
app.add_route("/api/purgequeues/{t}", PurgeQueues())
app.add_route("/api/limitcompanies", LimitCompanies())
app.add_route("/api/webhooks", RestHooks())
app.add_route("/api/webhooks/{id}", RestHook())
app.add_route("/api/resthooks", RestHooks())
app.add_route("/api/resthooks/test", RestHookTest())
app.add_route("/api/resthooks/{id}", RestHook())
app.add_route("/api/pollresthooks/{event}", PollRestHook())
app.add_route("/api/companylimits", CompanyLimits())
app.add_route("/api/companies", Companies())
app.add_route("/api/companies/{id}", Company())
app.add_route("/api/companies/{id}/users", CompanyUsers())
app.add_route("/api/companies/{id}/pendinglists", CompanyPendingLists())
app.add_route(
    "/api/companies/{id}/pendinglists/{listid}/approve", CompanyPendingListApprove()
)
app.add_route("/api/companies/{id}/credits", CompanyCredits())
app.add_route("/api/companies/{id}/campaigns/{campid}", CompanyCampaign())
app.add_route("/api/companies/{id}/broadcasts/{campid}", CompanyCampaign())
app.add_route("/api/companycampaigns", CompanyCampaigns())
app.add_route("/api/companycampaignstats", CompanyCampaignStats())
app.add_route("/api/companybroadcasts", CompanyCampaigns())
app.add_route("/api/companybroadcaststats", CompanyCampaignStats())
app.add_route("/api/moderationinfo", CompanyModerationInfo())
app.add_route("/api/onboarding", CompanyOnboarding())
app.add_route("/api/lists", lists.Lists())
app.add_route("/api/lists/{id}", lists.ListSingle())
app.add_route("/api/uploadfile", UploadFile())
app.add_route("/api/uploadlogfile", UploadLogFile())
app.add_route("/api/imageimport", ImageImport())
app.add_route("/api/imageupload", ImageUpload())
app.add_route("/api/beefreeauth", BeeFreeAuth())
app.add_route("/api/beefreemerge", BeeFreeMerge())
app.add_route("/api/lists/{id}/import", lists.ListImport())
app.add_route("/api/lists/{id}/importunsubs", lists.ListImportUnsubs())
app.add_route("/api/lists/{id}/addunsubs", lists.ListAddUnsubs())
app.add_route("/api/lists/{id}/add", lists.ListAdd())
app.add_route("/api/lists/{id}/export", lists.ListExport())
app.add_route("/api/lists/{id}/find", lists.ListFind())
app.add_route("/api/lists/{id}/tag", lists.ListTag())
app.add_route("/api/lists/{id}/domainstats", ListDomainStats())
app.add_route("/api/lists/{id}/deletedomains", lists.ListDeleteDomains())
app.add_route("/api/lists/{id}/feed", lists.ListFeed())
app.add_route("/api/lists/{id}/deletecontacts", lists.ListDeleteContacts())
app.add_route("/api/lists/{id}/bulkdelete", lists.ListBulkDelete())
app.add_route("/api/listfind/{id}", lists.ListFindStatus())
app.add_route("/api/domainthrottles", DomainThrottles())
app.add_route("/api/domainthrottles/{id}", DomainThrottle())
app.add_route("/api/contactexport", lists.ContactExport())
app.add_route("/api/contactdata/{email}", lists.ContactData())
app.add_route("/api/recenttags", lists.RecentTags())
app.add_route("/api/supplists", lists.SuppLists())
app.add_route("/api/supplists/{id}", lists.SuppList())
app.add_route("/api/supplists/{id}/import", lists.SuppListImport())
app.add_route("/api/supplists/{id}/add", lists.SuppListAdd())
app.add_route("/api/exclusion", lists.ExclusionLists())
app.add_route("/api/exclusion/{id}/add", lists.ExclusionListAdd())
app.add_route("/api/exports", lists.Exports())
app.add_route("/api/segments", lists.Segments())
app.add_route("/api/segments/{id}", lists.Segment())
app.add_route("/api/segments/{id}/export", lists.SegmentExport())
app.add_route("/api/segments/{id}/duplicate", lists.SegmentDuplicate())
app.add_route("/api/segments/{id}/tag", lists.SegmentTag())
app.add_route("/api/alltags", lists.AllTags())
app.add_route("/api/alltags/{tag}", lists.AllTagsRemove())
app.add_route("/api/allfields", lists.AllFields())
app.add_route("/api/campaigns", campaigns.Campaigns())
app.add_route("/api/campaigns/{id}", campaigns.Campaign())
app.add_route("/api/campaigns/{id}/duplicate", campaigns.CampaignDuplicate())
app.add_route("/api/campaigns/{id}/start", campaigns.CampaignStart())
app.add_route("/api/campaigns/{id}/test", campaigns.CampaignTest())
app.add_route("/api/campaigns/{id}/calculate", campaigns.CampaignCalculate())
app.add_route("/api/campaigns/{id}/cancel", campaigns.CampaignCancel())
app.add_route("/api/campaigns/{id}/domainstats", CampaignDomainStats())
app.add_route("/api/campaigns/{id}/msgs", CampaignMessages())
app.add_route("/api/campaigns/{id}/export", campaigns.CampaignExport())
app.add_route("/api/campaigns/{id}/update", campaigns.CampaignUpdate())
app.add_route("/api/campaigns/{id}/details", campaigns.CampaignDetails())
app.add_route("/api/campaigncalculate/{id}", campaigns.CampaignCalculateStatus())
app.add_route("/api/recentcampaigns", campaigns.RecentCampaigns())
app.add_route("/api/broadcasts", campaigns.Broadcasts())
app.add_route("/api/broadcasts/{id}", campaigns.Campaign())
app.add_route("/api/broadcasts/{id}/clientstats", campaigns.CampaignClientStats())
app.add_route("/api/broadcasts/{id}/duplicate", campaigns.CampaignDuplicate())
app.add_route("/api/broadcasts/{id}/start", campaigns.CampaignStart())
app.add_route("/api/broadcasts/{id}/test", campaigns.CampaignTest())
app.add_route("/api/broadcasts/{id}/calculate", campaigns.CampaignCalculate())
app.add_route("/api/broadcasts/{id}/cancel", campaigns.CampaignCancel())
app.add_route("/api/broadcasts/{id}/domainstats", CampaignDomainStats())
app.add_route("/api/broadcasts/{id}/msgs", CampaignMessages())
app.add_route("/api/broadcasts/{id}/export", campaigns.CampaignExport())
app.add_route("/api/broadcasts/{id}/update", campaigns.CampaignUpdate())
app.add_route("/api/broadcasts/{id}/details", campaigns.CampaignDetails())
app.add_route("/api/broadcastcalculate/{id}", campaigns.CampaignCalculateStatus())
app.add_route("/api/recentbroadcasts", campaigns.RecentCampaigns())
app.add_route("/api/savedrows", campaigns.SavedRows())
app.add_route("/api/savedrows/{id}", campaigns.SavedRow())
app.add_route("/api/funnels", funnels.Funnels())
app.add_route("/api/funnels/{id}", funnels.Funnel())
app.add_route("/api/funnels/{id}/messages", funnels.FunnelMessages())
app.add_route("/api/funnels/{id}/duplicate", funnels.FunnelDuplicate())
app.add_route("/api/messages", funnels.Messages())
app.add_route("/api/messages/{id}", funnels.Message())
app.add_route("/api/messages/{id}/duplicate", funnels.MessageDuplicate())
app.add_route("/api/messages/{id}/test", funnels.MessageTest())
app.add_route("/api/messages/{id}/clientstats", funnels.MessageClientStats())
app.add_route("/api/messages/{id}/domainstats", MessageDomainStats())
app.add_route("/api/messages/{id}/msgs", MessageMessages())
app.add_route("/api/forms", funnels.Forms())
app.add_route("/api/forms/{id}", funnels.Form())
app.add_route("/api/forms/{id}/duplicate", funnels.FormDuplicate())
app.add_route("/api/showform/{id}", funnels.ShowForm())
app.add_route("/api/showform/{id}/embed.js", funnels.ShowFormEmbed())
app.add_route("/api/trackform/{id}", funnels.TrackForm())
app.add_route("/api/postform/{id}", funnels.PostForm())
app.add_route("/api/postform/{id}.json", funnels.PostForm())
app.add_route("/api/transactional/send", transactional.Send())
app.add_route("/api/transactional/tags", transactional.Tags())
app.add_route("/api/transactional/recenttags", transactional.RecentTags())
app.add_route("/api/transactional/stats", transactional.Stats())
app.add_route("/api/transactional/log", transactional.Log())
app.add_route("/api/transactional/log/export", transactional.LogExport())
app.add_route("/api/transactional/tag/{tag}", transactional.Tag())
app.add_route(
    "/api/transactional/tag/{tag}/domainstats", transactional.TagDomainStats()
)
app.add_route("/api/transactional/tag/{tag}/msgs", transactional.TagMessages())
app.add_route("/api/transactional/templates", transactional.TxnTemplates())
app.add_route("/api/transactional/templates/{id}", transactional.TxnTemplate())
app.add_route(
    "/api/transactional/templates/{id}/duplicate", transactional.TxnTemplateDuplicate()
)
app.add_route("/api/transactional/templates/{id}/test", transactional.TxnTemplateTest())
app.add_route("/api/transactional/settings", transactional.TxnSettings())
app.add_route("/api/testemails", campaigns.TestEmails())
app.add_route("/api/supportcontact", SupportContact())
app.add_route("/api/openticket", OpenTicket())
app.add_route("/api/customerdashboard", CustomerDashboard())
app.add_route("/api/testlogs", events.TestLogsGet())
app.add_route("/api/track", events.Track())
app.add_route("/l", events.Track())
app.add_route("/api/links/{id}", events.Link())


def ready() -> None:
    log.info("Application worker process ready")


ready()
