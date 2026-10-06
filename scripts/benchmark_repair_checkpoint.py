"""Read-only comparison of full job decoding versus cancellation flag lookup."""
import argparse
import json
import sqlite3
import time
from pathlib import Path
from pcb_weaver.storage import write_json


def run(args):
    database = (args.root / "data" / "ledger.sqlite3").resolve(strict=True)
    with sqlite3.connect(database.as_uri()+"?mode=ro",uri=True) as db:
        def full():
            row = db.execute("SELECT request,result,cancel_requested FROM jobs WHERE id=?",(args.job,)).fetchone()
            assert row is not None
            json.loads(row[0])
            if row[1]:
                json.loads(row[1])
            events = db.execute("SELECT payload FROM job_events WHERE job=? ORDER BY seq",(args.job,)).fetchall()
            for item in events:
                json.loads(item[0])
            return bool(row[2])
        def flag():
            row = db.execute("SELECT cancel_requested FROM jobs WHERE id=?",(args.job,)).fetchone()
            assert row is not None
            return bool(row[0])
        measurements = {}
        for name,operation in (("full_job_decode",full),("flag_only",flag)):
            start = time.perf_counter()
            values = [operation() for _ in range(10)]
            measurements[name] = {"seconds":time.perf_counter()-start,"iterations":10,"value":values[-1]}
        assert measurements["full_job_decode"]["value"] == measurements["flag_only"]["value"]
        events,bytes_ = db.execute("SELECT count(*),sum(length(payload)) FROM job_events WHERE job=?",(args.job,)).fetchone()
    result = {"job":args.job,"read_only":True,"events":events,"event_payload_bytes":bytes_,"measurements":measurements}
    write_json(args.output,result)
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,required=True)
    parser.add_argument("--job",required=True)
    parser.add_argument("--output",type=Path,required=True)
    run(parser.parse_args())
