"""Helpers of the desktop app (the GTK part itself needs a display)."""

import http.server
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading

import pytest

from multidrop import desktop

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="GTK desktop app is Linux only")


def test_daemon_pid_needs_a_live_socket(tmp_path):
    sock = tmp_path / "multidropd.sock"
    assert desktop.daemon_pid(sock) is None
    with socket.socket(socket.AF_UNIX) as s:
        s.bind(str(sock))
        s.listen()
        assert desktop.daemon_pid(sock) == os.getpid()


def test_is_airdrop_session():
    proc = subprocess.Popen([sys.executable, "-c", "print(); import time; time.sleep(5)",
                             "airdrop", "run"], stdout=subprocess.PIPE)
    try:
        proc.stdout.readline()                  # exec'd: /proc/<pid>/cmdline is final
        assert desktop.is_airdrop_session(proc.pid)
        assert not desktop.is_airdrop_session(os.getpid())
    finally:
        proc.kill()
        proc.wait()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def address_string(self):
        return "unix"

    def log_message(self, *args):
        pass

    def _reply(self, status, body: bytes, ctype="application/json", chunked=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if chunked:
            for part in (body[:7], body[7:]):       # split mid-line
                self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/events"):
            events = b"".join(b"id: %d\ndata: %s\n\n" % (i, json.dumps({"seq": i}).encode())
                              for i in (1, 2))
            self._reply(200, b": keep-alive\n\n" + events, "text/event-stream", chunked=True)
        else:
            self._reply(200, b'{"alias": "pc"}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._reply(400, json.dumps({"error": f"no {body['x']}"}).encode())


@pytest.fixture
def api(tmp_path):
    sock = tmp_path / "s"
    server = socketserver.ThreadingUnixStreamServer(str(sock), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield desktop.Api(sock)
    server.shutdown()
    server.server_close()


def test_api_request_and_errors(api):
    assert api.request("GET", "/api/status") == {"alias": "pc"}
    with pytest.raises(desktop.ApiError, match="no thing"):
        api.request("POST", "/api/send", {"x": "thing"})


def test_api_events_parses_chunked_sse(api):
    assert [e["seq"] for e in api.events()] == [1, 2]


def test_api_unreachable(tmp_path):
    with pytest.raises(desktop.ApiError):
        desktop.Api(tmp_path / "none").request("GET", "/api/status")


def test_mirror_targets_from_announced_features():
    caps = {"56:DC:90:FE:41:BC": {"screen": True}, "10.0.0.66": {"screen": False}}
    assert desktop.is_mirror_target({"device_id": "56:dc:90:fe:41:bc", "ip": "10.0.0.7"}, caps)
    assert not desktop.is_mirror_target({"device_id": "34:7E", "ip": "10.0.0.66"}, caps)
    assert desktop.is_mirror_target({"device_id": "AA:BB", "ip": "10.0.0.9"}, caps)  # unknown


class _DoubletakeHandler(socketserver.StreamRequestHandler):
    """doubletake's daemon protocol: one JSON request, one JSON response."""
    seen: list = []

    def handle(self):
        req = json.loads(self.rfile.readline())
        type(self).seen.append(req)
        resp = {"ok": True, "state": "idle"}
        if req["cmd"] == "devices":
            resp["devices"] = [{"name": "Entrance", "model": "AppleTV6,2",
                                "ip": "10.0.0.7", "port": 7000}]
        self.wfile.write(json.dumps(resp).encode() + b"\n")


def test_mirror_request_protocol(tmp_path):
    sock = tmp_path / "dt.sock"
    server = socketserver.ThreadingUnixStreamServer(str(sock), _DoubletakeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        m = desktop.Mirror(sock)
        assert m.alive()
        assert m.request("devices")["devices"][0]["name"] == "Entrance"
        m.request("connect", target="10.0.0.7", port=7000, pin="")
        assert _DoubletakeHandler.seen[-1] == {"cmd": "connect", "target": "10.0.0.7",
                                               "port": 7000}
    finally:
        server.shutdown()
        server.server_close()
    assert not desktop.Mirror(tmp_path / "none").alive()


def test_parse_sinks_finds_airplay_and_default():
    dump = [
        {"id": 47, "type": "PipeWire:Interface:Node",
         "info": {"props": {"media.class": "Audio/Sink",
                            "node.name": "alsa_output.pci-0000_c4_00.6.analog-stereo"}}},
        {"id": 86, "type": "PipeWire:Interface:Node",
         "info": {"props": {"media.class": "Audio/Sink", "node.description": "Office",
                            "node.name": "raop_sink.Sonos-000000000001.local.192.168.1.69.7000"}}},
        {"id": 108, "type": "PipeWire:Interface:Node",
         "info": {"props": {"media.class": "Audio/Sink", "node.description": "Entrance",
                            "node.name": "raop_sink.Entrance.local.192.168.1.177.7000"}}},
        {"id": 90, "type": "PipeWire:Interface:Node",       # a stream, not a sink
         "info": {"props": {"media.class": "Stream/Output/Audio", "node.name": "raop_sink.x"}}},
        {"id": 33, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
         "metadata": [
             {"subject": 0, "key": "default.configured.audio.sink",
              "value": {"name": "bluez_output.00_C5_85_E1_DC_AB.1"}},
             {"subject": 0, "key": "default.audio.sink",
              "value": {"name": "raop_sink.Sonos-000000000001.local.192.168.1.69.7000"}}]},
    ]
    sinks, default, configured = desktop.parse_sinks(dump)
    assert [d["name"] for d in sinks] == ["Entrance", "Office"]
    assert default == sinks[1]["node"]
    assert json.loads(configured) == {"name": "bluez_output.00_C5_85_E1_DC_AB.1"}
