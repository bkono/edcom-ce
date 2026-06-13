#!/usr/bin/env python3

import os
import sys
from datetime import datetime

import shortuuid

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from api.shared.db import DB


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def main() -> None:
    from_email = required_env("EDCOM_TEST_FROM_EMAIL")
    ses_region = required_env("EDCOM_SES_REGION")
    ses_access = required_env("EDCOM_SES_ACCESS_KEY_ID")
    ses_secret = required_env("EDCOM_SES_SECRET_ACCESS_KEY")
    ses_domain = os.environ.get("EDCOM_SES_DOMAIN", from_email.split("@", 1)[1])

    db = DB()
    try:
        admin_company = db.companies.find_one({"admin": True})
        customer_company = db.companies.find_one({"admin": False})
        if admin_company is None or customer_company is None:
            raise SystemExit("Test setup must create one admin and one customer company")

        db.set_cid(customer_company["id"])
        user = db.users.find_one({"username": "user@edtest.ok"})
        if user is None or not user.get("apikey"):
            raise SystemExit("Test setup customer API user not found")

        db.set_cid(admin_company["id"])
        ses = db.ses.find_one({"name": "Attachment Integration SES"})
        ses_data = {
            "name": "Attachment Integration SES",
            "domain": ses_domain,
            "region": ses_region,
            "access": ses_access,
            "secret": ses_secret,
            "policytype": "ses",
        }
        if ses is None:
            ses_id = db.ses.add(ses_data)
        else:
            ses_id = ses["id"]
            db.ses.patch(ses_id, ses_data)

        route = db.routes.find_one({"name": "Attachment Integration SES Route"})
        now = datetime.utcnow().isoformat() + "Z"
        route_data = {
            "name": "Attachment Integration SES Route",
            "dirty": False,
            "rules": [
                {
                    "splits": [{"pct": 100, "policy": ses_id}],
                    "default": True,
                    "domaingroup": "",
                }
            ],
            "modified": now,
            "published": {
                "rules": [
                    {
                        "splits": [{"pct": 100, "policy": ses_id}],
                        "default": True,
                        "domaingroup": "",
                    }
                ],
                "usedefault": False,
            },
            "usedefault": False,
        }
        if route is None:
            route_id = db.routes.add(route_data)
        else:
            route_id = route["id"]
            db.routes.patch(route_id, route_data)

        db.set_cid(None)
        db.companies.patch(customer_company["id"], {"routes": [route_id]})

        db.set_cid(customer_company["id"])
        db.txnsettings.patch_singleton({"route": route_id})

        print(f"export EDCOM_API_KEY={user['apikey']}")
        print(f"export EDCOM_ROUTE_ID={route_id}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
