"""Tests for the STATE_DB field-completeness Telemetry Coverage Analyzer."""

import os
import socket

import pytest

from telemetry_coverage_analyzer import (
    classify,
    flatten_source,
    render_table,
    live_source,
    DISCLAIMER,
)


# A switch gNMI endpoint (HOST:PORT) to run the live test against. ./run.sh
# virtual exposes none, so this test is skipped unless you point it at one.
LIVE_GNMI = os.environ.get("COVERAGE_LIVE_GNMI", "").strip()


def gnmi_reachable(addr):
    if not addr or ":" not in addr:
        return False
    host, port = addr.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port)), timeout=2):
            return True
    except (OSError, ValueError):
        return False


CANNED = {
    "platform": "test-box",
    "label": "test data, not a capture",
    "state_db": {
        "TEMPERATURE_INFO|ASIC": {"temperature": "58.0", "high_threshold": "85"},
        "PSU_INFO|PSU 1": {"output_power": "220.1", "output_current": None},
        "PSU_INFO|PSU 2": {"output_power": "219.8", "output_current": ""},
    },
}


def test_classify():
    assert classify("58.0") == "populated"
    assert classify(0) == "populated"
    assert classify(None) == "null"
    assert classify("") == "null"
    assert classify("N/A") == "null"


def test_flatten_statuses():
    flat = flatten_source(CANNED["state_db"])
    assert flat[("TEMPERATURE_INFO", "temperature")] == "populated"
    # null in every entry → null
    assert flat[("PSU_INFO", "output_current")] == "null"
    # populated anywhere wins
    assert flat[("PSU_INFO", "output_power")] == "populated"


def test_render_union_and_symbols():
    other = {"TEMPERATURE_INFO|ASIC": {"temperature": "41.0", "warning_status": "false"}}
    out = render_table({"test-box": flatten_source(CANNED["state_db"]),
                        "other-box": flatten_source(other)},
                       canned_names=["test-box", "other-box"])
    assert "✓" in out and "∅" in out and "—" in out
    # union: warning_status only exists in other-box → test-box shows absent
    lines = [l for l in out.splitlines() if "warning_status" in l]
    assert lines and "—" in lines[0]
    assert DISCLAIMER in out


@pytest.mark.skipif(not gnmi_reachable(LIVE_GNMI),
                    reason="needs a switch gNMI endpoint: set COVERAGE_LIVE_GNMI=HOST:PORT "
                           "(./run.sh virtual exposes none)")
def test_live_source_shape():
    host, port = LIVE_GNMI.rsplit(":", 1)
    flat = live_source(host, int(port))
    assert flat[("TEMPERATURE_INFO", "temperature")] == "populated"
    # nothing seeds transceiver DOM on sonic-vs, so the table should be absent
    assert all(t != "TRANSCEIVER_DOM_SENSOR" or s == "absent"
               for (t, _), s in flat.items()) or \
        ("TRANSCEIVER_DOM_SENSOR", "temperature") not in flat
