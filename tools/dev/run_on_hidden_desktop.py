"""Run a command on a separate, invisible Windows desktop.

Windows, menus and focus changes of a click test then never reach the screen
you are working (or playing) on. Child processes inherit the desktop and get
CLICK_TEST_HIDDEN_DESKTOP=1 (Windows shows tray menus only on the visible
desktop, so the click test skips those steps). Output
goes to OUT_LOG and is printed when the command ends; the exit code is passed
through.

  python tools/dev/run_on_hidden_desktop.py OUT_LOG -- python tools/dev/win_click_test.py OUT_DIR
"""

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

DESKTOP_ALL = 0x01FF
INFINITE = 0xFFFFFFFF
CREATE_NO_WINDOW = 0x08000000


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


def run(log: str, argv: list[str], name: str = "multidrop-click-test") -> int:
    os.environ["CLICK_TEST_HIDDEN_DESKTOP"] = "1"       # inherited by the child
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.CreateDesktopW.restype = wintypes.HANDLE
    desktop = user32.CreateDesktopW(name, None, None, 0, DESKTOP_ALL, None)
    if not desktop:
        raise ctypes.WinError()
    try:
        # cmd only for the redirection; the command line is quoted by list2cmdline.
        command = f'cmd /c "{subprocess.list2cmdline(argv)} > "{log}" 2>&1"'
        si = STARTUPINFOW(cb=ctypes.sizeof(STARTUPINFOW), lpDesktop=f"WinSta0\\{name}")
        pi = PROCESS_INFORMATION()
        if not kernel32.CreateProcessW(None, ctypes.create_unicode_buffer(command), None, None,
                                       False, CREATE_NO_WINDOW, None, None,
                                       ctypes.byref(si), ctypes.byref(pi)):
            raise ctypes.WinError()
        kernel32.WaitForSingleObject(pi.hProcess, INFINITE)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        kernel32.CloseHandle(pi.hThread)
        kernel32.CloseHandle(pi.hProcess)
        return code.value
    finally:
        user32.CloseDesktop(desktop)


if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        sys.exit(__doc__)
    exit_code = run(sys.argv[1], sys.argv[3:])
    with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
        sys.stdout.write(f.read())
    sys.exit(exit_code)
