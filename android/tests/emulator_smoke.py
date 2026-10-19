"""Device smoke: real Android KeyStore TLS server, consent UI, MediaStore, navigation."""
import hashlib
import http.client
import json
from pathlib import Path
import re
import ssl
import subprocess
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

APP = "de.digitaldanis.multidrop.audio"
ACTIVITY = "de.digitaldanis.multidrop.MainActivity"
OUT = Path("android/build/smoke")
OUT.mkdir(parents=True, exist_ok=True)


def adb(*args):
    return subprocess.check_output(["adb", *args], text=True)


def ui():
    adb("shell", "uiautomator", "dump", "/sdcard/window.xml")
    return ET.fromstring(adb("shell", "cat", "/sdcard/window.xml"))


def tap(label):
    for node in ui().iter("node"):
        # Dialog buttons are shown in capitals ("ANNEHMEN").
        if label.casefold() in node.attrib.get("text", "").casefold():
            x1, y1, x2, y2 = map(int, re.findall(r"\d+", node.attrib["bounds"]))
            adb("shell", "input", "tap", str((x1+x2)//2), str((y1+y2)//2))
            return
    raise AssertionError(f"UI label missing: {label}")


def screenshot(name):
    with (OUT / f"{name}.png").open("wb") as image:
        subprocess.run(["adb", "exec-out", "screencap", "-p"], stdout=image, check=True)


def request(path, data=None):
    # This is a loopback ADB smoke probe; production clients pin the discovered certificate.
    conn = http.client.HTTPSConnection("127.0.0.1", 53317, context=ssl._create_unverified_context(), timeout=100)
    conn.request("GET" if data is None else "POST", "/api/localsend/v2" + path, body=data,
                 headers={} if data is None else {"Content-Type": "application/json"})
    response = conn.getresponse()
    code, payload = response.status, response.read()
    conn.close()
    return code, payload


try:
    adb("install", "-r", "android/build/MultiDrop-Android-0.2.0.apk")
    adb("shell", "pm", "grant", APP, "android.permission.POST_NOTIFICATIONS")
    adb("shell", "am", "start", "-n", f"{APP}/{ACTIVITY}")
    adb("forward", "tcp:53317", "tcp:53317")
    for attempt in range(30):
        try:
            code, body = request("/info")
            if code == 200:
                info = json.loads(body)
                assert info["protocol"] == "https"
                assert len(info["fingerprint"]) == 64
                break
        except (OSError, http.client.HTTPException):
            time.sleep(1)
    else:
        raise AssertionError("Android KeyStore TLS receiver did not start")
    screenshot("01-sharing")
    tap("Audio")
    screenshot("02-audio")
    tap("Lautsprecher auswählen")
    screenshot("03-airplay")
    adb("shell", "input", "keyevent", "4")
    tap("Teilen")
    payload = b"MultiDrop emulator receive test\n"
    meta = {"info": {"alias": "Desktop smoke", "fingerprint": "smoke", "port": 53318, "protocol": "http"},
            "files": {"one": {"id": "one", "fileName": "smoke-test.txt", "size": len(payload),
                              "fileType": "text/plain", "sha256": hashlib.sha256(payload).hexdigest()}}}
    with ThreadPoolExecutor() as pool:
        offer = pool.submit(request, "/prepare-upload", json.dumps(meta).encode())
        for _ in range(20):
            if "Dateien empfangen?" in ET.tostring(ui(), encoding="unicode"):
                break
            time.sleep(.5)
        screenshot("04-receive-consent")
        tap("Annehmen")
        code, body = offer.result(timeout=10)
        assert code == 200, (code, body)
    session = json.loads(body)
    code, body = request(f'/upload?sessionId={session["sessionId"]}&fileId=one&token={session["files"]["one"]}', payload)
    assert code == 200, (code, body)
    saved = adb("shell", "cat", "/sdcard/Download/MultiDrop/smoke-test.txt")
    assert saved.encode() == payload
    tap("Verlauf")
    screenshot("05-history")
    assert "smoke-test.txt" in ET.tostring(ui(), encoding="unicode")
    assert adb("shell", "pidof", APP).strip()
    print("PASS: Android API 35 launch, navigation, KeyStore TLS, explicit consent, MediaStore save, history")
finally:
    (OUT / "logcat.txt").write_text(adb("logcat", "-d"))
    try:
        (OUT / "ui.xml").write_text(ET.tostring(ui(), encoding="unicode"))
    except Exception:
        pass
