import requests
import re
import shortuuid
import random
import json
import os
import base64
import redis
import falcon
import smtplib
import quopri
import uuid
from typing import Dict, Tuple, List, Set, cast, Any, Iterable, Type, TypedDict
from urllib3 import Retry
from requests.adapters import HTTPAdapter
from email.header import Header
from email.utils import formatdate
from datetime import datetime, timedelta
from dateutil.tz import tzoffset, tzutc
import dateutil.parser
from io import StringIO, BytesIO
import boto3
from email.utils import formataddr, parseaddr
from fnmatch import fnmatch
from .db import json_obj, open_db, JsonObj, DB
from .utils import (
    run_task,
    MPDictReader,
    MPDictWriter,
    remove_newlines,
    redis_connect,
    handle_mg_error,
    domain_only,
    handle_sp_error,
    get_txn,
    get_webhost,
    get_webroot,
    get_webscheme,
    MTA_TIMEOUT,
    fix_empty_limit,
    fix_sink_url,
)
from xml.sax.saxutils import escape
from .tasks import tasks, LOW_PRIORITY
from .s3 import s3_read, s3_delete, s3_write, s3_read_stream
from .attachments import (
    AttachmentManifest,
    build_attachment_storage,
    build_raw_mime_message,
    get_attachment_config,
)
from . import contacts
from .log import get_logger
from .webhooks import send_webhooks

log = get_logger()

ssl_enabled_domains: Set[str] = set()
try:
    SUFFIX = ".nginx.conf"
    # list files named *.nginx.conf in the /config/linkcerts directory
    if os.path.exists("/config/linkcerts"):
        for f in os.listdir("/config/linkcerts"):
            if f.endswith(SUFFIX):
                ssl_enabled_domains.add(f[: -len(SUFFIX)].lower())
except:
    log.exception("error loading SSL-enabled domains")


def take_lock(db: DB, name: str) -> bool:
    lock_id = (
        uuid.uuid5(uuid.UUID("ee5d3320-10ac-43c6-b178-b1bede0d4f97"), name).int
        & (1 << 63) - 1
    )

    return bool(db.single(f"select pg_try_advisory_xact_lock({lock_id})"))


def client_domain(db: DB, fromdomain: str, cid: str, objid: str) -> str | None:
    return cast(
        str | None,
        db.single(
            """select data->>'name' from clientdkim
                        where data->>'name' = %s and cid = %s and
                        data->'mgentry' is not null and
                        data->'mgentry'->>'id' = %s and
                        data->>'verified' is not null and (data->>'verified')::boolean""",
            fromdomain,
            cid,
            objid,
        ),
    )


def send_rate(company: JsonObj) -> Tuple[int, bytes | None]:
    rdb = redis_connect()

    cid = company["id"]
    offset = company.get("tzoffset", 0)
    localtime = datetime.now(tzoffset("", timedelta(minutes=offset)))

    if localtime.hour < 7:
        localtime = localtime - timedelta(days=1)

    daykey = "sendrateday-%s:%s" % (cid, localtime.day)
    limithitkey = "limithit-%s:%s" % (cid, localtime.day)

    return int(rdb.get(daykey) or 0), rdb.get(limithitkey)


def check_test_limit(db: DB, company: JsonObj, email: str) -> None:
    cid = company["id"]
    paid = company.get("paid", False)
    offset = company.get("tzoffset", 0)

    if paid:
        return

    rdb = redis_connect()

    localtime = datetime.now(tzoffset("", timedelta(minutes=offset)))

    if localtime.hour < 7:
        localtime = localtime - timedelta(days=1)

    if company.get("inreview"):
        found = False
        db.set_cid(cid)
        for u in db.users.get_all():
            if u["username"].lower().strip() == email.lower().strip():
                found = True
                break
        db.set_cid(None)
        if not found:
            raise falcon.HTTPBadRequest(
                title="Invalid test email",
                description="Test messages for unapproved accounts can only be sent to the account owner",
            )
    else:
        key = "testemails-%s:%s" % (cid, localtime.day)

        if rdb.sismember(key, email):
            return

        cnt = rdb.scard(key)
        if cnt >= 10:
            raise falcon.HTTPBadRequest(
                title="Too many test addresses",
                description="You have sent tests to too many unique addresses today",
            )

        rdb.pipeline().sadd(key, email).expire(key, 60 * 60 * 24).execute()


def load_domain_throttles(db: DB, company: JsonObj) -> List[JsonObj]:
    db.set_cid(company["id"])
    try:
        r = [dt for dt in db.domainthrottles.find() if dt.get("active")]
        for dt in r:
            dt["domainsparsed"] = [
                d.strip().lower() for d in dt["domains"].split() if d.strip()
            ]
        return r
    finally:
        db.set_cid(None)


def check_send_limit(
    company: JsonObj,
    route: JsonObj,
    domain: str,
    domainthrottles: List[JsonObj],
    requested: int,
) -> int:
    cid = company["id"]
    minlimit = fix_empty_limit(company.get("minlimit"))
    hourlimit = fix_empty_limit(company.get("hourlimit"))
    daylimit = fix_empty_limit(company.get("daylimit"))
    monthlimit = fix_empty_limit(company.get("monthlimit"))
    offset = company.get("tzoffset", 0)
    paid = company.get("paid", False)
    trialend = company.get("trialend")
    inreview = company.get("inreview")
    persendlimit = fix_empty_limit(company.get("persendlimit"))

    log.debug("%s", company)

    localtime = datetime.now(tzoffset("", timedelta(minutes=offset)))

    if localtime.hour < 7:
        localtime = localtime - timedelta(days=1)

    log.debug("%s", localtime)

    domainminlimit: int | None = None
    domainhourlimit: int | None = None
    domaindaylimit: int | None = None

    domainminlimitexact: int | None = None
    domainhourlimitexact: int | None = None
    domaindaylimitexact: int | None = None

    log.debug("domain throttles: %s", domainthrottles)
    for dt in domainthrottles:
        if dt["route"] != route:
            continue

        domainmatch = False
        domainexactmatch = False
        for dp in dt["domainsparsed"]:
            if dp == domain:
                domainexactmatch = True
            if fnmatch(domain, dp):
                domainmatch = True

        dtminlimit = fix_empty_limit(dt.get("minlimit"))
        dthourlimit = fix_empty_limit(dt.get("hourlimit"))
        dtdaylimit = fix_empty_limit(dt.get("daylimit"))

        if domainexactmatch:
            if domainminlimitexact is None or (
                dtminlimit is not None and dtminlimit < domainminlimitexact
            ):
                domainminlimitexact = dtminlimit
            if domainhourlimitexact is None or (
                dthourlimit is not None and dthourlimit < domainhourlimitexact
            ):
                domainhourlimitexact = dthourlimit
            if domaindaylimitexact is None or (
                dtdaylimit is not None and dtdaylimit < domaindaylimitexact
            ):
                domaindaylimitexact = dtdaylimit

        if domainmatch:
            if domainminlimit is None or (
                dtminlimit is not None and dtminlimit < domainminlimit
            ):
                domainminlimit = dtminlimit
            if domainhourlimit is None or (
                dthourlimit is not None and dthourlimit < domainhourlimit
            ):
                domainhourlimit = dthourlimit
            if domaindaylimit is None or (
                dtdaylimit is not None and dtdaylimit < domaindaylimit
            ):
                domaindaylimit = dtdaylimit

    if domainminlimitexact is not None:
        domainminlimit = domainminlimitexact
    if domainhourlimitexact is not None:
        domainhourlimit = domainhourlimitexact
    if domaindaylimitexact is not None:
        domaindaylimit = domaindaylimitexact

    log.debug(
        "domain limits: %s %s %s", domainminlimit, domainhourlimit, domaindaylimit
    )

    if (
        (minlimit is not None and minlimit <= 0)
        or (hourlimit is not None and hourlimit <= 0)
        or (daylimit is not None and daylimit <= 0)
        or (monthlimit is not None and monthlimit <= 0)
        or (domainminlimit is not None and domainminlimit <= 0)
        or (domainhourlimit is not None and domainhourlimit <= 0)
        or (domaindaylimit is not None and domaindaylimit <= 0)
        or company.get("paused", False)
        or company.get("banned", False)
    ):
        log.debug("limit zero or company paused/banned, returning 0")
        return 0

    if inreview:
        log.debug("company in review, returning 0")
        return 0
    if not paid and trialend:
        trialenddate = (
            dateutil.parser.parse(trialend).astimezone(tzutc()).replace(tzinfo=None)
        )
        if trialenddate < datetime.utcnow():
            log.debug("trial ended, returning 0")
            return 0

    rdb = redis_connect()

    minkey = "sendratemin-%s:%s" % (cid, localtime.minute)
    hourkey = "sendratehour-%s:%s" % (cid, localtime.hour)
    daykey = "sendrateday-%s:%s" % (cid, localtime.day)
    monthkey = "sendratemonth-%s:%s" % (cid, localtime.month)
    domainminkey = "sendratemin-%s-%s-%s:%s" % (cid, route, domain, localtime.minute)
    domainhourkey = "sendratehour-%s-%s-%s:%s" % (cid, route, domain, localtime.hour)
    domaindaykey = "sendrateday-%s-%s-%s:%s" % (cid, route, domain, localtime.day)
    limithitkey = "limithit-%s:%s" % (cid, localtime.day)
    creditskey = "credits-%s" % cid
    creditsexpirekey = "credits_expire-%s" % cid

    with rdb.pipeline() as pipe:
        while True:
            try:
                pipe.watch(minkey)
                pipe.watch(hourkey)
                pipe.watch(daykey)
                pipe.watch(monthkey)
                pipe.watch(domainminkey)
                pipe.watch(domainhourkey)
                pipe.watch(domaindaykey)
                if paid:
                    pipe.watch(creditskey)
                    pipe.watch(creditsexpirekey)

                pipemin: bytes | None = cast(bytes | None, pipe.get(minkey))
                pipehour: bytes | None = cast(bytes | None, pipe.get(hourkey))
                pipeday: bytes | None = cast(bytes | None, pipe.get(daykey))
                pipemonth: bytes | None = cast(bytes | None, pipe.get(monthkey))

                mincnt = int(pipemin or 0)
                hourcnt = int(pipehour or 0)
                daycnt = int(pipeday or 0)
                monthcnt = int(pipemonth or 0)
                domainmincnt = None
                if domainminlimit is not None:
                    pipedomainmin = cast(bytes | None, pipe.get(domainminkey))
                    domainmincnt = int(pipedomainmin or 0)
                domainhourcnt = None
                if domainhourlimit is not None:
                    pipedomainhour = cast(bytes | None, pipe.get(domainhourkey))
                    domainhourcnt = int(pipedomainhour or 0)
                domaindaycnt = None
                if domaindaylimit is not None:
                    pipedomainday = cast(bytes | None, pipe.get(domaindaykey))
                    domaindaycnt = int(pipedomainday or 0)
                creditcnt, creditexpirecnt = 0, 0
                if paid:
                    pipecredits: bytes | None = cast(bytes | None, pipe.get(creditskey))
                    pipecreditsexpire: bytes | None = cast(
                        bytes | None, pipe.get(creditsexpirekey)
                    )
                    creditcnt = int(pipecredits or 0)
                    creditexpirecnt = int(pipecreditsexpire or 0)

                daylimitok = daylimit is not None and daycnt < daylimit

                if minlimit is not None and mincnt >= minlimit:
                    log.debug("minlimit %s hit with %s, returning 0", minlimit, mincnt)
                    return 0
                if hourlimit is not None and hourcnt >= hourlimit:
                    log.debug(
                        "hourlimit %s hit with %s, returning 0", hourlimit, hourcnt
                    )
                    return 0
                if daylimit is not None and daycnt >= daylimit:
                    log.debug("daylimit %s hit with %s, returning 0", daylimit, daycnt)
                    return 0
                if monthlimit is not None and monthcnt >= monthlimit:
                    log.debug(
                        "monthlimit %s hit with %s, returning 0", monthlimit, monthcnt
                    )
                    return 0
                if (
                    domainminlimit is not None
                    and domainmincnt is not None
                    and domainmincnt >= domainminlimit
                ):
                    log.debug(
                        "domainminlimit %s hit with %s, returning 0",
                        domainminlimit,
                        domainmincnt,
                    )
                    return 0
                if (
                    domainhourlimit is not None
                    and domainhourcnt is not None
                    and domainhourcnt >= domainhourlimit
                ):
                    log.debug(
                        "domainhourlimit %s hit with %s, returning 0",
                        domainhourlimit,
                        domainhourcnt,
                    )
                    return 0
                if (
                    domaindaylimit is not None
                    and domaindaycnt is not None
                    and domaindaycnt >= domaindaylimit
                ):
                    log.debug(
                        "domaindaylimit %s hit with %s, returning 0",
                        domaindaylimit,
                        domaindaycnt,
                    )
                    return 0
                if paid and (creditcnt + creditexpirecnt) <= 0:
                    log.debug("out of credits, returning 0")
                    return 0

                allowed = 9999999999999
                if minlimit is not None:
                    allowed = min(allowed, minlimit - mincnt)
                if hourlimit is not None:
                    allowed = min(allowed, hourlimit - hourcnt)
                if daylimit is not None:
                    allowed = min(allowed, daylimit - daycnt)
                if monthlimit is not None:
                    allowed = min(allowed, monthlimit - monthcnt)
                if domainminlimit is not None and domainmincnt is not None:
                    allowed = min(allowed, domainminlimit - domainmincnt)
                if domainhourlimit is not None and domainhourcnt is not None:
                    allowed = min(allowed, domainhourlimit - domainhourcnt)
                if domaindaylimit is not None and domaindaycnt is not None:
                    allowed = min(allowed, domaindaylimit - domaindaycnt)
                if paid:
                    allowed = min(allowed, creditcnt + creditexpirecnt)
                if persendlimit is not None:
                    allowed = min(allowed, persendlimit)

                log.debug("requested = %s, allowed = %s", requested, allowed)
                result = min(requested, allowed)

                if paid:
                    creditcnt -= result
                    if creditcnt < 0:
                        creditexpirecnt += creditcnt
                        creditcnt = 0

                log.debug("saving new counts")
                pipe.multi()

                pipe.set(minkey, mincnt + result, 60)
                pipe.set(hourkey, hourcnt + result, 60 * 60)
                pipe.set(daykey, daycnt + result, 60 * 60 * 24)
                pipe.set(monthkey, monthcnt + result, 60 * 60 * 24 * 31)
                if domainmincnt is not None:
                    pipe.set(domainminkey, domainmincnt + result, 60)
                if domainhourcnt is not None:
                    pipe.set(domainhourkey, domainhourcnt + result, 60 * 60)
                if domaindaycnt is not None:
                    pipe.set(domaindaykey, domaindaycnt + result, 60 * 60 * 24)
                if paid:
                    pipe.set(creditskey, creditcnt)
                    pipe.set(creditsexpirekey, creditexpirecnt)

                if daylimitok and daylimit is not None and daycnt + result >= daylimit:
                    pipe.set(
                        limithitkey, datetime.utcnow().isoformat() + "Z", 60 * 60 * 24
                    )

                pipe.execute()

                log.debug("returning %s", result)
                return result
            except redis.WatchError:
                log.debug("redis watch error, retrying")
                continue


unsubheaderre = re.compile(r"\{\{!!unsubheaderlink\}\}")


def fix_headers(h: str) -> str:
    if not h:
        return h

    h = unsubheaderre.sub(
        "{{!!webroot}}/?t=unsub&r={{!!trackingid}}&c={{!!campid}}&u={{!!uid}}", h
    )

    # remove all blank lines, then add an extra line at the end
    t = StringIO()
    for l in h.split("\n"):
        if l.strip():
            t.write("%s\n" % l)
    t.write("\r\n")

    return t.getvalue()


def parse_timeouts(val: str | int) -> List[int]:
    r = []
    for v in str(val).split(","):
        i = -1
        try:
            i = int(v.strip())
        except:
            pass
        if i >= 0:
            r.append(i)
    if not len(r):
        return [0]
    return r


def retry_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504]
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s


def test_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504]
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s


def sink_get_timeout(s: JsonObj, name: str, default: List[int]) -> List[int]:
    if name not in s:
        return default
    if s[name + "type"] == "mins":
        return [t * 60 for t in parse_timeouts(s[name])]
    elif s[name + "type"] == "hours":
        return [t * 60 * 60 for t in parse_timeouts(s[name])]
    return parse_timeouts(s[name])


def sink_get_settings(s: JsonObj, sinkid: str) -> JsonObj:
    custom = {}
    for c in s.get("customwait", []):
        if c["msg"].strip() and not c.get("type", ""):
            custom[c["msg"].strip().lower()] = sink_get_timeout(c, "val", [300])
    transient = {}
    for c in s.get("customwait", []):
        if c["msg"].strip() and c.get("type", "") == "transient":
            transient[c["msg"].strip().lower()] = True
    customnum = {}
    for c in s.get("customnumconns", []):
        if c["mx"].strip():
            customnum[c["mx"].strip().lower()] = c["val"]

    ipsettings = {
        "allips": True,
        "algorithm": "",
        "iplist": {},
        "sendcap": None,
        "captime": "",
        "sendrate": None,
    }

    for sink in s.get("sinks", ()):
        if sink["sink"] == sinkid:
            iplist = {}
            for ip, ips in sink["iplist"].items():
                iplist[ip] = {
                    "minnum": ips["minnum"],
                    "minpct": ips["minpct"],
                    "sendcap": ips.get("sendcap", "") or None,
                    "selected": ips["selected"],
                    "sendrate": ips.get("sendrate", "") or None,
                }
            ipsettings = {
                "allips": sink["allips"],
                "algorithm": sink.get("algorithm", ""),
                "iplist": iplist,
                "sendcap": sink.get("sendcap", "") or None,
                "captime": sink.get("captime", ""),
                "sendrate": sink.get("sendrate", "") or None,
            }
            break

    return {
        "numconns": s.get("numconns", 1),
        "customnumconns": customnum,
        "retryfor": s.get("retryfor", 72),
        "sendsperconn": s.get("sendsperconn", 20),
        "deferwaitsecs": sink_get_timeout(s, "deferwait", [300]),
        "customwaitsecs": custom,
        "connerrwaitsecs": sink_get_timeout(s, "connerrwait", [900]),
        "ipsettings": ipsettings,
        "transient": transient,
        "ratedefer": s.get("ratedefer", False),
        "ratedefercheckmins": s.get("ratedefercheckmins", 10),
        "ratedefertarget": s.get("ratedefertarget", 400),
        "ratedeferwaitsecs": sink_get_timeout(s, "ratedeferwait", [3600]),
    }


def sink_get_ips(s: JsonObj) -> Dict[str, Dict[str, str]]:
    ipdomains = {}
    for d in s.get("ipdata", ()):
        ipdomains[d["ip"]] = {
            "domain": d.get("domain", ""),
            "linkdomain": d.get("linkdomain", ""),
        }
    return ipdomains


def choose_backend(
    route: JsonObj,
    toaddr: str,
    domaingroups: Dict[str, JsonObj],
    policies: Dict[str, JsonObj],
    sinks: Dict[str, JsonObj],
    mailgun: Dict[str, JsonObj],
    ses: Dict[str, JsonObj],
    sparkpost: Dict[str, JsonObj],
    easylink: Dict[str, JsonObj],
    smtprelays: Dict[str, JsonObj],
) -> Tuple[JsonObj | None, str | None]:
    obj = None
    settingsid = None
    for rule in route["published"]["rules"]:
        ok = False
        if not rule["domaingroup"]:
            ok = True
        else:
            dg = domaingroups.get(rule["domaingroup"], None)
            if dg is None:
                continue
            for domain in dg["domains"].split():
                if fnmatch(toaddr.split("@")[1], domain):
                    ok = True
                    break
        if ok:
            pctval = random.randint(0, 99)
            splitpct = 0
            scaled_split_pcts = []

            total_splits = 0
            for i in range(len(rule["splits"])):
                scaled_split_pcts.append(rule["splits"][i]["pct"])
                total_splits += rule["splits"][i]["pct"]

            scaling = 1.0
            if total_splits > 0:
                scaling = 100 / total_splits

            for i in range(len(scaled_split_pcts)):
                scaled_split_pcts[i] = scaled_split_pcts[i] * scaling

            for i in range(len(rule["splits"])):
                split = rule["splits"][i]

                splitpct += scaled_split_pcts[i]
                if len(rule["splits"]) > 1 and splitpct < pctval:
                    continue
                if not split["policy"]:
                    continue
                policy = policies.get(split["policy"], None)
                if policy is None:
                    mg = mailgun.get(split["policy"], None)
                    if mg is None:
                        s = ses.get(split["policy"], None)
                        if s is None:
                            s = sparkpost.get(split["policy"], None)
                            if s is None:
                                s = easylink.get(split["policy"], None)
                                if s is None:
                                    s = smtprelays.get(split["policy"], None)
                                    if s is None:
                                        continue
                                    else:
                                        return s, "smtprelay"
                                else:
                                    return s, "easylink"
                            else:
                                return s, "sparkpost"
                        else:
                            return s, "ses"
                    else:
                        return mg, "mailgun"
                policy = policy.get("published", None)
                if policy is None:
                    continue

                ok = False
                for domain in policy["domains"].split():
                    if fnmatch(toaddr.split("@")[1], domain):
                        ok = True
                        break

                if ok:
                    sinkobjs = [
                        sinks.get(sink["sink"], None) for sink in policy["sinks"]
                    ]
                    sinkpctval = random.randint(0, 99)
                    sinksplitpct = 0

                    scaled_sink_pcts = []
                    total_sinks = 0
                    for i in range(len(policy["sinks"])):
                        scaled_sink_pcts.append(policy["sinks"][i]["pct"])
                        total_sinks += policy["sinks"][i]["pct"]

                    scaling = 1
                    if total_sinks > 0:
                        scaling = 100 / total_sinks

                    for i in range(len(scaled_sink_pcts)):
                        scaled_sink_pcts[i] = scaled_sink_pcts[i] * scaling

                    for i in range(len(policy["sinks"])):
                        sinkobj = None
                        if i < len(sinkobjs):
                            sinkobj = sinkobjs[i]

                        if sinkobj is not None:
                            sinksplitpct += scaled_sink_pcts[i]
                            if len(policy["sinks"]) == 1 or sinksplitpct >= sinkpctval:
                                obj = sinkobj
                                settingsid = split["policy"]
                                break

                if obj is not None:
                    break

        if obj is not None:
            break

    return obj, settingsid


def possible_backend_types(
    route: JsonObj,
    toaddr: str,
    domaingroups: Dict[str, JsonObj],
    policies: Dict[str, JsonObj],
    sinks: Dict[str, JsonObj],
    mailgun: Dict[str, JsonObj],
    ses: Dict[str, JsonObj],
    sparkpost: Dict[str, JsonObj],
    easylink: Dict[str, JsonObj],
    smtprelays: Dict[str, JsonObj],
) -> Set[str]:
    backend_types: Set[str] = set()
    domain = toaddr.split("@")[1]

    for rule in route["published"]["rules"]:
        rule_matches = False
        if not rule["domaingroup"]:
            rule_matches = True
        else:
            dg = domaingroups.get(rule["domaingroup"], None)
            if dg is None:
                continue
            for dg_domain in dg["domains"].split():
                if fnmatch(domain, dg_domain):
                    rule_matches = True
                    break
        if not rule_matches:
            continue

        rule_backend_types: Set[str] = set()
        for split in rule["splits"]:
            policyid = split.get("policy")
            if not policyid:
                continue
            if policyid in mailgun:
                rule_backend_types.add("mailgun")
                continue
            if policyid in ses:
                rule_backend_types.add("ses")
                continue
            if policyid in sparkpost:
                rule_backend_types.add("sparkpost")
                continue
            if policyid in easylink:
                rule_backend_types.add("easylink")
                continue
            if policyid in smtprelays:
                rule_backend_types.add("smtprelay")
                continue

            policy = policies.get(policyid, None)
            if policy is None or policy.get("published", None) is None:
                continue
            published = policy["published"]
            policy_matches = False
            for policy_domain in published["domains"].split():
                if fnmatch(domain, policy_domain):
                    policy_matches = True
                    break
            if not policy_matches:
                continue

            for sink in published["sinks"]:
                if sink["sink"] in sinks:
                    rule_backend_types.add("sink")

        if rule_backend_types:
            return rule_backend_types

    return backend_types


def route_backend_types_for_recipient(db: DB, route: JsonObj, toaddr: str) -> Set[str]:
    domaingroups = {}
    policies = {}
    sinks = {}
    mailgun = {}
    ses = {}
    sparkpost = {}
    easylink = {}
    smtprelays = {}
    oldcid = db.get_cid()
    db.set_cid(route["cid"])
    try:
        for dg in db.domaingroups.find():
            domaingroups[dg["id"]] = dg
        for p in db.policies.find():
            policies[p["id"]] = p
        for s in db.sinks.find():
            sinks[s["id"]] = s
        for m in db.mailgun.find():
            mailgun[m["id"]] = m
        for s in db.ses.find():
            ses[s["id"]] = s
        for s in db.sparkpost.find():
            sparkpost[s["id"]] = s
        for s in db.easylink.find():
            easylink[s["id"]] = s
        for s in db.smtprelays.find():
            smtprelays[s["id"]] = s
        return possible_backend_types(
            route,
            toaddr,
            domaingroups,
            policies,
            sinks,
            mailgun,
            ses,
            sparkpost,
            easylink,
            smtprelays,
        )
    finally:
        db.set_cid(oldcid)


def get_frontend_params(
    db: DB, usercid: str
) -> Tuple[bool, str, str, str, str, str, bool]:
    demo = False
    imagebucket = os.environ["s3_imagebucket"]
    bodydomain = ""
    headers = ""
    fromencoding = "none"
    subjectencoding = "none"
    usedkim = True
    company = db.companies.get(usercid)
    if company is not None:
        frontend = json_obj(
            db.row(
                "select id, cid, data - 'image' from frontends where id = %s",
                company["frontend"],
            )
        )
        if frontend is not None:
            bodydomain = frontend.get("bodydomain", "")
            headers = fix_headers(frontend.get("headers", ""))
            fromencoding = frontend.get("fromencoding", "")
            subjectencoding = frontend.get("subjectencoding", "")
            usedkim = frontend.get("usedkim", True)
        parentcompany = db.companies.get(company["cid"])
        if parentcompany is not None:
            demo = parentcompany.get("demo", False)
            imagebucket = parentcompany.get("s3_imagebucket", imagebucket)

    return (
        demo,
        imagebucket,
        bodydomain,
        headers,
        fromencoding,
        subjectencoding,
        usedkim,
    )


def get_settings(
    db: DB, obj: JsonObj
) -> Tuple[JsonObj, List[JsonObj], Dict[str, JsonObj], JsonObj, Set[str], Set[str]]:
    mtasettings = {}
    pauses = []
    warmups = {}
    dkim = {}
    allips: Set[str] = set()
    allsinks: Set[str] = set()
    oldcid = db.get_cid()
    db.set_cid(obj["cid"])
    try:
        for policy in db.policies.find():
            if policy.get("published", None) is not None:
                mtasettings[policy["id"]] = sink_get_settings(
                    policy["published"], obj["id"]
                )
        pauses = list(db.ippauses.find({"sinkid": obj["id"]}))
        for warmup in db.warmups.find({"sink": obj["id"]}):
            if warmup.get("published", None) is not None:
                warmups[warmup["id"]] = warmup["published"]
                warmups[warmup["id"]]["disabled"] = warmup.get("disabled", False)
        for sink in db.sinks.find():
            allips.update(d["ip"] for d in sink["ipdata"])
            allsinks.add(sink["id"])
        dkim = db.dkimentries.get_singleton()
    finally:
        db.set_cid(oldcid)

    return mtasettings, pauses, warmups, dkim, allips, allsinks


def update_sink_camp(db: DB, sinkid: str, camp: JsonObj, html: str) -> None:
    obj = db.sinks.get(sinkid)
    if obj is None:
        return

    demo, imagebucket, bodydomain, headers, fromencoding, subjectencoding, usedkim = (
        get_frontend_params(db, camp["cid"])
    )

    if demo:
        return

    mtasettings, pauses, warmups, dkim, allips, allsinks = get_settings(db, obj)

    fromemail = camp.get("returnpath") or camp["fromemail"]

    frm = formataddr(
        (
            remove_newlines(camp["fromname"]),
            remove_newlines(camp.get("fromemail") or camp["returnpath"]),
        )
    )

    if camp.get("replyto", ""):
        replyto = remove_newlines(camp["replyto"])
    else:
        replyto = remove_newlines(camp.get("fromemail") or camp["returnpath"])

    subject = remove_newlines(camp["subject"])

    url = fix_sink_url(obj["url"])

    r = requests.post(
        url + "/settings",
        json={
            "accesskey": obj["accesskey"],
            "sinkid": obj["id"],
            "mtasettings": mtasettings,
            "ippauses": pauses,
            "warmups": warmups,
            "allips": list(allips),
            "allsinks": list(allsinks),
            "ipdomains": sink_get_ips(obj),
            "dkim": dkim,
        },
        timeout=MTA_TIMEOUT,
    )
    r.raise_for_status()

    r = requests.post(
        url + "/update",
        json={
            "id": camp["id"],
            "accesskey": obj["accesskey"],
            "bodydomain": bodydomain,
            "fromencoding": fromencoding,
            "subjectencoding": subjectencoding,
            "usedkim": usedkim,
            "from": frm,
            "returnpath": fromemail,
            "replyto": replyto,
            "subject": subject,
            "template": html,
        },
        timeout=MTA_TIMEOUT,
    )
    r.raise_for_status()


def send_backend_mail(
    db: DB,
    usercid: str,
    route: JsonObj,
    html: str,
    fromaddr: str,
    returnpath: str,
    fromdomain: str,
    replyto: str,
    to: str,
    toaddr: str,
    subject: str,
    campid: str = "test",
    toname: str | None = None,
    attachments: List[AttachmentManifest] | None = None,
    raise_err: bool = False,
) -> bool:
    demo, imagebucket, bodydomain, headers, fromencoding, subjectencoding, usedkim = (
        get_frontend_params(db, usercid)
    )

    if demo:
        return True

    domaingroups = {}
    policies = {}
    sinks = {}
    mailgun = {}
    ses = {}
    sparkpost = {}
    easylink = {}
    smtprelays = {}
    oldcid = db.get_cid()
    db.set_cid(route["cid"])
    try:
        for dg in db.domaingroups.find():
            domaingroups[dg["id"]] = dg
        for p in db.policies.find():
            policies[p["id"]] = p
        for s in db.sinks.find():
            sinks[s["id"]] = s
        for m in db.mailgun.find():
            mailgun[m["id"]] = m
        for s in db.ses.find():
            ses[s["id"]] = s
        for s in db.sparkpost.find():
            sparkpost[s["id"]] = s
        for s in db.easylink.find():
            easylink[s["id"]] = s
        for s in db.smtprelays.find():
            smtprelays[s["id"]] = s
        obj, settingsid = choose_backend(
            route,
            toaddr,
            domaingroups,
            policies,
            sinks,
            mailgun,
            ses,
            sparkpost,
            easylink,
            smtprelays,
        )
    finally:
        db.set_cid(oldcid)

    if obj is None:
        raise Exception(
            "You must delete Drop All Mail from your postal route to send this message"
        )

    if attachments and settingsid != "ses":
        raise Exception(
            "Transactional attachments are only supported for SES postal routes"
        )

    if settingsid == "mailgun":
        clientdomain = db.single(
            "select data->>'name' from clientdkim where data->>'name' = %s and cid = %s and data->'mgentry' is not null and data->'mgentry'->>'id' = %s",
            fromdomain,
            usercid,
            obj["id"],
        )

        mailgun_send(
            obj,
            clientdomain,
            fromaddr,
            replyto,
            subject,
            html,
            campid,
            usercid,
            True,
            recips=[{"Email": toaddr, "!!to": to}],
            sync=True,
            raise_err=raise_err,
        )
        return False
    elif settingsid == "ses":
        ses_send(
            obj,
            fromaddr,
            replyto,
            subject,
            html,
            campid,
            usercid,
            True,
            recips=[{"Email": toaddr, "!!to": to}],
            sync=True,
            attachments=attachments,
            raise_err=raise_err,
        )
        return False
    elif settingsid == "sparkpost":
        sparkpost_send(
            obj,
            fromaddr,
            replyto,
            subject,
            html,
            campid,
            usercid,
            True,
            recips=[{"Email": toaddr, "!!to": to}],
            sync=True,
            raise_err=raise_err,
        )
        return False
    elif settingsid == "easylink":
        easylink_send(
            obj,
            fromaddr,
            replyto,
            subject,
            html,
            campid,
            usercid,
            True,
            recips=[{"Email": toaddr, "!!to": to}],
            sync=True,
            raise_err=raise_err,
        )
        return True
    elif settingsid == "smtprelay":
        smtprelay_send(
            obj,
            fromaddr,
            replyto,
            subject,
            html,
            campid,
            usercid,
            True,
            recips=[{"Email": toaddr, "!!to": to}],
            sync=True,
            raise_err=raise_err,
        )
        return True
    else:
        url = fix_sink_url(obj["url"])

        if obj.get("failed_update", False):
            mtasettings, pauses, warmups, dkim, allips, allsinks = get_settings(db, obj)

            r = requests.post(
                url + "/settings",
                json={
                    "accesskey": obj["accesskey"],
                    "sinkid": obj["id"],
                    "mtasettings": mtasettings,
                    "ippauses": pauses,
                    "warmups": warmups,
                    "allips": list(allips),
                    "allsinks": list(allsinks),
                    "ipdomains": sink_get_ips(obj),
                    "dkim": dkim,
                },
                timeout=MTA_TIMEOUT,
            )
            r.raise_for_status()

            db.sinks.patch(obj["id"], {"failed_update": False})

        if campid == "test":
            r = requests.post(
                url + "/send-addr",
                json={
                    "id": campid,
                    "from": fromaddr,
                    "returnpath": returnpath,
                    "replyto": replyto,
                    "subject": subject,
                    "accesskey": obj["accesskey"],
                    "template": html,
                    "to": to,
                    "email": toaddr,
                    "settingsid": settingsid,
                    "bodydomain": bodydomain,
                    "headers": headers,
                    "fromencoding": fromencoding,
                    "subjectencoding": subjectencoding,
                    "usedkim": usedkim,
                    "cid": usercid,
                },
                timeout=MTA_TIMEOUT,
            )
        else:
            domain = toaddr.split("@")[1]

            listdata = BytesIO()

            props = ["Email"]
            row = {"Email": toaddr}

            if toname:
                props.append("First Name")
                row["First Name"] = toname

            w = MPDictWriter(listdata, props)
            w.writeheader()
            w.writerow(row)

            r = requests.post(
                url + "/send-lists",
                json={
                    "id": campid,
                    "sendid": shortuuid.uuid(),
                    "domaincounts": {domain: 1},
                    "from": fromaddr,
                    "returnpath": returnpath,
                    "replyto": replyto,
                    "subject": subject,
                    "accesskey": obj["accesskey"],
                    "template": html,
                    "listdata": base64.b64encode(listdata.getvalue()).decode("ascii"),
                    "settingsid": settingsid,
                    "bodydomain": bodydomain,
                    "headers": headers,
                    "fromencoding": fromencoding,
                    "subjectencoding": subjectencoding,
                    "usedkim": usedkim,
                },
                timeout=MTA_TIMEOUT,
            )
        r.raise_for_status()

        return False


varre = re.compile(r"{{([^}]+)}}")
defflagre = re.compile(r"\s*default\s*=(.+)")

randchars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcdefghijklmnopqrstuvwxyz"

replacements = {
    "@aol.com": "!",
    "@aim.com": "#",
    "@gmail.com": "^",
    "@googlemail.com": ":",
    "@yahoo.com": "&",
    "@yahoo.co.uk": "*",
    "@rocketmail.com": "?",
    "@hotmail.com": "(",
    "@hotmail.co.uk": ")",
    "@live.com": "~",
    "@comcast.net": "{",
    "@att.net": "}",
    "@sbcglobal.net": "[",
    "@verizon.net": "]",
    "@charter.net": ",",
    "@cox.net": "|",
    "@earthlink.net": "<",
    "@bellsouth.net": ">",
}

emailwordre = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def encrypt(s: str | bytes) -> str:
    if isinstance(s, bytes):
        s = s.decode("utf-8")

    for k, v in replacements.items():
        s = s.replace(k, v)

    r = bytearray()
    r.append(random.randint(1, 253))
    for c in s.encode("utf-8"):
        r.append(c ^ r[0])

    return base64.urlsafe_b64encode(r).decode("utf-8").strip("=")


def unencrypt(s: str | bytes) -> str | None:
    try:
        if isinstance(s, bytes):
            s = s.decode("utf-8")
        padding = (4 - (len(s) % 4)) % 4
        b = base64.urlsafe_b64decode(s + ("=" * padding))
        key = b[0]
        s = "".join(chr(a ^ key) for a in b[1:])
        for k, v in replacements.items():
            s = s.replace(v, k)
        m = emailwordre.search(s)
        if not m:
            return None
        return m.group(0)
    except:
        return None


def setup_ses_webhooks(ses: JsonObj) -> Any:
    hook = "%s/api/seswebhook" % get_webroot()
    proto = get_webscheme()

    snsclient = boto3.client(
        "sns",
        region_name=ses["region"].strip(),
        aws_access_key_id=ses["access"].strip(),
        aws_secret_access_key=ses["secret"].strip(),
    )

    arn = snsclient.create_topic(Name="edcom-ses-%s" % ses["id"])["TopicArn"]

    found = False
    for sublist in snsclient.get_paginator("list_subscriptions_by_topic").paginate(
        TopicArn=arn
    ):
        for sub in sublist["Subscriptions"]:
            if sub["Protocol"] == proto and sub["Endpoint"] == hook:
                found = True
                break
        if found:
            break
    if not found:
        snsclient.subscribe(TopicArn=arn, Protocol=proto, Endpoint=hook)

    sesclient = boto3.client(
        "ses",
        region_name=ses["region"].strip(),
        aws_access_key_id=ses["access"].strip(),
        aws_secret_access_key=ses["secret"].strip(),
    )

    topics = sesclient.get_identity_notification_attributes(Identities=[ses["domain"]])[
        "NotificationAttributes"
    ]
    if ses["domain"] not in topics or topics[ses["domain"]].get("BounceTopic") != arn:
        sesclient.set_identity_notification_topic(
            Identity=ses["domain"], NotificationType="Bounce", SnsTopic=arn
        )
    if (
        ses["domain"] not in topics
        or topics[ses["domain"]].get("ComplaintTopic") != arn
    ):
        sesclient.set_identity_notification_topic(
            Identity=ses["domain"], NotificationType="Complaint", SnsTopic=arn
        )
    if ses["domain"] not in topics or topics[ses["domain"]].get("DeliveryTopic") != arn:
        sesclient.set_identity_notification_topic(
            Identity=ses["domain"], NotificationType="Delivery", SnsTopic=arn
        )

    return sesclient


def sparkpost_domain(sp: JsonObj) -> str:
    if sp.get("region", "") == "eu":
        return "https://api.eu.sparkpost.com/api/v1"
    else:
        return "https://api.sparkpost.com/api/v1"


def setup_sparkpost_webhooks(sp: JsonObj) -> None:
    hook = "%s/api/spwebhook" % get_webroot()

    r = requests.get(
        f"{sparkpost_domain(sp)}/webhooks",
        headers={"Authorization": sp["apikey"], "Content-Type": "application/json"},
    )
    handle_sp_error(r)

    if "results" not in r.json():
        raise Exception("Error setting up webhooks")

    exist = r.json()["results"]

    found = False
    names = set()
    for e in exist:
        if e["target"] == hook:
            found = True
        names.add(e["name"])

    if not found:
        unique_name = get_webhost()[:24]
        i = 2
        while unique_name in names:
            unique_name = f"{unique_name[:-5]} ({i})"
            i += 1

        r = requests.post(
            f"{sparkpost_domain(sp)}/webhooks",
            headers={"Authorization": sp["apikey"], "Content-Type": "application/json"},
            json={
                "name": unique_name,
                "target": hook,
                "events": ["delivery", "bounce", "spam_complaint", "delay"],
            },
        )
        handle_sp_error(r)


def mg_domain(mg: JsonObj) -> str:
    if mg.get("region", "") == "eu":
        return "https://api.eu.mailgun.net"
    else:
        return "https://api.mailgun.net"


def setup_mg_webhooks(mg: JsonObj, domain: str) -> None:
    hook = "%s/api/mgwebhook" % get_webroot()

    e = requests.get(
        f"{mg_domain(mg)}/v3/domains/{domain}/webhooks", auth=("api", mg["apikey"])
    )
    handle_mg_error(e)

    exist = e.json()["webhooks"]

    for t in ("delivered", "permanent_fail", "temporary_fail", "complained"):
        if t not in exist:
            handle_mg_error(
                requests.post(
                    f"{mg_domain(mg)}/v3/domains/{domain}/webhooks",
                    auth=("api", mg["apikey"]),
                    data={
                        "id": t,
                        "url": [hook],
                    },
                )
            )
        elif hook not in exist[t]["urls"]:
            handle_mg_error(
                requests.put(
                    f"{mg_domain(mg)}/v3/domains/{domain}/webhooks/{t}",
                    auth=("api", mg["apikey"]),
                    data={
                        "url": hook,
                    },
                )
            )


@tasks.task(priority=LOW_PRIORITY)
def do_ses_send_task(
    ses: JsonObj,
    frm: str,
    replyto: str,
    campid: str,
    campcid: str,
    is_camp: bool,
    recips: Iterable[JsonObj] | None,
    recipkey: str | None,
    othervars: Dict[str, Tuple[str, str]],
    write_err: bool,
    htmlkey: str,
    subject: str,
    attachments: List[AttachmentManifest] | None,
    raise_err: bool,
) -> None:
    do_ses_send(
        ses,
        frm,
        replyto,
        campid,
        campcid,
        is_camp,
        recips,
        recipkey,
        othervars,
        write_err,
        htmlkey,
        subject,
        attachments,
        raise_err,
    )


def do_ses_send(
    ses: JsonObj,
    frm: str,
    replyto: str,
    campid: str,
    campcid: str,
    is_camp: bool,
    recips: Iterable[JsonObj] | None,
    recipkey: str | None,
    othervars: Dict[str, Tuple[str, str]],
    write_err: bool,
    htmlkey: str,
    subject: str,
    attachments: List[AttachmentManifest] | None,
    raise_err: bool,
) -> None:
    stream = None
    try:
        try:
            data = s3_read(os.environ["s3_transferbucket"], htmlkey)
        except:
            return

        html = data.decode("utf-8")
        s3_delete(os.environ["s3_transferbucket"], htmlkey)

        with open_db() as db:
            clargs = dict(
                region_name=ses["region"].strip(),
                aws_access_key_id=ses["access"].strip(),
                aws_secret_access_key=ses["secret"].strip(),
            )
            sesclient = boto3.client("ses", **clargs)

            if recipkey is not None:
                stream = s3_read_stream(os.environ["s3_transferbucket"], recipkey)
                recips = MPDictReader(stream)

            if recips is None:
                recips = []

            attachment_storage = None
            if attachments:
                attachment_storage = build_attachment_storage(get_attachment_config())

            for r in recips:
                if "!!to" in r:
                    to = r["!!to"]
                else:
                    name = (
                        "%s %s" % (r.get("First Name", ""), r.get("Last Name", ""))
                    ).strip()
                    if name:
                        to = formataddr((name, r["Email"]))
                    else:
                        to = r["Email"]
                replace = {
                    "__trackingid": shortuuid.uuid(),
                    "__uid": encrypt(r["Email"]),
                    "__to": to,
                    "Email": r["Email"],
                }
                for v, vals in othervars.items():
                    lookup, defval = vals
                    if v == "!!rand":
                        replace["__rand"] = "".join(
                            random.choice(randchars) for _ in range(9)
                        )
                    else:
                        replace[v.replace(" ", "-").replace("!", "_")] = (
                            r.get(lookup) or defval
                        )

                def rf(m: re.Match[str]) -> str:
                    return cast(str, replace.get(m.group(1), ""))

                htmlreplaced = varre.sub(rf, html)
                subjectreplaced = varre.sub(rf, subject)
                trackingid = replace["__trackingid"]

                error = None
                kwargs = dict(
                    Source=frm,
                    ReplyToAddresses=[replyto],
                    Message={
                        "Subject": {
                            "Data": subjectreplaced,
                        },
                        "Body": {
                            "Html": {
                                "Data": htmlreplaced,
                            }
                        },
                    },
                    Destination={
                        "ToAddresses": [to],
                    },
                )
                try:
                    if attachments and attachment_storage is not None:
                        log.info(
                            "sending SES raw transactional message with %s attachments cid=%s campid=%s",
                            len(attachments),
                            campcid,
                            campid,
                        )
                        raw_message = build_raw_mime_message(
                            frm,
                            replyto,
                            to,
                            subjectreplaced,
                            htmlreplaced,
                            attachment_storage,
                            attachments,
                        )
                        status = sesclient.send_raw_email(
                            Source=frm,
                            Destinations=[to],
                            RawMessage={"Data": raw_message},
                        )
                    else:
                        status = sesclient.send_email(**kwargs)
                except Exception as e:
                    error = str(e)

                ts = datetime.utcnow()
                if campid == "test":
                    if error:
                        raise Exception(error)
                    else:
                        db.execute(
                            "insert into sesmessages (id, settingsid, cid, campid, is_camp, trackingid, ts) values (%s, %s, %s, %s, %s, %s, %s)",
                            status["MessageId"],
                            ses["id"],
                            campcid,
                            campid,
                            True,
                            trackingid,
                            ts,
                        )
                else:
                    domain = r["Email"].split("@")[1]

                    if error is None:
                        db.execute(
                            "insert into sesmessages (id, settingsid, cid, campid, is_camp, trackingid, ts) values (%s, %s, %s, %s, %s, %s, %s)",
                            status["MessageId"],
                            ses["id"],
                            campcid,
                            campid,
                            is_camp,
                            trackingid,
                            ts,
                        )
                    else:
                        log.error("SES Error: %s", error)
                        handle_soft_event(
                            db, r["Email"], campid, campcid, is_camp, error
                        )
                        incr_stats(
                            db,
                            0,
                            1,
                            campid,
                            is_camp,
                            ses["cid"],
                            campcid,
                            domain,
                            "ses",
                            ses["id"],
                        )
                        if raise_err:
                            raise Exception(error)
    except Exception as e:
        if write_err:
            with open_db() as db:
                db.campaigns.patch(
                    campid,
                    {
                        "finished_at": datetime.utcnow().isoformat() + "Z",
                        "error": str(e),
                    },
                )
        log.exception("error")
        if campid == "test" or raise_err:
            raise
    finally:
        if stream is not None:
            stream.close()


def mime_word(headername: str, s: str) -> str:
    is_unicode = False
    try:
        s.encode("ascii")
    except:
        is_unicode = True

    if len(s) + len(headername) + 2 > 76 or is_unicode:
        return Header(s, "utf-8", header_name=headername, maxlinelen=76).encode(
            linesep="\n"
        )
    return s


def add_test_log(db: DB, cid: str, email: str, msg: str) -> None:
    db.set_cid(cid)
    db.testlogs.add(
        {
            "to": email,
            "ts": datetime.now().isoformat() + "Z",
            "msg": msg,
        }
    )
    db.execute(
        "delete from testlogs where cid = %s and id not in (select id from testlogs where cid = %s order by data->>'ts' desc limit 12)",
        cid,
        cid,
    )
    db.set_cid(None)


def handle_soft_event(
    db: DB, email: str, c: str, campcid: str, is_camp: bool, msg: str | None
) -> None:
    code = msg
    if not code:
        code = None
    webhookev: JsonObj = {
        "type": "bounce",
        "email": email,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "code": code,
        "bouncetype": "soft",
    }
    if c.startswith("tx-"):
        campcid = c[3:]
        webhookev["source"] = {"tag": campcid}
        send_webhooks(db, campcid, [webhookev])
    else:
        db.execute(
            """insert into camplogs (campid, email, cmd, ts, code) values (%s, %s, 'soft', %s, %s)
                      on conflict (campid, email, cmd) do nothing""",
            c,
            email,
            datetime.utcnow(),
            code,
        )

        upd = {
            "email": email,
            "cmd": "soft",
            "campid": c,
        }
        contacts.update(db, campcid, upd)

        if is_camp:
            webhookev["source"] = {"broadcast": c}
        else:
            webhookev["source"] = {"funnelmsg": c}
        send_webhooks(db, campcid, [webhookev])


def incr_stats(
    db: DB,
    send: int,
    soft: int,
    campid: str,
    is_camp: bool,
    cid: str,
    campcid: str,
    domain: str,
    sinkid: str,
    settingsid: str,
) -> None:
    ts = datetime.utcnow()
    if len(campid) <= 30:
        if is_camp:
            db.execute(
                """update campaigns set data = data || jsonb_build_object('delivered', (data->>'delivered')::int + %s,
                                                                                    'send', (data->>'send')::int + %s,
                                                                                    'soft', (data->>'soft')::int + %s) where id = %s""",
                send,
                send,
                soft,
                campid,
            )
        else:
            db.execute(
                """update messages set data = data || jsonb_build_object('delivered', (data->>'delivered')::int + %s,
                                                                                'send', (data->>'send')::int + %s,
                                                                                'soft', (data->>'soft')::int + %s) where id = %s""",
                send,
                send,
                soft,
                campid,
            )
    else:
        campcidtmp, txntag, _ = get_txn(db, campid)
        if campcidtmp is None:
            return
        campcid = campcidtmp
        campid = "tx-%s" % campcid
    db.execute(
        """insert into hourstats (id, cid, campcid, ts, sinkid, domaingroupid, ip, settingsid, campid,
                    complaint, unsub, open, click, send, soft, hard, err, defercnt)
                    values (%s, %s, %s, date_trunc('hour', %s), %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict on constraint hourstats_uniq do update set
                    complaint = hourstats.complaint + excluded.complaint,
                    unsub =     hourstats.unsub     + excluded.unsub,
                    open =      hourstats.open      + excluded.open,
                    click =     hourstats.click     + excluded.click,
                    send =      hourstats.send      + excluded.send,
                    soft =      hourstats.soft      + excluded.soft,
                    hard =      hourstats.hard      + excluded.hard,
                    err =       hourstats.err       + excluded.err,
                    defercnt =  hourstats.defercnt  + excluded.defercnt""",
        shortuuid.uuid(),
        cid,
        campcid,
        ts,
        sinkid,
        domain,
        "pool",
        settingsid,
        campid,
        0,
        0,
        0,
        0,
        send,
        soft,
        0,
        0,
        0,
    )
    if campid.startswith("tx-"):
        db.execute(
            """insert into txnstats (id, cid, ts, tag, domain,
                        complaint, unsub, open, click, send, soft, hard, open_all, click_all)
                        values (%s, %s, date_trunc('hour', %s), %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        on conflict (ts, cid, tag, domain) do update set
                        complaint = txnstats.complaint + excluded.complaint,
                        unsub =     txnstats.unsub     + excluded.unsub,
                        open =      txnstats.open      + excluded.open,
                        click =     txnstats.click     + excluded.click,
                        send =      txnstats.send      + excluded.send,
                        soft =      txnstats.soft      + excluded.soft,
                        hard =      txnstats.hard      + excluded.hard,
                        open_all =  txnstats.open_all  + excluded.open_all,
                        click_all = txnstats.click_all + excluded.click_all
                        """,
            shortuuid.uuid(),
            campcid,
            ts,
            txntag,
            domain,
            0,
            0,
            0,
            0,
            send,
            soft,
            0,
            0,
            0,
        )


def link_webroot(obj: JsonObj) -> str:
    webhost = get_webhost().lower()
    linkdomain = domain_only(obj.get("linkdomain", "").strip().lower()) or webhost
    webroot = get_webroot().lower()

    # if linkdomain == webhost and webroot is https, or if linkdomain has SSL enabled on the platform, then use https
    if (
        webroot.startswith("https:") and linkdomain == webhost
    ) or linkdomain in ssl_enabled_domains:
        return f"https://{linkdomain}"
    else:  # otherwise use http://
        return f"http://{linkdomain}"


@tasks.task(priority=LOW_PRIORITY)
def do_smtprelay_send_task(
    smtp: JsonObj,
    frm: str,
    replyto: str,
    campid: str,
    campcid: str,
    is_camp: bool,
    recips: Iterable[JsonObj] | None,
    recipkey: str | None,
    othervars: Dict[str, Tuple[str, str]],
    write_err: bool,
    htmlkey: str,
    subject: str,
    raise_err: bool,
) -> None:
    do_smtprelay_send(
        smtp,
        frm,
        replyto,
        campid,
        campcid,
        is_camp,
        recips,
        recipkey,
        othervars,
        write_err,
        htmlkey,
        subject,
        raise_err,
    )


class SMTPRelayState(TypedDict):
    conn: smtplib.SMTP | smtplib.SMTP_SSL | None
    conn_sent: int


def do_smtprelay_send(
    smtp: JsonObj,
    frm: str,
    replyto: str,
    campid: str,
    campcid: str,
    is_camp: bool,
    recips: Iterable[JsonObj] | None,
    recipkey: str | None,
    othervars: Dict[str, Tuple[str, str]],
    write_err: bool,
    htmlkey: str,
    subject: str,
    raise_err: bool,
) -> None:
    linkwebroot = link_webroot(smtp)

    with open_db() as db:
        stream = None
        sent_emails = []
        try:
            try:
                data = s3_read(os.environ["s3_transferbucket"], htmlkey)
            except:
                return

            html = data.decode("utf-8")
            s3_delete(os.environ["s3_transferbucket"], htmlkey)

            if recipkey is not None:
                stream = s3_read_stream(os.environ["s3_transferbucket"], recipkey)
                recips = MPDictReader(stream)

            assert recips is not None

            tolist: List[JsonObj] = []

            _, fromaddr = parseaddr(frm)

            headers = re.sub("\n\n", "\n", smtp["headers"].strip())
            if headers:
                headers = "\r\n" + headers

            state: SMTPRelayState = {"conn": None, "conn_sent": 0}

            def open_conn() -> None:
                if state["conn"] is not None:
                    return

                cls: Type[smtplib.SMTP] | Type[smtplib.SMTP_SSL]
                if smtp["ssltype"] == "ssl":
                    cls = smtplib.SMTP_SSL
                else:
                    cls = smtplib.SMTP

                newconn = cls(
                    host=smtp["hostname"].strip(),
                    port=smtp["port"],
                    local_hostname=smtp["ehlohostname"].strip(),
                    timeout=10,
                )

                if smtp["ssltype"] == "starttls":
                    newconn.starttls()

                if smtp["useauth"]:
                    newconn.login(smtp["username"].strip(), smtp["password"])

                state["conn"] = newconn
                state["conn_sent"] = 0

            def do_send() -> None:
                for info in tolist:
                    send = 0
                    soft = 0
                    error = None
                    trackingid = info["trackingid"]

                    try:
                        msg = BytesIO()

                        msg.write(
                            f"""From: {mime_word('From', frm)}
Reply-To: {mime_word('Reply-To', replyto)}
To: {mime_word('To', info['to'])}
Subject: {mime_word('Subject', info['subject'])}
Date: {formatdate()}
MIME-Version: 1.0
Content-Type: text/html; charset="utf-8"
Content-Transfer-Encoding: quoted-printable
Message-ID: <{trackingid}@{smtp['ehlohostname'].strip()}>
List-Unsubscribe: {mime_word('List-Unsubscribe', f'<{linkwebroot}/l?t=unsub&r={trackingid}&c={campid}&u={encrypt(info["address"])}>')}
List-Unsubscribe-Post: List-Unsubscribe=One-Click{headers}

""".replace(
                                "\n", "\r\n"
                            ).encode(
                                "ascii"
                            )
                        )

                        msg.write(
                            quopri.encodestring(info["html"].encode("utf-8")).replace(
                                b"\n", b"\r\n"
                            )
                        )

                        open_conn()

                        assert state["conn"] is not None

                        state["conn"].sendmail(
                            fromaddr, info["address"], msg.getvalue()
                        )

                        state["conn_sent"] += 1
                        send += 1
                    except Exception as e:
                        log.error("SMTP Relay Error: %s", e)
                        if campid == "test":
                            add_test_log(db, campcid, info["address"], str(e))
                            raise
                        else:
                            soft += 1
                            error = str(e)
                            handle_soft_event(
                                db, info["address"], campid, campcid, is_camp, error
                            )

                    if (
                        smtp["msgsperconn"]
                        and state["conn_sent"] >= smtp["msgsperconn"]
                    ):
                        try:
                            assert state["conn"] is not None
                            state["conn"].quit()
                        except:
                            pass
                        state["conn"] = None

                    if campid == "test":
                        add_test_log(db, campcid, info["address"], "Success")
                    else:
                        ts = datetime.utcnow()
                        domain = info["address"].split("@")[1]
                        db.execute(
                            "insert into smtptracking (id, settingsid, ts) values (%s, %s, %s)",
                            trackingid,
                            smtp["id"],
                            ts,
                        )

                        incr_stats(
                            db,
                            send,
                            soft,
                            campid,
                            is_camp,
                            smtp["cid"],
                            campcid,
                            domain,
                            "smtprelay",
                            smtp["id"],
                        )

                        if not campid.startswith("tx-") and send:
                            sent_emails.append(info["address"])

                        if error and raise_err:
                            raise Exception(error)

            for r in recips:
                trackingid = shortuuid.uuid()

                recipvars = {}
                recipvars["__uid"] = encrypt(r["Email"])
                recipvars["__to"] = r["Email"]
                recipvars["__trackingid"] = trackingid
                for v, vals in othervars.items():
                    lookup, defval = vals
                    if v == "__rand":
                        recipvars["__rand"] = "".join(
                            random.choice(randchars) for _ in range(9)
                        )
                    else:
                        recipvars[v.replace(" ", "_").replace("!", "_")] = (
                            r.get(lookup) or defval
                        )

                if "!!to" in r:
                    to = r["!!to"]
                else:
                    name = (
                        "%s %s" % (r.get("First Name", ""), r.get("Last Name", ""))
                    ).strip()
                    if name:
                        to = formataddr((name, r["Email"]))
                    else:
                        to = r["Email"]
                replace: Dict[str, str] = {
                    "__trackingid": shortuuid.uuid(),
                    "__uid": encrypt(r["Email"]),
                    "__to": to,
                    "Email": r["Email"],
                }
                for v, vals in othervars.items():
                    lookup, defval = vals
                    if v == "!!rand":
                        replace["__rand"] = "".join(
                            random.choice(randchars) for _ in range(9)
                        )
                    else:
                        replace[v.replace(" ", "_").replace("!", "_")] = (
                            r.get(lookup) or defval
                        )

                def rf(m: re.Match[str]) -> str:
                    return replace.get(m.group(1), "")

                htmlreplaced = varre.sub(rf, html)
                subjectreplaced = varre.sub(rf, subject)
                trackingid = replace["__trackingid"]

                tolist.append(
                    {
                        "address": r["Email"],
                        "to": to,
                        "html": htmlreplaced,
                        "subject": subjectreplaced,
                        "trackingid": trackingid,
                    }
                )

                if len(tolist) >= 1:
                    do_send()

                    tolist = []

            if len(tolist) > 0:
                do_send()

            if state["conn"] is not None:
                try:
                    state["conn"].quit()
                except:
                    pass
        except Exception as e:
            if write_err:
                db.campaigns.patch(
                    campid,
                    {
                        "finished_at": datetime.utcnow().isoformat() + "Z",
                        "error": str(e),
                    },
                )
            log.exception("error")
            if campid == "test" or raise_err:
                raise
        finally:
            if stream is not None:
                stream.close()
            if len(sent_emails):
                contacts.add_send(db, campid, sent_emails)


@tasks.task(priority=LOW_PRIORITY)
def do_easylink_send_task(
    el: JsonObj,
    frm: str,
    replyto: str,
    campid: str,
    campcid: str,
    is_camp: bool,
    recips: Iterable[JsonObj] | None,
    recipkey: str | None,
    othervars: Dict[str, Tuple[str, str]],
    write_err: bool,
    htmlkey: str,
    subject: str,
    raise_err: bool,
) -> None:
    do_easylink_send(
        el,
        frm,
        replyto,
        campid,
        campcid,
        is_camp,
        recips,
        recipkey,
        othervars,
        write_err,
        htmlkey,
        subject,
        raise_err,
    )


def do_easylink_send(
    el: JsonObj,
    frm: str,
    replyto: str,
    campid: str,
    campcid: str,
    is_camp: bool,
    recips: Iterable[JsonObj] | None,
    recipkey: str | None,
    othervars: Dict[str, Tuple[str, str]],
    write_err: bool,
    htmlkey: str,
    subject: str,
    raise_err: bool,
) -> None:
    with open_db() as db:
        stream = None
        sent_emails = []
        try:
            try:
                data = s3_read(os.environ["s3_transferbucket"], htmlkey)
            except:
                return

            html = data.decode("utf-8")
            s3_delete(os.environ["s3_transferbucket"], htmlkey)

            if recipkey is not None:
                stream = s3_read_stream(os.environ["s3_transferbucket"], recipkey)
                recips = MPDictReader(stream)

            assert recips is not None

            tolist: List[JsonObj] = []

            fromname, fromaddr = parseaddr(frm)

            if campid == "test":
                s = test_session()
            else:
                s = retry_session()

            def do_send() -> None:
                for info in tolist:
                    xml = """
                    <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:hs="http://www.holidaywebservice.com/HolidayService_v2/">
                        <soapenv:Header>
                            <ns1:Request xmlns:ns1="http://ws.easylink.com/RequestResponse/2011/01">
                                <ns1:ReceiverKey>https://messaging.easylink.com/soap/sync</ns1:ReceiverKey>
                                <ns1:Authentication>
                                    <ns1:XDDSAuth>
                                        <ns1:RequesterID>%s</ns1:RequesterID>
                                        <ns1:Password>%s</ns1:Password>
                                    </ns1:XDDSAuth>
                                </ns1:Authentication>
                            </ns1:Request>
                        </soapenv:Header>
                        <soapenv:Body>
                            <JobSubmitRequest xmlns="http://ws.easylink.com/JobSubmit/2011/01">
                                <SubmitId>%s</SubmitId>
                                <DocumentSet>
                                    <Document ref="docREF_CSV">
                                        <DocType>text</DocType>
                                        <Filename>email_test.csv</Filename>
                                        <DocData format="base64">%s</DocData>
                                    </Document>
                                    <Document ref="email_html_text">
                                        <DocType>HTML</DocType>
                                        <DocData format="base64">%s</DocData>
                                    </Document>
                                </DocumentSet>
                                <Message>
                                    <JobOptions>
                                        <Delivery>
                                            <Schedule>express</Schedule>
                                        </Delivery>
                                        <PriorityBoost>0</PriorityBoost>
                                        <EnhancedEmailOptions>
                                            <Subject b64charset="UTF-8">%s</Subject>
                                            <FromDisplayName>%s</FromDisplayName>
                                            <HTMLOpenTracking>none</HTMLOpenTracking>
                                            <CharacterSet>UTF-8</CharacterSet>
                                        </EnhancedEmailOptions>
                                    </JobOptions>
                                    <Destinations>
                                        <Table ref="tblREF_CSV">
                                            <DocRef>docREF_CSV</DocRef>
                                        </Table>
                                    </Destinations>
                                    <Reports>
                                        <DeliveryReport>
                                            <DeliveryReportType>detail</DeliveryReportType>
                                        </DeliveryReport>
                                    </Reports>
                                    <Contents>
                                        <Part>
                                            <DocRef>email_html_text</DocRef>
                                            <Treatment>body</Treatment>
                                        </Part>
                                    </Contents>
                                </Message>
                            </JobSubmitRequest>
                        </soapenv:Body>
                    </soapenv:Envelope>""" % (
                        escape(el["username"]),
                        escape(el["password"]),
                        "%06d" % random.randint(0, 999999),
                        base64.b64encode(
                            ("ADDR,TYPE\n%s,internet\n" % info["address"]).encode(
                                "utf-8"
                            )
                        ).decode("ascii"),
                        base64.b64encode(info["html"].encode("utf-8")).decode("ascii"),
                        base64.b64encode(info["subject"].encode("utf-8")).decode(
                            "ascii"
                        ),
                        escape(fromname),
                    )

                    args = ["https://messaging.easylink.com/soap/sync"]
                    kwargs = dict(
                        headers={"Content-Type": "application/xml"},
                        data=xml,
                    )
                    error = None
                    try:
                        log.debug("%s", info["address"])
                        r = s.post(*args, **kwargs)  # type: ignore
                        log.debug("Reply: %s", r.text)
                        r.raise_for_status()
                    except Exception as e:
                        error = str(e)

                    domain = info["address"].split("@")[1]
                    if error:
                        if campid == "test":
                            add_test_log(db, campcid, info["address"], error)
                            raise Exception(error)
                        log.error("EasyLink Error: %s", error)
                        handle_soft_event(
                            db, info["address"], campid, campcid, is_camp, error
                        )
                        incr_stats(
                            db,
                            0,
                            1,
                            campid,
                            is_camp,
                            el["cid"],
                            campcid,
                            domain,
                            "easylink",
                            el["id"],
                        )
                        if raise_err:
                            raise Exception(error)
                    else:
                        if campid == "test":
                            add_test_log(db, campcid, info["address"], "Success")
                        else:
                            trackingid = info["trackingid"]

                            ts = datetime.utcnow()
                            db.execute(
                                "insert into eltracking (id, settingsid, ts) values (%s, %s, %s)",
                                trackingid,
                                el["id"],
                                ts,
                            )

                            incr_stats(
                                db,
                                1,
                                0,
                                campid,
                                is_camp,
                                el["cid"],
                                campcid,
                                domain,
                                "easylink",
                                el["id"],
                            )

                            if not campid.startswith("tx-"):
                                sent_emails.append(info["address"])

            for r in recips:
                trackingid = shortuuid.uuid()

                recipvars = {}
                recipvars["__uid"] = encrypt(r["Email"])
                recipvars["__to"] = r["Email"]
                recipvars["__trackingid"] = trackingid
                for v, vals in othervars.items():
                    lookup, defval = vals
                    if v == "__rand":
                        recipvars["__rand"] = "".join(
                            random.choice(randchars) for _ in range(9)
                        )
                    else:
                        recipvars[v.replace(" ", "_").replace("!", "_")] = (
                            r.get(lookup) or defval
                        )

                if "!!to" in r:
                    to = r["!!to"]
                else:
                    name = (
                        "%s %s" % (r.get("First Name", ""), r.get("Last Name", ""))
                    ).strip()
                    if name:
                        to = formataddr((name, r["Email"]))
                    else:
                        to = r["Email"]
                replace: Dict[str, str] = {
                    "__trackingid": shortuuid.uuid(),
                    "__uid": encrypt(r["Email"]),
                    "__to": to,
                    "Email": r["Email"],
                }
                for v, vals in othervars.items():
                    lookup, defval = vals
                    if v == "!!rand":
                        replace["__rand"] = "".join(
                            random.choice(randchars) for _ in range(9)
                        )
                    else:
                        replace[v.replace(" ", "_").replace("!", "_")] = (
                            r.get(lookup) or defval
                        )

                def rf(m: re.Match[str]) -> str:
                    return replace.get(m.group(1), "")

                htmlreplaced = varre.sub(rf, html)
                subjectreplaced = varre.sub(rf, subject)
                trackingid = replace["__trackingid"]

                tolist.append(
                    {
                        "address": r["Email"],
                        "html": htmlreplaced,
                        "subject": subjectreplaced,
                        "trackingid": trackingid,
                    }
                )

                if len(tolist) >= 1:
                    do_send()

                    tolist = []

            if len(tolist) > 0:
                do_send()
        except Exception as e:
            if write_err:
                db.campaigns.patch(
                    campid,
                    {
                        "finished_at": datetime.utcnow().isoformat() + "Z",
                        "error": str(e),
                    },
                )
            log.exception("error")
            if campid == "test" or raise_err:
                raise
        finally:
            if stream is not None:
                stream.close()
            if len(sent_emails):
                contacts.add_send(db, campid, sent_emails)


@tasks.task(priority=LOW_PRIORITY)
def do_sparkpost_send_task(
    sp: JsonObj,
    frm: str,
    replyto: str,
    campid: str,
    campcid: str,
    is_camp: bool,
    recips: Iterable[JsonObj] | None,
    recipkey: str | None,
    othervars: Dict[str, Tuple[str, str]],
    write_err: bool,
    htmlkey: str,
    subject: str,
    raise_err: bool,
) -> None:
    do_sparkpost_send(
        sp,
        frm,
        replyto,
        campid,
        campcid,
        is_camp,
        recips,
        recipkey,
        othervars,
        write_err,
        htmlkey,
        subject,
        raise_err,
    )


def do_sparkpost_send(
    sp: JsonObj,
    frm: str,
    replyto: str,
    campid: str,
    campcid: str,
    is_camp: bool,
    recips: Iterable[JsonObj] | None,
    recipkey: str | None,
    othervars: Dict[str, Tuple[str, str]],
    write_err: bool,
    htmlkey: str,
    subject: str,
    raise_err: bool,
) -> None:
    try:
        try:
            data = s3_read(os.environ["s3_transferbucket"], htmlkey)
        except:
            return

        html = data.decode("utf-8")
        s3_delete(os.environ["s3_transferbucket"], htmlkey)

        if recipkey is not None:
            stream = s3_read_stream(os.environ["s3_transferbucket"], recipkey)
            recips = MPDictReader(stream)

        assert recips is not None

        tolist: List[JsonObj] = []

        fromname, fromaddr = parseaddr(frm)

        if campid == "test":
            s = test_session()
        else:
            s = retry_session()

        def do_send() -> None:
            data = {
                "content": {
                    "from": {
                        "name": fromname,
                        "email": fromaddr,
                    },
                    "subject": subject,
                    "html": html,
                    "reply_to": replyto,
                },
                "metadata": {
                    "settingsid": sp["id"],
                    "cid": campcid,
                    "campid": campid,
                    "is_camp": str(is_camp),
                },
                "recipients": tolist,
                "campaign_id": campid,
                "return_path": fromaddr,
                "options": {
                    "click_tracking": False,
                    "open_tracking": False,
                },
            }

            args = [f"{sparkpost_domain(sp)}/transmissions"]
            kwargs = dict(
                headers={
                    "Authorization": sp["apikey"],
                    "Content-Type": "application/json",
                },
                json=data,
            )
            try:
                r = s.post(*args, **kwargs)  # type: ignore
                handle_sp_error(r)
            except Exception as e:
                if campid != "test":
                    with open_db() as db:
                        countsbydomain: Dict[str, int] = {}
                        for to in tolist:
                            domain = to["address"]["email"].split("@")[1]
                            countsbydomain[domain] = countsbydomain.get(domain, 0) + 1

                            handle_soft_event(
                                db,
                                to["address"]["email"],
                                campid,
                                campcid,
                                is_camp,
                                str(e),
                            )
                        for domain, count in countsbydomain.items():
                            incr_stats(
                                db,
                                0,
                                count,
                                campid,
                                is_camp,
                                sp["cid"],
                                campcid,
                                domain,
                                "sparkpost",
                                sp["id"],
                            )
                        if raise_err:
                            raise
                else:
                    raise

        for r in recips:
            trackingid = shortuuid.uuid()

            recipvars = {}
            recipvars["__uid"] = encrypt(r["Email"])
            recipvars["__to"] = r["Email"]
            recipvars["__trackingid"] = trackingid
            recipvars["Email"] = r["Email"]
            for v, vals in othervars.items():
                lookup, defval = vals
                if v == "__rand":
                    recipvars["__rand"] = "".join(
                        random.choice(randchars) for _ in range(9)
                    )
                else:
                    recipvars[v.replace(" ", "_").replace("!", "_")] = (
                        r.get(lookup) or defval
                    )

            if "!!to" in r:
                toname, toaddr = parseaddr(r["!!to"])
            else:
                toname = (
                    "%s %s" % (r.get("First Name", ""), r.get("Last Name", ""))
                ).strip()
                toaddr = r["Email"]
            tolist.append(
                {
                    "address": {
                        "email": toaddr,
                        "name": toname,
                    },
                    "metadata": {
                        "trackingid": trackingid,
                    },
                    "substitution_data": recipvars,
                }
            )

            if len(tolist) >= 1000:
                do_send()

                tolist = []

        if len(tolist) > 0:
            do_send()
    except Exception as e:
        if write_err:
            with open_db() as db:
                db.campaigns.patch(
                    campid,
                    {
                        "finished_at": datetime.utcnow().isoformat() + "Z",
                        "error": str(e),
                    },
                )
        log.exception("error")
        if campid == "test" or raise_err:
            raise


@tasks.task(priority=LOW_PRIORITY)
def do_mailgun_send_task(
    mg: JsonObj,
    frm: str,
    replyto: str,
    subject: str,
    htmlkey: str,
    campid: str,
    usercid: str,
    is_camp: bool,
    unsuburl: str,
    domain: str,
    recips: Iterable[JsonObj] | None,
    recipkey: str | None,
    othervars: Dict[str, Tuple[str, str]],
    write_err: bool,
    raise_err: bool,
) -> None:
    do_mailgun_send(
        mg,
        frm,
        replyto,
        subject,
        htmlkey,
        campid,
        usercid,
        is_camp,
        unsuburl,
        domain,
        recips,
        recipkey,
        othervars,
        write_err,
        raise_err,
    )


def do_mailgun_send(
    mg: JsonObj,
    frm: str,
    replyto: str,
    subject: str,
    htmlkey: str,
    campid: str,
    usercid: str,
    is_camp: bool,
    unsuburl: str,
    domain: str,
    recips: Iterable[JsonObj] | None,
    recipkey: str | None,
    othervars: Dict[str, Tuple[str, str]],
    write_err: bool,
    raise_err: bool,
) -> None:
    stream = None
    try:
        try:
            data = s3_read(os.environ["s3_transferbucket"], htmlkey)
        except:
            return

        html = data.decode("utf-8")
        s3_delete(os.environ["s3_transferbucket"], htmlkey)

        if recipkey is not None:
            stream = s3_read_stream(os.environ["s3_transferbucket"], recipkey)
            recips = MPDictReader(stream)

        assert recips is not None

        allvars: JsonObj = {}
        tolist: List[str] = []

        if campid == "test":
            s = test_session()
        else:
            s = retry_session()

        def do_send() -> None:
            data = {
                "from": frm,
                "to": tolist,
                "subject": subject,
                "html": html,
                "h:Reply-To": replyto,
                "v:settingsid": mg["id"],
                "v:cid": usercid,
                "v:campid": campid,
                "v:is_camp": str(is_camp),
                "v:trackingid": "%recipient.!!trackingid%",
                "recipient-variables": json.dumps(allvars),
            }

            args = [f"{mg_domain(mg)}/v3/{domain}/messages"]
            kwargs = {"auth": ("api", mg["apikey"]), "data": data}
            try:
                r = s.post(*args, **kwargs)  # type: ignore
                handle_mg_error(r)
            except Exception as e:
                if campid != "test":
                    with open_db() as db:
                        countsbydomain: Dict[str, int] = {}
                        for to in tolist:
                            _, email = parseaddr(to)
                            if email:
                                emaildomain = email.split("@")[1]
                                countsbydomain[emaildomain] = (
                                    countsbydomain.get(emaildomain, 0) + 1
                                )
                                handle_soft_event(
                                    db, email, campid, usercid, is_camp, str(e)
                                )
                        for emaildomain, count in countsbydomain.items():
                            incr_stats(
                                db,
                                0,
                                count,
                                campid,
                                is_camp,
                                mg["cid"],
                                usercid,
                                emaildomain,
                                "mailgun",
                                mg["id"],
                            )
                        if raise_err:
                            raise
                else:
                    raise

        for r in recips:
            recipvars = {}

            recipvars["!!trackingid"] = shortuuid.uuid()
            recipvars["!!uid"] = encrypt(r["Email"])
            for v, vals in othervars.items():
                lookup, defval = vals
                if v == "!!rand":
                    recipvars["!!rand"] = "".join(
                        random.choice(randchars) for _ in range(9)
                    )
                else:
                    recipvars[v.replace(" ", "-")] = r.get(lookup) or defval

            if "!!to" in r:
                to = r["!!to"]
            else:
                name = (
                    "%s %s" % (r.get("First Name", ""), r.get("Last Name", ""))
                ).strip()
                if name:
                    to = formataddr((name, r["Email"]))
                else:
                    to = r["Email"]
            allvars[r["Email"]] = recipvars
            tolist.append(to)

            if len(tolist) >= 1000:
                do_send()

                tolist = []
                allvars = {}

        if len(tolist) > 0:
            do_send()
    except Exception as e:
        if write_err:
            with open_db() as db:
                db.campaigns.patch(
                    campid,
                    {
                        "finished_at": datetime.utcnow().isoformat() + "Z",
                        "error": str(e),
                    },
                )
        log.exception("error")
        if campid == "test" or raise_err:
            raise
    finally:
        if stream is not None:
            stream.close()


def get_defval(var: str) -> Tuple[str, str]:
    defval = ""
    if "," in var:
        var, flag = var.split(",", 1)
        m = defflagre.search(flag)
        if m:
            defval = m.group(1)
    return var, defval


def get_vars(texts: Tuple[str, str]) -> Dict[str, Tuple[str, str]]:
    othervars = {}
    for txt in texts:
        for var in varre.findall(txt):
            var, defval = get_defval(var)
            if var in (
                "!!to",
                "Email",
                "!!domain",
                "!!webroot",
                "!!campid",
                "!!trackingid",
                "!!uid",
            ):
                continue
            if var not in othervars:
                othervars[var] = (var, defval)
            elif var in othervars and defval != othervars[var][1]:
                cnt = 2
                while True:
                    newvarname = "%s%s" % (var, cnt)
                    if newvarname not in othervars:
                        othervars[newvarname] = (var, defval)
                        break
                    cnt += 1
    return othervars


def ses_send(
    ses: JsonObj,
    frm: str,
    replyto: str,
    subject: str,
    html: str | bytes,
    campid: str,
    campcid: str,
    is_camp: bool,
    recips: Iterable[JsonObj] | None = None,
    recipkey: str | None = None,
    sync: bool = False,
    write_err: bool = False,
    attachments: List[AttachmentManifest] | None = None,
    raise_err: bool = False,
) -> None:
    domain: str = ses["domain"]
    linkwebroot = link_webroot(ses)

    if not isinstance(html, str):
        html = html.decode("utf-8")

    othervars = get_vars((subject, html))

    def replacefunc(m: re.Match[str]) -> str:
        tagname = m.group(1)

        tagname, defval = get_defval(tagname)

        if tagname == "!!domain":
            return domain
        if tagname == "!!webroot":
            return linkwebroot
        if tagname == "!!campid":
            return campid

        for realname, vals in othervars.items():
            var, d = vals

            if var == tagname and d == defval:
                tagname = realname
                break

        return "{{%s}}" % tagname.replace(" ", "-").replace("!", "_")

    html = varre.sub(replacefunc, html)
    subject = varre.sub(replacefunc, subject)

    htmlkey = "sessend/%s.html" % shortuuid.uuid()
    s3_write(os.environ["s3_transferbucket"], htmlkey, html.encode("utf-8"))

    if sync:
        do_ses_send(
            ses,
            frm,
            replyto,
            campid,
            campcid,
            is_camp,
            recips,
            recipkey,
            othervars,
            write_err,
            htmlkey,
            subject,
            attachments,
            raise_err,
        )
    else:
        if recips is not None:
            recips = list(recips)
        run_task(
            do_ses_send_task,
            ses,
            frm,
            replyto,
            campid,
            campcid,
            is_camp,
            recips,
            recipkey,
            othervars,
            write_err,
            htmlkey,
            subject,
            attachments,
            raise_err,
        )


def sparkpost_send(
    sp: JsonObj,
    frm: str,
    replyto: str,
    subject: str,
    html: str | bytes,
    campid: str,
    campcid: str,
    is_camp: bool,
    recips: Iterable[JsonObj] | None = None,
    recipkey: str | None = None,
    sync: bool = False,
    write_err: bool = False,
    raise_err: bool = False,
) -> None:
    linkwebroot = link_webroot(sp)
    # unsuburl = '%s/?t=unsub&r={{__trackingid}}&c=%s&u={{__uid}}' % (linkwebroot, campid)

    if not isinstance(html, str):
        html = html.decode("utf-8")

    othervars = get_vars((subject, html))

    def replacefunc(m: re.Match[str]) -> str:
        tagname = m.group(1)

        tagname, defval = get_defval(tagname)

        # if tagname == '!!domain':
        #    return domain
        if tagname == "!!webroot":
            return linkwebroot
        if tagname == "!!campid":
            return campid

        for realname, vals in othervars.items():
            var, d = vals

            if var == tagname and d == defval:
                tagname = realname
                break

        return "{{%s}}" % tagname.replace(" ", "_").replace("!", "_")

    html = varre.sub(replacefunc, html)
    subject = varre.sub(replacefunc, subject)

    htmlkey = "sphtml/%s.html" % shortuuid.uuid()
    s3_write(os.environ["s3_transferbucket"], htmlkey, html.encode("utf-8"))

    if sync:
        do_sparkpost_send(
            sp,
            frm,
            replyto,
            campid,
            campcid,
            is_camp,
            recips,
            recipkey,
            othervars,
            write_err,
            htmlkey,
            subject,
            raise_err,
        )
    else:
        if recips is not None:
            recips = list(recips)
        run_task(
            do_sparkpost_send_task,
            sp,
            frm,
            replyto,
            campid,
            campcid,
            is_camp,
            recips,
            recipkey,
            othervars,
            write_err,
            htmlkey,
            subject,
            raise_err,
        )


def smtprelay_send(
    smtp: JsonObj,
    frm: str,
    replyto: str,
    subject: str,
    html: str | bytes,
    campid: str,
    campcid: str,
    is_camp: bool,
    recips: Iterable[JsonObj] | None = None,
    recipkey: str | None = None,
    sync: bool = False,
    write_err: bool = False,
    raise_err: bool = False,
) -> None:
    linkwebroot = link_webroot(smtp)
    # unsuburl = '%s/?t=unsub&r={{__trackingid}}&c=%s&u={{__uid}}' % (linkwebroot, campid)

    if not isinstance(html, str):
        html = html.decode("utf-8")

    othervars = get_vars((subject, html))

    def replacefunc(m: re.Match[str]) -> str:
        tagname = m.group(1)

        tagname, defval = get_defval(tagname)

        if tagname == "!!webroot":
            return linkwebroot
        if tagname == "!!campid":
            return campid

        for realname, vals in othervars.items():
            var, d = vals

            if var == tagname and d == defval:
                tagname = realname
                break

        return "{{%s}}" % tagname.replace(" ", "_").replace("!", "_")

    html = varre.sub(replacefunc, html)
    subject = varre.sub(replacefunc, subject)

    htmlkey = "smtphtml/%s.html" % shortuuid.uuid()
    s3_write(os.environ["s3_transferbucket"], htmlkey, html.encode("utf-8"))

    if sync:
        do_smtprelay_send(
            smtp,
            frm,
            replyto,
            campid,
            campcid,
            is_camp,
            recips,
            recipkey,
            othervars,
            write_err,
            htmlkey,
            subject,
            raise_err,
        )
    else:
        if recips is not None:
            recips = list(recips)
        run_task(
            do_smtprelay_send_task,
            smtp,
            frm,
            replyto,
            campid,
            campcid,
            is_camp,
            recips,
            recipkey,
            othervars,
            write_err,
            htmlkey,
            subject,
            raise_err,
        )


def easylink_send(
    sp: JsonObj,
    frm: str,
    replyto: str,
    subject: str,
    html: str | bytes,
    campid: str,
    campcid: str,
    is_camp: bool,
    recips: Iterable[JsonObj] | None = None,
    recipkey: str | None = None,
    sync: bool = False,
    write_err: bool = False,
    raise_err: bool = False,
) -> None:
    linkwebroot = link_webroot(sp)
    # unsuburl = '%s/?t=unsub&r={{__trackingid}}&c=%s&u={{__uid}}' % (linkwebroot, campid)

    if not isinstance(html, str):
        html = html.decode("utf-8")

    othervars = get_vars((subject, html))

    def replacefunc(m: re.Match[str]) -> str:
        tagname = m.group(1)

        tagname, defval = get_defval(tagname)

        # if tagname == '!!domain':
        #    return domain
        if tagname == "!!webroot":
            return linkwebroot
        if tagname == "!!campid":
            return campid

        for realname, vals in othervars.items():
            var, d = vals

            if var == tagname and d == defval:
                tagname = realname
                break

        return "{{%s}}" % tagname.replace(" ", "_").replace("!", "_")

    html = varre.sub(replacefunc, html)
    subject = varre.sub(replacefunc, subject)

    htmlkey = "elhtml/%s.html" % shortuuid.uuid()
    s3_write(os.environ["s3_transferbucket"], htmlkey, html.encode("utf-8"))

    if sync:
        do_easylink_send(
            sp,
            frm,
            replyto,
            campid,
            campcid,
            is_camp,
            recips,
            recipkey,
            othervars,
            write_err,
            htmlkey,
            subject,
            raise_err,
        )
    else:
        if recips is not None:
            recips = list(recips)
        run_task(
            do_easylink_send_task,
            sp,
            frm,
            replyto,
            campid,
            campcid,
            is_camp,
            recips,
            recipkey,
            othervars,
            write_err,
            htmlkey,
            subject,
            raise_err,
        )


def mailgun_send(
    mg: JsonObj,
    clientdomain: str | None,
    frm: str,
    replyto: str,
    subject: str,
    html: str | bytes,
    campid: str,
    usercid: str,
    is_camp: bool,
    recips: Iterable[JsonObj] | None = None,
    recipkey: str | None = None,
    sync: bool = False,
    write_err: bool = False,
    raise_err: bool = False,
) -> None:
    domain: str = mg["domain"]

    if clientdomain is not None:
        domain = clientdomain

    setup_mg_webhooks(mg, domain)

    linkwebroot = link_webroot(mg)
    unsuburl = "%s/?t=unsub&r=%%recipient.!!trackingid%%&c=%s&u=%%recipient.!!uid%%" % (
        linkwebroot,
        campid,
    )

    if not isinstance(html, str):
        html = html.decode("utf-8")

    othervars = get_vars((subject, html))

    def replacefunc(m: re.Match[str]) -> str:
        tagname = m.group(1)

        tagname, defval = get_defval(tagname)

        if tagname == "!!to":
            return "%recipient%"
        if tagname == "Email":
            return "%recipient_email%"
        if tagname == "!!domain":
            return domain
        if tagname == "!!webroot":
            return linkwebroot
        if tagname == "!!campid":
            return campid

        for realname, vals in othervars.items():
            var, d = vals

            if var == tagname and d == defval:
                tagname = realname
                break

        return "%%recipient.%s%%" % tagname.replace(" ", "-")

    html = varre.sub(replacefunc, html)
    subject = varre.sub(replacefunc, subject)

    htmlkey = "mghtml/%s.html" % shortuuid.uuid()
    s3_write(os.environ["s3_transferbucket"], htmlkey, html.encode("utf-8"))

    if sync:
        do_mailgun_send(
            mg,
            frm,
            replyto,
            subject,
            htmlkey,
            campid,
            usercid,
            is_camp,
            unsuburl,
            domain,
            recips,
            recipkey,
            othervars,
            write_err,
            raise_err,
        )
    else:
        if recips is not None:
            recips = list(recips)
        run_task(
            do_mailgun_send_task,
            mg,
            frm,
            replyto,
            subject,
            htmlkey,
            campid,
            usercid,
            is_camp,
            unsuburl,
            domain,
            recips,
            recipkey,
            othervars,
            write_err,
            raise_err,
        )
