"""Windows shell integration, per user (no admin rights): start with Windows
and an entry in Explorer's "Send to" menu. Plus network access through the
Windows Firewall, the one part that needs an admin prompt.

The names match packaging/windows/multidrop.iss, so the installer and the
tray menu switch the same entries.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

APP_NAME = "MultiDrop"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launcher() -> tuple[str, list[str]]:
    """Program and arguments that start the tray app: the .exe when frozen,
    else pythonw (no console window) with the module."""
    if getattr(sys, "frozen", False):
        return sys.executable, []
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else exe), ["-m", "multidrop.windows"]


def _icon() -> str:
    """The .exe's own icon; in a source checkout the repository's .ico (a
    PyInstaller temp dir would be gone after exit)."""
    if getattr(sys, "frozen", False):
        return f"{sys.executable},0"
    return f"{Path(__file__).resolve().parents[2] / 'data' / 'icons' / 'multidrop.ico'},0"


def _command(*extra: str) -> str:
    program, args = launcher()
    return subprocess.list2cmdline([program, *args, *extra])


# -- start with Windows ------------------------------------------------------

def autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if on:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _command("--no-browser"))
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


# -- Explorer "Send to" --------------------------------------------------------

def sendto_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "SendTo"


def sendto_link() -> Path:
    return sendto_dir() / f"{APP_NAME}.lnk"


def sendto_enabled() -> bool:
    return sendto_link().exists()


def set_sendto(on: bool) -> None:
    link = sendto_link()
    if not on:
        link.unlink(missing_ok=True)
        return
    program, args = launcher()
    create_shortcut(link, program, subprocess.list2cmdline(args), _icon())


# -- network access (Windows Firewall) ------------------------------------------
#
# Phones can only reach MultiDrop if the firewall lets them in. One rule for
# every network type, but only for devices on the same local network
# (remoteip=localsubnet): Windows calls most Wi-Fis "public", and nothing on
# the internet ever gets through. Firewall rules are per machine, so this
# needs admin rights once: the installer, or MultiDrop itself on first start,
# runs MultiDrop elevated with --allow-network, which calls allow_network().

FIREWALL_RULE = f"{APP_NAME} (local network)"
OLD_FIREWALL_RULES = (APP_NAME,)        # private networks only, before 0.2


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def firewall_rule_present() -> bool:
    return _run(["netsh", "advfirewall", "firewall", "show", "rule",
                 f"name={FIREWALL_RULE}"]).returncode == 0


def firewall_commands(tcp: list[int], udp: list[int]) -> list[list[str]]:
    """Commands that replace MultiDrop's rules: deletes first, then adds."""
    base = ["netsh", "advfirewall", "firewall"]
    rule = [f"name={FIREWALL_RULE}", "dir=in", "action=allow", "profile=any",
            "remoteip=localsubnet"]
    ports = lambda ps: "localport=" + ",".join(map(str, sorted(set(ps))))  # noqa: E731
    deletes = [base + ["delete", "rule", f"name={name}"]
               for name in (FIREWALL_RULE, *OLD_FIREWALL_RULES)]
    return [*deletes,
            base + ["add", "rule", *rule, "protocol=TCP", ports(tcp)],
            base + ["add", "rule", *rule, "protocol=UDP", ports(udp)]]


def allow_network(tcp: list[int], udp: list[int]) -> int:
    """Runs elevated. 0 if the rules are in place."""
    failed = 0
    for cmd in firewall_commands(tcp, udp):
        r = _run(cmd)
        if cmd[3] == "add":             # a delete fails when there is nothing to delete
            failed += r.returncode != 0
    return 1 if failed else 0


def request_network_access(extra_args: list[str] = ()) -> bool:
    """Start MultiDrop elevated with --allow-network; Windows shows its admin
    prompt. False if the user said no or it failed."""
    program, args = launcher()
    script = ("$p = Start-Process -FilePath $env:MD_PROGRAM -ArgumentList $env:MD_ARGS "
              "-Verb RunAs -WindowStyle Hidden -Wait -PassThru -ErrorAction Stop; "
              "exit $p.ExitCode")
    # Values go through the environment, so paths are never parsed as code.
    env = {**os.environ, "MD_PROGRAM": program,
           "MD_ARGS": subprocess.list2cmdline([*args, "--allow-network", *extra_args])}
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=env, capture_output=True, timeout=600,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode == 0


def create_shortcut(link: Path, target: str, arguments: str, icon: str) -> None:
    """Write a .lnk with WScript.Shell; values go through the environment, so
    paths are never parsed as PowerShell code."""
    link.parent.mkdir(parents=True, exist_ok=True)
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:MD_LINK); "
              "$s.TargetPath = $env:MD_TARGET; $s.Arguments = $env:MD_ARGS; "
              "$s.IconLocation = $env:MD_ICON; $s.Description = 'Send with MultiDrop'; "
              "$s.Save()")
    env = {**os.environ, "MD_LINK": str(link), "MD_TARGET": target,
           "MD_ARGS": arguments, "MD_ICON": icon}
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                   env=env, check=True, capture_output=True, timeout=30,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
