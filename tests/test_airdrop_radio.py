"""Radio contract and session lifecycle, without root, Wi-Fi or Bluetooth."""

import asyncio
import json
import sys

import pytest

from multidrop import airdrop_radio as radio
from multidrop.core import TransferError

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="AirDrop radio (omdrop-owl) is Linux only")


@pytest.fixture
def helper(tmp_path, monkeypatch):
    helper = tmp_path / "omdrop-discoverable"
    log = tmp_path / "calls"
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"visible": False}))
    helper.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
with open(os.environ["RADIO_CALLS"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
state = json.loads(Path(os.environ["RADIO_STATE"]).read_text())
if sys.argv[1] == "probe":
    print(json.dumps({"backend": "owl", "hardware": True,
                      "missing": state.get("missing", [])}))
elif sys.argv[1] == "status":
    print(json.dumps({"visible": state["visible"]}))
elif sys.argv[1] == "start":
    sys.exit(state.get("start_code", 0))
''')
    helper.chmod(0o755)
    pkexec = tmp_path / "pkexec"
    pkexec.write_text('#!/bin/sh\nexec "$@"\n')
    pkexec.chmod(0o755)
    import os
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("RADIO_CALLS", str(log))
    monkeypatch.setenv("RADIO_STATE", str(state))
    monkeypatch.setattr(radio, "HELPER", helper)
    return log, state


async def test_helper_contract_and_cleanup(helper):
    log, _ = helper
    owl = radio.OwlRadio()
    await owl.start(600)
    await owl.stop()
    await owl.stop()
    assert log.read_text().splitlines() == ["probe --json", "status --json", "start 600", "stop"]


@pytest.mark.parametrize("state, error", [
    ({"visible": False, "missing": [{"id": "radio", "say": "AP on 6375 MHz"}]}, "6375"),
    ({"visible": True}, "already in use"),
    ({"visible": False, "start_code": 5}, "another process"),
    ({"visible": False, "start_code": 3}, "failed \\(3\\)"),
])
async def test_blocked_or_foreign_radio_is_not_stopped(helper, state, error):
    log, path = helper
    path.write_text(json.dumps(state))
    owl = radio.OwlRadio()
    with pytest.raises(TransferError, match=error):
        await owl.start(600)
    await owl.stop()
    assert "stop" not in log.read_text().splitlines()
    if state.get("missing") or state["visible"]:
        assert "start 600" not in log.read_text()


async def test_missing_helper_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(radio, "HELPER", tmp_path / "missing")
    with pytest.raises(TransferError, match="install the .deb"):
        await radio.OwlRadio().probe()


def test_session_matches_upstream_announcements(make_config):
    original = make_config("laptop")
    original.airdrop.port = 1234
    original.airdrop.bind = "127.0.0.1"
    original.airdrop.mdns_addresses = ["127.0.0.1"]
    cfg = radio.session_config(original)
    assert cfg.airdrop.enabled and cfg.airdrop.interface == "awdl0"
    assert cfg.airdrop.port == 8771 and cfg.airdrop.service_id == "mac"
    assert not cfg.airdrop.mdns and not cfg.airdrop.mdns_addresses and not cfg.airdrop.bind
    assert original.airdrop.port == 1234 and not original.airdrop.enabled


async def test_receiver_failure_stops_owned_radio(helper, make_config, monkeypatch):
    from multidrop import daemon
    monkeypatch.setattr(daemon, "_check_not_running", lambda p: None)

    async def fail(*a, **kw):
        raise TransferError("receiver failed")
    monkeypatch.setattr(daemon, "run", fail)
    with pytest.raises(TransferError, match="receiver failed"):
        await radio.run_session(make_config("laptop"), 10)
    assert helper[0].read_text().splitlines()[-1] == "stop"


async def test_existing_daemon_does_not_start_radio(helper, make_config, monkeypatch):
    from multidrop import daemon

    def fail(path):
        raise daemon.AlreadyRunning("already running")
    monkeypatch.setattr(daemon, "_check_not_running", fail)
    with pytest.raises(TransferError, match="stop multidropd"):
        await radio.run_session(make_config("laptop"), 10)
    assert not helper[0].exists()


async def test_radio_expiry_ends_session(helper):
    stop = asyncio.Event()
    await asyncio.wait_for(radio._watch(radio.OwlRadio(), stop, 60), 4)
    assert stop.is_set()


async def test_full_session_serves_and_cleans_up(helper, make_config, monkeypatch, tmp_path, capsys):
    from multidrop.daemon import socket_path
    cfg = make_config("AirDrop session")
    cfg.localsend.enabled = cfg.quickshare.enabled = False
    cfg.airdrop.enabled = True
    cfg.airdrop.bind = "127.0.0.1"
    cfg.airdrop.port = 0
    cfg.airdrop.mdns = False
    cfg.daemon.notifications = False
    cfg.daemon.web_port = 0
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(radio, "session_config", lambda c: c)
    # The fake helper reports expiry after the real daemon has started.
    await radio.run_session(cfg, 1)
    assert "AirDrop ready for 1 min. Web UI:" in capsys.readouterr().out
    assert helper[0].read_text().splitlines()[-1] == "stop"
    assert not socket_path(cfg).exists()


async def test_wifi_switched_before_radio_and_restored_after_failure(helper, make_config,
                                                                     monkeypatch):
    from multidrop import daemon
    monkeypatch.setattr(daemon, "_check_not_running", lambda p: None)
    order = []

    class FakeWifi:
        async def prepare(self, minutes):
            order.append(("prepare", minutes, helper[0].exists()))
            return "Wi-Fi temporarily on channel 6"

        async def restore(self):
            order.append(("restore", helper[0].read_text().splitlines()[-1]))

    async def fail(*a, **kw):
        raise TransferError("receiver failed")
    monkeypatch.setattr(daemon, "run", fail)
    with pytest.raises(TransferError):
        await radio.run_session(make_config("laptop"), 10, wifi=FakeWifi())
    assert order == [("prepare", 10, False), ("restore", "stop")]
