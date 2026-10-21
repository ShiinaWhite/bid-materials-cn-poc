import json
import subprocess
import sys


def processes():
    script = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
              "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'Umi-OCR|RapidOCR|dowse|python' } | "
              "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine,CreationDate | ConvertTo-Json -Compress")
    p = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                       capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    text = p.stdout.decode("utf-8-sig").strip()
    data = json.loads(text) if text else []
    return data if isinstance(data, list) else [data]


def owned_processes(root):
    from pathlib import Path
    root = Path(root).resolve()
    return [p for p in processes() if p.get("ExecutablePath")
            and Path(p["ExecutablePath"]).resolve().is_relative_to(root)]


def stop_console(process):
    """Signal only the child's hidden console, never the user's console."""
    code = ("import ctypes,time,sys; k=ctypes.WinDLL('kernel32',use_last_error=True); "
            "H=ctypes.WINFUNCTYPE(ctypes.c_int,ctypes.c_uint); h=H(lambda event:1); "
            "k.SetConsoleCtrlHandler.argtypes=[H,ctypes.c_int]; "
            "k.FreeConsole(); "
            "ok=k.AttachConsole(int(sys.argv[1])); "
            "k.SetConsoleCtrlHandler(h,True); "
            "sent=k.GenerateConsoleCtrlEvent(1,0) if ok else 0; "
            "time.sleep(.3); k.FreeConsole(); sys.exit(0 if sent else 1)")
    return subprocess.run([sys.executable, "-B", "-c", code, str(process.pid)],
                          capture_output=True, timeout=5,
                          creationflags=subprocess.CREATE_NO_WINDOW).returncode == 0
