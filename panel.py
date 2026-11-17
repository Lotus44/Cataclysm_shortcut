#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cataclysm Shortcut —— 弹出式面板（无常驻窗口，CDDA / CCB 通用）

正常使用时本模块不显示任何窗口：只有按下全局热键（默认 F4 / F5 / F6）才会在屏幕中央
弹出一个深色面板，操作完成后自动淡出。

为避免拖入第三方依赖，确认框与气泡都是自己用 tkinter 画的。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from typing import Any, Callable, Optional

# ---------------------------------------------------------------- 主题

BG = "#14161a"
BG_ALT = "#1b1e24"
BG_SEL = "#2b3138"
#: 当前生效快照（刚存的新快照 / 刚读取的快照）的整行底色，比普通行更亮
BG_ACTIVE = "#33404a"
FG = "#d7dade"
FG_DIM = "#8b939c"
ACCENT = "#e8a33d"      # CDDA 味的琥珀色
ACCENT_DIM = "#8a6220"
OK = "#7fb069"
DANGER = "#d9534f"
LINE = "#2a2e35"

PANEL_W = 620
PANEL_H = 430


def _ui_font(size: int = 10, bold: bool = False) -> tuple:
    family = "Microsoft YaHei UI" if os.name == "nt" else "Helvetica"
    return (family, size, "bold") if bold else (family, size)


def _mono_font(size: int = 10) -> tuple:
    family = "Consolas" if os.name == "nt" else "Courier"
    return (family, size)


def fmt_relative(ts: float) -> str:
    delta = max(0.0, time.time() - ts)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // 86400)} 天前"


def fmt_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            return f"{int(num)} B" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"


def open_in_explorer(path: str) -> None:
    try:
        if os.path.isdir(path):
            os.startfile(path)  # type: ignore[attr-defined]
        elif os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    except Exception:
        logging.exception("打开目录失败: %s", path)


# ---------------------------------------------------------------- 小部件


class FlatButton(tk.Frame):
    """无边框扁平按钮（tk.Button 在深色主题下太丑）。"""

    def __init__(
        self,
        master,
        text: str,
        command: Callable[[], Any],
        *,
        accent: bool = False,
        danger: bool = False,
        width: Optional[int] = None,
    ):
        super().__init__(master, bg=BG_ALT, highlightthickness=1, highlightbackground=LINE,
                         highlightcolor=LINE, cursor="hand2")
        self._command = command
        base_fg = ACCENT if accent else (DANGER if danger else FG)
        self._label = tk.Label(
            self, text=text, bg=BG_ALT, fg=base_fg, font=_ui_font(10),
            padx=12, pady=5,
        )
        self._label.pack(fill="both", expand=True)
        if width:
            self._label.configure(width=width)

        for widget in (self, self._label):
            widget.bind("<Enter>", lambda _e: self._paint(BG_SEL, "#ffffff" if accent else FG))
            widget.bind("<Leave>", lambda _e: self._paint(BG_ALT, base_fg))
            widget.bind("<Button-1>", lambda _e: self._command())

    def _paint(self, bg: str, fg: str) -> None:
        self.configure(bg=bg)
        self._label.configure(bg=bg, fg=fg)

    def set_enabled(self, enabled: bool) -> None:
        self._label.configure(fg=FG if enabled else FG_DIM)
        self.configure(cursor="hand2" if enabled else "arrow")

    def set_text(self, text: str) -> None:
        try:
            self._label.configure(text=text)
        except Exception:
            pass


class Toast:
    """屏幕右下角的状态气泡。"""

    def __init__(self, master: tk.Misc):
        self.master = master
        self.win: Optional[tk.Toplevel] = None
        self._job: Optional[str] = None

    def show(self, message: str, *, error: bool = False, timeout: float = 3.0) -> None:
        self.hide()
        win = tk.Toplevel(self.master)
        self.win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", 0.94)
        except Exception:
            pass
        frame = tk.Frame(win, bg=BG_ALT, highlightthickness=1,
                         highlightbackground=DANGER if error else ACCENT_DIM)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame,
            text=message,
            bg=BG_ALT,
            fg=DANGER if error else FG,
            font=_ui_font(10),
            wraplength=420,
            justify="left",
            padx=14,
            pady=9,
        ).pack()
        win.update_idletasks()
        w = win.winfo_reqwidth()
        h = win.winfo_reqheight()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{sw - w - 24}+{sh - h - 64}")
        win.bind("<Button-1>", lambda _e: self.hide())
        if timeout > 0:
            self._job = self.master.after(int(timeout * 1000), self.hide)

    def hide(self) -> None:
        if self._job:
            try:
                self.master.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None


def confirm(master: tk.Misc, title: str, message: str,
            ok_text: str = "确定", cancel_text: str = "取消") -> bool:
    """自绘确认框（返回 True/False）。"""
    result = {"value": False}
    dlg = tk.Toplevel(master)
    dlg.overrideredirect(True)
    dlg.attributes("-topmost", True)
    dlg.configure(bg=BG)

    outer = tk.Frame(dlg, bg=BG, highlightthickness=1, highlightbackground=ACCENT_DIM)
    outer.pack(fill="both", expand=True)

    tk.Label(outer, text=title, bg=BG, fg=ACCENT, font=_ui_font(11, bold=True),
             anchor="w", padx=18).pack(fill="x", pady=(14, 4))
    tk.Label(outer, text=message, bg=BG, fg=FG, font=_ui_font(10), justify="left",
             wraplength=430, anchor="w", padx=18).pack(fill="x")

    row = tk.Frame(outer, bg=BG)
    row.pack(fill="x", padx=18, pady=16)

    def _done(value: bool) -> None:
        result["value"] = value
        dlg.destroy()

    FlatButton(row, cancel_text, lambda: _done(False)).pack(side="right", padx=(8, 0))
    FlatButton(row, ok_text, lambda: _done(True), accent=True).pack(side="right")

    dlg.bind("<Return>", lambda _e: _done(True))
    dlg.bind("<Escape>", lambda _e: _done(False))

    dlg.update_idletasks()
    w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
    sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
    dlg.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
    dlg.focus_force()
    dlg.grab_set()
    dlg.wait_window()
    return result["value"]


# ---------------------------------------------------------------- 主面板


def ask_text(master: tk.Misc, title: str, message: str,
             initial: str = "") -> Optional[str]:
    """自绘文字输入框。返回输入的字符串；取消时返回 None。"""
    result: dict[str, Optional[str]] = {"value": None}
    dlg = tk.Toplevel(master)
    dlg.overrideredirect(True)
    dlg.attributes("-topmost", True)
    dlg.configure(bg=BG)

    outer = tk.Frame(dlg, bg=BG, highlightthickness=1, highlightbackground=ACCENT_DIM)
    outer.pack(fill="both", expand=True)

    tk.Label(outer, text=title, bg=BG, fg=ACCENT, font=_ui_font(11, bold=True),
             anchor="w", padx=18).pack(fill="x", pady=(14, 4))
    tk.Label(outer, text=message, bg=BG, fg=FG, font=_ui_font(10), justify="left",
             wraplength=430, anchor="w", padx=18).pack(fill="x")

    var = tk.StringVar(value=initial)
    entry = tk.Entry(outer, textvariable=var, bg=BG_ALT, fg=FG, insertbackground=FG,
                     relief="flat", font=_ui_font(11), width=34)
    entry.pack(fill="x", padx=18, pady=(12, 4), ipady=5)
    entry.focus_set()
    entry.selection_range(0, tk.END)

    hint = tk.Label(outer, text="留空 = 取消永久标记", bg=BG, fg=FG_DIM,
                    font=_ui_font(8), anchor="w", padx=18)
    hint.pack(fill="x")

    row = tk.Frame(outer, bg=BG)
    row.pack(fill="x", padx=18, pady=16)

    def _done(value: Optional[str]) -> None:
        result["value"] = value
        dlg.destroy()

    FlatButton(row, "取消", lambda: _done(None)).pack(side="right", padx=(8, 0))
    FlatButton(row, "确定", lambda: _done(var.get()), accent=True).pack(side="right")

    dlg.bind("<Return>", lambda _e: _done(var.get()))
    dlg.bind("<Escape>", lambda _e: _done(None))

    dlg.update_idletasks()
    dlg.deiconify()
    dlg.update_idletasks()
    w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
    sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
    dlg.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
    dlg.grab_set()
    dlg.focus_force()
    dlg.wait_window()
    return result["value"]


class QuickPanel:
    """弹出式操作面板：列表选快照、按键操作、自动隐藏。"""

    def __init__(self, app):
        self.app = app
        self.manager = app.manager
        self.config = app.config
        self.snapshots: list[Any] = []      # 倒序（最新在前）
        #: 当前生效的快照编号（刚拍的 / 刚读回的），用于列表高亮
        self.active_sid: Optional[int] = None
        self.visible = False
        self.modal_open = False             # 有模态对话框时不做自动隐藏
        self._autohide_job: Optional[str] = None
        self._last_activity = time.time()
        self._confirm_until = 0.0
        self._confirm_sid: Optional[int] = None
        self._game_running = False
        self._proc_job: Optional[str] = None
        #: 快照文件夹占用统计（后台线程扫盘，避免上万个小文件卡住界面）
        self._size_running = False
        self._size_pending = False

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Cataclysm Shortcut")
        self.root.configure(bg=BG)

        self.toast_win = Toast(self.root)
        self.win: Optional[tk.Toplevel] = None
        self._build()
        self._schedule_process_check()

    # -------------------------------------------------- 构建

    def _build(self) -> None:
        win = tk.Toplevel(self.root)
        self.win = win
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG)

        shell = tk.Frame(win, bg=BG, highlightthickness=1, highlightbackground=ACCENT_DIM)
        shell.pack(fill="both", expand=True)
        win.bind("<ButtonPress-1>", self._drag_start)
        win.bind("<B1-Motion>", self._drag_move)

        # ---- 标题栏
        head = tk.Frame(shell, bg=BG_ALT)
        head.pack(fill="x")
        head.bind("<ButtonPress-1>", self._drag_start)
        head.bind("<B1-Motion>", self._drag_move)

        title = tk.Label(head, text="Cataclysm Shortcut", bg=BG_ALT, fg=ACCENT,
                         font=_ui_font(11, bold=True))
        title.pack(side="left", padx=(12, 6), pady=7)
        title.bind("<ButtonPress-1>", self._drag_start)
        title.bind("<B1-Motion>", self._drag_move)

        self.game_label = tk.Label(head, text="● 游戏未运行", bg=BG_ALT, fg=FG_DIM, font=_ui_font(9))
        self.game_label.pack(side="left", padx=(4, 0))

        close = tk.Label(head, text="✕", bg=BG_ALT, fg=FG_DIM, font=_ui_font(10),
                         cursor="hand2", padx=12)
        close.pack(side="right", pady=6)
        close.bind("<Button-1>", lambda _e: self.hide())
        close.bind("<Enter>", lambda _e: close.configure(fg="#ffffff"))
        close.bind("<Leave>", lambda _e: close.configure(fg=FG_DIM))

        # ---- 世界信息
        info = tk.Frame(shell, bg=BG)
        info.pack(fill="x", padx=14, pady=(10, 2))
        self.world_label = tk.Label(info, text="…", bg=BG, fg=FG, font=_ui_font(10, bold=True),
                                    anchor="w", justify="left")
        self.world_label.pack(fill="x")
        self.sub_label = tk.Label(info, text="", bg=BG, fg=FG_DIM, font=_ui_font(9),
                                  anchor="w", justify="left")
        self.sub_label.pack(fill="x")

        # ---- 按钮行（分两行，避免挤爆面板宽度）
        row = tk.Frame(shell, bg=BG)
        row.pack(fill="x", padx=14, pady=(10, 4))
        self.snap_button = FlatButton(row, "▣ 快速保存并快照  (F5)", self.app.do_snapshot, accent=True)
        self.snap_button.pack(side="left")
        self.restore_button = FlatButton(row, "↺ 还原最新  (F6)", self.restore_latest)
        self.restore_button.pack(side="left", padx=(8, 0))
        FlatButton(row, "⟳ 刷新", self.refresh).pack(side="right")

        row2 = tk.Frame(shell, bg=BG)
        row2.pack(fill="x", padx=14, pady=(0, 2))
        self.disk_button = FlatButton(row2, "只存磁盘快照  (Ctrl+F5)", self.snapshot_disk)
        self.disk_button.pack(side="left")
        self.rename_button = FlatButton(row2, "✎ 命名/永久存档", self.rename_selected)
        self.rename_button.pack(side="left", padx=(8, 0))
        FlatButton(row2, "热键设置", self.open_hotkey_settings).pack(side="right")
        FlatButton(row2, "目录", self.open_snapshot_dir).pack(side="right", padx=(0, 8))

        # ---- 保留配额提示
        self.quota_line = tk.Label(
            shell, text="", bg=BG, fg=FG_DIM, font=_ui_font(8),
            anchor="w", justify="left", wraplength=PANEL_W - 40,
        )
        self.quota_line.pack(fill="x", padx=14)

        # ---- 快照文件夹占用（逻辑大小 vs 硬链接实占）
        self.size_line = tk.Label(
            shell, text="快照文件夹：统计中…", bg=BG_ALT, fg=FG, font=_ui_font(9),
            anchor="w", justify="left", padx=10, pady=4, wraplength=PANEL_W - 40,
        )
        self.size_line.pack(fill="x", padx=14, pady=(6, 0))

        # ---- 快照列表
        listwrap = tk.Frame(shell, bg=BG)
        listwrap.pack(fill="both", expand=True, padx=14, pady=(2, 4))

        self.listbox = tk.Listbox(
            listwrap,
            bg=BG_ALT,
            fg=FG,
            selectbackground=BG_SEL,
            selectforeground=ACCENT,
            font=_ui_font(10),
            activestyle="none",
            highlightthickness=0,
            borderwidth=0,
            relief="flat",
            height=7,
            exportselection=False,
        )
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll = tk.Scrollbar(listwrap, orient="vertical", command=self.listbox.yview,
                              width=10, bg=BG_ALT, troughcolor=BG, borderwidth=0,
                              highlightthickness=0, relief="flat")
        scroll.pack(side="right", fill="y")
        self.listbox.configure(yscrollcommand=scroll.set)

        self.listbox.bind("<Double-Button-1>", lambda _e: self.restore_selected())
        self.listbox.bind("<<ListboxSelect>>", lambda _e: self._update_detail())
        self.listbox.bind("<Return>", lambda _e: self.restore_selected())
        self.listbox.bind("<Delete>", lambda _e: self.delete_selected())
        self.listbox.bind("<MouseWheel>", self._on_wheel)
        for key in "123456789":
            self.listbox.bind(f"<Key-{key}>", lambda _e, k=key: self._restore_by_digit(k))

        self.detail = tk.Label(shell, text="", bg=BG, fg=FG_DIM, font=_ui_font(9),
                               anchor="w", justify="left", wraplength=PANEL_W - 40)
        self.detail.pack(fill="x", padx=14)

        # ---- 快捷键提示（面板可见时的局部按键）
        self.hint = tk.Label(
            shell,
            text=("[1-9] 选择/再按一次还原   [Enter] 还原   [Del] 删除   [Esc] 隐藏\n"
                  "热键可在「热键设置」里改（点输入框直接按新键）"),
            bg=BG, fg=FG_DIM, font=_ui_font(8), anchor="w", justify="left", wraplength=PANEL_W - 40,
        )
        self.hint.pack(fill="x", padx=14, pady=(6, 0))

        self.hotkey_line = tk.Label(
            shell, text="", bg=BG, fg=ACCENT_DIM, font=_ui_font(8),
            anchor="w", justify="left", wraplength=PANEL_W - 40,
        )
        self.hotkey_line.pack(fill="x", padx=14)

        # ---- 状态栏
        self.status = tk.Label(shell, text="就绪", bg=BG_ALT, fg=FG_DIM, font=_ui_font(9),
                               anchor="w", justify="left", padx=12, pady=6,
                               wraplength=PANEL_W - 24)
        self.status.pack(fill="x", side="bottom")

        # ---- 全局按键（只在面板可见时生效）
        self.root.bind_all("<Escape>", lambda _e: self._on_escape())
        self.root.bind_all("<Control-q>", lambda _e: self.app.quit())

        self.win.bind("<Enter>", lambda _e: self._touch())
        self.win.bind("<Motion>", lambda _e: self._touch())
        self.win.bind("<Button-1>", lambda _e: self._touch())

    # -------------------------------------------------- 拖动

    def _drag_start(self, event) -> None:
        self._drag_origin = (event.x_root - self.win.winfo_x(), event.y_root - self.win.winfo_y())

    def _drag_move(self, event) -> None:
        if not hasattr(self, "_drag_origin"):
            return
        dx, dy = self._drag_origin
        self.win.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")
        self._touch()

    # -------------------------------------------------- 显示 / 隐藏

    def _fit_to_content(self) -> None:
        """让面板高度跟着内容走——状态栏换行、热键提示多一行都不会被截掉。

        取 ``winfo_reqheight()``（当前内容需要的总高）作为窗口高度，
        上限是屏幕高度留出边距。位置保持不变。
        """
        if self.win is None:
            return
        try:
            self.win.update_idletasks()
            needed = self.win.winfo_reqheight()
            screen_h = self.win.winfo_screenheight()
            # 实测窗口实际高度会比请求值少十几像素（Tk 与窗管的差异），
            # 于是最底部的状态栏会被压扁。这里补一点余量。
            needed += 24
            target = max(340, min(needed, screen_h - 120))
            width = max(PANEL_W, self.win.winfo_width())
            self.win.geometry(f"{width}x{target}+{self.win.winfo_x()}+{self.win.winfo_y()}")
        except Exception:
            logging.debug("自适应高度失败", exc_info=True)

    def show(self) -> None:
        if self.win is None:
            return
        self.refresh()
        if self.app.hotkeys is not None:
            self.apply_hotkeys(self.app.hotkeys)
        if not self.visible:
            # 先按内容量一次高度，避免固定的 PANEL_H 把多出来的行截掉
            self.win.update_idletasks()
            needed = self.win.winfo_reqheight()
            sh = self.win.winfo_screenheight()
            w = PANEL_W
            h = max(340, min(needed, sh - 120))
            sw = self.win.winfo_screenwidth()
            self.win.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(24, (sh - h) // 3)}")
        self.win.deiconify()
        self.win.attributes("-topmost", True)
        self.win.lift()
        # 映射之后再校正一次高度：未映射时标签还没完成换行布局，
        # 上面量到的 reqheight 会偏小（实测会矮 17 像素，把底部提示裁掉）。
        self._fit_to_content()
        self.visible = True
        self._touch()
        try:
            self.listbox.focus_set()
            if self.snapshots and self.listbox.size() and not self.listbox.curselection():
                self.listbox.selection_set(0)
                self._update_detail()
        except Exception:
            pass
        self._schedule_autohide()

    def hide(self) -> None:
        if self.win is None:
            return
        self.win.withdraw()
        self.visible = False
        self._cancel_autohide()

    def _cancel_autohide(self) -> None:
        if self._autohide_job:
            try:
                self.root.after_cancel(self._autohide_job)
            except Exception:
                pass
            self._autohide_job = None

    def _touch(self) -> None:
        self._last_activity = time.time()
        self._schedule_autohide()

    def _schedule_autohide(self) -> None:
        self._cancel_autohide()
        seconds = float(self.config["panel_autohide_seconds"] or 0)
        if seconds <= 0:
            return
        self._autohide_job = self.root.after(int(seconds * 1000), self._maybe_autohide)

    def _maybe_autohide(self) -> None:
        if not self.visible:
            return
        if self.app.busy or self.modal_open:
            self._schedule_autohide()
            return
        try:
            px, py = self.win.winfo_pointerxy()
            x, y = self.win.winfo_rootx(), self.win.winfo_rooty()
            inside = x <= px <= x + self.win.winfo_width() and y <= py <= y + self.win.winfo_height()
        except Exception:
            inside = False
        if inside:
            self._schedule_autohide()
            return
        self.hide()
        self.toast("面板已自动隐藏　Ctrl+Alt+A 可再次打开", timeout=2.0)

    def _on_escape(self) -> None:
        if self.visible:
            self.hide()

    # -------------------------------------------------- 进程状态

    def _schedule_process_check(self) -> None:
        self._proc_job = self.root.after(2500, self._check_process)

    def _check_process(self) -> None:
        """定时探测游戏是否在运行（用于面板标题上的状态点）。"""
        running = False
        try:
            import winapi

            from cdda_snapshot import self_module

            main = self_module()
            exe = getattr(main, "GAME_EXE", "cataclysm-tiles.exe")
            running = bool(winapi.is_process_running(exe))
        except Exception:
            logging.debug("游戏进程探测失败", exc_info=True)
        if running != self._game_running:
            self._game_running = running
            self.game_label.configure(
                text="● 游戏运行中" if running else "● 游戏未运行",
                fg=OK if running else FG_DIM,
            )
        self._schedule_process_check()

    # -------------------------------------------------- 数据刷新

    def refresh(self) -> None:
        world = self.app.current_world()
        self.app.world = world
        if not world:
            self.snapshots = []
            self._fill_list()
            self.world_label.configure(text="没有找到存档世界")
            self.sub_label.configure(text=f"存档目录: {self.manager.save_dir}")
            self.detail.configure(text="")
            self._update_size()
            return

        info = self.manager.world_info(world)
        gt = info.game_time.strftime("%Y-%m-%d %H:%M") if info.game_time else "—"
        self.world_label.configure(text=f"世界「{info.name}」　角色「{info.character}」")
        try:
            mtime = os.path.getmtime(os.path.join(info.path, "world_timestamp.json"))
            ago = fmt_relative(mtime)
        except OSError:
            ago = "—"
        self.sub_label.configure(text=f"最后存档 {ago}　游戏时间 {gt}")

        try:
            self.snapshots = list(reversed(self.manager.snapshots(world)))
        except Exception:
            logging.exception("读取快照列表失败")
            self.snapshots = []
        self._update_quota(world)
        self._update_size()
        # 每份快照的内容指纹只在刷新时算一次。
        # 判据：存档目录的内容与它相同 —— 刚拍的新快照、刚读取的那一份都会命中。
        self.active_sid = self._active_sid()
        self._fill_list()

    def _update_quota(self, world: str) -> None:
        """显示"可自动删除的份数 / 上限"，以及永久存档份数。"""
        try:
            count, keep, permanent = self.manager.auto_prune_state(world)
        except Exception:
            logging.debug("读取配额失败", exc_info=True)
            return
        if keep <= 0:
            text = f"自动删除：已关闭（不限份数）"
        else:
            text = f"自动删除：{count}/{keep} 份"
            if count >= keep:
                text += "（下次快照将删除最旧一份）"
        if permanent:
            text += f"　★ 永久存档 {permanent} 份（不受影响）"
        color = ACCENT if (keep > 0 and count >= keep) else FG_DIM
        try:
            self.quota_line.configure(text=text, fg=color)
        except Exception:
            pass

    def _update_size(self) -> None:
        """后台统计快照文件夹的逻辑大小与硬链接实占，算完回主线程更新标签。"""
        if self._size_running:
            self._size_pending = True
            return
        self._size_running = True

        def worker() -> None:
            data = None
            try:
                data = self.manager.usage()
            except Exception:
                logging.debug("统计快照占用失败", exc_info=True)

            def done() -> None:
                self._size_running = False
                try:
                    if data:
                        self._apply_size(data)
                    if self._size_pending:
                        self._size_pending = False
                        self._update_size()
                except Exception:
                    logging.debug("刷新占用显示失败", exc_info=True)

            try:
                self.app.post(done)
            except Exception:
                self._size_running = False

        threading.Thread(target=worker, name="snapshot-usage", daemon=True).start()

    def _apply_size(self, data: dict) -> None:
        saved = int(data.get("saved", 0) or 0)
        text = (f"快照文件夹 {fmt_size(data.get('logical', 0))}"
                f" → 硬链接实占 {fmt_size(data.get('actual', 0))}")
        if saved > 0:
            text += f"（已省 {fmt_size(saved)}）"
        files = int(data.get("files", 0) or 0)
        if files:
            text += f"　{files} 个文件"
        try:
            self.size_line.configure(text=text, fg=ACCENT if saved > 0 else FG)
        except Exception:
            pass

    def _fill_list(self) -> None:
        self.listbox.delete(0, tk.END)
        if not self.snapshots:
            key = "F5"
            if self.app.hotkeys is not None:
                key = self.app.hotkeys.label("snapshot", "F5")
            self.listbox.insert(tk.END, f"    （还没有快照 —— 按 {key} 或点「创建快照」）")
            self.listbox.itemconfigure(0, foreground=FG_DIM)
            self.detail.configure(text="")
            return

        active_sid = self.active_sid
        for idx, snap in enumerate(self.snapshots, start=1):
            kind = {"manual": "", "backup": "·备份", "auto": "·自动"}.get(
                snap.kind, f"·{snap.kind}")
            # 永久存档加 ★ 并显示自定义名字，一眼能看出哪些不会被自动删除
            star = "★" if snap.permanent else " "
            tag = f"  [{snap.name}]" if snap.name else ""
            # 当前生效的那份（刚建的 / 刚读回来的）加 ▶ 并整行高亮
            is_active = (snap.sid == active_sid)
            cursor = "▶" if is_active else " "
            # 可疑档：确定没通过校验的标 "⚠未校验"，旧版本没有校验记录的标 "?旧档"。
            # 分级是为了别喊狼来了 —— 旧档大多只是地图没动过。
            level, reason = self._suspect(snap)
            warn = {"bad": " ⚠未校验", "legacy": " ?旧档"}.get(level, "")
            line = (
                f"{idx:>2}.{star}{cursor}{snap.label()}{tag}  "
                f"{snap.created_local.strftime('%m-%d %H:%M:%S')}  "
                f"({fmt_relative(snap.created)})  "
                f"{fmt_size(snap.total_bytes):>9}  "
                f"{snap.file_count:>4} 文件 {kind}{warn}"
            )
            self.listbox.insert(tk.END, line)
            if is_active:
                self.listbox.itemconfigure(idx - 1, background=BG_ACTIVE, foreground=ACCENT)
            elif level == "bad":
                self.listbox.itemconfigure(idx - 1, foreground=DANGER)
            elif level == "legacy":
                self.listbox.itemconfigure(idx - 1, foreground=ACCENT_DIM)
            elif snap.permanent:
                self.listbox.itemconfigure(idx - 1, foreground=OK)
            elif snap.kind == "backup":
                self.listbox.itemconfigure(idx - 1, foreground=FG_DIM)
        self._update_detail()

    def _suspect(self, snap) -> tuple:
        """该快照的 (可疑级别, 原因)；没问题就是 ("", "")。

        判定逻辑在 cdda_snapshot.SnapshotManager.suspicion() 里，这里只做取用，
        任何异常都不该影响面板显示。
        """
        try:
            return self.manager.suspicion(snap)
        except Exception:
            logging.debug("可疑判定失败", exc_info=True)
            return ("", "")

    def _active_sid(self) -> Optional[int]:
        """计算哪一份快照是"当前"的（开销较大，只在 refresh 时调一次）。"""
        world = self.app.world
        if not world:
            return None
        try:
            from cdda_snapshot import self_module

            core = self_module()
            if core is None:
                return None
            mgr = self.manager
            path = os.path.join(mgr.save_dir, world)
            if not os.path.isdir(path):
                return None
            live = core._fingerprint(path)
            for snap in self.snapshots:
                if mgr.snapshot_fingerprint(snap) == live:
                    return snap.sid
        except Exception:
            logging.debug("判定当前快照失败", exc_info=True)
        return None

    # -------------------------------------------------- 交互

    def _on_wheel(self, event) -> None:
        self.listbox.yview_scroll(-1 if event.delta > 0 else 1, "units")
        self._touch()

    def _update_detail(self) -> None:
        snap = self.selected()
        if not snap:
            self.detail.configure(text="")
            return
        gt = "—"
        if snap.game_time:
            from cdda_snapshot import decode_game_timestamp

            parsed = decode_game_timestamp(snap.game_time)
            gt = parsed.strftime("%Y-%m-%d %H:%M") if parsed else "—"
        linked = f"　硬链接复用 {snap.linked_files}" if snap.linked_files else ""
        note = f"　备注: {snap.note}" if snap.note else ""
        perm = f"　★ 永久存档「{snap.name}」（不会被自动删除）" if snap.permanent else ""
        active = "　▶ 当前生效" if snap.sid == self.active_sid else ""
        level, reason = self._suspect(snap)
        mark = {"bad": "⚠ ", "legacy": "? "}.get(level, "")
        suspect = f"　{mark}{reason}" if reason else ""
        self.detail.configure(
            text=(
                f"选中 {snap.display_name}　游戏时间 {gt}　{snap.character}　"
                f"{fmt_size(snap.total_bytes)} / {snap.file_count} 文件"
                f"{linked}{note}{perm}{active}{suspect}"
            )
        )

    def selected(self) -> Optional[Any]:
        try:
            sel = self.listbox.curselection()
        except Exception:
            return None
        if not sel:
            return None
        idx = int(sel[0])
        if 0 <= idx < len(self.snapshots):
            return self.snapshots[idx]
        return None

    def _restore_by_digit(self, key: str) -> None:
        if not self.visible:
            return
        try:
            idx = int(key) - 1
        except ValueError:
            return
        if 0 <= idx < len(self.snapshots):
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(idx)
            self.listbox.see(idx)
            self._update_detail()
            snap = self.snapshots[idx]
            if self._confirm_sid == snap.sid and time.time() < self._confirm_until:
                self._confirm_sid = None
                self.app.do_restore(snap)
            else:
                self._confirm_sid = snap.sid
                self._confirm_until = time.time() + 3.0
                self.set_status(
                    f"再按一次 {key} 确认还原到 {snap.label()}（或按 Enter 并确认）",
                    error=False,
                )
                self.toast(f"再按一次 {key} 确认还原 {snap.label()}", timeout=3.0)
            self._touch()

    def restore_selected(self) -> None:
        snap = self.selected()
        if not snap:
            self.toast("先选中一份快照", error=True)
            return
        self.app.do_restore(snap)

    def snapshot_disk(self) -> None:
        """只打包磁盘上已有的存档，不打扰游戏。"""
        self.app.do_snapshot(mode="disk", note="磁盘快照")

    # -------------------------------------------------- 命名 / 永久存档

    def rename_selected(self) -> None:
        """给选中的快照命名（同时把它设为永久存档，隔离在自动删除之外）。"""
        snap = self.selected()
        if not snap:
            self.toast("先选中一份快照", error=True)
            return
        new_name = ask_text(
            self.root,
            "命名快照 · 永久存档",
            f"给 {snap.label()} 起个名字。命名后它将标记为★永久存档，\n"
            f"不计入「每世界保留 {self.config['keep_per_world']} 份」的自动删除。\n"
            f"清空名字则取消永久标记。",
            initial=snap.name,
        )
        if new_name is None:          # 取消
            return
        try:
            self.manager.rename(snap, new_name)
        except Exception as exc:
            logging.exception("命名失败")
            self.toast(f"命名失败: {exc}", error=True)
            return
        self.refresh()
        if new_name.strip():
            self.toast(f"{snap.label()} 已命名为「{new_name.strip()}」，不会被自动删除")
        else:
            self.toast(f"{snap.label()} 已取消永久标记")

    def restore_latest(self) -> None:
        if not self.snapshots:
            self.toast("还没有任何快照", error=True)
            return
        self.app.do_restore(self.snapshots[0])

    def delete_selected(self) -> None:
        snap = self.selected()
        if not snap:
            return
        if snap.kind == "backup":
            pass
        if not confirm(
            self.root,
            "删除快照",
            f"确定删除快照 {snap.label()}（{snap.created_local.strftime('%Y-%m-%d %H:%M:%S')}，"
            f"{fmt_size(snap.total_bytes)}）？\n此操作不可撤销。\n\n"
            f"注意：其它快照里未变动的文件是硬链接共用的，删除本快照不会影响它们。",
        ):
            return
        try:
            self.manager.delete(snap)
            self.toast(f"已删除 {snap.label()}")
        except Exception as exc:
            logging.exception("删除快照失败")
            self.toast(f"删除失败: {exc}", error=True)
        self.refresh()

    def open_snapshot_dir(self) -> None:
        world = self.app.world
        if world:
            from cdda_snapshot import safe_world_dirname

            target = os.path.join(self.manager.root, safe_world_dirname(world))
            if os.path.isdir(target):
                open_in_explorer(target)
                self._touch()
                return
        open_in_explorer(self.manager.root)

    # -------------------------------------------------- 热键

    def apply_hotkeys(self, listener) -> None:
        """按实际生效的绑定刷新界面上的按键提示。"""
        try:
            snap = listener.label("snapshot", "F5")
            disk = listener.label("snapshot_disk", "Ctrl+F5")
            restore = listener.label("restore_latest", "F6")
            self.snap_button.set_text(f"▣ 快速保存并快照  ({snap})")
            self.disk_button.set_text(f"只存磁盘快照  ({disk})")
            self.restore_button.set_text(f"↺ 还原最新  ({restore})")
            self.hotkey_line.configure(
                text=(
                    f"全局热键　{listener.label('toggle_panel', 'F4')} 面板　"
                    f"{snap} 快照(存盘·不退出)　{disk} 仅磁盘存档　"
                    f"{restore} 回档　{listener.label('exit_tool', 'Ctrl+Alt+Q')} 退出"
                )
            )
        except Exception:
            logging.exception("刷新热键提示失败")

    def open_hotkey_settings(self) -> None:
        """热键设置对话框（可按键捕获、可测可用性）。"""
        from cdda_snapshot import self_module

        main = self_module()
        logging.info("打开热键设置：主模块=%s", getattr(main, "__name__", main))
        if main is None:
            self.toast("无法加载主模块", error=True)
            return
        try:
            changed = HotkeyDialog(self, main).show()
            logging.info("热键设置对话框已关闭，changed=%s", changed)
        except Exception as exc:
            logging.exception("热键设置失败")
            self.toast(f"热键设置失败: {exc}", error=True)
            return
        if changed:
            self.hide()
            self.app.reload_hotkeys()

    # -------------------------------------------------- 状态 / 气泡

    def set_status(self, message: str, *, busy: bool = False, error: bool = False) -> None:
        color = DANGER if error else (ACCENT if busy else FG_DIM)
        try:
            self.status.configure(text=message, fg=color)
        except Exception:
            return
        # 状态文字换行后高度会变，跟着重新适配，避免下半截被裁掉
        if self.visible:
            self._fit_to_content()

    def toast(self, message: str, *, error: bool = False, timeout: float = 3.0) -> None:
        if not self.config["toast_enabled"]:
            return
        try:
            self.toast_win.show(message, error=error, timeout=timeout)
        except Exception:
            logging.exception("气泡显示失败")


# ---------------------------------------------------------------- 热键设置


#: tkinter keysym -> winapi 可识别的按键名
_KEYSYM_MAP = {
    "space": "space", "Return": "enter", "KP_Enter": "enter", "Tab": "tab",
    "Escape": "escape", "BackSpace": "backspace", "Delete": "delete",
    "Insert": "insert", "Home": "home", "End": "end",
    "Prior": "pageup", "Next": "pagedown",
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "minus": "-", "equal": "=", "bracketleft": "[", "bracketright": "]",
    "semicolon": ";", "apostrophe": "'", "comma": ",", "period": ".",
    "slash": "/", "backslash": "\\", "grave": "`",
}

_MODIFIER_KEYSYMS = {
    "Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
    "Super_L", "Super_R", "Meta_L", "Meta_R", "Caps_Lock", "Num_Lock",
}


def keysym_to_hotkey(event) -> Optional[str]:
    """把一次按键事件翻译成 'ctrl+alt+f5' 这样的配置字符串。"""
    keysym = event.keysym or ""
    if keysym in _MODIFIER_KEYSYMS:
        return None

    parts: list[str] = []
    state = int(event.state or 0)
    if state & 0x0004:
        parts.append("ctrl")
    if state & 0x0008:  # Mod1 = Alt
        parts.append("alt")
    if state & 0x0001:
        parts.append("shift")

    key = ""
    if len(keysym) == 1 and keysym.isalnum():
        key = keysym.lower()
    elif keysym.startswith("F") and keysym[1:].isdigit():
        key = keysym.lower()
    elif keysym in _KEYSYM_MAP:
        key = _KEYSYM_MAP[keysym]
    elif keysym in ("Control_L", "Control_R"):
        return None
    else:
        return None

    # Shift 已经体现在大写字母里时不必再记 shift
    if key.isalpha() and (state & 0x0001) and len(keysym) == 1 and keysym.isupper():
        parts = [p for p in parts if p != "shift"]

    parts.append(key)
    return "+".join(parts)


class HotkeyDialog:
    """热键设置：点输入框后直接按组合键捕获，可即时测试可用性。"""

    ROWS = [
        ("toggle_panel", "打开 / 关闭面板", "最常用的入口，建议单键"),
        ("snapshot", "拍快照", "先让游戏存档，再整目录快照"),
        ("restore_latest", "回到最新快照", "自动退出游戏、还原、重启"),
        ("exit_tool", "退出工具", "建议带修饰键，避免误按"),
    ]

    def __init__(self, panel: QuickPanel, main_module):
        self.panel = panel
        self.main = main_module
        self.config = panel.config
        self.result: dict[str, str] = {}
        self._changed = False

    # -------------------------------------------------- 捕获

    def _capture(self, action: str, var: tk.StringVar, entry: tk.Entry) -> None:
        def _grab(event):
            spec = keysym_to_hotkey(event)
            if not spec:
                return "break"
            var.set(spec)
            ok, message = self.main.probe_hotkey(spec)
            hint_var = self.hints[action]
            if ok:
                hint_var.set("可用")
                self._paint(entry, OK)
            else:
                hint_var.set("不可用：" + message)
                self._paint(entry, DANGER)
            return "break"

        entry.bind("<KeyPress>", _grab)
        entry.bind("<Button-1>", lambda _e: (entry.focus_set(), self._mark_capturing(action)))

    def _mark_capturing(self, action: str) -> None:
        self.hints[action].set("请直接按下想要的组合键…")

    @staticmethod
    def _paint(entry: tk.Entry, color: str) -> None:
        try:
            entry.configure(fg=color)
        except Exception:
            pass

    # -------------------------------------------------- 对话框

    def show(self) -> bool:
        panel = self.panel
        panel.modal_open = True
        dlg = tk.Toplevel(panel.root)
        dlg.title("热键设置")
        dlg.configure(bg=BG)
        # 不要用 transient()：它的父窗口是隐藏的 Tk root，Tk 会把 transient 子窗口
        # 一并撤销（state=withdrawn / geom=1x1），对话框就永远不显示。
        # 用 topmost + grab_set 已经足够保证它是模态且在最前。
        dlg.resizable(False, False)
        dlg.protocol("WM_DELETE_WINDOW", lambda: self._finish(dlg, False))

        shell = tk.Frame(dlg, bg=BG, highlightthickness=1, highlightbackground=ACCENT_DIM)
        shell.pack(fill="both", expand=True)

        tk.Label(shell, text="全局热键", bg=BG, fg=ACCENT, font=_ui_font(12, bold=True),
                 anchor="w", padx=18).pack(fill="x", pady=(16, 2))
        tk.Label(
            shell,
            text="点击输入框后，直接按下你想要的组合键即可。改完点「保存并重启」生效。",
            bg=BG, fg=FG_DIM, font=_ui_font(9), anchor="w", justify="left",
            wraplength=520, padx=18,
        ).pack(fill="x")

        grid = tk.Frame(shell, bg=BG)
        grid.pack(fill="x", padx=18, pady=(12, 4))

        self.vars: dict[str, tk.StringVar] = {}
        self.hints: dict[str, tk.StringVar] = {}
        listener = panel.app.hotkeys

        for row_index, (action, label, note) in enumerate(self.ROWS):
            current = ""
            if listener is not None and listener.resolved.get(action):
                current = listener.resolved[action]
            else:
                candidates = self.main.hotkey_candidates(self.config["hotkeys"].get(action))
                current = candidates[0] if candidates else ""

            tk.Label(grid, text=label, bg=BG, fg=FG, font=_ui_font(10), anchor="w"
                     ).grid(row=row_index, column=0, sticky="w", pady=5, padx=(0, 10))

            var = tk.StringVar(value=current)
            self.vars[action] = var
            entry = tk.Entry(
                grid, textvariable=var, width=18, justify="center",
                bg=BG_ALT, fg=ACCENT, insertbackground=FG, relief="flat",
                font=_mono_font(11),
            )
            entry.grid(row=row_index, column=1, sticky="w", pady=5, ipady=4)
            self._capture(action, var, entry)
            entry.bind("<FocusIn>", lambda _e, a=action: self._mark_capturing(a))

            hint_var = tk.StringVar(value=note)
            self.hints[action] = hint_var
            tk.Label(grid, textvariable=hint_var, bg=BG, fg=FG_DIM, font=_ui_font(8),
                     anchor="w", justify="left", wraplength=250
                     ).grid(row=row_index, column=2, sticky="w", padx=(10, 0))

            FlatButton(
                grid, "测试",
                lambda a=action: self._test(a),
            ).grid(row=row_index, column=3, sticky="e", padx=(10, 0))

        # ---- 托盘开关
        tray_row = tk.Frame(shell, bg=BG)
        tray_row.pack(fill="x", padx=18, pady=(8, 0))
        self.tray_var = tk.BooleanVar(value=bool(self.config["tray_icon"]))
        tray_check = tk.Checkbutton(
            tray_row, text="在系统托盘显示图标（热键被占用时的备用入口，左键点图标开面板）",
            variable=self.tray_var, bg=BG, fg=FG, selectcolor=BG_ALT,
            activebackground=BG, activeforeground=ACCENT, font=_ui_font(9),
            anchor="w", justify="left", highlightthickness=0, borderwidth=0,
        )
        tray_check.pack(fill="x")

        tk.Label(
            shell,
            text=("提示：F5 在 CDDA 里默认绑定了「切换小键盘数字键导航」，"
                  "两者会互相干扰，介意的话给 F5 换成 Ctrl+F5 之类。"),
            bg=BG, fg=ACCENT_DIM, font=_ui_font(8), anchor="w", justify="left",
            wraplength=520, padx=18,
        ).pack(fill="x", pady=(8, 0))

        # ---- 底部按钮
        buttons = tk.Frame(shell, bg=BG)
        buttons.pack(fill="x", padx=18, pady=(12, 16))
        FlatButton(buttons, "取消", lambda: self._finish(dlg, False)).pack(side="right")
        FlatButton(buttons, "保存并重启", lambda: self._save(dlg), accent=True
                   ).pack(side="right", padx=(0, 8))
        FlatButton(buttons, "恢复默认", self._restore_defaults).pack(side="left")

        # 先建立窗口与尺寸，再决定绝对位置。
        # 注意顺序：transient() 之后窗口可能处于 withdrawn，必须先 deiconify 再量尺寸，
        # 否则测到的是 1x1，位置也会算错。
        dlg.update_idletasks()
        dlg.deiconify()
        dlg.update_idletasks()
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        px = panel.win.winfo_rootx() + max(0, (panel.win.winfo_width() - w) // 2)
        py = panel.win.winfo_rooty() + 40
        dlg.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        dlg.update_idletasks()
        # 映射之后再定位一次：Windows 有时会把尚未映射的 Toplevel 的位置重置掉
        dlg.geometry(f"+{max(0, px)}+{max(0, py)}")
        dlg.attributes("-topmost", True)
        dlg.lift()
        dlg.grab_set()
        dlg.focus_force()
        dlg.update_idletasks()
        logging.info(
            "热键设置窗口: req=%dx%d geom=%s mapped=%s viewable=%s state=%s",
            w, h, dlg.geometry(), dlg.winfo_ismapped(), dlg.winfo_viewable(), dlg.state(),
        )

        self._dlg = dlg
        panel.root.wait_window(dlg)
        panel.modal_open = False
        return self._changed

    # -------------------------------------------------- 动作

    def _test(self, action: str) -> None:
        spec = self.vars[action].get().strip()
        if not spec:
            self.hints[action].set("还没设置按键")
            return
        ok, message = self.main.probe_hotkey(spec)
        current = self.panel.app.hotkeys.resolved.get(action) if self.panel.app.hotkeys else None
        if ok:
            self.hints[action].set("可用（本工具已占用的键测不出来，保存后以实际生效为准）")
        elif spec == current:
            self.hints[action].set("正被本工具使用中")
        else:
            self.hints[action].set("不可用：" + message)

    def _restore_defaults(self) -> None:
        defaults = self.main.DEFAULT_CONFIG["hotkeys"]
        for action, _label, _note in self.ROWS:
            candidates = self.main.hotkey_candidates(defaults.get(action))
            self.vars[action].set(candidates[0] if candidates else "")
            self.hints[action].set("已填回默认值")

    def _save(self, dlg: tk.Toplevel) -> None:
        chosen: dict[str, str] = {}
        for action, label, _note in self.ROWS:
            spec = self.vars[action].get().strip()
            if not spec:
                self.hints[action].set("请先设置按键")
                return
            chosen[action] = spec
        # 同一颗键不能同时绑两个动作
        seen: dict[str, str] = {}
        for action, spec in chosen.items():
            if spec in seen:
                names = dict((a, l) for a, l, _ in self.ROWS)
                self.panel.toast(f"{spec} 已被「{names[seen[spec]]}」占用", error=True)
                return
            seen[spec] = action

        hotkeys = self.config["hotkeys"]
        for action, spec in chosen.items():
            hotkeys[action] = [spec] + [
                c for c in self.main.hotkey_candidates(hotkeys.get(action)) if c != spec
            ]
        self.config["tray_icon"] = bool(self.tray_var.get())
        try:
            self.config.save()
        except Exception as exc:
            logging.exception("保存热键失败")
            self.panel.toast(f"保存失败: {exc}", error=True)
            return
        self._changed = True
        self._finish(dlg, True)

    def _finish(self, dlg: tk.Toplevel, _ok: bool) -> None:
        try:
            dlg.grab_release()
        except Exception:
            pass
        try:
            dlg.destroy()
        except Exception:
            pass
