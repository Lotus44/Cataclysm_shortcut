# -*- coding: utf-8 -*-
"""创建快捷方式：Cataclysm_shortcut.lnk

用 Python 生成 .lnk（通过 WScript.Shell COM），而不是让 PowerShell 脚本里带中文：
Windows PowerShell 5.1 会把无 BOM 的 UTF-8 脚本按 ANSI(936/GBK) 读，
脚本内的中文字面量会被打乱；Python 处理 Unicode 没有这个坑。

用法:  python make_shortcut.py [快捷方式输出目录]
"""

from __future__ import annotations

import os
import subprocess
import sys

SHORTCUT_NAME = "Cataclysm_shortcut.lnk"
PYTHON_FALLBACKS = [
    r"C:\Python314\pythonw.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"),
]


def find_pythonw() -> str | None:
    try:
        out = subprocess.run(
            ["where.exe", "pythonw"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout or ""
        for line in out.splitlines():
            candidate = line.strip()
            if candidate and os.path.isfile(candidate):
                return candidate
    except Exception:
        pass
    for candidate in PYTHON_FALLBACKS:
        if os.path.isfile(candidate):
            return candidate
    import glob

    matches = glob.glob(r"C:\Python*\pythonw.exe")
    return matches[0] if matches else None


def create_shortcut(here: str, pythonw: str, script: str, link_path: str) -> None:
    """通过 WScript.Shell 写 .lnk（避免依赖非标准库）。"""
    ps = (
        "$ErrorActionPreference='Stop';"
        "$s=New-Object -ComObject WScript.Shell;"
        f"$l=$s.CreateShortcut('{link_path}');"
        f"$l.TargetPath='{pythonw}';"
        f"$l.Arguments='\"{script}\"';"
        f"$l.WorkingDirectory='{here}';"
        "$l.WindowStyle=7;"
        f"$l.IconLocation='{pythonw},0';"
        "$l.Description='Cataclysm Shortcut - save snapshot tool for CDDA / CCB';"
        "$l.Save()"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "powershell failed").strip())


def clean_dir_arg(raw: str) -> str:
    """把命令行传来的目录参数洗干净。

    ``create_shortcut.bat`` 用 ``"%~dp0"`` 传目录，而 ``%~dp0`` **自带结尾反斜杠**：
    命令行变成 ``"E:\\...\\CDDA快照工具\\"``。按 Windows 的参数解析规则，
    引号前的反斜杠会**转义那个引号**，于是 Python 拿到的 argv 里带着一个字面量
    引号 —— 路径变成 ``...\\CDDA快照工具"``，再去拼 ``cdda_snapshot.pyw``
    自然就"找不到文件"（实测就栽在这，而且中文目录名让报错更难读）。
    这里统一剥掉首尾引号和结尾的分隔符。
    """
    text = (raw or "").strip().strip('"').strip()
    return os.path.abspath(text.rstrip("\\/") or ".")


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if len(argv) > 1:
        here = clean_dir_arg(argv[1])
    else:
        here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "cdda_snapshot.pyw")
    if not os.path.isfile(script):
        print(f"[错误] 找不到 {script}")
        return 1

    pythonw = find_pythonw()
    if not pythonw:
        print("[错误] 找不到 pythonw.exe，请先安装 Python 3.9+。")
        return 1
    print("pythonw :", pythonw)

    link_path = os.path.join(here, SHORTCUT_NAME)
    try:
        create_shortcut(here, pythonw, script, link_path)
    except Exception as exc:
        print("[错误] 创建快捷方式失败:", exc)
        return 1

    if not os.path.isfile(link_path):
        print("[错误] 快捷方式未生成:", link_path)
        return 1

    print("快捷方式已创建:", link_path)
    print("  目标:", pythonw, '"' + script + '"')
    print("  工作目录:", here)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
