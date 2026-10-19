"""Click test of the Windows app, entirely on 127.0.0.1.

Starts the real tray app (python -m multidrop.windows) in a throw-away profile
(own APPDATA, runtime dir, config; discovery on loopback only, no browser
window) and a simulated "phone" (multidrop receive). Then:

  1. Explorer's "Send to": a second start with a file hands it over, and the
     app window (WebView2) comes up listing the file
  2. app window: "Got it" on the welcome card, the "Send to" setting on and
     off, closing only hides the window
  3. web UI (headless Chromium): a click on the phone sends the file
  4. tray menu: "Show in "Send to" menu" on and off (shortcut in the temp profile)
  5. tray menu: "Play audio on" opens the AirPlay speaker list
  6. tray menu: "Quit" ends the app

The tray menu is opened with the message Explorer sends on a right-click on
the icon; window and menu entries are clicked through UI Automation (the
mouse is not moved). Prints one line per step; screenshots and the app log go to OUT_DIR.

  pip install playwright pywinauto
  python -m playwright install chromium-headless-shell
  python tools/dev/win_click_test.py [OUT_DIR]

Without disturbing the screen you are using (own invisible desktop):

  python tools/dev/run_on_hidden_desktop.py OUT_DIR/run.log -- python tools/dev/win_click_test.py OUT_DIR
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WM_NOTIFY = 0x400 + 11      # pystray's tray callback message
WM_RBUTTONUP = 0x0205
WM_KEYDOWN, VK_ESCAPE = 0x0100, 0x1B


def free_port(kind=socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(pred, what: str, timeout: float = 20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = pred()
        if result:
            return result
        time.sleep(0.2)
    raise AssertionError(f"timed out: {what}")


def step(text: str) -> None:
    print(f"ok  {text}", flush=True)


def process_tree(pid: int) -> list[int]:
    """pid and its children: a venv's python.exe starts the real interpreter
    as a child process."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}').ProcessId"],
                         capture_output=True, text=True, timeout=30).stdout
    return [pid, *map(int, out.split())]


def app_window(pid: int):
    """The visible app window, found among the app's processes."""
    from pywinauto import Desktop
    pids = set(process_tree(pid))
    return next((w for w in Desktop(backend="uia").windows(title="MultiDrop")
                 if w.process_id() in pids), None)


def tray_menu_item(pid: int, name: str):
    """Open the tray icon's menu and return its entry `name`."""
    import ctypes

    from pywinauto import Desktop, findwindows
    hwnds = [h for p in process_tree(pid)
             for h in findwindows.find_windows(process=p, visible_only=False,
                                               class_name_re=r"MultiDrop\d+SystemTrayIcon")]
    assert hwnds, "no tray icon window"
    for hwnd in hwnds:
        ctypes.windll.user32.PostMessageW(hwnd, WM_NOTIFY, 0, WM_RBUTTONUP)
    menu = Desktop(backend="uia").window(class_name="#32768", control_type="Menu")
    menu.wait("visible", timeout=10)
    return menu.child_window(title=name, control_type="MenuItem")


def main(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="multidrop-click-"))
    mc, pc_ls, pc_qs = free_port(socket.SOCK_DGRAM), free_port(), free_port()
    phone_ls, phone_qs = free_port(), free_port()
    cfg = root / "pc.toml"
    cfg.write_text(f'''alias = "pc"
download_dir = "{(root / 'pc-in').as_posix()}"
state_dir = "{(root / 'pc-state').as_posix()}"
[localsend]
port = {pc_ls}
bind = "127.0.0.1"
multicast_port = {mc}
multicast_interface = "127.0.0.1"
share_port = 0
share_bind = "127.0.0.1"
[quickshare]
port = {pc_qs}
bind = "127.0.0.1"
mdns_addresses = ["127.0.0.1"]
ble = false
[daemon]
web_port = 0
''')
    # Loopback only: the firewall is irrelevant, and its admin prompt must not appear.
    (root / "pc-state").mkdir()
    (root / "pc-state" / "windows-network-asked").touch()
    py = sys.executable
    # A no-op "browser": the app must not open windows on the desktop.
    env = {**os.environ, "MULTIDROP_CONFIG": str(cfg), "XDG_RUNTIME_DIR": str(root / "rt"),
           "XDG_DATA_HOME": str(root / "data"), "APPDATA": str(root / "appdata"),
           "BROWSER": f"{Path(py).as_posix()} -c pass",
           # WebView2 builds its UI Automation tree only on request; force it.
           "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--force-renderer-accessibility"}
    phone_env = {**env, "MULTIDROP_CONFIG": str(root / "none.toml"),
                 "XDG_RUNTIME_DIR": str(root / "rt-phone"),
                 "XDG_DATA_HOME": str(root / "phone-data")}
    inbox = root / "phone-in"
    phone = subprocess.Popen(
        [py, "-m", "multidrop", "--alias", "phone", "--port", str(phone_ls),
         "--multicast-port", str(mc), "--multicast-interface", "127.0.0.1",
         "--qs-port", str(phone_qs), "--mdns-address", "127.0.0.1", "--no-ble",
         "receive", "--accept", "all", "--dir", str(inbox)],
        env=phone_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    app = subprocess.Popen([py, "-m", "multidrop.windows", "--no-browser"], env=env)
    url_file = root / "rt" / "multidrop" / "url"
    try:
        url = wait(lambda: url_file.exists() and url_file.read_text().strip(), "app start")
        step("tray app started")

        photo = root / "Foto.jpg"
        photo.write_bytes(os.urandom(200_000))
        second = subprocess.run([py, "-m", "multidrop.windows", str(photo)], env=env, timeout=60)
        assert second.returncode == 0, f"second start: exit {second.returncode}"
        step("Send to: second start handed the file over")

        win = wait(lambda: app_window(app.pid), "app window shown after Send to")
        wait(lambda: any(d.window_text().endswith("Foto.jpg")       # name = full path
                         for d in win.descendants(control_type="ListItem")),
             "file listed in the window")
        try:
            win.capture_as_image().save(out / "0-window.png")
        except OSError:                 # no screen grab on a hidden desktop
            pass
        step("app window came up with the handed-over file")
        from pywinauto import Desktop
        spec = Desktop(backend="uia").window(handle=win.handle)
        spec.child_window(title="Got it", control_type="Button").wait("visible", 15).invoke()
        wait((root / "pc-state" / "windows-welcome-done").exists, "welcome remembered")
        step('window: "Got it" on the welcome card')
        from multidrop import winshell
        blocked = not winshell.firewall_rule_present()

        def notice_shown():
            return any(d.window_text() == "Let phones connect"
                       for d in win.descendants(control_type="Button"))
        wait(lambda: notice_shown() == blocked, "firewall notice matches the firewall")
        step(f"window: firewall notice {'shown' if blocked else 'hidden'} (rule "
             f"{'missing' if blocked else 'present'})")
        link = root / "appdata" / "Microsoft" / "Windows" / "SendTo" / "MultiDrop.lnk"
        box = wait(lambda: next((c for c in win.descendants(control_type="CheckBox")
                                 if "right-click" in c.window_text()), None),
                   '"Send to" setting in the window')
        box.toggle()
        wait(link.exists, "Send to shortcut from the window")
        box.toggle()
        wait(lambda: not link.exists(), "Send to shortcut removed from the window")
        step('window: "Send to" setting switched on and off')
        win.close()
        wait(lambda: app_window(app.pid) is None, "window hidden after closing")
        assert app.poll() is None, "closing the window ended the app"
        step("closing the window keeps MultiDrop running")

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(url)
            page.wait_for_selector("#selection-section:not([hidden]) li:text('Foto.jpg')")
            page.screenshot(path=str(out / "1-selection.png"))
            step("UI lists the handed-over file")
            page.click("#refresh")
            card = page.locator("#devices .card", has=page.locator(".name:text-is('phone')"))
            card.wait_for(timeout=20_000)
            card.locator(".name").click()
            wait(lambda: (inbox / "Foto.jpg").exists()
                 and (inbox / "Foto.jpg").read_bytes() == photo.read_bytes(),
                 "file at the phone", 30)
            page.wait_for_selector("#selection-section", state="hidden")
            page.screenshot(path=str(out / "2-sent.png"))
            browser.close()
            assert not errors, errors
        step("click on the phone sent the file")

        if os.environ.get("CLICK_TEST_HIDDEN_DESKTOP"):
            # Windows opens tray menus only on the visible desktop.
            print("skip tray menu steps (hidden desktop)", flush=True)
            app.terminate()
        else:
            tray_menu_item(app.pid, 'Show in "Send to" menu').invoke()
            wait(link.exists, "Send to shortcut")
            tray_menu_item(app.pid, 'Show in "Send to" menu').invoke()
            wait(lambda: not link.exists(), "Send to shortcut removed")
            step('tray: "Send to" entry switched on and off')

            import ctypes

            from pywinauto import Desktop
            tray_menu_item(app.pid, "Play audio on").expand()
            menus = wait(lambda: [w for w in Desktop(backend="uia").windows(class_name="#32768")
                                  if w.descendants(title="This computer")] and
                         Desktop(backend="uia").windows(class_name="#32768"),
                         "AirPlay speaker submenu", 10)
            for menu in reversed(menus):    # Esc to the menus themselves, never as global input
                ctypes.windll.user32.PostMessageW(menu.handle, WM_KEYDOWN, VK_ESCAPE, 0)
            step('tray: "Play audio on" lists the speakers')

            tray_menu_item(app.pid, "Quit").invoke()
            assert app.wait(20) == 0, f"exit code {app.returncode}"
            step("tray: Quit ended the app")
        print("PASS", flush=True)
        return 0
    except Exception as e:
        print(f"FAIL {type(e).__name__}: {e}", flush=True)
        return 1
    finally:
        for p in (app, phone):
            if p.poll() is None:
                p.kill()
        log = root / "rt" / "multidrop.log"
        if log.exists():
            (out / "multidrop.log").write_bytes(log.read_bytes())


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1
                  else Path(tempfile.gettempdir()) / "multidrop-click"))
