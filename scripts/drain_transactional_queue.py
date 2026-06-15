#!/usr/bin/env python3

import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

os.environ["SYNC_TASKS"] = "1"

from api.shared.db import open_db
from api.transactional import check_txns


def main() -> None:
    check_txns()
    with open_db() as db:
        queued = db.single("select count(id) from txnqueue")
        if queued:
            raise SystemExit(f"txnqueue still has {queued} rows after drain")
        latest = list(
            db.execute(
                """
                select data from txnsends
                order by ts desc
                limit 5
                """
            )
        )
        for data, in latest:
            if data.get("event") == "Error":
                raise SystemExit(f"latest transactional send error: {data.get('error')}")
    print("Transactional queue drained")


if __name__ == "__main__":
    main()
