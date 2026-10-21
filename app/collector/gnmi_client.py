"""Real gNMI (sonic-db target) client + payload parsing for real SONiC switches.

Adapted from an earlier, working real-telemetry pipeline of ours rather than
written from scratch. Kept as a standalone module so fabric_exporter.py's
real-switch path can import it directly.

Some SONiC telemetry builds don't answer the Capabilities RPC, which pygnmi's client
insists on at connect time — so this uses a thin client on pygnmi's generated
protos: fetch the server's self-signed cert, trust exactly that cert, and call
Get directly.
"""
import json
import ssl

import grpc
from pygnmi.spec.v080.gnmi_pb2 import GetRequest, Path, PathElem
from pygnmi.spec.v080.gnmi_pb2_grpc import gNMIStub

STATE_PATHS = ["TEMPERATURE_INFO", "PSU_INFO", "FAN_INFO"]


def to_gnmi_path(path_str, target=None):
    elems = [PathElem(name=seg) for seg in path_str.split("/") if seg]
    return Path(target=target, elem=elems) if target else Path(elem=elems)


def _cert_override(pem):
    """First SAN (or CN) from the server's self-signed cert, for
    grpc.ssl_target_name_override — mirrors pygnmi skip_verify (lab only)."""
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(pem)
    try:
        sans = cert.extensions.get_extension_for_oid(
            x509.oid.ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        )
        for kind in (x509.DNSName, x509.IPAddress, x509.RFC822Name):
            values = sans.value.get_values_for_type(kind)
            if values:
                return str(values[0])
    except x509.ExtensionNotFound:
        pass
    cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
    return cn[0].value if cn else ""


class SonicGnmiClient:
    def __init__(self, host, port, timeout=10):
        self.timeout = timeout
        pem = ssl.get_server_certificate((host, port)).encode()
        creds = grpc.ssl_channel_credentials(root_certificates=pem)
        options = [("grpc.ssl_target_name_override", _cert_override(pem))]
        self._channel = grpc.secure_channel(f"{host}:{port}", creds, options=options)
        self._stub = gNMIStub(self._channel)

    def get(self, target, paths):
        req = GetRequest(
            prefix=Path(target=target),
            path=[to_gnmi_path(p) for p in paths],
            encoding=4,  # JSON_IETF
        )
        resp = self._stub.Get(req, timeout=self.timeout)
        vals = {}
        for notif in resp.notification:
            for upd in notif.update:
                key = "/".join(e.name for e in upd.path.elem)
                vals[key] = json.loads(upd.val.json_ietf_val)
        return vals


# ─── payload → dashboard-metric-contract samples (adapted from the same pipeline) ──
# Missing or non-numeric fields are skipped — the dashboard shows gaps, never made-up
# zeros. Samples are (metric_name, labels, float_value).

_STATE_MAPPINGS = {
    "TEMPERATURE_INFO": [("temperature", "sonic_temperature_celsius", "sensor")],
    "PSU_INFO": [
        ("output_power", "sonic_psu_output_power_watts", "psu"),
        ("input_power", "sonic_psu_input_power_watts", "psu"),
    ],
    "FAN_INFO": [("speed", "sonic_fan_rpm", "fan")],
}

_PORT_STAT_METRICS = {
    "SAI_PORT_STAT_PFC_3_RX_PKTS": "sonic_pfc_rx_pause_frames",
    "SAI_PORT_STAT_PFC_3_TX_PKTS": "sonic_pfc_tx_pause_frames",
    "SAI_PORT_STAT_IF_IN_OCTETS": "sonic_interface_received_bytes",
    "SAI_PORT_STAT_IF_OUT_OCTETS": "sonic_interface_transmitted_bytes",
    "SAI_PORT_STAT_IF_IN_ERRORS": "sonic_interface_input_errors",
    "SAI_PORT_STAT_IF_OUT_DISCARDS": "sonic_interface_output_discards",
}

_QUEUE_STAT_METRICS = {
    "SAI_QUEUE_STAT_WRED_ECN_MARKED_PACKETS": "sonic_ecn_marked_packets",
    "SAI_QUEUE_STAT_DROPPED_PACKETS": "sonic_queue_wred_dropped_packets",
}


def _num(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def samples_from_state_db(instance, tables):
    for table, mappings in _STATE_MAPPINGS.items():
        entries = tables.get(table) or {}
        for entry_name, fields in entries.items():
            for field, metric, label_key in mappings:
                value = _num(fields.get(field))
                if value is not None:
                    yield metric, {"instance": instance, label_key: entry_name}, value


def samples_from_port_counters(instance, interface, stats):
    labels = {"instance": instance, "interface": interface}
    for stat, metric in _PORT_STAT_METRICS.items():
        value = _num(stats.get(stat))
        if value is not None:
            yield metric, labels, value


def samples_from_queue_counters(instance, queues):
    for queue_name, stats in queues.items():
        interface = queue_name.rsplit(":", 1)[0]
        labels = {"instance": instance, "interface": interface}
        for stat, metric in _QUEUE_STAT_METRICS.items():
            value = _num(stats.get(stat))
            if value is not None:
                yield metric, labels, value
