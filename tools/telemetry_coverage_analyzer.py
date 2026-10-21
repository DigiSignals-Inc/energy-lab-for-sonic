#!/usr/bin/env python3
"""Telemetry Coverage Analyzer — flag which STATE_DB fields are populated vs null per platform.

Same gNMI API everywhere — completely different field coverage. Live columns
query a switch's gNMI server (STATE_DB target) with the SonicGnmiClient in
app/collector/gnmi_client.py. Dump columns come from JSON files you pass with
--dump, shaped {"platform": NAME, "state_db": {"TABLE|entry": {field: value}}};
the repo ships none, so a dump column is only as real as the file behind it.

Usage:
    telemetry_coverage_analyzer.py --live sw1=<switch-ip>:8080 \
                      --dump my-platform-dump.json ...

./run.sh virtual does not expose a gNMI port, so --live needs a switch (real or
virtual) whose gNMI server you can reach.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "collector"))

TABLES = ["TEMPERATURE_INFO", "PSU_INFO", "FAN_INFO", "TRANSCEIVER_DOM_SENSOR"]
SYMBOL = {"populated": "✓", "null": "∅", "absent": "—"}
DISCLAIMER = (
    "Dump columns come from the JSON files passed with --dump; they are not "
    "live reads."
)


def classify(value):
    if value is None or value == "" or str(value).strip().upper() == "N/A":
        return "null"
    return "populated"


def flatten_source(state_db):
    """{'TABLE|entry': {field: value}} → {(table, field): status}.

    A field is populated if any entry populates it; null if present anywhere
    but never populated; rows for absent fields come from the cross-source
    union at render time.
    """
    flat = {}
    for key, fields in state_db.items():
        table = key.split("|", 1)[0]
        for field, value in (fields or {}).items():
            status = classify(value)
            current = flat.get((table, field))
            if current != "populated":
                flat[(table, field)] = status if current is None or status == "populated" else current
    return flat


def live_source(host, port):
    import grpc
    from gnmi_client import SonicGnmiClient  # app/collector/gnmi_client.py

    client = SonicGnmiClient(host, port)
    state_db = {}
    for table in TABLES:
        # one path per GET: the server fails the whole request on any absent
        # table (NotFound) — an absent table is exactly what we're measuring
        try:
            vals = client.get("STATE_DB", [table])
        except grpc.RpcError:
            continue
        entries = vals.get(table, {}).get(table, vals.get(table, {}))
        for entry, fields in entries.items():
            if isinstance(fields, dict):
                state_db[f"{table}|{entry}"] = fields
    return flatten_source(state_db)


def render_table(sources, canned_names=()):
    """sources: {name: {(table, field): status}} → aligned text table."""
    rows = sorted({key for flat in sources.values() for key in flat})
    names = list(sources)
    field_width = max(len(f"{t}.{f}") for t, f in rows) + 2
    col_width = max([len(n) for n in names] + [6]) + 2

    out = ["Field".ljust(field_width) + "".join(n.rjust(col_width) for n in names)]
    out.append("-" * (field_width + col_width * len(names)))
    last_table = None
    for table, field in rows:
        if table != last_table:
            out.append(table)
            last_table = table
        cells = "".join(
            SYMBOL[sources[n].get((table, field), "absent")].rjust(col_width)
            for n in names
        )
        out.append(f"  .{field}".ljust(field_width) + cells)
    out.append("")
    out.append("✓ populated   ∅ present but null/empty   — key absent")
    if canned_names:
        out.append(DISCLAIMER)
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="append", default=[],
                        metavar="NAME=HOST:PORT")
    parser.add_argument("--dump", action="append", default=[], metavar="FILE.json")
    args = parser.parse_args(argv)

    dump_paths = list(args.dump)

    sources, canned = {}, []
    for spec in args.live:
        name, addr = spec.split("=", 1)
        host, port = addr.rsplit(":", 1)
        sources[f"{name} (live)"] = live_source(host, int(port))
    for path in dump_paths:
        doc = json.loads(Path(path).read_text())
        sources[doc["platform"]] = flatten_source(doc["state_db"])
        canned.append(doc["platform"])

    if not sources:
        parser.error("nothing to compare: pass --live and/or --dump")
    print(render_table(sources, canned_names=canned))


if __name__ == "__main__":
    main()
