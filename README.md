# Cataclysm Shortcut

服务于 **CDDA**（Cataclysm: Dark Days Ahead）与 **CCB**（Cataclysm: Cleanwater Bomb）的存档快照工具：
完全独立于游戏，使用最老派的存档文件保存与还原的方法，享受最old-school的独立游戏快速存取体验！

原项目名「CDDA 快照工具」，因同时服务 CDDA 与 CCB 而更名为 **Cataclysm Shortcut**。当前版本 **0.9**。

* 平台：Windows 10 / 11（依赖 Win32 输入注入、NTFS 硬链接）
* 依赖：Python 3.9+（含 tkinter，官方安装包自带），无第三方库
* 游戏：识别进程名 `cataclysm-tiles.exe`，CDDA 与 CCB 通用

---

## 1. 它解决什么问题

* CDDA 系列只有「保存并退出」，而你只有一份存档；CCB具有快照功能，但不能一键随存随取。不手动复制整个存档，你就永远不会知道如果刚才不冲进去会怎样：正如树林中有两条小径，而你只能择其一。

Cataclysm Shortcut 把这件事变成一次按键：

* **F5**：游戏存盘 → 确认存盘完成 → 保存世界目录快照 → 自动重开游戏并读回存档。
* **F6**：随时回到最近一张快照（自动退出游戏、替换存档、重启到该世界）。
* **F4**：打开面板，从列表里挑任意一张快照还原，或给它起名设为永久存档。

---

## 2. 功能一览

| 功能 | 说明 |
| - | - |
| 一键快照 | F5：先触发游戏存盘，再对**整个世界目录**（角色 `.sav`、`maps/`、`overmaps/`、`.mm1/`、`master.gsav`）做同一时刻的快照 |
| 一致性校验 | 拷贝前等目录静默、拷贝中验证源目录未变、拷贝后逐项比对；不通过就整份丢弃并报错，绝不保存半新半旧的档 |
| 不打扰游戏 | Ctrl+F5 只快照磁盘上已有的存档，同步磁盘存档至快照列表 |
| 增量占用 | 未变化的文件用 NTFS 硬链接指向上一份快照，长局游戏下空间接近增量备份，每份快照仍可独立还原 |
| 占用统计 | 面板里显示快照文件夹的「逻辑大小 → 硬链接实占（已节省空间）」与文件数；后台扫描，界面先行 |
| 多存档点 | 每个世界一份快照列表，面板里按 1–9 选中、再按一次才还原（Enter/双击立即还原） |
| 永久存档 | 命名快照★标，隔离于存档覆盖之外 |
| 自动清理 | 每个世界默认保留 10 份可自动删除的快照，超出删除最旧的一份 |
| 当前档标记 | 快照列表标 ▶，显示当前载入快照 |
| 坏档提示 | 未通过校验的档标 ⚠，旧版本产生的档标 ?，只在疑似不一致时才提示 |
| 全局热键 | 无需切窗口，面板/快照/回档/退出都有可自定义的全局热键，并带候选键兜底 |
| 弹出面板 | 按热键才出现在屏幕中央，可拖动，25 秒无操作自动隐藏 |
| 托盘兜底 | 热键全被别的软件占用时，左键点托盘图标开面板，右键出菜单 |
| 状态气泡 | 快照/还原的进度用屏幕角标气泡提示，不弹对话框打断游戏 |
| CCB 兼容 | 自动确认 CCB 的Lua 模组安全提示，最低化重启延迟 |
| Steam 计时 | 可选 `--steam-launch` 包装模式，读档重开游戏时 Steam 仍认为游戏在运行，游玩时长不被切断 |
| 命令行 | `--list`、`--snapshot`、`--verify`、`--recover` 等，便于脚本与排查 |

---

## 3. 安装

### 3.1 准备

1. 安装 [Python 3.9 或更新版本](https://www.python.org/downloads/)。安装时勾选 **Add python.exe to PATH**。
装好后在命令行里能运行 `python --version` 即可。
2. 确认游戏目录执行文件名为 `cataclysm-tiles.exe`（CDDA 与 CCB 都使用这个文件名）。

### 3.2 放到游戏目录

把整个 `Cataclysm_shortcut` 文件夹放进游戏根目录，与 `cataclysm-tiles.exe` **同级**：

```text
C:\Games\CDDA\
├─ cataclysm-tiles.exe
├─ save\
└─ Cataclysm_shortcut\        ← 本工具文件夹
   ├─ cdda_snapshot.pyw
   ├─ panel.py
   ├─ winapi.py
   ├─ start_cataclysm_shortcut.bat
   ├─ watch_with_game.bat
   ├─ create_shortcut.bat
   ├─ make_shortcut.py
   └─ README.md
```

工具是**自包含**的：配置、日志、快照数据都写在 `Cataclysm_shortcut/.cdda_snapshots/` 里。
升级游戏版本时，把这个文件夹整个拷到新游戏目录即可。

### 3.3 启动

双击 **`start_cataclysm_shortcut.bat`**。它会用 `pythonw.exe` 在后台启动工具，不弹控制台窗口，
然后工具常驻托盘、注册全局热键。

想更省事，可以：

* 双击 `create_shortcut.bat` 生成 `Cataclysm_shortcut.lnk`，以后直接双击快捷方式；
* 把 `watch_with_game.bat`（或它的快捷方式）放进开机启动目录（`Win+R` → `shell:startup`），
工具会随系统常驻，之后无论从 Steam、桌面还是命令行启动游戏，热键都已经在工作。

> 工具同时只运行一个实例；重复启动会提示已经在运行。

### 3.4 可选：让 Steam 正确统计游玩时长

CDDA 通常以「非 Steam 游戏」加入 Steam。默认情况下 Steam 只能直接启动 `cataclysm-tiles.exe`，
无法同时带上本工具；而工具在还原存档时会重启游戏，Steam 就会认为游戏已退出，游玩时长被切断。

解决办法是把 Steam 快捷方式指向 Python，让工具成为 Steam 亲手拉起的进程：

1. Steam → 库 → 右键游戏 → 属性 → 快捷方式；
2. **目标**填 `pythonw.exe` 的完整路径（`where pythonw` 可查到）；
3. **起始选项**填：

```text
   "C:\Games\CDDA\Cataclysm_shortcut\cdda_snapshot.pyw" --steam-launch
   ```

这样游戏是本工具的子进程，读档重启只换子进程，Steam 的「运行中」状态不会中断。
若你同时开着开机常驻的那份工具，它会检测到 Steam 拉起的实例并自动让位（最多等约 12 秒，
取决于它当时是否正在快照/还原）。

---

## 4. 使用方法

### 4.1 默认热键

| 热键 | 动作 |
| - | - |
| `F4` | 打开 / 关闭面板 |
| `F5` | 快速保存并快照（游戏存档 → 拍快照 → 自动重开读回） |
| `Ctrl+F5` | 只快照磁盘上已有的存档 |
| `F6` | 还原最近一张快照（自动退出游戏 → 还原 → 重启） |
| `Ctrl+Alt+Q` | 退出工具 |

每个动作都配了候选键（如 F4 失败自动试 F8、Alt+Z），常见软件抢占 F4/F5/F6 时工具会自动退到下一候选。
实际生效的键显示在面板底部与按钮文字里。

### 4.2 拍一张快照

按 **F5**。工具会：

1. 找到游戏窗口，发送「ESC → e」触发游戏内快速保存（CDDA 的存盘不退出游戏）；
2. 等世界目录连续安静 `snapshot_settle_seconds`（默认 0.4 秒）；
3. 再校验一遍源目录没有变化、快照内容与源目录逐项相同；
4. 用硬链接把快照写进 `.cdda_snapshots/<世界名>/#NNN/`；
5. 因为 CDDA 的「保存并退出」语义，这一步做完游戏会退出，工具随即自动重开游戏并读回该世界。

一次 F5 通常约 1.4 秒（游戏在跑），Ctrl+F5 约 0.2 秒。游戏没在运行时会直接拍当前磁盘状态。

> 不想让 F5 重启游戏，可以把配置项 `snapshot_save_mode` 改成 `"disk"`，
> 或直接用 Ctrl+F5。

### 4.3 还原

* **F6**：直接还原该世界最新的一张，**立即执行，不再弹确认框**（按错就是真的还原，注意别误触）。
* **面板**：F4 打开面板 → 用方向键或 `1`–`9` 选中某张：

  * `Enter` 或双击 → 立即还原；
  * 数字键需要**按两次**：第一次只是选中并提示「再按一次确认还原」，第二次才真正执行。

还原过程全自动：先让游戏存档退出 →（可选）备份当前进度 → 原子替换世界目录 → 重启游戏到该世界。
如果磁盘上的存档比所选快照新 **30 秒以上**，工具会自动先把当前进度备份成一份快照再还原；想让每次还原都备份，把 `backup_before_restore` 设为 `true`。

### 4.4 面板

按 F4 打开，窗口出现在屏幕中央，可拖动；25 秒无操作自动隐藏（`panel_autohide_seconds`）。

* **按钮**：快速保存并快照、还原最新、刷新、只存磁盘快照、命名/永久存档、热键设置、目录。
* **占用统计行**：窗口上方一行 `快照文件夹 X → 硬链接实占 Y（已省 Z）　N 个文件`——X 是把每份快照都按完整大小累加的结果，Y 是硬链接去重后的真实占盘，Z 是省下的空间；统计整个 `.cdda_snapshots`（包括备份、日志），耗时随文件数增长，所以放在后台线程里跑，打开面板立刻可用，算完自动填入。
* **列表**：`序号.★▶ 快照标签  时间（相对）  大小  文件数  类型`

  * `★` 永久存档（不会被自动删除）
  * `▶` 与你当前存档完全一致的那份
  * `⚠未校验` 未通过一致性校验的档；`?旧档` 旧版本留下、没有校验记录
* **列表快捷键**：`1`–`9` 选中（再按一次还原）、`Enter` 还原、`Del` 删除、`Esc` 隐藏。
* **命名/永久存档**：选中快照 → 点「命名/永久存档」→ 输入名字。命名后它不参与自动删除；
把名字清空即取消永久标记。
* **目录**：在资源管理器里打开当前世界的快照目录。
* **底部状态栏**：显示当前进度、实际生效的热键、遇到的错误。

### 4.5 自动清理

每个世界默认保留 **10 份**可自动删除的快照（`keep_per_world`）。超出时，最旧的一份普通快照会被删除，
永久存档（★）和被标记为备份的档不计入、也不会被删。删除某份快照不会影响其它快照——未变化的文件是硬链接共用的。

### 4.6 托盘

* 左键单击托盘图标：打开 / 关闭面板
* 右键：菜单（面板、拍快照、只存磁盘快照、还原最新、打开目录、热键设置、重载配置、退出）

热键被别的软件抢光时，托盘是最后一个入口。不需要托盘可在配置里设 `"tray_icon": false`。

### 4.7 数据在哪、占多大

所有运行数据都在工具自己的文件夹里：

```text
Cataclysm_shortcut/.cdda_snapshots/
├─ config.json              配置（首次运行自动生成）
├─ manifest.json            快照索引
├─ cdda_snapshot.log        运行日志（排错先看这里）
└─ <世界名>/
   ├─ #001/ #002/ …        各份快照（完整的世界目录）
   └─ snapshots/            标注为「快照」的存档目录
```

由于硬链接去重，新增一份快照通常只占「这一档改动过的地图文件」的大小；
`.cdda_snapshots` 的实占空间远小于各份快照大小之和。命令行 `--list` 会同时给出
「累计大小」与「实占磁盘」；打开面板时，上方那行统计会直接显示
`快照文件夹 逻辑大小 → 硬链接实占（已省多少）　文件数`，随快照增删自动刷新。

统计范围是整个 `.cdda_snapshots`（含 `_backup/`、日志、配置），所以数字略大于快照本身之和；
两万多个文件的真实目录扫一遍约 1 秒，属正常。

### 4.8 命令行

```bat
python cdda_snapshot.pyw --list                 :: 列出世界与快照
python cdda_snapshot.pyw --snapshot             :: 立刻拍一张（默认先触发游戏存盘）
python cdda_snapshot.pyw --snapshot --no-quicksave
python cdda_snapshot.pyw --snapshot --world "我的世界"
python cdda_snapshot.pyw --verify               :: 体检快照一致性
python cdda_snapshot.pyw --recover              :: 还原中断后自愈
python cdda_snapshot.pyw --dedupe               :: 删除内容完全相同的重复快照（每组只留最早一份）
python cdda_snapshot.pyw --drop-auto-snapshots  :: 清掉自动快照
```

`--root <游戏目录>` 可手动指定游戏根目录（默认从脚本位置向上查找 `cataclysm-tiles.exe`）。

---

## 5. 可改键

### 5.1 在面板里改（推荐）

1. 按 `F4` 打开面板 → 点 **「热键设置」**；
2. 点一下想改的输入框，**直接按下你想要的组合键**（例如 `Ctrl+Alt+F9`）；
3. 输入框右侧会立即显示「可用」或「不可用：原因」，可点 **「测试」** 验证；
4. 改完点 **「保存并重启」**，工具自动重启，新热键立即生效。

对话框里可改四个动作：打开/关闭面板、拍快照、回到最新快照、退出工具。
「只存磁盘快照」的热键（默认 Ctrl+F5）目前只能改配置文件。

### 5.2 在 config.json 里改

关掉工具，编辑 `Cataclysm_shortcut/.cdda_snapshots/config.json` 的 `hotkeys` 段，再启动工具。
每个动作可以写**一个字符串**，也可以写**候选列表**（按顺序尝试，第一个注册成功的生效）：

```json
"hotkeys": {
  "toggle_panel":   ["f4", "f8", "alt+z"],
  "snapshot":       ["f5", "f7", "ctrl+alt+s"],
  "snapshot_disk":  ["ctrl+f5", "ctrl+shift+f5"],
  "restore_latest": ["f6", "f9", "ctrl+f6", "ctrl+alt+l"],
  "exit_tool":      ["ctrl+alt+q", "ctrl+shift+q"]
}
```

写法规则：

* 修饰键在前，用 `+` 连接，修饰键为 `ctrl`、`alt`、`shift`；
* 主键可以是 `a`–`z`、`0`–`9`、`f1`–`f24`，或
`space`、`enter`、`tab`、`escape`、`backspace`、`delete`、`insert`、`home`、`end`、
`pageup`、`pagedown`、`up`、`down`、`left`、`right` 与 `-`、`=`、`[`、`]`、`;`、
`'`、`,`、`.`、`/`、`\\`、``` 等符号；
* 字母不区分大小写，`shift+a` 等价于 `A`；
* **强烈建议给「退出工具」保留带修饰键的组合**，避免在游戏里误按退出。

### 5.3 键被占用怎么办

别的常驻软件（QQ、微信、录屏、游戏加速器）经常占用 F4/F5/F6 与 `Ctrl+Alt+*`。
工具按候选列表逐个尝试注册，第一个成功的生效；面板底部的「全局热键」一行会显示**实际生效**的键。
如果全部候选都失败：

* 点托盘图标打开面板（面板里所有操作都能用鼠标完成），或
* 在热键设置里换一个没被占用的组合。

---

## 6. 配置项

配置在 `Cataclysm_shortcut/.cdda_snapshots/config.json`，改完重启工具生效。
首次运行会自动写入默认值；下面只列常用的，完整键见 `cdda_snapshot.pyw` 里的 `DEFAULT_CONFIG`。

| 键 | 默认 | 说明 |
| - | - | - |
| `quicksave_before_snapshot` | `true` | 快照前是否先让游戏存一次档 |
| `snapshot_save_mode` | `"fresh"` | `fresh`：存盘后再快照并自动重开读回；`disk`：只快照磁盘上的存档 |
| `relaunch_after_fresh_snapshot` | `true` | fresh 模式拍完是否自动重开游戏并读回 |
| `quicksave_timeout` | `10.0` | 等游戏开始写盘的最长秒数 |
| `quicksave_fast_poll_seconds` | `1.5` | 触发存盘后细粒度轮询的时长 |
| `quicksave_menu_delay` | `0.1` | 发 ESC 后等多久再发 `e`（游戏内保存） |
| `press_enter_on_game_start` | `true` | 启动游戏后自动回车，按掉 CCB 的 Lua 模组提示 |
| `startup_enter_delay` | `1.5` | 窗口出现后等多久再投递回车 |
| `snapshot_consistent_attempts` | `3` | 校验失败时整份重做的次数 |
| `snapshot_settle_seconds` | `0.4` | 拷贝前要求源目录静默多久（秒），小于存档段间隔会拍到半新半旧 |
| `allow_inconsistent_snapshot` | `false` | 校验不过时是否仍保留（默认丢弃，宁可不存绝不存坏） |
| `keep_per_world` | `10` | 每个世界保留多少份可自动删除的快照 |
| `relaunch_after_restore` | `true` | 还原后是否自动重启游戏 |
| `backup_before_restore` | `false` | 还原前是否先把当前进度存成快照 |
| `use_hardlinks` | `true` | 用硬链接节省空间；关掉则每份完整拷贝 |
| `toast_enabled` | `true` | 是否显示屏幕角标气泡 |
| `panel_autohide_seconds` | `25.0` | 面板无操作自动隐藏秒数（0 = 不隐藏） |
| `tray_icon` | `true` | 是否显示托盘图标 |
| `load_ready_stall_seconds` | `5.0` | 重启游戏后日志停止增长多久算「加载完成」 |
| `verify_lag_warn_seconds` | `60` | 角色档比地图新出多少秒时提示「可能不一致」 |
| `autostart_with_game` | `false` | 是否等待游戏进程出现后自动就绪 |
| `auto_snapshot_on_launch` | `false` | 游戏加载完成后是否自动拍一张「出发档」 |
| `exit_with_game` | `false` | 游戏退出后是否连工具一起退出 |
| `hotkeys` | 见上 | 各动作的热键或候选键列表 |
| `steam_launch_into_world` | `true` | `--steam-launch` 模式下是否直接进入最近的世界 |
| `steam_exit_with_game` | `true` | `--steam-launch` 模式下游戏退出时是否连工具退出（避免时长虚高） |

---

## 7. 快照一致性

CDDA 的一次存档是**分段写入**的：角色档先落盘，地图、超图随后。若在中间拷贝，就会得到
“新角色、旧地图”——读回去表现为地图回溯、角色与地图不同步，甚至报
`xxx/yyy 处已有一个敌人`。所以工具在拷贝前后各做一次校验：

1. 拷贝前等整个世界目录连续安静 `snapshot_settle_seconds`；
2. 拷贝期间记录源目录指纹，结束后再算一次，要求完全一致；
3. 快照与源目录逐文件比对大小、时间戳；
4. 任何一步不通过就**整份丢弃并重做**（最多 `snapshot_consistent_attempts` 次），
仍不通过则报错退出，绝不把可疑档放进列表。

面板里的 `⚠未校验` 就是没通过校验的档；这类档默认根本不会被保留。

---

## 8. 恢复与故障处理

* **还原到一半崩溃 / 断电**：下次启动时工具会检查还原标记与 `*.restoring` 暂存目录并自动完成或回滚。
也可手动运行 `python cdda_snapshot.pyw --recover`。
* **工具起不来**：先看 `Cataclysm_shortcut/.cdda_snapshots/cdda_snapshot.log` 末尾。
* **热键没反应**：多半被其它软件占用，看面板底部的实际热键，或改用托盘。
* **找不到 pythonw**：确认 Python 已安装且勾选了 Add to PATH；重开一个命令行窗口再试。
* **杀毒软件拦截**：工具会发送模拟按键，个别安全软件会误报；把工具目录加入信任即可。
* **换了游戏版本**：整个 `Cataclysm_shortcut` 文件夹拷到新版本目录，快照与配置一起带走。

---

## 9. 已知限制

* 仅支持 Windows：依赖 Win32 `SendInput`、窗口查找与 NTFS 硬链接。
* 硬链接要求快照目录与存档目录在**同一磁盘卷**；跨卷时自动退化为完整拷贝（更占空间，功能正常）。

---

## 10. 文件清单

| 文件 | 作用 |
| - | - |
| `cdda_snapshot.pyw` | 主程序：快照/还原、全局热键、托盘、命令行。`.pyw` 表示用 pythonw 运行、无控制台窗口（文件名保持不变以兼容旧数据） |
| `panel.py` | 弹出式面板与自绘对话框（tkinter） |
| `winapi.py` | Win32 封装：模拟按键、窗口查找、托盘、单实例、硬链接 |
| `start_cataclysm_shortcut.bat` | 常规启动（后台、无控制台） |
| `watch_with_game.bat` | 常驻监听模式启动（`--autostart`），可放开机启动目录 |
| `create_shortcut.bat` | 生成 `Cataclysm_shortcut.lnk` 快捷方式 |
| `make_shortcut.py` | 上面那个批处理实际调用的建快捷方式脚本 |
| `README.md` | 本文档 |
| `LICENSE` | MIT 许可证 |
| `.gitattributes` | 统一换行（`.bat` 强制 CRLF，源码与文档 LF） |
| `.gitignore` | 排除运行数据（`.cdda_snapshots/`、`__pycache__/`、`*.lnk` 等） |

运行后还会生成 `.cdda_snapshots/`（配置、日志、快照），已加入 `.gitignore`。

---

## 11. 版本

**0.9** —— 首个公开发布版本：CDDA 与 CCB 通用
**0.9.1** —— 面板新增「快照文件夹占用 / 硬链接实占」统计行

## 12. 许可

[MIT](LICENSE)

