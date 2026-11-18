#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cataclysm Shortcut —— CDDA / CCB 快照工具
========================================

为 CDDA（Cataclysm: Dark Days Ahead）与 CCB（Cataclysm: Cleanwater Bomb）
提供「随时快照 / 随时读档 / 单世界多存档点」的能力，无需重编译游戏。

设计要点
--------
* 快照单位是**整个世界目录** ``save/<世界名>/``，因为角色 (.sav)、地图 (maps/)、
  超图 (overmaps/)、地图记忆 (.mm1/)、master.gsav 必须来自同一时刻，只备份 .sav 会坏档。
* 快照是**目录**而非压缩包：未变化的文件用 NTFS 硬链接指向上一份快照，
  长局游戏下空间占用接近增量备份，而每份快照本身都是自包含、可独立还原的。
* 快照前先让游戏快速保存（ESC → e），**并且必须通过一致性校验**才落盘：
  拷贝**之前**先等整个世界目录连续安静 `snapshot_settle_seconds` 秒（这是"这次
  存盘写完了"的唯一凭据），拷贝期间再验一次源目录没变、快照与源目录逐项相同。
  CDDA 的一次存档是分段落写的（角色档先、地图/超图后）。窗口必须大于**相邻两段
  之间的间隔**，实测该间隔最大约 114 ms（166 个样本），故默认取 1.0 秒（8.8 倍余量）。
  少了静默窗口就会拍到"角色新、地图旧"的半新半旧状态 —— 读回去就是物品位置回溯、
  角色与地图不同步，以及"xxx/yyy 处已有一个敌人""Shot along ... out-of-bounds"。
  校验不过就**整份丢弃**（宁可不存，绝不存坏），绝不把半新半旧的档发布进快照列表。
* 性能：静默窗口是唯一必要的等待，且只在**真的有写入者**时才做。窗口只做一次
  （拷贝前），并按"最后一次写入的年龄"精确计算还差多久；目录本来就安静够久时
  零等待。实测 F5 约 1.4 秒、Ctrl+F5 约 0.17 秒、游戏没在跑时约 0.10 秒
  （139 文件 / 4.1 MB）。详见 README「快照一致性」一节。
* 还原时自动：请游戏存档退出 -> 备份当前状态 -> 原子上换 -> 重启游戏到该世界。
* 还原中途崩溃可恢复：使用 ``restore_marker`` + ``*.restoring`` 暂存目录。

运行
----
    pythonw cdda_snapshot.pyw          # 正常使用（无控制台窗口，全局热键）
    （脚本文件名保持不变，是为了兼容已有的配置与快照数据）
    python  cdda_snapshot.pyw --list   # 命令行列出快照
    python  cdda_snapshot.pyw --snapshot [--no-quicksave]   # 命令行拍一张快照

只依赖 Python 标准库 + tkinter。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wintypes
import datetime as _dt
import json
import logging
import os
import queue
import shutil
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------------------
# 基础常量
# --------------------------------------------------------------------------------------

APP_NAME = "Cataclysm Shortcut"
APP_VERSION = "0.9"

SNAPSHOT_ROOT_NAME = ".cdda_snapshots"
MANIFEST_NAME = "manifest.json"
SNAP_META_NAME = "snapshot.json"
LOG_NAME = "cdda_snapshot.log"
RESTORE_MARKER = ".restore_pending.json"
STAGING_SUFFIX = ".restoring"

GAME_EXE = "cataclysm-tiles.exe"

DEFAULT_CONFIG: dict[str, Any] = {
    # 快照前是否先让游戏存一次档（确保地图脏页落盘，快照与当前进度一致）
    "quicksave_before_snapshot": True,
    # 取档方式：
    #   "fresh"   让游戏存档再快照。CDDA 只有「保存并退出」，所以这一步必然让游戏
    #             退出；工具会在拍完后**自动把游戏重新打开并读回该存档**，
    #             全程你只需要按一下 F5。（默认）
    #   "disk"    只快照磁盘上已有的存档，完全不打扰游戏（进度可能滞后）
    "snapshot_save_mode": "fresh",
    # fresh 模式下，拍完自动重启游戏并读回存档
    "relaunch_after_fresh_snapshot": True,
    # 等待游戏开始写盘的最长秒数（判据：.sav 文件时间发生变化）。
    # 注意这是"等它**开始**写"，不是"等它写完"：CDDA 收到存档键后几乎立刻
    # 先写角色档（实测 .sav 先落盘、地图/超图晚 0.5 秒以上），所以这里不需要
    # 留很大余量；写完与否由随后的静默窗口负责。
    # 这个值同时决定"什么都没发生"时白等多久 —— 而"游戏判定无改动可存"和
    # "按键没落到菜单上"这两种情况在磁盘上完全看不出区别，都会走满超时。
    # 原来是 30 秒，按一次 F5 如果没有改动就要干等 30 秒，太长了。
    "quicksave_timeout": 10.0,
    # 触发存档后的"细粒度轮询"持续多久（秒）。这段时间内用 50ms 探一次，
    # 之后放宽到 100ms / 200ms，避免长时间空转烧 CPU。
    "quicksave_fast_poll_seconds": 1.5,
    # ESC 之后等多久才发 `e`（秒）。**这是盲目等待**：等的是游戏弹出 ESC 浮层，
    # 而浮层何时出现没有可观测信号。太短则 `e` 会落到游戏世界（裸 e = EXAMINE，
    # 只弹查看界面、存档不会发生，而调用方只会看到"目录毫无动静"）。
    #
    # 实测（2026-09-30，真实 TUTORIAL 世界 + 真实游戏，每一档都确实写进了盘）：
    #   0.60 / 0.35 / 0.25 / 0.20 / 0.15 / 0.12 / 0.08 / 0.05 / 0.00 全部成功，
    #   每个值重复两次也都成功 —— 也就是说**真正的下限小于 50 毫秒**：
    #   发 ESC 与发 e 之间本来就隔着 _tap_post 自带的 50ms（按下→抬起），
    #   而 CDDA 收到 ESC 后一帧内就把浮层弹出来了，这 50ms 已经够。
    # 下限既然测不出来（小于我们能构造的最小间隔），默认值就取一个有明确余量的数：
    # 0.15 秒 ≈ 3 倍余量，同时比原来的 0.6 秒省下 0.45 秒。
    # 想榨干最后一点可以设 0.05；不建议设 0，那等于完全没有余量。
    "quicksave_menu_delay": 0.1,
    # ---------------------------------------------------------------- 启动提示
    # CCB（Cataclysm: Cleanwater Bomb）每次启动都会先显示一个「Lua 模组是可执行
    # 程序……」的安全提示界面，**必须按任意键才能继续**；不按的话游戏会一直停在
    # 这里（日志停在第二条 Setting language 之后，加载进度看起来像"卡住了"）。
    # 工具每次**自己拉起游戏**后自动向窗口投递一次回车把它关掉。
    "press_enter_on_game_start": True,
    # 回车投递时机：游戏窗口出现后再等多久（秒）。实测提示界面在窗口出现后约
    # 1 秒画出（第二条 Setting language 之后），1.5 秒已有余量，又不会拖到让
    # "日志停滞 = 已就绪"的判定先把提示界面误当成加载完成。
    "startup_enter_delay": 1.5,
    # ---------------------------------------------------------------- 一致性
    # 拷贝期间源目录仍有变动就整份丢弃重做，最多试几次。
    "snapshot_consistent_attempts": 3,
    # 拷贝前后要求源目录"冻住"多久，才认定这次存盘真的写完了（秒）。
    # CDDA 一次存档是分几段落的（角色档先写、地图/超图后写）。
    #
    # **窗口必须大于存档内部相邻两段之间的最大间隔**，否则就会正好卡在两段
    # 中间取到"半新半旧"的状态。这个间隔实测过（`python _f5_settle.py hist`，
    # 从 10 次真实存档里提出 166 个样本）：
    #     最大 114 ms / 99 分位 100 ms / 90 分位 75 ms / 中位 6 ms
    # 注意别把"跨度"当"间隔"：同一次存档里 .sav 到地图的**跨度**可以有 0.53 秒，
    # 但那是若干个 20~100 ms 的间隔累计出来的，没有哪个单段间隔接近它。
    # 所以 1.0 秒已经是最大间隔的 8.8 倍；而且拷完还有两道校验兜底 ——
    # 万一某一段真的落在拷贝期间，会被判为"不一致"而整份重做，不是直接发布。
    # 想把 F5 再压 0.4 秒可以设 0.6（5.3 倍）；不建议低于 0.4。
    "snapshot_settle_seconds": 0.4,
    # 校验实在过不去时怎么办：
    #   False（默认）丢弃这次快照并报错 —— 宁可不存，绝不存坏。
    #                半新半旧的档一旦落进列表，F6 还原的正是"最新那份"，
    #                就会变成物品位置回溯 / 地图与角色不同步。
    #   True         保留内容但标记为不可信（旧行为，不推荐）。
    "allow_inconsistent_snapshot": False,
    # 每个世界保留多少份**可自动删除**的快照。超出时自动删除最旧的一份。
    # 被命名过的「永久存档」不计入这个数量，也不会被删。
    "keep_per_world": 10,
    # 还原时是否自动重启游戏到该世界
    "relaunch_after_restore": True,
    # 还原前是否把当前进度也存成快照。
    # 默认关：读档就是为了低成本试错，多存一次会让每次读档白等好几秒。
    "backup_before_restore": False,
    # 是否创建硬链接以节省空间（关掉则每份快照完整拷贝）
    "use_hardlinks": True,
    # 状态提示是否使用屏幕角标气泡
    "toast_enabled": True,
    # 面板自动隐藏（秒，0 = 不自动隐藏）
    "panel_autohide_seconds": 25.0,
    # 全局热键：可以写一个字符串，也可以写候选列表（按顺序尝试，第一个能注册成功的生效）。
    # 别人的常驻软件（QQ、微信等）经常占用 F4/F5/F6 与 ctrl+alt+* ，所以每个动作都留了
    # 退路；真机上 F5 通常可用，被占时会自动退到下一候选。
    # 也可以在面板里按「热键设置」改，改完工具自动重启立即生效。
    "hotkeys": {
        "toggle_panel": ["f4", "f8", "alt+z"],
        # F5 = 存盘快照：让游戏保存并退出 -> 打包 -> 自动重开游戏读回存档
        # 候选里不放 ctrl+f5（留给磁盘快照），避免两个动作抢同一颗键
        "snapshot": ["f5", "f7", "ctrl+alt+s"],
        # 只打包磁盘上已有的存档，不打扰游戏
        "snapshot_disk": ["ctrl+f5", "ctrl+shift+f5"],
        "restore_latest": ["f6", "f9", "ctrl+f6", "ctrl+alt+l"],
        "exit_tool": ["ctrl+alt+q", "ctrl+shift+q"],
    },
    # 托盘图标：热键全被别的软件占用时的兜底入口
    "tray_icon": True,
    # 重启游戏后，日志停止增长多少秒就认为"已经加载完、可以玩了"
    "load_ready_stall_seconds": 5.0,
    # 角色档比地图/超图新出多少秒以上，就在体检里提示"可能不一致"。
    # 注意这是**启发式**：地图本来就没改动就不写，老 mtime 也可能完全正常。
    "verify_lag_warn_seconds": 60,
    # 与游戏同时启动：工具常驻后台，检测到 CDDA 进程出现就算"就绪"
    "autostart_with_game": False,
    # 游戏加载完成后自动拍一张"出发档"。
    # 默认关：正常"读取世界"也会触发，内容往往和已有快照相同，纯属多占硬盘。
    "auto_snapshot_on_launch": False,
    # 游戏退出后是否连工具一起退出（配合开机自启时用）
    "exit_with_game": False,
    # ---- Steam 包装模式（只有命令行带 --steam-launch 时才生效）----
    # 把 Steam 非 Steam 快捷方式的「目标」改成 pythonw.exe + 本脚本 + --steam-launch，
    # 于是 Steam 亲手拉起的进程是**本工具**，游戏是本工具的子进程。读档重启时
    # 只换子进程，Steam 的锚点一直活着 —— 这就是"重载也计时"的关键。
    # 启动时若找得到可用的世界，就带 --world 直接进入（否则停在主菜单）。
    "steam_launch_into_world": True,
    # 游戏正常退出时连工具一起退出。否则 Steam 会以为游戏还在跑，
    # 把工具挂托盘的时间也计进游玩时长（虚高）。
    "steam_exit_with_game": True,
}


def _fix_console() -> None:
    """让 CLI 模式能正确输出中文。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - 老解释器
            pass


def self_module():
    """返回本文件对应的模块对象。

    ``cdda_snapshot.pyw`` 可能以 ``__main__`` 身份运行，也可能被 ``import
    cdda_snapshot`` 加载（.pyw 也在 Python 的扩展名搜索列表里）。panel.py 需要拿到
    同一个模块对象，才能和主程序共享同一套全局状态。
    """
    mod = sys.modules.get("cdda_snapshot")
    if mod is not None:
        return mod
    return sys.modules.get("__main__")


# --------------------------------------------------------------------------------------
# 路径与通用工具
# --------------------------------------------------------------------------------------


def find_game_root(start: Optional[str] = None) -> str:
    """从脚本位置出发向上寻找游戏根目录。

    插件现在放在游戏目录下的一个子文件夹里（见 :func:`snapshot_root`），
    所以这里靠"向上找 ``cataclysm-tiles.exe``"来定位游戏根目录：
    脚本所在目录没有 exe，再往上一层就是游戏根目录。
    """
    candidates: list[str] = []
    here = os.path.abspath(start or os.path.dirname(os.path.abspath(__file__)))
    candidates.append(here)
    candidates.append(os.getcwd())
    for base in candidates:
        cur = base
        for _ in range(4):
            if os.path.isfile(os.path.join(cur, GAME_EXE)):
                return cur
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    return os.path.abspath(candidates[0])


#: 本工具（插件）自己所在的文件夹。
#: 所有插件文件与快照数据都放在这里 —— 于是"插件 = 一个文件夹"，
#: 游戏更新时把它整个拷到新版本的目录下即可，代码/配置/快照一起走。
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

#: 老数据只迁移一次
_LEGACY_MIGRATION_DONE = False


def snapshot_root(game_root: Optional[str] = None) -> str:
    """快照数据目录（config / log / manifest / 各份快照）的绝对路径。

    **固定在插件所在的文件夹里**（``<插件目录>/.cdda_snapshots``），
    而不是像老版本那样放在游戏根目录。理由就是"便于游戏更新时迁移"：
    插件只要是一个自包含的文件夹，把游戏换成新版本后，
    把这个文件夹拷过去就全有了（代码、配置、快照数据），
    不会出现"代码搬走了、快照还留在旧游戏目录里"这种情况。

    ``game_root`` 只用于**一次性迁移老数据**：老版本把数据放在
    ``<game_root>/.cdda_snapshots``。新位置不存在、老位置存在时，直接
    ``os.rename`` 搬过来 —— 同一磁盘卷上的目录改名是纯元数据操作，
    瞬间完成，且快照之间靠硬链接共享的数据**完全不受影响**。
    搬不动（跨卷、被占用）就退回继续用老位置，绝不因为迁移失败而起不来。

    注意：硬链接要求快照与存档在**同一磁盘卷**。插件放在游戏目录下时天然满足；
    如果谁把插件文件夹挪到别的盘，``create()`` 会自动退回普通拷贝（只是更占空间）。
    """
    global _LEGACY_MIGRATION_DONE
    primary = os.path.join(PLUGIN_DIR, SNAPSHOT_ROOT_NAME)
    if _LEGACY_MIGRATION_DONE or not game_root:
        return primary
    _LEGACY_MIGRATION_DONE = True

    legacy = os.path.join(os.path.abspath(game_root), SNAPSHOT_ROOT_NAME)
    if os.path.abspath(legacy) == os.path.abspath(primary):
        return primary                          # 插件就摆在游戏根目录（老布局）
    if not os.path.isdir(legacy):
        return primary
    if os.path.isdir(primary):
        # 两边都有：以插件目录里的为准，旧的那份原样留着（不偷偷删用户数据）
        try:
            stale = bool(os.listdir(legacy))
        except OSError:
            stale = False
        if stale:
            logging.warning(
                "旧位置还留着一份快照数据：%s（现在以 %s 为准；确认不用了可以自己删）",
                legacy, primary,
            )
        return primary
    try:
        os.rename(legacy, primary)
        logging.info("已把快照数据迁移到插件目录：%s -> %s", legacy, primary)
        return primary
    except OSError as exc:
        logging.warning("快照数据迁移失败（%s），继续使用旧位置：%s", exc, legacy)
        return legacy


STEAM_TAKEOVER_NAME = "steam_takeover.flag"
STEAM_TAKEOVER_SECONDS = 15.0


def steam_takeover_path(root: Optional[str] = None) -> str:
    return os.path.join(root or snapshot_root(), STEAM_TAKEOVER_NAME)


def request_steam_takeover(root: Optional[str] = None) -> None:
    """Steam 要接管时留下的"请你让位"标记。

    为什么需要：工具是单实例的，而 Steam 亲手拉起的那个进程**必须自己**成为
    "正在跑的那一个" —— 否则 Steam 的抓手（它 spawn 的进程）立刻退出，时长计 0。
    常驻实例看到这个标记就安静退出（只退工具，不碰游戏进程）。
    """
    path = steam_takeover_path(root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()} {time.time():.3f}\n")
    except OSError:
        logging.exception("写入 Steam 接管标记失败：%s", path)


def steam_takeover_pending(root: Optional[str] = None, *,
                           max_age: float = STEAM_TAKEOVER_SECONDS) -> bool:
    """标记是否"还新鲜"：太老的（崩溃残留）不算，免得下次启动莫名退出。"""
    try:
        age = time.time() - os.path.getmtime(steam_takeover_path(root))
    except OSError:
        return False
    return 0.0 <= age <= max_age


def clear_steam_takeover(root: Optional[str] = None) -> None:
    try:
        os.remove(steam_takeover_path(root))
    except OSError:
        pass


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(num)} B"
            return f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} TB"


def decode_char_filename(filename: str) -> Optional[str]:
    """把存档文件名里的 ``#<base64>`` 解码成角色名。"""
    if not filename.startswith("#"):
        return None
    core = filename.split(".", 1)[0][1:]
    if not core:
        return None
    try:
        raw = base64.b64decode(core + "=" * (-len(core) % 4))
        name = raw.decode("utf-8")
    except Exception:
        return None
    return name if name and name.isprintable() else None


def decode_save_timestamp(value: Any) -> Optional[_dt.datetime]:
    """CDDA 的 save_timestamp 是“YYYYMMDDhhmmss + 纳秒”的 20 位字符串。"""
    if value is None:
        return None
    text = str(value).strip().strip('"')
    if len(text) < 14 or not text[:14].isdigit():
        return None
    try:
        return _dt.datetime.strptime(text[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None


SEASON_DAYS = 91
SEASON_NAMES = ("春", "夏", "秋", "冬")

def format_game_turn(turn: Optional[int], season_days: int = SEASON_DAYS) -> str:
    if turn is None:
        return "—"
    days, secs = divmod(max(0, int(turn)), 86400)
    year, rem = divmod(days, 4 * season_days)
    season, day = divmod(rem, season_days)
    return (f"{year + 1} 年 {SEASON_NAMES[season]} {day + 1} 日 "
            f"{secs // 3600:02d}:{secs % 3600 // 60:02d}")

def read_game_turn(world_dir: str) -> Optional[int]:
    """从该世界最新的角色存档（gzip JSON）里读游戏内 turn；1 turn = 1 秒。"""
    try:
        entries = os.listdir(world_dir)
    except OSError:
        return None
    best, best_mtime = "", -1.0
    for entry in entries:
        low = entry.lower()
        if not entry.startswith("#") or ".sav" not in low:
            continue
        try:
            mt = os.path.getmtime(os.path.join(world_dir, entry))
        except OSError:
            continue
        if mt > best_mtime:
            best, best_mtime = entry, mt
    if not best:
        return None
    try:
        with gzip.open(os.path.join(world_dir, best), "rt", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except Exception:
        return None
    for holder in (data, data.get("avatar") or {}, data.get("player") or {}):
        if isinstance(holder, dict) and "turn" in holder:
            try:
                return int(holder["turn"])
            except (TypeError, ValueError):
                pass
    return None

def safe_world_dirname(world: str) -> str:
    """保证世界名可以安全地用作目录/文件名（保留中文，剔除 Windows 非法字符）。"""
    bad = '<>:"/\\|?*'
    out = "".join("_" if (ch in bad or ord(ch) < 32) else ch for ch in world).strip()
    out = out.rstrip(". ")
    return out or "world"


# --------------------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------------------


class Config:
    #: 已经提示过"缺这个键、用了默认值"的键名（避免每次读都刷日志）
    _warned_missing: set = set()

    def __init__(self, path: str):
        self.path = path
        self.data = json.loads(json.dumps(DEFAULT_CONFIG))
        existed = os.path.isfile(path)
        self.load()
        if not existed:
            # 首次运行就把默认配置落盘，用户才有文件可以直接编辑热键
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                self.save()
            except OSError:
                logging.warning("无法写入默认配置: %s", path)

    def load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
        except Exception:
            logging.warning("配置文件损坏，使用默认值: %s", self.path)
            return
        if isinstance(user, dict):
            for key, value in user.items():
                if key == "hotkeys" and isinstance(value, dict):
                    for action, spec in value.items():
                        # 允许写单个字符串，也允许写候选列表
                        specs = spec if isinstance(spec, list) else [spec]
                        specs = [s for s in specs if isinstance(s, str) and s.strip()]
                        if not specs:
                            continue
                        defaults = DEFAULT_CONFIG["hotkeys"].get(action, [])
                        # 用户显式指定的放最前，内置退路继续兜底
                        merged = list(specs) + [d for d in defaults if d not in specs]
                        self.data["hotkeys"][action] = merged
                else:
                    self.data[key] = value

    def save(self) -> None:
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def __getitem__(self, key: str) -> Any:
        """读配置项；缺失时回退到内置默认值。

        这里刻意**不**直接 ``self.data[key]``：老版本写入的 config.json 里没有
        后加的键，一旦代码里裸取就会 ``KeyError``，而且是那种"整个快照功能全废、
        但界面只报一句看不懂的错"的故障（本项目实际踩过：
        ``snapshot_consistent_attempts`` 漏进 DEFAULT_CONFIG，导致每次快照
        都在拷贝前抛 KeyError）。回退到默认值能让旧配置自动获得新行为的默认。
        """
        if key in self.data:
            return self.data[key]
        if key in DEFAULT_CONFIG:
            if key not in Config._warned_missing:
                Config._warned_missing.add(key)
                logging.warning("配置项 %s 缺失，使用内置默认值 %r（只提示一次）",
                                key, DEFAULT_CONFIG[key])
            return DEFAULT_CONFIG[key]
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self.data[key] = value


# --------------------------------------------------------------------------------------
# 快照数据模型
# --------------------------------------------------------------------------------------


@dataclass
class WorldInfo:
    name: str
    path: str
    character: str = "(未知)"
    character_key: str = ""
    save_time: Optional[_dt.datetime] = None    #现实存档时间
    save_time_raw: str = ""
    mods: list[str] = field(default_factory=list)

    @property
    def display(self) -> str:
        return f"{self.name} / {self.character}"


@dataclass
class Snapshot:
    sid: int
    world: str
    path: str
    created: float
    character: str = ""
    note: str = ""
    kind: str = "manual"
    game_time: str = ""
    file_count: int = 0
    total_bytes: int = 0
    parent_sid: Optional[int] = None
    linked_files: int = 0
    #: 用户给它起的名字（用于永久存档）
    name: str = ""
    #: 永久存档：不受"保留 N 份"的自动删除影响
    permanent: bool = False
    #: 拍摄时是否通过了一致性校验。
    #: None = 旧版本拍的快照，没有这个字段（只能用启发式兜底判断）。
    capture_verified: Optional[bool] = None
    #: 拍摄时按快照实际内容算出的指纹（旧档可能为空或不符，只当快速过滤器用）
    content_fingerprint: str = ""
    game_turn: Optional[int] = None

    @property
    def created_local(self) -> _dt.datetime:
        return _dt.datetime.fromtimestamp(self.created)

    @property
    def display_name(self) -> str:
        """列表里显示的名字：有自定义名字就用它，否则用 #编号。"""
        return self.name or self.label()

    def label(self) -> str:
        return f"#{self.sid:03d}"

    def to_json(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "world": self.world,
            "created": self.created,
            "character": self.character,
            "note": self.note,
            "kind": self.kind,
            "game_time": self.game_time,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "parent_sid": self.parent_sid,
            "linked_files": self.linked_files,
            "name": self.name,
            "permanent": self.permanent,
            "capture_verified": self.capture_verified,
            "content_fingerprint": self.content_fingerprint,
            "game_turn": self.game_turn
        }

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "Snapshot":
        verified = obj.get("capture_verified", None)
        return Snapshot(
            sid=int(obj.get("sid", 0)),
            world=str(obj.get("world", "")),
            path="",
            created=float(obj.get("created", 0)),
            character=str(obj.get("character", "")),
            note=str(obj.get("note", "")),
            kind=str(obj.get("kind", "manual")),
            game_time=str(obj.get("game_time", "")),
            file_count=int(obj.get("file_count", 0)),
            total_bytes=int(obj.get("total_bytes", 0)),
            parent_sid=obj.get("parent_sid"),
            linked_files=int(obj.get("linked_files", 0)),
            name=str(obj.get("name", "") or ""),
            permanent=bool(obj.get("permanent", False)),
            capture_verified=None if verified is None else bool(verified),
            content_fingerprint=str(obj.get("content_fingerprint", "") or ""),
            game_turn=obj.get("game_turn"),

        )


# --------------------------------------------------------------------------------------
# 快照管理器
# --------------------------------------------------------------------------------------


class SnapshotError(RuntimeError):
    pass


class SnapshotInconsistent(SnapshotError):
    """快照没通过一致性校验，已整份丢弃（宁可不存，绝不存坏）。

    单独一个类型是为了让上层能把"存了个半新半旧的档"和"这次干脆没存成"
    分开提示：前者会变成读档后的地图时间回溯，后者只是让你重拍一次。
    """


class LatencyTrace:
    """记录"按下 F5 → 快照开始写入"各阶段的时间戳，供日志与面板展示。

    全局单例就够了：`SnapshotApp.busy` 已经把快照操作串行化，同一时刻不可能有
    两次快照在跑。用单调时钟（perf_counter），不受系统时间调整影响。

    阶段划分：
        hotkey ──投递按键──> key ──等游戏写盘──> saved ──静默窗口──> copy（开始拷贝）
    """

    def __init__(self) -> None:
        self.t0 = 0.0
        self.marks: dict[str, float] = {}
        self._finished = ""

    @property
    def armed(self) -> bool:
        return self.t0 > 0.0 and not self._finished

    def start(self) -> None:
        self.t0 = time.perf_counter()
        self.marks.clear()
        self._finished = ""

    def start_if_idle(self) -> None:
        """热键路径已经 start() 过就别覆盖 —— 那样会把热键投递的时间吃掉。"""
        if not self.armed:
            self.start()

    def mark(self, name: str) -> None:
        if self.armed:
            self.marks[name] = time.perf_counter()

    @property
    def total(self) -> float:
        """从起点到"开始写入"的秒数；不足或缺阶段时返回 0。"""
        if not self.t0 or "copy" not in self.marks:
            return 0.0
        return self.marks["copy"] - self.t0

    def finish(self) -> str:
        """生成一句话摘要并"收尾"（此后 mark 不再生效，避免污染下一次快照）。"""
        if not self.t0 or "copy" not in self.marks:
            return ""
        segs = []
        order = (("key", "投递按键"), ("saved", "等游戏写盘"), ("copy", "静默窗口"))
        prev = self.t0
        for name, label in order:
            if name in self.marks:
                segs.append(f"{label} {self.marks[name] - prev:.2f}s")
                prev = self.marks[name]
        self._finished = f"{self.total:.2f} 秒（" + " / ".join(segs) + "）"
        self.t0 = 0.0
        return self._finished


#: 全局延迟记录器（见 LatencyTrace 的说明：busy 已串行化，不需要并发保护）
TRACE = LatencyTrace()


class SnapshotManager:
    def __init__(self, game_root: str, config: Config, data_root: Optional[str] = None):
        """``data_root`` 是快照数据目录（config/manifest/各份快照所在）。

        默认按这个优先级决定：

        1. 显式传入的 ``data_root``；
        2. **配置文件所在的那个目录** —— 因为配置永远和数据放在一起
           （``<data_root>/config.json``）。这条让"临时 root"的测试天然隔离：
           测试自己造一个 ``<临时目录>/.cdda_snapshots/config.json``，
           数据就会老老实实待在临时目录里，绝不会写进真实安装。
           （第一版没有这条，测试直接把 6 份假快照写进了用户的 manifest，
           实测踩过。）
        3. 否则用 :func:`snapshot_root`：插件自己文件夹里的 ``.cdda_snapshots``。
        """
        self.game_root = os.path.abspath(game_root)
        self.config = config
        self.save_dir = os.path.join(self.game_root, "save")
        resolved = data_root
        if not resolved and getattr(config, "path", ""):
            candidate = os.path.dirname(os.path.abspath(config.path))
            if os.path.basename(candidate) == SNAPSHOT_ROOT_NAME:
                resolved = candidate
        # 快照数据跟着插件走，不跟游戏根目录走（见 snapshot_root 的说明）
        self.root = os.path.abspath(resolved) if resolved else snapshot_root(self.game_root)
        self.manifest_path = os.path.join(self.root, MANIFEST_NAME)
        self.backup_dir = os.path.join(self.root, "_backup")
        self.log_path = os.path.join(self.root, LOG_NAME)
        self._lock = threading.RLock()
        #: 最近一次快照是否取得了"完全一致"的状态（拷贝期间目录没被动过）
        self.last_snapshot_consistent = True
        #: 快照路径 -> 可疑原因（空串 = 没问题），避免面板反复 stat 上百个文件
        self._suspicion_cache: dict[str, str] = {}
        #: 最近一次快照"从按下热键到开始写入"的秒数与分段摘要（0/"" = 无）
        self.last_capture_seconds = 0.0
        self.last_capture_latency = ""
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self.backup_dir, exist_ok=True)

    # -------------------------------------------------- 基础

    def list_worlds(self) -> list[WorldInfo]:
        if not os.path.isdir(self.save_dir):
            return []
        worlds = []
        for name in sorted(os.listdir(self.save_dir)):
            path = os.path.join(self.save_dir, name)
            if os.path.isdir(path):
                worlds.append(self._read_world(name))
        return worlds

    def world_info(self, world: str) -> WorldInfo:
        return self._read_world(world)

    def _read_world(self, world: str) -> WorldInfo:
        path = os.path.join(self.save_dir, world)
        info = WorldInfo(name=world, path=path)
        ts_file = os.path.join(path, "save_timestamp.json")
        if os.path.isfile(ts_file):
            try:
                with open(ts_file, "r", encoding="utf-8", errors="replace") as fh:
                    info.save_time_raw = str(json.load(fh))
                info.save_time = decode_save_timestamp(info.save_time_raw)
            except Exception:
                pass

        mods_file = os.path.join(path, "mods.json")
        if os.path.isfile(mods_file):
            try:
                with open(mods_file, "r", encoding="utf-8", errors="replace") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    info.mods = [str(x) for x in data]
            except Exception:
                pass

        # 角色判定：取 .sav / .sav.zzip 中 mtime 最新的那个
        best_mtime = -1.0
        best_name = ""
        try:
            entries = os.listdir(path)
        except OSError:
            entries = []
        for entry in entries:
            low = entry.lower()
            if ".sav" not in low or entry.startswith("."):
                continue
            if not entry.startswith("#"):
                continue
            try:
                mtime = os.path.getmtime(os.path.join(path, entry))
            except OSError:
                continue
            if mtime > best_mtime:
                best_mtime = mtime
                best_name = entry
        if best_name:
            info.character_key = best_name.split(".", 1)[0]
            info.character = decode_char_filename(best_name) or info.character
        return info

    # -------------------------------------------------- manifest

    def _load_manifest(self) -> dict[str, Any]:
        if not os.path.isfile(self.manifest_path):
            return {"version": 1, "next_sid": 1, "snapshots": []}
        try:
            with open(self.manifest_path, "r", encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except Exception:
            logging.exception("manifest 读取失败，重建")
            return {"version": 1, "next_sid": 1, "snapshots": []}
        data.setdefault("version", 1)
        data.setdefault("next_sid", 1)
        data.setdefault("snapshots", [])
        return data

    def _save_manifest(self, data: dict[str, Any]) -> None:
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.manifest_path)
        # 名单一变，可疑判定就该重算
        self._suspicion_cache.clear()

    def snapshots(self, world: Optional[str] = None, verify: bool = False) -> list[Snapshot]:
        with self._lock:
            data = self._load_manifest()
            out: list[Snapshot] = []
            dirty = False
            for raw in data["snapshots"]:
                snap = Snapshot.from_json(raw)
                snap.path = os.path.join(self.root, safe_world_dirname(snap.world), snap.label())
                if not os.path.isdir(snap.path):
                    dirty = True  # 目录被手工删掉 -> 从 manifest 剔除
                    continue
                if snap.capture_verified is None:
                    # manifest 里没有这个字段（旧版本写的）：从快照自己的
                    # snapshot.json 取一次，避免把"已校验"误判成"未知"。
                    meta_path = os.path.join(snap.path, SNAP_META_NAME)
                    if os.path.isfile(meta_path):
                        try:
                            with open(meta_path, "r", encoding="utf-8") as fh:
                                value = json.load(fh).get("capture_verified")
                            if value is not None:
                                snap.capture_verified = bool(value)
                        except Exception:
                            pass
                if verify:
                    count, size = dir_stats(snap.path)
                    if count != snap.file_count or size != snap.total_bytes:
                        snap.file_count, snap.total_bytes = count, size
                        for raw_entry in data["snapshots"]:
                            if int(raw_entry.get("sid", -1)) == snap.sid:
                                raw_entry["file_count"] = count
                                raw_entry["total_bytes"] = size
                        dirty = True
                out.append(snap)
            if dirty:
                self._save_manifest(data)
            out.sort(key=lambda s: (s.world, s.sid))
            if world is not None:
                out = [s for s in out if s.world == world]
            return out

    def usage(self) -> dict[str, Any]:
        """统计快照数据目录的"逻辑大小"与"硬链接去重后的实占"。

        CDDA 的世界目录动辄上万个文件，同一份文件在每份快照里都是同一个硬链接；
        直接把每份快照的 ``total_bytes`` 加起来会得到"全都完整拷贝"的假象。
        这里按 (st_dev, st_ino) 只计一次，得到真正吃掉的磁盘空间；
        ``logical`` 则是逐份累加的表观大小，两者之差就是硬链接省下来的量。
        """
        logical = 0
        actual = 0
        files = 0
        seen: set[tuple[int, int]] = set()
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                try:
                    st = os.stat(os.path.join(dirpath, name))
                except OSError:
                    continue
                logical += st.st_size
                files += 1
                if st.st_nlink > 1:
                    key = (st.st_dev, st.st_ino)
                    if key in seen:
                        continue
                    seen.add(key)
                actual += st.st_size
        return {
            "root": self.root,
            "logical": logical,
            "actual": actual,
            "saved": max(0, logical - actual),
            "files": files,
        }

    def latest_for(self, world: str) -> Optional[Snapshot]:
        snaps = self.snapshots(world)
        return snaps[-1] if snaps else None

    # -------------------------------------------------- 创建快照

    def create(
        self,
        world: str,
        *,
        note: str = "",
        kind: str = "manual",
        progress=None,
        force: bool = False,
        skip_if_identical: bool = True,
    ) -> Snapshot:
        """对 ``save/<world>`` 做一次自包含快照。

        * ``skip_if_identical=True``（默认）：内容与已有快照相同时直接复用它，不新建。
        * ``force=True``：跳过去重检查，强制建一份。
        * ``skip_if_identical=False``：也跳过去重检查。这个用于"刚刚真的让游戏存过盘"
          的场景——那次存盘本身就是玩家的一次操作，即使落盘内容碰巧和旧快照一样，
          也应该留一个时间点。
        """
        with self._lock:
            world_path = os.path.join(self.save_dir, world)
            if not os.path.isdir(world_path):
                raise SnapshotError(f"找不到世界目录: {world_path}")

            # 先算内容指纹：1) 用于跳过重复快照 2) 存进元数据供以后判断
            content_fp = _fingerprint(world_path)

            info = self._read_world(world)
            data = self._load_manifest()

            # 内容没变就别再存一份——省硬盘，也省时间
            if skip_if_identical and not force:
                same = self.find_identical(world, content_fp)
                if same is not None:
                    logging.info("世界内容与快照 %s 完全相同，跳过创建", same.label())
                    return same

            sid = int(data["next_sid"])
            dest_root = os.path.join(self.root, safe_world_dirname(world))
            os.makedirs(dest_root, exist_ok=True)

            # 目录快照（硬链接去重）优先；失败则退回 zip
            snap_dir = os.path.join(dest_root, f"#{sid:03d}")
            if os.path.exists(snap_dir):
                shutil.rmtree(snap_dir, ignore_errors=True)

            # 上一份快照，用于硬链接复用
            prev_dir: Optional[str] = None
            prev_sid: Optional[int] = None
            for snap in reversed(self.snapshots(world)):
                prev_dir = snap.path
                prev_sid = snap.sid
                break

            use_links = bool(self.config["use_hardlinks"])
            prev_index = _index_dir(prev_dir) if (use_links and prev_dir) else {}
            # 本次快照"从按下 F5 到开始写入"的秒数（0 = 没有热键起点，如备份路径）
            self.last_capture_latency = ""
            self.last_capture_seconds = 0.0

            def _report(msg: str) -> None:
                if progress:
                    try:
                        progress(msg)
                    except Exception:
                        pass

            # ---- 一致性拷贝（宁可不存，绝不存坏）----
            #
            # 游戏在运行时（或刚触发存档时）会继续写盘，而 CDDA 的一次存档是
            # **分几段落**的：角色档最先写，地图/超图/视野随后才写。实测抓到的
            # 写入序列（2026-09-30 14:51:16 那次快速保存）：
            #
            #   14:51:16.904  #char.sav.zzip      <- 角色档
            #   14:51:17.178  overmaps/o.1.1.zzip <- 超图
            #   14:51:17.436  maps/10.3.1.zzip    <- 地图，比角色档晚 0.53 秒
            #   14:51:17.639  #char.pt            <- 收尾
            #
            # 只要在中间任何一刻把目录拷走，就会得到"角色档是新的、地图档还是
            # 旧的"这种半新半旧的状态。读回去就是物品位置回溯、角色与地图不同步，
            # 以及"xxx/yyy 处已有一个敌人""Shot along ... out-of-bounds"这类报错
            # （子地图和超图的怪物/坐标来自不同时刻）。
            #
            # **只有"目录连续安静了 settle 秒"才是"这次存盘写完了"的凭据。**
            # 实测一次存档内部相邻两段的间隔最大约 114 ms（166 个样本，
            # 见 `_f5_settle.py hist`），所以 settle 默认取 1.0 秒（8.8 倍余量）。
            # 注意"跨度"和"间隔"不是一回事：.sav 到地图的跨度可以有 0.53 秒，
            # 但那是多个 20~100 ms 间隔累出来的，单段间隔没有那么大。
            #
            # 三道校验，顺序执行，全过才算数：
            #   0) 拷贝之前先等源目录安静 settle 秒（游戏没在跑时天然成立，直接跳过）；
            #   1) 拷贝期间源目录没被动过（拷贝前后索引比对）；
            #   2) 快照与源目录**逐项相同**（路径+大小+mtime 完全相等）。
            #
            # 为什么不校验 0 与 1/2 各来一遍、拷贝前后各等一个 settle：
            # 安静窗口只要**跨住拷贝**就够 —— 拷贝前已安静 settle 秒、拷贝期间又
            # 验证没变，合计安静 settle+拷贝耗时，本身就盖过 settle 了。
            # 原来"拷贝前 1.5 秒 + 拷贝后 1.5 秒"实测白等 3.1 秒里的 1.5 秒。
            # 校验 0 是**必需**的：像 #178 那次，地图是在拷贝**结束之后**才落盘的，
            # 只比对拷贝前后的索引根本发现不了，能拦住它的只有安静窗口。
            max_attempts = max(1, int(self.config["snapshot_consistent_attempts"] or 1))
            settle = max(0.0, float(self.config["snapshot_settle_seconds"] or 0.0))
            allow_bad = bool(self.config["allow_inconsistent_snapshot"])
            game_writing = is_process_running(GAME_EXE)
            stable = False
            attempts_used = 0
            why = "未开始校验"
            snap_index: dict[str, tuple[int, int]] = {}
            file_count = total_bytes = linked = copied = 0
            for attempt in range(1, max_attempts + 1):
                attempts_used = attempt

                # 校验 0：等静默窗口。**不管游戏在不在跑都要等** ——
                # 写入者未必是游戏（另一个实例、备份软件、手工拷贝……），
                # 而"目录已经安静够久"时 `_wait_source_frozen` 的快路径会零等待秒回，
                # 所以这道校验既便宜又不该省。第一版按"游戏没在跑就跳过"来做，
                # 结果回归测试 T6（外部线程持续写盘）就能钻进两道拷贝校验之间的缝。
                quiet = True
                if settle > 0:
                    if game_writing:
                        _report(f"正在确认存档已写完（目录需安静 {settle:.1f} 秒）…")
                    quiet = self._wait_source_frozen_unlocked(
                        world_path, settle, progress=_report)

                before_index = _index_dir(world_path)
                if os.path.isdir(snap_dir):
                    shutil.rmtree(snap_dir, ignore_errors=True)
                os.makedirs(snap_dir, exist_ok=True)
                linked = copied = total_bytes = file_count = 0

                # 「按下 F5 → 快照开始写入」的分段记录（就在第一次落盘之前打点）
                TRACE.mark("copy")
                self.last_capture_seconds = TRACE.total
                self.last_capture_latency = TRACE.finish()
                if self.last_capture_latency:
                    logging.info("按下 F5 → 开始写入：%s", self.last_capture_latency)

                for rel in sorted(before_index):
                    src = os.path.join(world_path, rel)
                    dst = os.path.join(snap_dir, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    size, mtime_ns = before_index[rel]
                    try:
                        if use_links and prev_index.get(rel) == (size, mtime_ns):
                            if _try_hardlink(prev_dir, rel, dst):
                                linked += 1
                            else:
                                shutil.copy2(src, dst)
                                copied += 1
                        else:
                            shutil.copy2(src, dst)
                            copied += 1
                    except OSError as exc:
                        raise SnapshotError(f"复制失败 {rel}: {exc}") from exc
                    total_bytes += size
                    file_count += 1
                    if file_count % 200 == 0:
                        _report(f"已复制 {file_count} 个文件…")

                # 校验 1+2：一次扫描源目录、一次扫描快照目录，两件事一起判
                # （指纹也由这份索引派生，不再多走两遍目录）
                now_index = _index_dir(world_path)
                changed = _index_diff(before_index, now_index)
                snap_index = _index_dir(snap_dir)
                mirror = (not changed) and snap_index == now_index

                if not quiet:
                    why = f"等不到 {settle:.1f} 秒的静默窗口（游戏一直在写盘）"
                elif changed:
                    why = f"拷贝期间有 {len(changed)} 个文件被改动"
                elif not mirror:
                    why = "拷贝结束后源目录又变了（快照与存档不一致）"
                else:
                    why = ""

                if not why:
                    stable = True
                    break

                logging.warning("快照第 %d/%d 次尝试未通过一致性校验：%s，重做",
                                attempt, max_attempts, why)
                if progress:
                    progress(f"检测到游戏仍在写盘（{why}），重新快照…")
                time.sleep(0.6)

            if stable:
                logging.info("快照 #%03d 一致性校验通过（镜像一致 + 静默 %.1f 秒）",
                             sid, settle)
            else:
                logging.warning(
                    "快照 #%03d 连续 %d 次未能取得一致状态（%s）",
                    sid, attempts_used, why,
                )
                if not allow_bad:
                    # 关键：宁可这次不存，也绝不发布一份"半新半旧"的档。
                    # 一旦落进 manifest，F6 还原的正是"最新那份"，
                    # 也就是最不可信的这份 —— 那才是"地图时间回溯"的真正入口。
                    shutil.rmtree(snap_dir, ignore_errors=True)
                    self.last_snapshot_consistent = False
                    raise SnapshotInconsistent(
                        f"游戏一直在写盘，{attempts_used} 次都没能取到一致的存档状态"
                        f"（{why}），本次快照已丢弃，没有写入任何档点。\n"
                        f"请等游戏里「正在存档…」提示消失、画面稳定后再按一次；"
                        f"或把 config.json 的 snapshot_settle_seconds 调大"
                        f"（当前 {settle:.1f} 秒）。"
                    )
                if progress:
                    progress("警告：游戏一直在写盘，本次快照可能不是完全一致的状态")
            self.last_snapshot_consistent = stable

            if file_count == 0:
                shutil.rmtree(snap_dir, ignore_errors=True)
                raise SnapshotError("世界目录里没有任何存档文件，快照取消。")

            # 指纹按**快照实际内容**算（直接用刚才那次扫描的结果，不再走一遍目录），
            # 不按拷贝前那一刻算：拷贝前后源目录可能已经变过，沿用旧值会让元数据撒谎。
            actual_fp = _fingerprint_from_index(snap_index)

            meta = {
                "sid": sid,
                "world": world,
                "character": info.character,
                "character_key": info.character_key,
                "created": time.time(),
                "note": note,
                "kind": kind,
                "game_time": info.save_time_raw,
                "file_count": file_count,
                "total_bytes": total_bytes,
                "linked_files": linked,
                "copied_files": copied,
                "save_time_raw": info.save_time_raw,
                "mods": info.mods,
                "content_fingerprint": actual_fp,
                "capture_verified": stable,
                "capture_attempts": attempts_used,
                "capture_seconds": round(self.last_capture_seconds, 3),
                "capture_latency": self.last_capture_latency,
                "app_version": APP_VERSION,
            }
            with open(os.path.join(snap_dir, SNAP_META_NAME), "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False, indent=2)

            snap = Snapshot(
                sid=sid,
                world=world,
                path=snap_dir,
                created=meta["created"],
                character=info.character,
                note=note,
                kind=kind,
                game_time=info.save_time_raw,
                file_count=file_count,
                total_bytes=total_bytes,
                parent_sid=prev_sid,
                linked_files=linked,
                capture_verified=stable,
                game_turn=read_game_turn(info.path),
            )
            # 提交阶段必须**重新读一遍** manifest：静默期里放开了锁，
            # 别人（面板刷新时的自愈整理等）可能已经改过它，
            # 拿进入时的旧副本覆盖回去会丢掉那次改动。
            data = self._load_manifest()
            if any(int(s.get("sid", -1)) == sid for s in data["snapshots"]):
                # 理论上到不了这里（app 层 busy 串行化），但真撞上就干净地退出，
                # 绝不把两个不同内容写成同一个 #编号。
                shutil.rmtree(snap_dir, ignore_errors=True)
                raise SnapshotError(
                    f"快照编号 #{sid:03d} 已被占用（有另一个快照操作在同时进行），"
                    f"本次已取消，请重试。"
                )
            data["snapshots"].append(snap.to_json())
            data["next_sid"] = max(int(data.get("next_sid", 1)), sid + 1)
            self._save_manifest(data)

            logging.info(
                "快照 #%03d 完成: %s (%d 文件, %s, 硬链接 %d)",
                sid, world, file_count, human_size(total_bytes), linked,
            )
            self.prune(world)
            return snap

    @staticmethod
    def _wait_source_frozen(world_path: str, seconds: float,
                            timeout: float = 10.0, progress=None) -> bool:
        """等源目录"连续 ``seconds`` 秒没有任何写入"，返回是否等到。

        凭什么说连续 quiet：目录里**最新的 mtime 就是最后一次写入的时刻**
        （mtime 只会被写入推后）。所以 ``现在 - 最新mtime >= seconds``
        本身就是"过去 seconds 秒内没有写入"的**充分证据**，不需要睡满再比两次。

        **性能**（这条很关键）：老写法是"扫一次 → 睡满 seconds → 再扫一次，
        两次相同才算安静"。如果等待是从存档**中途**开始的（F5 正是如此：
        检测到 .sav 变化时地图还没写完），第一次比对照样不等，于是又睡一轮 ——
        实测白等 **2×1.5=3.0 秒**。现在改成按"最新写入的年龄"精确算还差多久：
        差 1.3 秒就只睡 1.3 秒，最坏也只是 `seconds - age`，不会翻倍。

        ``seconds <= 0`` 时直接返回 True。返回 False 表示超时（目录一直在被写），
        调用方必须丢弃重做，不能当成"存档写完了"。

        顺带一个快路径：目录本来就已安静够久（游戏停在主菜单、游戏没开、
        Ctrl+F5 磁盘快照）时，一次扫描就返回，**零等待**。
        """
        if seconds <= 0:
            return True

        def last_write_age(index: dict[str, tuple[int, int]]) -> float:
            newest_ns = max((mt for _size, mt in index.values()), default=0)
            return (time.time() - newest_ns / 1e9) if newest_ns else float("inf")

        index = _index_dir(world_path)
        age = last_write_age(index)
        if age >= seconds:
            return True                      # 本来就安静够久了，秒回

        deadline = time.time() + max(seconds, timeout)
        while time.time() < deadline:
            if progress:
                try:
                    progress(f"等待游戏把这次存档写完…（还需 {max(seconds - age, 0):.1f} 秒）")
                except Exception:
                    pass
            time.sleep(min(max(seconds - age, 0.05), 0.5))
            cur = _index_dir(world_path)
            # 两次索引相同 = 这段里确实没人写（大小也一并在索引里，
            # 正在写的大文件会因大小变化被抓出来）；年龄够 = 从最后一次写入
            # 算起已经满了 seconds。两条合起来才是完整的安静窗口。
            if cur == index and last_write_age(cur) >= seconds:
                return True
            index, age = cur, last_write_age(cur)
        return False

    def _wait_source_frozen_unlocked(self, world_path: str, seconds: float,
                                     timeout: float = 10.0, progress=None) -> bool:
        """同 :meth:`_wait_source_frozen`，但等待期间**放开 self._lock**。

        静默期可以长达十几秒，而面板刷新要走 ``snapshots()`` 拿同一把锁：
        占着锁等就会把整个界面冻住，连"等待游戏把这次存档写完…"
        这类进度提示都刷不出来。

        放开锁是安全的：
        * 新快照在提交进 manifest 之前对外不存在，读方看不到半成品；
        * 提交阶段会重新读一遍 manifest，不会覆盖这段空档里别人的改动。
        """
        self._lock.release()
        try:
            return self._wait_source_frozen(world_path, seconds, timeout, progress)
        finally:
            self._lock.acquire()

    # -------------------------------------------------- 重复检测 / 去重

    def snapshot_fingerprint(self, snap: Snapshot) -> Optional[str]:
        """取快照的内容指纹。

        **以现场计算为准**，不优先用元数据里存的值：指纹算法会随版本演进，
        旧快照元数据里存的是老算法的结果，拿它跟当前世界比对永远不相等
        （实测踩过：97 个文件逐个一致，指纹却不同）。
        算法变了以后现场重算会自愈，不需要迁移脚本。
        """
        if os.path.isdir(snap.path):
            return _fingerprint(snap.path)
        return None

    def find_identical(self, world: str, fingerprint: str) -> Optional[Snapshot]:
        """找出与该指纹内容相同的快照（内容一致才返回）。

        **性能**：只对**校验过的**快照用元数据里存的 ``content_fingerprint``
        当"快速否定过滤器" —— 那种档的存值是当时按快照实际内容算的，可信，
        不等就一定不是同一个进度，省掉一次全目录扫描。
        旧档没有可信存值（实测 #171~#178 的存值全都与实际不符），一律现场重算。
        用错方向也是安全的：过滤器只可能漏判"重复"，那只会多存一份，不会误判成相同。
        """
        for snap in reversed(self.snapshots(world)):
            stored = snap.content_fingerprint
            if snap.capture_verified is True and stored and stored != fingerprint:
                continue
            if self.snapshot_fingerprint(snap) == fingerprint:
                return snap
        return None

    def suspicion(self, snap: Snapshot) -> tuple[str, str]:
        """这份快照有没有"可能是半新半旧"的嫌疑？返回 ``(级别, 原因)``。

        级别：

        * ``"bad"``    —— **确定**没通过一致性校验（``capture_verified is False``，
          只有把 ``allow_inconsistent_snapshot`` 打开才会留下这种档）。
        * ``"legacy"`` —— **不确定**：旧版本拍的档没有校验记录，只能用时间一致性
          启发式猜（角色档比地图/超图新出 ``verify_lag_warn_seconds`` 秒以上）。
          这只是启发式：地图本来没改动就不会重写，老 mtime 完全可能正常。
        * ``""``       —— 没发现问题。

        分开两级的理由是别"喊狼来了"：旧档里多数只是地图没动过，如果把
        8/10 份快照都标成红的，真出问题的那份反而没人看。
        空串级别表示"未知但没发现异常"，不算可疑。
        """
        cached = self._suspicion_cache.get(snap.path)
        if cached is not None:
            return cached

        level, reason = "", ""
        if snap.capture_verified is False:
            level = "bad"
            reason = "拍摄时未通过一致性校验（可能半新半旧）"
        elif snap.capture_verified is None:
            # 旧版本拍的档没有校验记录：复用体检里的时间一致性启发式，
            # 别再写一份判定（第一版就是自己另写、还把 overmaps 和 maps
            # 混在一起取最新值，正好漏掉了 #178 这种"地图旧、超图新"的档）。
            try:
                lag = float(self.config["verify_lag_warn_seconds"] or 0)
                for warning in snapshot_health(snap, lag)["warnings"]:
                    if "半新半旧" in warning:
                        level = "legacy"
                        reason = f"{warning}（旧版本拍的，无校验记录）"
                        break
            except Exception:
                logging.debug("可疑判定失败", exc_info=True)
        out = (level, reason)
        self._suspicion_cache[snap.path] = out
        return out

    def find_world_duplicate(self, world: str) -> Optional[Snapshot]:
        """当前世界目录是否已经和某份快照内容相同。"""
        world_path = os.path.join(self.save_dir, world)
        if not os.path.isdir(world_path):
            return None
        return self.find_identical(world, _fingerprint(world_path))

    def dedupe(self, world: Optional[str] = None, *, progress=None) -> list[int]:
        """删掉内容完全相同的重复快照，每组只留最早的一份。

        重复快照就是同一个进度存了多遍，纯粹浪费空间。因为快照之间大量文件是
        硬链接共享的，删掉多余的份也不会影响到保留的那份。
        返回被删除的快照编号。
        """
        removed: list[int] = []
        worlds = [world] if world else [w.name for w in self.list_worlds()]
        for name in worlds:
            groups: dict[str, list[Snapshot]] = {}
            for snap in self.snapshots(name):
                fp = self.snapshot_fingerprint(snap)
                if not fp:
                    continue
                groups.setdefault(fp, []).append(snap)
            for fp, snaps in groups.items():
                if len(snaps) < 2:
                    continue
                # 保留最早的一份，其余删掉
                for victim in snaps[1:]:
                    if progress:
                        try:
                            progress(f"删除重复快照 {victim.label()}（与 "
                                     f"{snaps[0].label()} 内容相同）")
                        except Exception:
                            pass
                    self.delete(victim)
                    removed.append(victim.sid)
        if removed:
            logging.info("去重完成，删除了 %d 份重复快照: %s", len(removed), removed)
        return removed

    # -------------------------------------------------- 存档新鲜度

    def save_age(self, world: str) -> Optional[float]:
        """磁盘上的存档距现在多久（秒）。没有存档返回 None。"""
        mt = sav_mtime(os.path.join(self.save_dir, world))
        return (time.time() - mt) if mt else None

    def disk_newer_than(self, snap: Snapshot) -> float:
        """磁盘上的世界目录比这份快照新多少秒（0 = 不比它新）。

        还原会整目录覆盖，所以"磁盘比目标快照新"就意味着**有进度要被丢掉**。
        2026-11-05 那次回退 37 分钟就是这样：磁盘 16:30:49 刚存过档，
        最新快照却是 15:53 的 #523，还原时没备份、也没提示。
        """
        mt = sav_mtime(os.path.join(self.save_dir, snap.world))
        if not mt:
            return 0.0
        return max(0.0, float(mt) - float(snap.created))

    # -------------------------------------------------- 删除 / 剪枝

    def delete(self, snap: Snapshot) -> None:
        with self._lock:
            data = self._load_manifest()
            data["snapshots"] = [
                raw for raw in data["snapshots"] if int(raw.get("sid", -1)) != snap.sid
            ]
            self._save_manifest(data)
            shutil.rmtree(snap.path, ignore_errors=True)
            logging.info("已删除快照 #%03d (%s)", snap.sid, snap.world)

    def prune(self, world: str) -> list[int]:
        keep = int(self.config["keep_per_world"] or 0)
        if keep <= 0:
            return []
        removed: list[int] = []
        with self._lock:
            # 参与"保留 N 份"计数的只有**可自动删除**的快照：
            #   * 永久存档（用户命名过）永远不动
            #   * backup / auto 是工具自己的产物，不计入也不删
            prunable = [
                s for s in self.snapshots(world)
                if s.kind not in ("backup", "auto") and not s.permanent
            ]
            while len(prunable) > keep:
                victim = prunable.pop(0)      # snapshots() 按 sid 升序 => 最老的在前
                self.delete(victim)
                removed.append(victim.sid)
        if removed:
            logging.info("按每世界保留 %d 份策略，自动删除最旧的快照: %s", keep, removed)
        return removed

    # -------------------------------------------------- 命名 / 永久存档

    def rename(self, snap: Snapshot, name: str, *, permanent: bool = True) -> Snapshot:
        """给快照命名，并（默认）把它标记为永久存档，隔离在自动删除之外。

        名字清空且 ``permanent=False`` 时等于取消永久标记、恢复普通快照。
        """
        clean = (name or "").strip()
        with self._lock:
            data = self._load_manifest()
            for raw in data["snapshots"]:
                if int(raw.get("sid", -1)) != snap.sid:
                    continue
                raw["name"] = clean
                # 有名字就默认永久；显式传 permanent=False 可强制取消
                raw["permanent"] = bool(clean) and permanent
                self._save_manifest(data)
                break
            else:
                raise SnapshotError(f"快照 {snap.label()} 不在索引里")

            # 同步写进快照目录里的元数据，便于拷走后仍能看出它的名字
            meta_path = os.path.join(snap.path, SNAP_META_NAME)
            try:
                if os.path.isfile(meta_path):
                    with open(meta_path, "r", encoding="utf-8",
                              errors="replace") as fh:
                        meta = json.load(fh)
                    meta["name"] = clean
                    meta["permanent"] = bool(clean) and permanent
                    with open(meta_path, "w", encoding="utf-8") as fh:
                        json.dump(meta, fh, ensure_ascii=False, indent=2)
            except Exception:
                logging.warning("写入快照元数据失败（索引已更新）", exc_info=True)

            snap.name = clean
            snap.permanent = bool(clean) and permanent
            logging.info("快照 %s 命名为 %r（永久=%s）",
                         snap.label(), clean, snap.permanent)
            return snap

    def auto_prune_state(self, world: str) -> tuple[int, int, int]:
        """返回 (可自动删除的份数, 上限, 永久存档份数)，供界面显示。"""
        keep = int(self.config["keep_per_world"] or 0)
        snaps = self.snapshots(world)
        prunable = [s for s in snaps
                    if s.kind not in ("backup", "auto") and not s.permanent]
        permanent = [s for s in snaps if s.permanent]
        return len(prunable), keep, len(permanent)

    # -------------------------------------------------- 还原

    def pending_restore(self, world: str) -> Optional[dict[str, Any]]:
        path = os.path.join(self.save_dir, world, RESTORE_MARKER)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def recover_if_needed(self) -> list[str]:
        """启动时自愈：还原中途崩溃留下的暂存目录。"""
        messages: list[str] = []
        if not os.path.isdir(self.save_dir):
            return messages
        for name in os.listdir(self.save_dir):
            if not name.endswith(STAGING_SUFFIX):
                continue
            world = name[: -len(STAGING_SUFFIX)]
            staging = os.path.join(self.save_dir, name)
            world_path = os.path.join(self.save_dir, world)
            marker = self.pending_restore(world)
            # 暂存目录里是「还原前的世界」；若目标世界不存在说明还原没做完
            if not os.path.isdir(world_path):
                os.rename(staging, world_path)
                messages.append(f"已恢复 {world} 的还原前备份（上次还原未完成）")
            else:
                shutil.rmtree(staging, ignore_errors=True)
                messages.append(f"已清理 {world} 的多余暂存目录")
            if marker is not None:
                try:
                    os.remove(os.path.join(world_path, RESTORE_MARKER))
                except OSError:
                    pass
        return messages

    def restore(
        self,
        snap: Snapshot,
        *,
        make_backup: bool = True,
        progress=None,
    ) -> dict[str, Any]:
        """把世界目录原子上换为快照内容。调用前必须已确保游戏已退出。"""

        def _report(msg: str) -> None:
            if progress:
                try:
                    progress(msg)
                except Exception:
                    pass

        with self._lock:
            world = snap.world
            world_path = os.path.join(self.save_dir, world)
            staging = world_path + STAGING_SUFFIX
            backup_snap: Optional[Snapshot] = None

            if is_process_running(GAME_EXE):
                raise SnapshotError("游戏仍在运行，请先退出游戏再还原快照。")
            if not os.path.isdir(snap.path):
                raise SnapshotError(f"快照目录不存在: {snap.path}")

            # 1) 还原前自动备份
            if make_backup and os.path.isdir(world_path):
                _report("正在备份当前进度…")
                stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                try:
                    backup_snap = self.create(
                        world,
                        note=f"还原 #{snap.sid:03d} 前的自动备份（{stamp}）",
                        kind="backup",
                        progress=progress,
                    )
                except SnapshotError as exc:
                    logging.warning("还原前自动备份失败: %s", exc)

            # 2) 写还原标记 + 把旧世界整体挪进暂存目录
            _report("正在上换存档目录…")
            if os.path.isdir(world_path):
                if os.path.isdir(staging):
                    shutil.rmtree(staging, ignore_errors=True)
                pending = {
                    "world": world,
                    "target_sid": snap.sid,
                    "target_path": snap.path,
                    "staging": staging,
                    "started": time.time(),
                }
                with open(os.path.join(world_path, RESTORE_MARKER), "w", encoding="utf-8") as fh:
                    json.dump(pending, fh, ensure_ascii=False, indent=2)
                os.makedirs(staging, exist_ok=True)
                for entry in os.listdir(world_path):
                    if entry == RESTORE_MARKER:
                        continue
                    os.rename(os.path.join(world_path, entry), os.path.join(staging, entry))

            # 3) 拷贝快照内容过来
            os.makedirs(world_path, exist_ok=True)
            copied = 0
            total = max(1, snap.file_count)
            for rel in iter_save_files(snap.path):
                src = os.path.join(snap.path, rel)
                dst = os.path.join(world_path, rel)
                _copy_into(src, dst)
                copied += 1
                if copied % 200 == 0:
                    _report(f"已还原 {copied}/{total} 个文件…")

            # 4) 清理标记与暂存
            marker_path = os.path.join(world_path, RESTORE_MARKER)
            try:
                os.remove(marker_path)
            except OSError:
                pass
            shutil.rmtree(staging, ignore_errors=True)

            logging.info(
                "已还原 #%03d -> %s（%d 文件，自动备份 #%s）",
                snap.sid, world, copied,
                backup_snap.sid if backup_snap else "-",
            )
            return {
                "world": world,
                "restored_sid": snap.sid,
                "backup_sid": backup_snap.sid if backup_snap else None,
                "files": copied,
            }

    # -------------------------------------------------- 导出 / 导入

    def export_zip(self, snap: Snapshot, zip_path: str) -> str:
        import zipfile

        os.makedirs(os.path.dirname(os.path.abspath(zip_path)), exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for rel in iter_save_files(snap.path):
                zf.write(os.path.join(snap.path, rel), rel)
            zf.writestr(
                SNAP_META_NAME + ".export.json",
                json.dumps(snap.to_json(), ensure_ascii=False, indent=2),
            )
        return zip_path


# --------------------------------------------------------------------------------------
# 目录 / 文件辅助
# --------------------------------------------------------------------------------------


# 这些名字是工具自己的簿记文件，绝不能混进世界目录的备份内容里
BOOKKEEPING_NAMES = (RESTORE_MARKER, SNAP_META_NAME, MANIFEST_NAME)


def iter_save_files(root: str) -> Iterable[str]:
    """遍历世界目录（或快照目录）内所有应备份的文件，返回相对路径。

    工具自己的簿记文件（还原标记、快照元信息）一律跳过：否则某次误写进世界目录的
    ``snapshot.json`` 会被当成存档一起快照并还原回去，污染快照目录的元数据。
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, root)
        for name in sorted(filenames):
            if name in BOOKKEEPING_NAMES:
                continue
            if rel_dir == ".":
                yield name
            else:
                yield os.path.join(rel_dir, name)


def dir_stats(root: str) -> tuple[int, int]:
    """返回 (存档文件数, 逻辑总大小)。工具自己的簿记文件不计入。"""
    count = 0
    size = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name in BOOKKEEPING_NAMES:
                continue
            try:
                size += os.path.getsize(os.path.join(dirpath, name))
                count += 1
            except OSError:
                pass
    return count, size


def allocated_bytes(root: str) -> int:
    """实际占用磁盘空间（同一硬链接只算一次）。"""
    seen: set[tuple[int, int]] = set()
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += getattr(st, "st_blocks", 0) * 512 or st.st_size
    return total


def _index_dir(root: Optional[str]) -> dict[str, tuple[int, int]]:
    """相对路径 -> (大小, mtime_ns)，用于判断能否硬链接复用。"""
    index: dict[str, tuple[int, int]] = {}
    if not root or not os.path.isdir(root):
        return index
    for rel in iter_save_files(root):
        try:
            st = os.stat(os.path.join(root, rel))
        except OSError:
            continue
        index[rel] = (st.st_size, st.st_mtime_ns)
    return index


def _fingerprint_from_index(index: dict[str, tuple[int, int]]) -> str:
    """从已经扫好的索引算内容指纹（不再走一遍目录）。

    ``_fingerprint`` 与 ``_index_dir`` 必须是**同一份数据**的两种视图，
    否则"快照等于源目录"这类判断会在两个函数之间出现口径差。
    所以指纹一律由索引派生，且只在这里实现一次。
    """
    import hashlib

    h = hashlib.sha1()
    for rel in sorted(index):
        size, mtime_ns = index[rel]
        h.update(rel.replace("\\", "/").encode("utf-8", "replace"))
        h.update(b"\0")
        h.update(f"{size}:{mtime_ns}".encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _fingerprint(root: str) -> str:
    """目录内容指纹：所有文件的 相对路径 + 大小 + mtime_ns 的 sha1。

    用来回答"这一份和那一份是不是同一个进度"。快照与存档在同一个 NTFS 卷上，
    没有落盘的内容不可能变 mtime，所以这个判据足够可靠，而且不用读文件内容。

    **性能**：一次全目录 stat。139 文件实测约 6 ms；拷一份快照要扫好几次，
    所以能复用索引的地方一律复用（见 ``_fingerprint_from_index``）。
    """
    return _fingerprint_from_index(_index_dir(root))


def _index_diff(before: dict[str, tuple[int, int]],
                now: dict[str, tuple[int, int]]) -> list[str]:
    """两个索引之间被改动/新增/删除的文件（相对路径）。纯比较，不碰磁盘。"""
    changed: list[str] = [rel for rel, sig in before.items() if now.get(rel) != sig]
    changed += [rel for rel in now if rel not in before]
    return changed


def _changed_since(root: str, index: dict[str, tuple[int, int]]) -> list[str]:
    """返回自 ``index`` 之后被改动/新增/删除的文件（相对路径）。

    用于确认"拷贝期间源目录有没有被动过"。
    """
    return _index_diff(index, _index_dir(root))


def _try_hardlink(prev_root: Optional[str], rel: str, dst: str) -> bool:
    if not prev_root:
        return False
    src = os.path.join(prev_root, rel)
    if not os.path.isfile(src):
        return False
    try:
        if os.path.exists(dst):
            os.remove(dst)
        os.link(src, dst)
        return True
    except OSError:
        return False


def _copy_into(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        try:
            os.chmod(dst, stat.S_IWRITE)
        except OSError:
            pass
        os.remove(dst)
    shutil.copy2(src, dst)


# --------------------------------------------------------------------------------------
# Windows 平台能力（热键 / 托盘 / 进程 / 窗口）统一来自 winapi 模块
# --------------------------------------------------------------------------------------

from winapi import (  # noqa: E402  (需要在 GAME_EXE 定义后导入)
    HotkeyListener,
    TrayIcon,
    find_window_by_process,
    format_hotkey,
    hotkey_candidates,
    is_process_running,
    kill_processes_named,
    parse_hotkey,
    probe_hotkey,
    press_enter,
    quicksave_game,
    sav_mtime,
    save_and_quit_game,
    single_instance,
)


def find_game_window() -> int:
    """游戏主窗口句柄（0 = 未找到）。"""
    return find_window_by_process(GAME_EXE)


#: CCB 关掉启动提示后立刻会写进 debug.log 的日志行（用来确认回车确实送达）
STARTUP_PROMPT_MARKER = "Executing Lua-first Platform Mod metadata"


def _startup_prompt_gone(log_path: str, since: int = 0) -> bool:
    """日志是否已经越过 CCB 启动提示。

    ``since`` 之后出现模组扫描日志（按键已送达）或 tileset 加载行
    （提示早已关闭）都说明不需要再投回车。
    """
    try:
        with open(log_path, "rb") as fh:
            fh.seek(max(0, int(since)))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return False
    return STARTUP_PROMPT_MARKER in text or "Loaded tileset:" in text


def dismiss_startup_prompt(game_root: str, delay: float = 1.5,
                           attempts: int = 4) -> bool:
    """CCB 启动提示自动回车：等窗口出现 → 稍等 → 投递回车 → 用日志确认。

    要关掉的界面是 CCB 新增的「Lua 模组是可执行程序……」安全提示
    （每次启动都出现，必须按任意键）。实测（2026-11-07，build 2026-11-05-2043）：

    * 窗口出现后约 1 秒画出提示（日志里第二条 ``Setting language`` 之后）；
    * 用 PostMessage 投递 ``VK_RETURN`` 即可关闭，游戏随即开始扫描模组；
    * 扫描开始时会立刻写出 ``Executing Lua-first Platform Mod metadata``，
      这条日志就是"按键真的送达"的硬证据——投递失败/太早时可以据此重试。

    只在工具**自己拉起游戏**时调用；玩家手动开游戏时工具不碰键盘。
    """
    log_path = os.path.join(game_root, "config", "debug.log")
    try:
        start = os.path.getsize(log_path) if os.path.isfile(log_path) else 0
    except OSError:
        start = 0

    deadline = time.time() + 120.0
    hwnd = 0
    while time.time() < deadline:
        hwnd = find_game_window()
        if hwnd:
            break
        time.sleep(0.1)
    if not hwnd:
        logging.warning("启动提示：等不到游戏窗口，跳过自动回车")
        return False

    time.sleep(max(0.0, float(delay)))
    for attempt in range(1, attempts + 1):
        # 提示已被关掉（或本版本没有提示）时绝不再补按键：
        # 多余的 Enter 会落到主菜单/世界里，可能触发不该有的确认。
        if _startup_prompt_gone(log_path, start):
            logging.info("启动提示：启动流程已越过提示界面，无需回车")
            return True
        # 每次重试都重新取句柄：重启时旧窗口可能还没销毁，先抓到的句柄会失效，
        # 一直用它投递等于什么都没发（实测踩过：游戏卡在提示上，工具报启动失败）。
        hwnd = find_game_window()
        if not hwnd:
            logging.info("启动提示：游戏窗口已消失，停止自动回车")
            return False
        try:
            delivered = press_enter(hwnd)
        except Exception:
            logging.warning("启动提示：投递回车失败", exc_info=True)
            return False
        if not delivered:
            logging.info("启动提示：第 %d 次回车没投出去（窗口可能未就绪），重试", attempt)
            time.sleep(0.4)
            continue
        confirm_deadline = time.time() + 6.0
        while time.time() < confirm_deadline:
            if _startup_prompt_gone(log_path, start):
                logging.info("启动提示已按回车关闭（第 %d 次投递）", attempt)
                return True
            time.sleep(0.15)
        logging.info("启动提示：第 %d 次回车后仍未看到模组扫描日志，重试", attempt)
        time.sleep(0.4)
    logging.warning("启动提示：投递了 %d 次回车仍未确认关闭", attempts)
    return False


class GameLoadTracker:
    """跟踪 CDDA 重启后的加载进度，用于"什么时候才真的能玩"。

    本机实测的加载时间线（`--world` 直进）：

    ==================  =========
    阶段                 累计时间
    ==================  =========
    进程起来 / 窗口出现   +0.3s
    渲染器就绪            +0.6s
    静态数据加载完        +9.7s
    真正可玩（近似）      +15.7s
    ==================  =========

    也就是说窗口秒出但**十几秒内不能操作**，玩家需要知道还要等多久。
    这里通过跟随 `config/debug.log` 的阶段标记来回报，并观察到日志停止增长后
    判定为"已就绪"。
    """

    #: (阶段文案, 日志里的标志串)
    PHASES = [
        ("游戏进程已启动", "Starting log."),
        ("渲染器就绪", "Active renderer:"),
        ("正在加载游戏数据…", "Setting language"),
        ("正在加载图形资源…", "Loaded tileset:"),
    ]

    def __init__(self, log_path: str, *, stall_seconds: float = 5.0,
                 timeout: float = 180.0):
        self.log_path = log_path
        self.stall_seconds = stall_seconds
        self.timeout = timeout

    def watch(self, on_phase=None, on_ready=None) -> None:
        t0 = time.perf_counter()
        try:
            start = os.path.getsize(self.log_path) if os.path.isfile(self.log_path) else 0
        except OSError:
            start = 0
        # 进程可能要几百毫秒才创建/写日志，先等一下它的第一条记录
        deadline_first = time.perf_counter() + 15.0
        while time.perf_counter() < deadline_first:
            try:
                if os.path.isfile(self.log_path) and os.path.getsize(self.log_path) != start:
                    break
            except OSError:
                pass
            time.sleep(0.2)

        seen: set[str] = set()
        pos = start
        last_size = start
        last_growth = time.perf_counter()
        deadline = time.perf_counter() + self.timeout
        ready_reason = "超时"

        while time.perf_counter() < deadline:
            time.sleep(0.3)
            try:
                size = os.path.getsize(self.log_path)
            except OSError:
                continue
            if size > pos:
                try:
                    with open(self.log_path, "rb") as fh:
                        fh.seek(pos)
                        chunk = fh.read().decode("utf-8", "replace")
                except OSError:
                    chunk = ""
                pos = size
                for label, needle in self.PHASES:
                    if label in seen or needle not in chunk:
                        continue
                    seen.add(label)
                    if on_phase:
                        self._safe(on_phase, label, time.perf_counter() - t0)
            if size != last_size:
                last_size = size
                last_growth = time.perf_counter()
            elif (time.perf_counter() - last_growth >= self.stall_seconds
                  and "正在加载图形资源…" in seen):
                # 图形资源加载完且日志不再增长 -> 认为已经可玩
                ready_reason = "日志已稳定"
                break

        elapsed = time.perf_counter() - t0
        logging.info("游戏加载跟踪结束：%.1f 秒（%s），阶段=%s",
                     elapsed, ready_reason, sorted(seen))
        if on_ready:
            self._safe(on_ready, elapsed, ready_reason)

    @staticmethod
    def _safe(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            logging.exception("加载进度回调失败")


class GameWatcher(threading.Thread):
    """守望 CDDA 进程：从 Steam / 桌面 / 命令行启动都能被认出来。

    为什么要这样：用户平时是通过 **Steam 非 Steam 快捷方式**启动游戏的，
    那种方式没法在命令行里挂包装器（改 shortcuts.vdf 是二进制格式，风险高）。
    所以反过来做——工具常驻后台盯着进程，游戏一起来它就已经就绪，
    效果等同于"和游戏一起启动"，而且不依赖任何启动方式。

    状态变化时回调：
      ``on_launch(detect_seconds)``  游戏进程出现
      ``on_ready(load_seconds)``     游戏加载完成（可选，需要读 debug.log）
      ``on_exit(run_seconds)``       游戏进程消失
    """

    def __init__(self, game_root: str, exe_name: str, config: Config, *,
                 interval: float = 2.5):
        super().__init__(daemon=True, name="cdda-game-watch")
        self.game_root = game_root
        self.exe_name = exe_name
        self.config = config
        self.interval = interval
        self._stop = threading.Event()
        #: 每次轮询都会调一次（不改变进程状态也调），用来做"Steam 接管"这类旁路检查
        self._on_tick = None
        #: 刚做完还原时置为真，避免紧接着又自动拍一张重复的快照
        self.suppress_auto_snapshot_until = 0.0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # pragma: no cover - 线程体
        was_running = is_process_running(self.exe_name)
        started_at: Optional[float] = None
        if was_running:
            started_at = time.time()
            logging.info("守望启动时游戏已在运行")
        while not self._stop.is_set():
            time.sleep(self.interval)
            if self._stop.is_set():
                break
            if self._on_tick:
                try:
                    self._on_tick()
                except Exception:
                    logging.exception("守望 tick 回调失败")
                if self._stop.is_set():
                    break
            running = is_process_running(self.exe_name)

            if running and not was_running:
                started_at = time.time()
                logging.info("检测到游戏已启动")
                self._emit("on_launch")
            elif not running and was_running:
                dur = time.time() - (started_at or time.time())
                started_at = None
                logging.info("检测到游戏已退出（运行了 %.1f 秒）", dur)
                self._emit("on_exit", dur)
            was_running = running

    # 子类/回调由 app 注入
    def bind(self, on_launch=None, on_ready=None, on_exit=None, on_tick=None) -> None:
        self._on_launch = on_launch
        self._on_ready = on_ready
        self._on_exit = on_exit
        self._on_tick = on_tick

    def _emit(self, name: str, *args) -> None:
        fn = getattr(self, f"_{name}", None)
        if fn:
            try:
                fn(*args)
            except Exception:
                logging.exception("守望回调 %s 失败", name)


def wait_for_game_loaded(game_root: str, exe_name: str, config: Config, *,
                         timeout: float = 180.0,
                         already_running: bool = False) -> Optional[float]:
    """等到"游戏真的可玩"，返回耗时秒数。

    判据：窗口出现 + ``config/debug.log`` 里出现新的 ``Loaded tileset:``。
    这比"进程存在"准得多——进程起来 0.3 秒就有窗口，但十几秒内还不能操作。
    """
    log_path = os.path.join(game_root, "config", "debug.log")
    t0 = time.time()
    try:
        start_size = os.path.getsize(log_path) if os.path.isfile(log_path) else 0
    except OSError:
        start_size = 0

    window_seen = False
    tileset_seen = False
    deadline = t0 + timeout
    while time.time() < deadline:
        if not is_process_running(exe_name):
            return None                      # 游戏没了，别等了
        if not window_seen and find_game_window():
            window_seen = True
        try:
            if os.path.isfile(log_path) and os.path.getsize(log_path) > start_size:
                with open(log_path, "rb") as fh:
                    fh.seek(start_size)
                    chunk = fh.read().decode("utf-8", "replace")
                start_size = os.path.getsize(log_path)
                if "Loaded tileset:" in chunk:
                    tileset_seen = True
        except OSError:
            pass
        if window_seen and tileset_seen:
            # 给一小段缓冲，让世界真正进入可交互状态
            time.sleep(float(config["load_ready_stall_seconds"]))
            return time.time() - t0
        time.sleep(0.3)
    return None


@dataclass
class SaveOutcome:
    """一次"让游戏存盘"的结果。"""

    saved: bool = False
    #: "quicksave"（ESC→e，游戏留在世界）/ "save_quit"（Shift+S→Y，退回主菜单）/ None
    method: Optional[str] = None
    #: 已发出快速保存、但游戏没有覆写存档。按 CDDA 的防冗余机制，
    #: 这通常意味着"当前状态与磁盘上的一致，没什么可存"。
    #: **注意**：它只说明"这一次没有新写入"，**不代表磁盘内容与最新快照一致**——
    #: 玩家可能几秒前刚手动存过档，磁盘比快照新。判断"有没有新东西要拍"必须
    #: 拿磁盘内容去和最新快照比指纹（find_world_duplicate / create(skip_if_identical)）。
    no_change: bool = False

    @property
    def needs_relaunch(self) -> bool:
        """只有「保存并退出」那条路需要事后重启游戏。"""
        return self.saved and self.method == "save_quit"

    @property
    def disk_is_current(self) -> bool:
        """磁盘上的存档是否就是"按下快照那一刻"的进度。

        快速保存没写盘时成立——游戏判定无改动才不覆写，说明磁盘不落后于当前状态。
        **但它不表示磁盘 == 最新快照**（见 :attr:`no_change` 的说明）。
        """
        return self.saved or self.no_change


def trigger_game_save(hwnd: int, world_path: str, config: Config,
                      progress=None) -> SaveOutcome:
    """让游戏存一次档，并确认磁盘状态。

    只走「快速保存」（ESC → 小写 e，见 :func:`winapi.quicksave_game`）：
    它**不退出游戏世界**，实测约 0.3 秒。

    **关于"没写盘"**：CDDA 有防冗余机制——当前状态与磁盘一致时，
    快速保存会跳过写盘（游戏里会显示"正在存档，可能需要点时间。"但不覆写文件）。
    所以 ``.sav`` 的 mtime 没变**不等于按键失败**，只说明没有新东西可存。
    但这**只对"那一刻"成立**：它不保证磁盘内容等于最新快照（玩家可能刚手动存过档），
    所以调用方不能据此跳过拍快照，必须继续按内容指纹去重。
    """
    before = sav_mtime(world_path)
    if not hwnd:
        return SaveOutcome(False, None)

    if progress:
        progress("正在快速保存（ESC → e）…")
    delay = float(config["quicksave_menu_delay"] or 0.0)
    TRACE.mark("key")
    quicksave_game(hwnd, menu_delay=delay)
    if _wait_for_world_write(world_path, before, config, progress):
        # 这里**不**再自己等目录安静：那件事已经收拢到 SnapshotManager.create()
        # 里统一做了（校验 0）。分两处做会变成"拷贝前等一遍、拷贝后又等一遍"，
        # 实测白等 1.5 秒（1.61s + 1.53s 里多出来的那一份）。
        # 这里只需要知道"游戏确实写了盘"，剩下的交给 create() 的一致性校验。
        TRACE.mark("saved")
        return SaveOutcome(True, "quicksave")

    # 没写盘：99% 是"无改动可存"。只有当游戏已经不在运行时才算真失败，
    # 那种情况才值得退回「保存并退出」。
    if not is_process_running(GAME_EXE):
        logging.info("快速保存后游戏已不在运行，回退到「保存并退出」方式")
        if progress:
            progress("游戏似乎已退出，改用「保存并退出」…")
        save_and_quit_game(hwnd, world_path, focus=False)
        wrote = _wait_for_world_write(world_path, before, config, progress)
        return SaveOutcome(wrote, "save_quit")

    logging.info("快速保存未覆写存档（游戏判定无改动）：磁盘上没有新写入，"
                 "改按磁盘内容与最新快照比对")
    if progress:
        progress("游戏判定没有改动可存，按磁盘现状核对快照…")
    return SaveOutcome(False, "quicksave", no_change=True)


def _wait_for_world_write(world_path: str, before: float, config: Config,
                          progress=None) -> bool:
    """等到存档目录的 .sav 被写入；期间持续回报等待时长。

    **自适应轮询**：刚触发存档后是"马上就会有动静"的阶段，用 50 ms 细粒度探；
    过了 `quicksave_fast_poll_seconds` 还没动静，说明游戏慢（大世界写盘）或这次
    真的没东西可存，再密探就是白烧 CPU（每次探测要扫目录）。所以逐步放宽到
    100 ms / 200 ms。

    实测收益：游戏在 e 之后 30~120 ms 写完 .sav 时，固定 200 ms 轮询平均要多等
    50~150 ms；改成 50 ms 后基本等于"写盘一完成就发现"。
    """
    deadline = time.time() + float(config["quicksave_timeout"])
    started = time.time()
    fast_until = started + max(0.5, float(config["quicksave_fast_poll_seconds"] or 0.0))
    while time.time() < deadline:
        now = time.time()
        if now < fast_until:
            time.sleep(0.05)
        elif now < fast_until + 2.0:
            time.sleep(0.1)
        else:
            time.sleep(0.2)
        if sav_mtime(world_path) != before:
            if progress:
                progress(f"存档已写入磁盘（{(time.time() - started) * 1000:.0f} 毫秒）")
            return True
        waited = time.time() - started
        if progress and waited >= 1.0:
            progress(f"等待游戏写盘…（{waited:.0f} 秒）")
    return False


# --------------------------------------------------------------------------------------
# 主控制器（热键 + 无常驻窗口面板）
# --------------------------------------------------------------------------------------


class SnapshotApp:
    """无常驻窗口：平时只挂着全局热键，按键才弹出面板/气泡。"""

    def __init__(self, game_root: str, *, steam_launch: bool = False):
        self.game_root = game_root
        #: True = 本次是被 Steam 亲手拉起的（快捷方式目标指向本工具）。
        #: 这时工具要自己启动游戏，并在游戏正常退出时一并退出，
        #: 让 Steam 的"正在运行"锚点与"游戏在不在"保持一致。
        self.steam_launch = bool(steam_launch)
        self.config = Config(os.path.join(snapshot_root(game_root), "config.json"))
        self.manager = SnapshotManager(game_root, self.config)
        self.root = None  # type: ignore[assignment]
        self.panel = None  # type: ignore[assignment]
        self.toast = None  # type: ignore[assignment]
        self.hotkeys: Optional[HotkeyListener] = None
        self.tray: Optional[TrayIcon] = None
        self.watcher: Optional[GameWatcher] = None
        #: 忙碌时排队的快照请求 (note, kind)，做完当前操作后自动补上
        self._pending_snapshot: Optional[tuple] = None
        #: 上一次存盘是否走了「保存并退出」（那条路需要事后重启游戏读回）
        self._last_save_needs_relaunch = False
        #: 到此刻为止"游戏进程消失"不算玩家退出游戏（读档重启的中断窗口）。
        #: 只在 --steam-launch 下有意义：那里 exit_with_game 是强制的，
        #: 不挡住这个窗口就会在重启游戏的中途把工具自己杀掉。
        self._suppress_exit_until = 0.0
        #: 待办：等游戏加载完成后自动收起面板（读取快照后置为 True）
        self.hide_panel_when_ready = False
        self.mutex: Any = None
        self.busy = False
        self.world: Optional[str] = None
        self._hide_job: Optional[str] = None
        self._status = ""
        #: 工作线程 → Tk 主线程的回调队列。**绝不能在非主线程直接调 Tk**：
        #: threaded Tcl 下 root.after() 会阻塞等主线程，而主线程此时可能正等
        #: SnapshotManager._lock（快照 worker 全程持锁），双向等待就是死锁
        #: （2026-10-24 实测：F5 快照进行中按 F4 打开面板，整个工具冻死）。
        self._post_queue: "queue.Queue[Any]" = queue.Queue()

    # -------------------------------------------------- 启动

    def setup_logging(self) -> None:
        os.makedirs(self.manager.root, exist_ok=True)
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        # CLI 模式可能已经用 basicConfig 装过同一个文件 handler，避免日志重复
        target = os.path.abspath(self.manager.log_path)
        for existing in root.handlers:
            if isinstance(existing, logging.FileHandler):
                if os.path.abspath(getattr(existing, "baseFilename", "")) == target:
                    return
        handler = logging.FileHandler(self.manager.log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(handler)

    def run(self, *, selftest_seconds: float = 0.0, selftest_hotkey_dialog: bool = False,
            selftest_longstatus: bool = False) -> int:
        # 让面板模块能拿到本模块对象，避免 .pyw 以 __main__ 身份运行时被重复导入
        sys.modules.setdefault("cdda_snapshot", sys.modules["__main__"])
        from panel import QuickPanel

        self.setup_logging()
        logging.info("%s v%s 启动，游戏目录 %s", APP_NAME, APP_VERSION, self.game_root)

        for message in self.manager.recover_if_needed():
            logging.warning("自愈: %s", message)

        self.panel = QuickPanel(self)
        self.root = self.panel.root

        # 主线程泵：工作线程的回调只走队列，不跨线程调 Tk（见 post 的说明）
        self.root.after(16, self._pump_post_queue)

        self.hotkeys = HotkeyListener(
            {k: v for k, v in self.config["hotkeys"].items()},
            post=lambda action: self.post(lambda a=action: self.on_hotkey(a)),
        )
        self.hotkeys.start()
        self.hotkeys.wait_ready()
        for err in self.hotkeys.errors:
            logging.warning("热键: %s", err)
            self.notify(f"热键不可用：{err}", error=True)
        # 把实际生效的键位交给面板显示（可能因为冲突而换成了备用键）
        self.panel.apply_hotkeys(self.hotkeys)
        for action, (wanted, actual) in self.hotkeys.fallbacks.items():
            self.notify(
                f"{format_hotkey(wanted)} 被占用，已自动改用 {format_hotkey(actual)}",
                timeout=6.0,
            )

        self.notify(
            f"已就绪　{self.hotkeys.label('toggle_panel', 'F4')} 打开面板",
            timeout=4.0,
        )

        if self.config["tray_icon"]:
            self.start_tray()

        if self.config["autostart_with_game"] or self.config["auto_snapshot_on_launch"] \
                or self.config["exit_with_game"] or self.steam_launch:
            self.start_game_watcher()

        if self.steam_launch:
            # Steam 亲手拉起的进程必须自己把游戏带起来，否则 Steam 库里就只是
            # "工具在跑"，游戏反而要玩家手动开。
            self._log_steam_context()
            self.root.after(600, self.launch_game_from_steam)

        if selftest_seconds > 0:
            self.root.after(200, self.show_panel)
            if selftest_hotkey_dialog:
                self.root.after(1200, lambda: self.panel.open_hotkey_settings())
            if selftest_longstatus:
                self.root.after(
                    1500,
                    lambda: self.say(
                        "这是一条很长的状态文字，用来验证面板高度会跟着内容变高，"
                        "而不会把最后几行裁掉。快照 #001 完成：89 个文件 / 2.08 MB，"
                        "硬链接复用 74 个文件，用时 4.5 秒。如果这段文字完整可见，"
                        "说明自适应生效。"
                    ),
                )
            self.root.after(int(selftest_seconds * 1000), self.quit)

        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            if self.hotkeys:
                self.hotkeys.stop()
            if self.tray:
                self.tray.stop()
            if self.watcher:
                self.watcher.stop()
        return 0

    # -------------------------------------------------- Steam 包装模式

    def _log_steam_context(self) -> None:
        """把 Steam 注入的环境变量记进日志，方便排查"到底有没有被 Steam 认领"。"""
        keys = ("SteamAppId", "SteamGameId", "SteamClientLaunch", "SteamEnv",
                "SteamPath", "SteamOverlayGameId")
        found = {k: os.environ.get(k) for k in keys if os.environ.get(k)}
        logging.info("Steam 包装模式启动：%s", found or "（没有 Steam 环境变量）")

    def launch_game_from_steam(self) -> None:
        """Steam 拉起本工具后，由工具把游戏带起来。

        **为什么要多这一层**：Steam 对非 Steam 快捷方式只有"它亲手 CreateProcess
        的那个进程"这一个抓手，进程一没了就停止计时；而读档必须杀掉游戏再重启。
        让 Steam 拉起工具、工具再拉起游戏，重启时只换子进程，Steam 的抓手一直在。

        **参数怎么带**：游戏由我们 Popen，所以 ``--world`` 照样能传 —— 这正是
        "直接 ``steam://rungameid`` 重启"做不到的部分（那种方式传不了参数）。
        """
        if is_process_running(GAME_EXE):
            logging.info("Steam 包装模式：游戏已在运行，不重复启动")
            return
        exe = os.path.join(self.game_root, GAME_EXE)
        if not os.path.isfile(exe):
            self.notify("找不到 cataclysm-tiles.exe，无法启动游戏。", error=True)
            return
        world = self.current_world() if self.config["steam_launch_into_world"] else None
        if not world:
            logging.info("Steam 包装模式：没有可用世界，按原样启动（停在主菜单）")
            try:
                subprocess.Popen([exe], cwd=self.game_root, close_fds=True)
            except Exception:
                logging.exception("Steam 包装模式：启动游戏失败")
                self.notify("启动游戏失败，请手动打开游戏。", error=True)
                return
            self._dismiss_startup_prompt_soon()
            return
        logging.info("Steam 包装模式：启动游戏并直接进入世界「%s」", world)
        self._hold_game_exit(30.0)
        if self.relaunch_game(
            world,
            on_phase=lambda label, secs: self.say_async(f"{label}（{secs:.1f}s）", busy=True),
            on_ready=self._on_game_ready,
        ):
            self.hide_panel_when_ready = True
        else:
            self.notify("启动游戏失败，请手动打开游戏。", error=True)

    def _hold_game_exit(self, seconds: float) -> None:
        """在 ``seconds`` 秒内，忽略"游戏进程消失"。"""
        self._suppress_exit_until = max(self._suppress_exit_until, time.time() + seconds)

    # -------------------------------------------------- 与游戏同时启动

    def start_game_watcher(self) -> None:
        """守望游戏进程：从 Steam 启动也能被认出来。

        配合「开机自启 + autostart_with_game」，效果就是"工具永远和游戏在一起"，
        而且不依赖任何启动方式（Steam 非 Steam 快捷方式、桌面图标、命令行都行）。
        """
        self.watcher = GameWatcher(self.game_root, GAME_EXE, self.config)
        self.watcher.bind(
            on_launch=self._on_game_launch,
            on_exit=self._on_game_exit,
            on_tick=self._check_steam_takeover,
        )
        self.watcher.start()
        logging.info("已启动游戏守望（间隔 %.1f 秒）", self.watcher.interval)

    def _check_steam_takeover(self) -> None:
        """（守望线程）Steam 想接管时，让常驻的这个实例退位。"""
        if self.steam_launch:
            return                                  # 自己就是 Steam 那个，不用让
        if not steam_takeover_pending(snapshot_root(self.game_root)):
            return
        self.post(self._yield_to_steam)

    def _yield_to_steam(self) -> None:
        """（Tk 线程）安静退出，把单实例位置让给 Steam 亲手拉起的进程。"""
        if self.steam_launch:
            return
        if not steam_takeover_pending(snapshot_root(self.game_root)):
            return
        if self.busy:
            # 正在拍快照/还原，这一轮先不动；标记还在，下一轮会再来
            logging.info("Steam 接管请求：正在忙（快照/还原），等这轮做完再让位")
            return
        clear_steam_takeover(snapshot_root(self.game_root))
        logging.info("Steam 启动接管：本实例让位退出（游戏进程不动，由 Steam 那个实例接管）")
        self.quit()

    def _on_game_launch(self) -> None:
        """游戏进程刚出现。"""
        self.post(lambda: self.say("检测到 CDDA 已启动…", busy=False))
        if not self.config["auto_snapshot_on_launch"]:
            return
        if time.time() < self.watcher.suppress_auto_snapshot_until:
            logging.info("刚做过还原，跳过本次自动快照")
            return
        threading.Thread(
            target=self._auto_snapshot_after_load, daemon=True,
            name="auto-snapshot",
        ).start()

    def _auto_snapshot_after_load(self) -> None:
        """等游戏真的可玩之后，自动拍一张"出发档"。"""
        elapsed = wait_for_game_loaded(
            self.game_root, GAME_EXE, self.config,
            timeout=float(self.config["quicksave_timeout"]) + 170.0,
            already_running=True,
        )
        if elapsed is None:
            logging.info("自动快照：没有等到游戏加载完成，放弃")
            return
        logging.info("自动快照：游戏就绪于 %.1f 秒，开始拍出发档", elapsed)
        self.post(lambda: self.notify(
            f"游戏已就绪（{elapsed:.0f} 秒），正在拍出发档…", timeout=3.0))
        # 让 do_snapshot 走正常流程（它自己会先存档再打包）
        self.post(lambda: self.request_snapshot(note="自动·出发档", kind="auto"))

    def _on_game_exit(self, run_seconds: float) -> None:
        """游戏退出。"""
        if time.time() < self._suppress_exit_until:
            # 读档重启的中断窗口：这不是"玩家关了游戏"，工具必须活着，
            # 否则 Steam 的锚点断掉（而且新游戏还没起来就没人再拉起它了）。
            logging.info("游戏进程短暂消失（读档重启中），忽略退出回调")
            return
        steam_exit = self.steam_launch and self.config["steam_exit_with_game"]
        if not (self.config["exit_with_game"] or steam_exit):
            self.post(lambda: self.say("游戏已退出，工具继续待命。", busy=False))
            return
        logging.info("游戏已退出，%s一并退出工具",
                     "Steam 包装模式下" if steam_exit else "按配置")
        self.post(self.quit)

    # -------------------------------------------------- 托盘图标

    TRAY_CMD_PANEL = 0xE001
    TRAY_CMD_SNAPSHOT = 0xE002
    TRAY_CMD_SNAPSHOT_DISK = 0xE007
    TRAY_CMD_RESTORE = 0xE003
    TRAY_CMD_FOLDER = 0xE004
    TRAY_CMD_HOTKEYS = 0xE005
    TRAY_CMD_RELOAD = 0xE006
    TRAY_CMD_QUIT = 0xE0FF

    def start_tray(self) -> bool:
        """托盘图标：热键全被占用时也能打开面板。"""
        items = [
            (self.TRAY_CMD_PANEL, "打开面板"),
            (self.TRAY_CMD_SNAPSHOT, "拍快照（存盘并自动读回）"),
            (self.TRAY_CMD_SNAPSHOT_DISK, "只存磁盘快照（不打扰游戏）"),
            (self.TRAY_CMD_RESTORE, "回到最新快照"),
            (self.TRAY_CMD_FOLDER, "打开快照目录"),
            (self.TRAY_CMD_HOTKEYS, "热键设置…"),
            (self.TRAY_CMD_QUIT, "退出工具"),
        ]
        icon_path = os.path.join(self.game_root, "data", "cataicon.ico")
        self.tray = TrayIcon(
            f"{APP_NAME}（{self.hotkeys.label('toggle_panel', 'F4')} 打开面板）",
            items,
            on_command=lambda cmd: self.post(lambda c=cmd: self.on_tray_command(c)),
            on_left_click=lambda: self.post(self.toggle_panel),
            icon_path=icon_path if os.path.isfile(icon_path) else None,
        )
        if self.tray.start():
            return True
        logging.warning("托盘图标启动失败: %s", self.tray.last_error)
        self.tray = None
        return False

    def on_tray_command(self, cmd: int) -> None:
        logging.info("托盘命令: %s", cmd)
        if cmd == self.TRAY_CMD_PANEL:
            self.toggle_panel()
        elif cmd == self.TRAY_CMD_SNAPSHOT:
            self.do_snapshot()
        elif cmd == self.TRAY_CMD_SNAPSHOT_DISK:
            self.do_snapshot(mode="disk", note="磁盘快照")
        elif cmd == self.TRAY_CMD_RESTORE:
            self.do_restore_latest()
        elif cmd == self.TRAY_CMD_FOLDER:
            if self.panel:
                self.panel.open_snapshot_dir()
        elif cmd == self.TRAY_CMD_HOTKEYS:
            if self.panel:
                self.panel.open_hotkey_settings()
        elif cmd == self.TRAY_CMD_RELOAD:
            self.reload_hotkeys()
        elif cmd == self.TRAY_CMD_QUIT:
            self.quit()

    def reload_hotkeys(self) -> None:
        """重新读取配置并重启工具（热键改动后立即生效）。"""
        self.notify("正在按新配置重启工具…", timeout=3.0)
        self.post(lambda: self.restart_tool())

    def restart_tool(self) -> None:
        """重启工具自身（热键改动后立即生效）。

        直接拉起新进程会撞上单实例互斥体，所以先起新进程并让它带 ``--wait-mutex``
        自己重试，本进程随即退出，把互斥体让出去。
        """
        script = os.path.abspath(__file__)
        launcher = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.isfile(launcher):
            launcher = sys.executable
        try:
            subprocess.Popen(
                [launcher, script, "--wait-mutex", "--root", self.game_root],
                cwd=self.game_root,
                close_fds=True,
            )
        except Exception:
            logging.exception("重启工具失败")
            return
        self.quit()

    # -------------------------------------------------- 状态提示

    def post(self, fn) -> None:
        """从工作线程安全地回到 Tk 主线程执行。

        只入队、绝不接触 Tk：threaded Tcl 里 ``root.after()`` 的跨线程调用会
        **阻塞**等待主线程处理，而主线程可能在等 ``SnapshotManager._lock``
        （快照 worker 全程持锁）——两者互等就是死锁。由主线程上的
        :meth:`_pump_post_queue` 定时取出执行。
        """
        if self.root is not None:
            try:
                self._post_queue.put(fn)
                return
            except Exception:
                pass
        try:
            fn()
        except Exception:
            logging.exception("主线程回调失败")

    def _pump_post_queue(self) -> None:
        """主线程定时泵：执行工作线程投递过来的回调（16 ms 一轮）。"""
        while True:
            try:
                fn = self._post_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                logging.exception("主线程回调失败")
        if self.root is None:
            return
        try:
            self.root.after(16, self._pump_post_queue)
        except Exception:
            pass  # 窗口已销毁，泵自然停止

    def notify(self, message: str, *, error: bool = False, timeout: float = 3.0) -> None:
        logging.info("提示: %s", message)
        self._status = message
        if self.panel:
            self.say(message, error=error)
        self.show_toast(message, error=error, timeout=timeout)

    def say(self, message: str, *, busy: bool = False, error: bool = False) -> None:
        """更新面板状态（面板不存在时安静忽略，方便 CLI/测试路径复用同一套逻辑）。"""
        if self.panel:
            self.panel.set_status(message, busy=busy, error=error)

    def say_async(self, message: str, *, busy: bool = False, error: bool = False) -> None:
        """从工作线程更新面板状态。"""
        self.post(lambda: self.say(message, busy=busy, error=error))

    def show_toast(self, message: str, *, error: bool = False, timeout: float = 3.0) -> None:
        if not self.config["toast_enabled"] or not self.panel:
            return
        try:
            self.panel.toast(message, error=error, timeout=timeout)
        except Exception:
            logging.exception("气泡显示失败")

    # -------------------------------------------------- 热键派发

    def on_hotkey(self, action: str) -> None:
        logging.info("热键触发: %s", action)
        # 热键路径一律 interactive=False：不弹面板，不打断游戏。
        if action == "snapshot":
            # F5：快速保存 + 快照（游戏留在世界里）
            # 从**热键线程刚收到按键**这一刻开始计时，这样日志里的
            # "按下 F5 → 开始写入"包含热键投递到 Tk 主线程的那一段。
            TRACE.start()
            self.do_snapshot(interactive=False)
        elif action == "snapshot_disk":
            # Ctrl+F5：只打包磁盘上已有的存档，不碰游戏
            TRACE.start()
            self.do_snapshot(interactive=False, mode="disk", note="磁盘快照")
        elif action == "restore_latest":
            self.do_restore_latest()
        elif action == "toggle_panel":
            self.toggle_panel()
        elif action == "exit_tool":
            self.quit()

    def quit(self) -> None:
        if self.root:
            self.root.quit()
            self.root.destroy()

    # -------------------------------------------------- 世界识别

    def current_world(self) -> Optional[str]:
        """当前世界：优先用最后修改的存档目录，其次读 config/lastworld.json。"""
        best: Optional[str] = None
        best_mtime = -1.0
        for info in self.manager.list_worlds():
            try:
                mtime = os.path.getmtime(os.path.join(info.path, "save_timestamp.json"))
            except OSError:
                try:
                    mtime = os.path.getmtime(info.path)
                except OSError:
                    continue
            if mtime > best_mtime:
                best_mtime = mtime
                best = info.name
        if best:
            return best
        last = os.path.join(self.game_root, "config", "lastworld.json")
        if os.path.isfile(last):
            try:
                with open(last, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                name = data.get("world_name")
                if name and os.path.isdir(os.path.join(self.manager.save_dir, name)):
                    return name
            except Exception:
                pass
        return None

    # -------------------------------------------------- 操作：快照

    def request_snapshot(self, note: str = "", kind: str = "manual",
                         force: bool = False) -> None:
        """请求拍快照；如果正忙就排队，做完当前操作自动补上。

        自动快照（游戏加载完成时触发）可能正好撞上进行中的操作，
        排队比直接丢弃更符合预期。
        """
        if self.busy:
            logging.info("操作进行中，快照请求已排队（%s）", note or kind)
            self._pending_snapshot = (note, kind, force)
            self.say("已排队一张快照，等当前操作完成后自动执行…", busy=True)
            return
        self.do_snapshot(note=note, kind=kind, force=force)

    def _run_pending_snapshot(self) -> None:
        if not self._pending_snapshot:
            return
        pending = self._pending_snapshot
        self._pending_snapshot = None
        if len(pending) == 3:
            note, kind, force = pending
        else:                                   # 兼容旧的两元组
            note, kind = pending
            force = False
        logging.info("执行排队的快照（%s）", note or kind)
        self.post(lambda: self.do_snapshot(note=note, kind=kind, force=force))

    def do_snapshot(self, *, interactive: bool = True,
                    note: str = "", kind: str = "manual",
                    force: bool = False, mode: Optional[str] = None) -> None:
        if self.busy:
            self.notify("上一个操作还没结束，请稍候…", error=True)
            return
        # 面板/CLI 触发的路径没有热键起点，就从这里开始计时（热键路径已 start 过）。
        TRACE.start_if_idle()
        world = self.current_world()
        if not world:
            self.notify("没找到任何存档世界，先在游戏里存一次档吧。", error=True)
            return
        self.busy = True
        # 热键触发（interactive=False）绝不弹面板，避免打断游戏；
        # 面板按钮触发时面板本来就开着，也不需要再弹。
        if interactive:
            self.show_panel_if_hidden()
        self.say(f"正在为「{world}」创建快照…", busy=True)

        # 取档方式在进入 worker 之前定下来：worker 内部不能再给 mode 赋值，
        # 否则 Python 会把它当 worker 的局部变量，导致 UnboundLocalError。
        if mode is None:
            mode = "fresh" if force else str(
                self.config["snapshot_save_mode"] or "fresh").lower()
        mode = mode.lower()

        def worker() -> None:
            try:
                world_path = os.path.join(self.manager.save_dir, world)
                saved = False
                no_change = False
                # 窗口枚举只做一次：这里和 _fresh_save 里各查一遍纯属重复
                # （每次约 1.8 ms，都在"按下 F5 到开始写入"这段里）
                hwnd = find_game_window()
                game_was_running = bool(hwnd)

                # fresh 模式（拍前先让游戏存档）是否启用，看配置。
                # 关掉它就等价于"只拍磁盘现状"，和 disk 模式同一条路径。
                # 注意：这里只能**读** mode，不能在 worker 里给 mode 赋值——
                # 那会让 Python 把它当 worker 的局部变量，直接 UnboundLocalError。
                fresh = (mode == "fresh") and bool(self.config["quicksave_before_snapshot"])

                if fresh:
                    if game_was_running:
                        self.say_async("正在快速保存（ESC → e）…", busy=True)
                    outcome = self._fresh_save(world_path, hwnd)
                    saved = outcome.saved
                    no_change = outcome.no_change
                    if no_change:
                        # 「游戏没写盘」只说明这一刻没有新东西可存，**不等于**磁盘内容
                        # 与最新快照一致——玩家可能几秒前刚手动存过档，磁盘比最新快照
                        # 新得多（2026-11-05 16:30 就是这么丢了 37 分钟的进度：
                        # 16:30:49 游戏存了档，16:30:54 按 F5 被判"无改动"，
                        # 于是直接 return，什么都没拍，18:01 还原 #523 就回退了）。
                        # 所以这里不能提前返回，交给下面的按内容去重逻辑判定：
                        # 与最新快照相同就复用（不占空间），不同就新建。
                        logging.info("快速保存未覆写（游戏判定无改动）：改按磁盘现状核对内容，"
                                     "与最新快照不同仍会新建快照")
                        self.post(lambda: self.say(
                            "游戏没写盘，按磁盘现状核对快照…", busy=True))
                    if not saved and not no_change and game_was_running:
                        self.post(
                            lambda: self.notify(
                                "没能让游戏存档，改按当前磁盘状态快照。"
                                "（先按 Esc 回到角色视角、退出背包/菜单，再试一次）",
                                error=True, timeout=6.0,
                            )
                        )
                else:
                    # disk 模式：完全不打扰游戏。但要让用户知道磁盘上的存档有多新。
                    age = self.manager.save_age(world)
                    if game_was_running and age is not None and age > 120:
                        self.post(lambda a=age: self.notify(
                            f"快照用的是磁盘存档（{a / 60:.0f} 分钟前保存的）。"
                            f"想连当前进度一起留档，用 "
                            f"{self.hotkeys.label('snapshot', 'F5') if self.hotkeys else 'F5'}"
                            f"（会自动重开游戏并读回）。",
                            timeout=8.0,
                        ))

                # 只有"没发生新存盘"时才允许用去重跳过
                if not force and not saved:
                    dup = self.manager.find_world_duplicate(world)
                    if dup is not None:
                        logging.info("磁盘内容与快照 %s 相同，跳过创建", dup.label())
                        self.post(lambda d=dup: self.after_duplicate(d))
                        return

                snap = self.manager.create(
                    world,
                    note=note,
                    kind=kind,
                    force=force,
                    # 真的存过盘 -> 那份落盘内容就是玩家的一次操作，必须留档
                    skip_if_identical=not saved,
                    progress=lambda msg: self.post(lambda m=msg: self.say(m, busy=True)),
                )
                consistent = self.manager.last_snapshot_consistent
                self.post(lambda s=snap: self.after_snapshot(s))
                if not consistent:
                    # 只有把 allow_inconsistent_snapshot 打开才会走到这里
                    # （默认是丢弃并报错，见 SnapshotInconsistent）。
                    self.post(lambda s=snap: self.notify(
                        f"{s.label()} 可能不一致：拍摄时游戏一直在写盘。"
                        f"建议等游戏存盘提示消失后再拍一份，这份不要当作可靠档点。",
                        error=True, timeout=10.0,
                    ))

                # 「保存并退出」会让游戏停在主菜单，需要重新拉起来读回；
                # 快速保存成功时游戏仍在世界里，什么都不用做。
                if mode == "fresh" and self._last_save_needs_relaunch:
                    if self.config["relaunch_after_fresh_snapshot"]:
                        self.post(lambda: self._relaunch_after_fresh(world))
                    else:
                        logging.info("relaunch_after_fresh_snapshot=false，不自动重启"
                                     "（游戏停在主菜单，需要你自己进去）")
                        self.post(lambda: self.notify(
                            "存档已完成、快照已保存；游戏停在主菜单（配置里关掉了自动重启）。",
                            timeout=6.0,
                        ))
            except SnapshotInconsistent as exc:
                # 一致性校验没过：本次快照已经被丢掉，没有产生任何档点。
                # 这和"快照失败"要分开说——坏档点才是读档后地图时间回溯的入口。
                logging.warning("快照因一致性校验未通过被丢弃: %s", exc)
                self.post(lambda e=exc: self.after_inconsistent(e))
            except Exception as exc:
                logging.exception("快照失败")
                self.post(lambda e=exc: self.after_error("快照失败", e))
            finally:
                self.busy = False

        threading.Thread(target=worker, daemon=True, name="snapshot-worker").start()

    def _fresh_save(self, world_path: str, hwnd: int = 0) -> SaveOutcome:
        """让游戏快速保存一次，返回是否确实写盘成功。

        实测事实（2026-09-19 版本，逐条用截图 + 存档 mtime 核对）：

        * 游戏里按 ESC 弹出的是**浮层菜单**，其中「快速保存」= 小写 ``e``，
          「保存并退出」= 大写 ``S``；
        * **ESC 和 e 都必须用 PostMessage 投递**——SendInput 送不到 ESC，
          而 e 用 SendInput 会被当大写、只会关掉菜单；
        * 快速保存**不退出游戏世界**，实测约 0.3 秒，不需要任何重启；
        * 游戏**不会**在游玩中自动存档（站着不动 180 秒，存档目录零写入），
          所以这一步是拿到最新进度的唯一途径。

        因此这里既不需要等进程退出，也不需要重新启动游戏。

        **返回值必须是 :class:`SaveOutcome`，任何分支都不能返回裸 bool。**
        这里踩过：游戏没运行时原本写的是 ``return False``，而调用方按
        ``outcome.saved`` 取值，于是"游戏没开就按 F5"直接
        ``AttributeError: 'bool' object has no attribute 'saved'``，
        界面只显示一句「快照失败」（2026-09-29 13:37:57 日志里就是这条）。
        """
        # hwnd 由调用方传入（do_snapshot 已经查过窗口了，别重复枚举）
        if not hwnd:
            hwnd = find_game_window()
        if not hwnd:
            logging.info("游戏没在运行，无法快速保存；将按磁盘状态快照")
            self._last_save_needs_relaunch = False
            return SaveOutcome(False, None)
        self.say_async("正在快速保存（ESC → e）…", busy=True)
        outcome = trigger_game_save(
            hwnd, world_path, self.config,
            progress=lambda msg: self.post(lambda m=msg: self.say(m, busy=True)),
        )
        self._last_save_needs_relaunch = outcome.needs_relaunch
        return outcome

    def _relaunch_after_fresh(self, world: str) -> None:
        """回退路径专用：走的是「保存并退出」，游戏停在主菜单，需要重新拉起读回。

        用 ``--world`` 启动会**直接进入该世界**（实测确认），不需要任何菜单操作，
        也就避开了"从主菜单读档要按两次 Enter"这种易碎的界面操作。
        """
        if is_process_running(GAME_EXE):
            self.say("正在重新启动游戏…", busy=True)
            self._hold_game_exit(180.0)
            self._kill_game()
            if not self._wait_process_exit(8.0):
                logging.warning("旧进程没有结束，放弃自动重启（存档已拍好）")
                self.notify("游戏进程没有正常结束，请手动重新打开游戏。",
                            error=True, timeout=8.0)
                return
        info = self.manager.world_info(world)
        self.say("正在重新启动游戏并读回该世界…", busy=True)
        if self.relaunch_game(
            world, info.character,
            on_phase=lambda label, secs: self.say_async(f"{label}（{secs:.1f}s）", busy=True),
            on_ready=self._on_game_ready,
        ):
            if self.watcher:
                self.watcher.suppress_auto_snapshot_until = time.time() + 600
            self._hold_game_exit(20.0)
            self.notify("游戏已重新启动，正在读回该世界…", timeout=4.0)
            # 同样交给 _on_game_ready 去收面板，别在加载中途就藏掉
            self.hide_panel_when_ready = True
        else:
            self.notify("游戏启动失败，请手动打开游戏。", error=True, timeout=5.0)

    def after_duplicate(self, same: Snapshot) -> None:
        """世界内容与已有快照完全相同：不新建，也不占硬盘。"""
        msg = f"内容与 {same.label()} 完全相同，未新建快照（省了一份硬盘空间）"
        logging.info(msg)
        self.say(msg, busy=False)
        self.notify(msg, timeout=3.0)
        self._run_pending_snapshot()

    def after_snapshot(self, snap: Snapshot) -> None:
        self.panel.refresh()
        latency = self.manager.last_capture_latency
        extra = f"　写入用时 {latency}" if latency else ""
        self.say(
            f"快照 {snap.label()} 完成：{snap.file_count} 文件 / "
            f"{human_size(snap.total_bytes)}{extra}",
            busy=False,
        )
        self.notify(f"快照 {snap.label()} 已保存（{human_size(snap.total_bytes)}）", timeout=2.5)
        self._run_pending_snapshot()

    def after_error(self, title: str, exc: Exception) -> None:
        self.say(f"{title}：{exc}", busy=False, error=True)
        self.notify(f"{title}：{exc}", error=True, timeout=5.0)
        self._run_pending_snapshot()

    def after_inconsistent(self, exc: Exception) -> None:
        """一致性校验没通过：本次快照已丢弃，明确告诉用户下一步做什么。

        **这不是"快照失败"，而是"故意不存"** —— 半新半旧的档一旦落进列表，
        F6 还原的正是"最新那份"，读回去就是物品位置回溯、地图与角色不同步。
        所以提示里重点是"没有产生任何档点"和"怎么再拍一次"，避免用户以为
        已经拍好了。
        """
        self.say(f"未创建快照（已丢弃）：{exc}", busy=False, error=True)
        self.notify(
            "游戏仍在写盘，本次快照已丢弃（没有产生任何档点）。"
            "等游戏里存档提示消失后再按一次。",
            error=True, timeout=8.0,
        )
        self._run_pending_snapshot()

    # -------------------------------------------------- 操作：还原

    def do_restore_latest(self) -> None:
        world = self.current_world()
        if not world:
            self.notify("没找到存档世界。", error=True)
            return
        snap = self.manager.latest_for(world)
        if not snap:
            self.notify(f"「{world}」还没有任何快照。", error=True)
            return
        self.do_restore(snap)

    def do_restore(self, snap: Snapshot) -> None:
        if self.busy:
            self.notify("上一个操作还没结束，请稍候…", error=True)
            return
        # 可疑档（半新半旧）也允许还原 —— 它可能是你唯一的救命稻草 ——
        # 但必须让人先知道读回去可能出现物品位置回溯/怪物重复。
        # 提醒写进还原状态行里（会被后续 say 覆盖，所以不能单独 say 一句），
        # 同时弹一条气泡，保证在重启游戏的十几秒里都看得见。
        why = self.manager.suspicion(snap)
        if why[1]:
            logging.warning("还原可疑快照 %s（%s）：%s", snap.label(), why[0], why[1])
            self.notify(f"{snap.label()} 可能半新半旧（{why[1]}），仍按你的选择还原。",
                        error=True, timeout=8.0)
        # 磁盘上的存档比这份快照新 → 还原会整目录覆盖，这段进度就没了。
        # 这种情况**自动**先备份一份（不依赖 backup_before_restore 配置），
        # 并且要让玩家在重启游戏的十几秒里看得见。
        newer = self.manager.disk_newer_than(snap)
        make_backup = bool(self.config["backup_before_restore"]) or newer >= 30.0
        if newer >= 30.0:
            logging.warning("磁盘存档比 %s 新约 %.0f 分钟，还原会覆盖它；本次自动先备份",
                            snap.label(), newer / 60.0)
            self.notify(f"磁盘上的存档比 {snap.label()} 新约 {newer / 60:.0f} 分钟——"
                        f"还原会覆盖它，已自动先备份一份。", timeout=10.0)
        self.busy = True
        self.show_panel()
        head = f"正在还原 {snap.label()}（{snap.world}）"
        if why[1]:
            head += f"　⚠ {why[1]}"
        self.say(head + "…", busy=True)

        def worker() -> None:
            try:
                if is_process_running(GAME_EXE):
                    # 接下来的"杀进程 → 铺文件 → 重启"会把游戏从进程表里抹掉一段时间，
                    # 不能让守望把这当成玩家关游戏（Steam 模式下会直接退出工具）。
                    self._hold_game_exit(180.0)
                    self.say_async("正在结束游戏并还原…", busy=True)
                    if not self.quit_game():
                        raise SnapshotError("游戏没有在预期时间内退出，还原已取消（你的存档未被改动）。")
                result = self.manager.restore(
                    snap,
                    make_backup=make_backup,
                    progress=lambda msg: self.post(lambda m=msg: self.say(m, busy=True)),
                )
                self.post(lambda r=result: self.after_restore(snap, r))
            except Exception as exc:
                logging.exception("还原失败")
                self.post(lambda e=exc: self.after_error("还原失败", e))
            finally:
                self.busy = False

        threading.Thread(target=worker, daemon=True, name="restore-worker").start()

    def after_restore(self, snap: Snapshot, result: dict[str, Any]) -> None:
        backup = result.get("backup_sid")
        extra = f"，还原前进度已备份为 #{backup:03d}" if backup else ""
        self.say(f"已还原 {snap.label()}{extra}", busy=False)
        self.notify(f"已回到快照 {snap.label()}{extra}", timeout=4.0)
        if not self.config["relaunch_after_restore"]:
            self.world = snap.world
            return
        # 我们马上就要重启游戏；这次启动不该再自动拍一张（内容与刚还原的一样）
        if self.watcher:
            self.watcher.suppress_auto_snapshot_until = time.time() + 600
        self.say("正在重启游戏…", busy=True)
        if self.relaunch_game(
            snap.world,
            snap.character,
            on_phase=lambda label, secs: self.say_async(f"{label}（{secs:.1f}s）", busy=True),
            on_ready=self._on_game_ready,
        ):
            self._hold_game_exit(20.0)
            self.notify("游戏已重新启动，正在加载世界…", timeout=3.0)
            # 面板**不在这里**收起：留在屏幕上让你看清"当前生效"的高亮是哪一份。
            # 真正的收起放在 _on_game_ready（游戏加载完、进了世界）之后。
            self.hide_panel_when_ready = True
        else:
            self.notify("游戏启动失败，请手动打开游戏。", error=True, timeout=5.0)

    def close_panel(self) -> None:
        """把面板收起来（等价于按一次 F4 隐藏）。"""
        if self.panel and self.panel.visible:
            self.panel.hide()

    def _on_game_ready(self, elapsed: float, reason: str) -> None:
        """游戏加载完成、真正进了世界 —— 这时才收起面板。

        放在这里而不是重启那一刻：重启后还有十几秒加载时间，
        面板留在屏幕上，你正好能看清"当前生效"的是哪一份快照。
        """
        message = f"游戏已就绪，可以继续游玩（加载 {elapsed:.1f} 秒）"
        logging.info(message + f" [{reason}]")
        self.say(message, busy=False)
        if self.config["toast_enabled"]:
            self.post(lambda: self.panel and self.panel.toast(message, timeout=4.0))
        if getattr(self, "hide_panel_when_ready", False):
            self.hide_panel_when_ready = False
            logging.info("游戏已加载完成，立即收起面板")
            self.close_panel()

    # -------------------------------------------------- 游戏进程控制

    def quit_game(self) -> bool:
        """结束游戏进程，准备还原存档。

        读档的语义就是「丢弃当前进度回到快照」，所以**不做**优雅存档退出：
        优雅退出要先 Shift+S 再确认、等游戏收尾，实测要 6~8 秒，纯属浪费。
        直接结束进程，还原前也用不到它的存档。
        """
        if not is_process_running(GAME_EXE) and not find_game_window():
            return True
        self.say_async("正在结束游戏进程…", busy=True)
        self._kill_game()
        return self._wait_process_exit(8.0)

    @staticmethod
    def _wait_process_exit(seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if not is_process_running(GAME_EXE):
                return True
            time.sleep(0.5)
        return not is_process_running(GAME_EXE)

    @staticmethod
    def _kill_game() -> None:
        try:
            kill_processes_named(GAME_EXE)
        except Exception:
            logging.exception("结束游戏进程失败")

    def _dismiss_startup_prompt_soon(self) -> None:
        """CCB 启动提示：每次由工具拉起游戏后，自动投递一次回车把它关掉。

        必须放在线程里：它要等窗口出现、再等提示画出来，不能挡住重启流程
        本身（调用方还要继续跟踪加载进度、更新面板）。
        """
        if not self.config["press_enter_on_game_start"]:
            return
        threading.Thread(
            target=dismiss_startup_prompt,
            args=(self.game_root,),
            kwargs={"delay": float(self.config["startup_enter_delay"])},
            name="startup-prompt-enter",
            daemon=True,
        ).start()

    def relaunch_game(self, world: str, character: str = "",
                      on_phase=None, on_ready=None) -> bool:
        """重启游戏并跟踪它的加载进度。

        CDDA 实测加载时间线（本机）：进程起来 0.3 秒就出窗口，但静态数据
        （3000+ 个 JSON 与 tileset）要 ~9 秒，整局可玩约 15 秒。这段时间工具原本
        完全静默、玩家只能干等；现在把阶段回报出来，并在真正就绪时通知一次。
        """
        exe = os.path.join(self.game_root, GAME_EXE)
        if not os.path.isfile(exe):
            return False
        # 先写 config/lastworld.json：这样即使命令行参数没被接住，
        # 主菜单的「继续」也指向还原后的世界与角色（需要点一次继续，但一定能进）。
        if character:
            self.write_lastworld(world, character)
        try:
            subprocess.Popen(
                [exe, "--world", world],
                cwd=self.game_root,
                close_fds=True,
            )
            logging.info("已重启游戏: %s --world %s (%s)", exe, world, character or "-")
            self._dismiss_startup_prompt_soon()
        except Exception:
            logging.exception("重启游戏失败")
            return False

        threading.Thread(
            target=GameLoadTracker(
                os.path.join(self.game_root, "config", "debug.log"),
                stall_seconds=float(self.config["load_ready_stall_seconds"]),
            ).watch,
            args=(on_phase, on_ready),
            name="game-load-watch",
            daemon=True,
        ).start()
        return True

    def write_lastworld(self, world: str, character: str) -> bool:
        """把「上次游玩的世界/角色」写成该快照的状态，供主菜单『继续』使用。"""
        path = os.path.join(self.game_root, "config", "lastworld.json")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.isfile(path):
                shutil.copy2(path, path + ".cdda_snapshot_bak")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {"world_name": world, "character_name": character},
                    fh,
                    ensure_ascii=False,
                    indent=2,
                )
            logging.info("已更新 lastworld.json -> %s / %s", world, character)
            return True
        except Exception:
            logging.exception("写入 lastworld.json 失败")
            return False

    # -------------------------------------------------- 面板

    def show_panel(self) -> None:
        if self.panel:
            self.panel.show()

    def show_panel_if_hidden(self) -> None:
        """面板没显示时才弹出来。

        用于"按钮点击要走面板、热键触发不要弹"这种共用同一个入口的场景。
        """
        if self.panel and not self.panel.visible:
            self.show_panel()

    def toggle_panel(self) -> None:
        if not self.panel:
            return
        if self.panel.visible:
            self.panel.hide()
        else:
            self.panel.show()


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def snapshot_health(snap: Snapshot, lag_warn_seconds: float = 60.0) -> dict[str, Any]:
    """检查一份快照的完整性，返回可读的诊断结果。

    重点看**时间一致性**：一次存档会把角色、地图、超图一起写完。
    如果快照里"角色档"明显比"地图/超图"新很多，说明它很可能是
    在两次写入之间取下来的半新半旧状态 —— 读回去会出现物品位置回溯、
    角色与地图不同步，甚至"附近已有一个敌人"这类报错。

    ``lag_warn_seconds`` 就是配置里的 ``verify_lag_warn_seconds``。
    """
    import json as _json

    out: dict[str, Any] = {"sid": snap.sid, "label": snap.label(),
                           "world": snap.world, "issues": [], "warnings": []}
    if not os.path.isdir(snap.path):
        out["issues"].append("快照目录不存在")
        return out

    files = list(iter_save_files(snap.path))
    out["file_count"] = len(files)

    has_char = any(".sav" in f.lower() for f in files)
    has_map = any(f.lower().startswith("maps" + os.sep) for f in files)
    has_over = any(f.lower().startswith("overmaps" + os.sep) for f in files)
    if not has_char:
        out["issues"].append("缺少角色存档（.sav）")
    if not has_map:
        out["warnings"].append("没有地图文件（maps/）")
    if not has_over:
        out["warnings"].append("没有超图文件（overmaps/）")

    ts_path = os.path.join(snap.path, "save_timestamp.json")
    out["save_timestamp"] = ""
    if os.path.isfile(ts_path):
        try:
            with open(ts_path, "r", encoding="utf-8", errors="replace") as fh:
                out["save_timestamp"] = str(_json.load(fh))
        except Exception:
            out["warnings"].append("save_timestamp.json 无法解析")
    else:
        out["warnings"].append("缺少 save_timestamp.json")

    # 时间一致性
    def newest(pred) -> float:
        best = 0.0
        for rel in files:
            low = rel.lower()
            if pred(low):
                try:
                    best = max(best, os.path.getmtime(os.path.join(snap.path, rel)))
                except OSError:
                    pass
        return best

    char_t = newest(lambda s: ".sav" in s)
    map_t = newest(lambda s: s.startswith("maps" + os.sep))
    over_t = newest(lambda s: s.startswith("overmaps" + os.sep))
    out["char_mtime"] = char_t
    out["map_mtime"] = map_t
    out["overmap_mtime"] = over_t

    # 角色档比地图/超图新出很多 => 可疑
    for label, other in (("地图", map_t), ("超图", over_t)):
        if char_t and other and (char_t - other) > lag_warn_seconds:
            out["warnings"].append(
                f"角色档比{label}新 {(char_t - other) / 60:.0f} 分钟 —— "
                f"可能是半新半旧的状态"
            )
    out["graph_span"] = max(char_t, map_t, over_t) - min(
        t for t in (char_t, map_t, over_t) if t) if any((char_t, map_t, over_t)) else 0.0
    return out


def cli_verify(manager: SnapshotManager) -> int:
    """体检：检查所有快照的完整性与时间一致性。"""
    total = 0
    flagged = 0
    for info in manager.list_worlds():
        snaps = manager.snapshots(info.name)
        if not snaps:
            continue
        print(f"\n世界「{info.name}」")
        for snap in snaps:
            total += 1
            h = snapshot_health(snap, manager.config["verify_lag_warn_seconds"])
            mark = "OK"
            if h["issues"]:
                mark = "损坏"
                flagged += 1
            elif h["warnings"]:
                mark = "可疑"
                flagged += 1
            print(f"  [{mark:4s}] {snap.label()}  {snap.created_local:%m-%d %H:%M:%S}  "
                  f"{h.get('file_count', 0):>4} 文件  {snap.name or snap.note}")
            for w in h["warnings"]:
                print(f"           ! {w}")
            for w in h["issues"]:
                print(f"           X {w}")
    print(f"\n共 {total} 份快照，{flagged} 份需要留意。")
    if flagged:
        print("提示：「可疑」通常意味着快照是在游戏写盘过程中取下的。")
        print("      这类快照读取后可能出现物品位置回溯或地图不同步，建议重拍。")
    return 0


def cli_dedupe(manager: SnapshotManager) -> int:
    """清理内容完全相同的重复快照。"""
    before = 0
    for info in manager.list_worlds():
        before += len(manager.snapshots(info.name))
    print(f"去重前共 {before} 份快照，正在比较内容…")
    removed = manager.dedupe(progress=lambda msg: print("  " + msg))
    after = 0
    for info in manager.list_worlds():
        after += len(manager.snapshots(info.name))
    if removed:
        print(f"已删除 {len(removed)} 份重复快照: {removed}")
        print(f"剩余 {after} 份")
    else:
        print("没有发现内容重复的快照。")
    return 0


def cli_drop_auto(manager: SnapshotManager) -> int:
    """删除所有 kind=auto 的自动出发档。"""
    victims = []
    for info in manager.list_worlds():
        victims.extend(s for s in manager.snapshots(info.name) if s.kind == "auto")
    if not victims:
        print("没有自动出发档。")
        return 0
    freed = sum(s.total_bytes for s in victims)
    print(f"找到 {len(victims)} 份自动出发档，逻辑大小合计 {human_size(freed)}：")
    for s in victims:
        print(f"  {s.label()}  {s.created_local:%m-%d %H:%M:%S}  "
              f"{human_size(s.total_bytes):>9}  {s.note!r}")
    print("\n注意：这些快照之间大量文件是硬链接共享的，所以实际释放的空间会小于上面的合计。")
    removed = 0
    for s in victims:
        manager.delete(s)
        removed += 1
    print(f"已删除 {removed} 份。")
    return 0


def cli_list(manager: SnapshotManager) -> int:
    worlds = manager.list_worlds()
    if not worlds:
        print("没有找到任何存档世界。")
        return 0
    for info in worlds:
        snaps = manager.snapshots(info.name)
        total = sum(s.total_bytes for s in snaps)
        world_root = os.path.join(manager.root, safe_world_dirname(info.name))
        physical = allocated_bytes(world_root) if os.path.isdir(world_root) else 0
        print(
            f"\n世界「{info.name}」  角色: {info.character}  快照 {len(snaps)} 份"
            f"  /  累计 {human_size(total)}（实占磁盘 {human_size(physical)}，硬链接去重后）"
        )
        print(f"  存档: {info.path}")
        print(f"  快照: {world_root}")
        if not snaps:
            print("  （暂无快照）")
            continue
        for snap in snaps:
            when = snap.created_local.strftime("%m-%d %H:%M:%S")
            gtime = decode_save_timestamp(snap.game_time)
            gtext = gtime.strftime("%Y-%m-%d %H:%M") if gtime else "—"
            print(
                f"  {snap.label()}  {when}  游戏内 {gtext}  "
                f"{human_size(snap.total_bytes):>10}  {snap.file_count:>4} 文件"
                f"  硬链接 {snap.linked_files:>4}  [{snap.kind}]"
            )
    print()
    return 0


def cli_snapshot(manager: SnapshotManager, config: Config, quicksave: bool, world: Optional[str]) -> int:
    app_world = world
    if not app_world:
        worlds = manager.list_worlds()
        if not worlds:
            print("没有找到任何存档世界。")
            return 1
        app_world = max(
            worlds, key=lambda w: os.path.getmtime(w.path) if os.path.isdir(w.path) else 0
        ).name
    if quicksave:
        hwnd = find_game_window()
        if hwnd:
            print("已向游戏窗口发送存档指令，等待写盘…")
            ok = trigger_game_save(
                hwnd, os.path.join(manager.save_dir, app_world), config,
                progress=lambda m: print("  " + m),
            )
            if not ok:
                print("  游戏没有在预期时间内写盘，按当前磁盘状态快照。")
        else:
            print("游戏未在运行，直接按当前磁盘状态快照。")
    snap = manager.create(app_world, kind="manual", progress=lambda m: print("  " + m))
    print(
        f"快照 {snap.label()} 已创建：世界「{snap.world}」角色「{snap.character}」"
        f" {snap.file_count} 文件 / {human_size(snap.total_bytes)}"
        f"（硬链接复用 {snap.linked_files} 个文件）"
    )
    print(f"位置: {snap.path}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    _fix_console()
    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}")
    parser.add_argument("--root", help="游戏根目录（默认自动探测）")
    parser.add_argument("--list", action="store_true", help="列出所有世界与快照")
    parser.add_argument("--snapshot", action="store_true", help="立刻创建一份快照")
    parser.add_argument("--world", help="指定世界名（配合 --snapshot）")
    parser.add_argument("--no-quicksave", action="store_true", help="快照前不向游戏发送存档键")
    parser.add_argument("--recover", action="store_true", help="执行还原自愈检查后退出")
    parser.add_argument("--gui-selftest", type=float, default=0.0,
                        metavar="SECONDS", help="打开面板运行若干秒后自动退出（开发自检用）")
    parser.add_argument("--selftest-hotkey-dialog", action="store_true",
                        help="自检时自动打开热键设置对话框（开发用）")
    parser.add_argument("--selftest-longstatus", action="store_true",
                        help="自检时塞入一条超长状态文字，验证面板高度自适应（开发用）")
    parser.add_argument("--wait-mutex", action="store_true",
                        help="启动时等待单实例锁（内部用于改完热键后自动重启）")
    parser.add_argument("--autostart", action="store_true",
                        help="常驻后台守望 CDDA，游戏一启动就自动就绪（配合开机自启）")
    parser.add_argument("--autostart-off", action="store_true",
                        help="关闭常驻守望")
    parser.add_argument("--steam-launch", action="store_true",
                        help="由 Steam 拉起时使用：工具常驻、自己带 --world 启动游戏；"
                             "读档重启只换子进程，Steam 的计时不会中断")
    parser.add_argument("--dedupe", action="store_true",
                        help="清理内容完全相同的重复快照（每组只留最早一份）")
    parser.add_argument("--verify", action="store_true",
                        help="体检：检查所有快照的完整性与时间一致性（能查出"
                             "游戏写盘过程中拍下的可疑快照）")
    parser.add_argument("--drop-auto-snapshots", action="store_true",
                        help="删除所有 kind=auto 的自动出发档（游戏在读取世界时"
                             "会重写文件，这类快照内容基本重复，纯占空间）")
    args = parser.parse_args(argv)

    game_root = os.path.abspath(args.root) if args.root else find_game_root()
    config = Config(os.path.join(snapshot_root(game_root), "config.json"))
    manager = SnapshotManager(game_root, config)

    if args.autostart or args.autostart_off:
        config["autostart_with_game"] = bool(args.autostart)
        config.save()
        print(f"已{'开启' if args.autostart else '关闭'}常驻守望（autostart_with_game"
              f"={config['autostart_with_game']}）")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        filename=manager.log_path,
        encoding="utf-8",
    )

    for message in manager.recover_if_needed():
        print("自愈:", message)

    if args.recover:
        print("自愈检查完成。")
        return 0
    if args.dedupe:
        return cli_dedupe(manager)
    if args.verify:
        return cli_verify(manager)
    if args.drop_auto_snapshots:
        return cli_drop_auto(manager)
    if args.list:
        return cli_list(manager)
    if args.snapshot:
        return cli_snapshot(manager, config, not args.no_quicksave, args.world)

    is_first, mutex = single_instance()
    if not is_first and args.wait_mutex:
        # 改完热键后自动重启的路径：旧进程正在退出，这里重试拿锁
        deadline = time.time() + 15.0
        while not is_first and time.time() < deadline:
            time.sleep(0.3)
            is_first, mutex = single_instance()
    if not is_first and args.steam_launch:
        # 常驻实例可能正占着单实例位置。请它让位、再把锁抢回来 ——
        # 必须是「Steam 亲手拉起的这个进程」持有单实例并活着，Steam 才认为
        # 游戏在运行；换个已有进程接管是没用的（Steam 的抓手指的是它 spawn 的那个）。
        logging.warning("Steam 包装模式：已有实例在运行，本次启动请求接管"
                        "（等常驻实例让位，最多 12 秒）")
        request_steam_takeover(snapshot_root(game_root))
        deadline = time.time() + 12.0
        while not is_first and time.time() < deadline:
            time.sleep(0.25)
            is_first, mutex = single_instance()
        if is_first:
            clear_steam_takeover(snapshot_root(game_root))
            logging.info("Steam 接管成功：让位的常驻实例已退出，本实例继续")
    if not is_first:
        message = f"{APP_NAME} 已经在运行了。"
        print(message)
        # 弹框只是给"双击了两次"的用户一个解释，**绝不能用在自动化路径上**：
        # showinfo 是模态的，没人点"确定"就会一直阻塞 —— 实测把
        # `--gui-selftest` 挂死过一次（进程僵在那里，日志一个字都没写）。
        # 所以自检类调用一律只打印，不弹框。
        scripted = bool(args.gui_selftest or args.selftest_hotkey_dialog
                        or args.selftest_longstatus or args.steam_launch)
        if args.steam_launch:
            # 绝不能弹模态框：Steam 启动是自动化路径，弹框会把 Steam 那个进程
            # 挂在"等待确定"的状态里，玩家看到的就是"点了启动没反应"。
            logging.warning("Steam 包装模式：接管失败（常驻实例 12 秒内没让位），本次启动退出。"
                            "若常驻的是旧版本（没有接管机制），先停掉 watch_with_game.bat；"
                            "否则多半是它正卡在拍快照/还原，再点一次启动即可。")
        if not scripted:
            try:
                import tkinter as tk

                root = tk.Tk()
                root.withdraw()
                import tkinter.messagebox as mb

                info = Config(os.path.join(snapshot_root(game_root), "config.json"))
                candidates = hotkey_candidates(info["hotkeys"].get("toggle_panel"))
                panel_key = format_hotkey(candidates[0]) if candidates else "F4"
                mb.showinfo(APP_NAME, f"{message}\n\n面板热键：{panel_key}\n也可以点托盘图标打开面板。")
                root.destroy()
            except Exception:
                pass
        return 0

    app = SnapshotApp(game_root, steam_launch=bool(args.steam_launch))
    app.mutex = mutex
    return app.run(
        selftest_seconds=args.gui_selftest,
        selftest_hotkey_dialog=args.selftest_hotkey_dialog,
        selftest_longstatus=args.selftest_longstatus,
    )


if __name__ == "__main__":
    try:
        from panel import QuickPanel  # noqa: F401
    except ImportError:
        pass
    sys.exit(main())
