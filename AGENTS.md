# 轻存 · Agent 交接说明

给接手这个仓库的 Agent（Claude Code、Codex 等）看的。先读完这一页，再动代码。

## 这是什么

轻存（QingSave）是用户自己用的本地视频工具，处理 DJI Action / Osmo 360 运动相机素材。三大功能：

1. **压缩**：H.265 批量压缩，按拍摄日期归档到 `MM.DD` 文件夹，命名 `YYYYMMDD_HHMMSS_CMP_001.mp4`，校验通过后才处理原片。
2. **SD 卡导入（macOS 与 Windows）**：插卡自动检测（macOS 用 `diskutil`，Windows 按盘符类型：可移动=卡，固定且非系统盘=目标），严格逐个文件「复制 → 读回校验 → 删卡上原片」，复制到固态硬盘（优先 `/Volumes/Data`，满了换其他固定盘），命名 `YYYYMMDD_HHMMSS_ORG_001.mp4`，SHA-256 读回校验后才删卡上原片。
3. **素材日历与整理**（`/library` 页面）：扫描整块硬盘，识别原片 / 压缩版、时间冲突、重复文件、原片+压缩版并存，给原片加 `_ORG` 后缀（可撤销），日历视图，以及在硬盘根目录生成离线网页 `轻存素材日历.html`。

用户说中文，用语音输入，经常把「轻存」打成「清存 / 清纯」。回复用中文。

## 运行

- 依赖：Python 3.10+（带 tkinter）、FFmpeg 与 ffprobe 在 PATH 里（或放在 `bin/`）、`pip install -r requirements.txt`。
- macOS：`bash 启动-macOS.command`；新电脑可 `bash tools/install-mac-app.sh` 生成 `~/Applications/轻存.app`。
- Windows：双击 `启动-Windows.cmd`（需自己装 Python），或用免安装包：在 Mac 上 `python tools/build-windows.py <下载目录> <输出.zip>` 打包便携 Python 3.13 + FFmpeg + Send2Trash，解压后双击 `启动轻存.cmd`，不需要管理员权限。便携 Python 没有 tkinter，所以 Windows 上的文件选择用 PowerShell 系统对话框，`compressor.py` 里 tkinter 是可选导入。
- 手动：`python web_app.py --port 3912`，浏览器开 `http://127.0.0.1:3912/`，素材日历在 `/library`。
- 用户这台 Mac 的 Dock 里的 `~/Applications/轻存.app` 是原生包装（Mach-O，读取 Info.plist 的 `QingSaveRoot` 指向本仓库），它启动 `web_app.py --no-browser --port 0`。改完 Python 后要退出并重新打开 app 才生效；**重启前先确认没有任务在跑**：`/health` 的 `busy` 为 false，且 `/sd/state` 的 `running` 为 false（POST 需要页面里的 token）。

## 常驻、日志与断点继续

- `launch.py`：后台启动服务（Windows 用 pythonw，无窗口；端口优先 39390），已在运行就只重新打开页面；`--quit` 安全退出。服务把端口和 token 写在 `<数据目录>/instance.json`。关浏览器不影响服务。Windows 包里的「启动轻存 / 重新打开轻存页面 / 退出轻存.cmd」都是调用它。
- 数据目录（`applog.DATA_DIR`）：Windows `%APPDATA%\QingSave`，macOS `~/Library/Application Support/QingSave`。里面有 `logs/YYYY-MM-DD.log`（所有消息）、`logs/events-YYYY-MM.jsonl`（每个压缩 / 导入任务的结果、编码器、大小、耗时）、`queue.json`（压缩队列）、`sd-imports.jsonl`（导入记录）。
- 压缩队列每 1.5 秒存盘。退出（`/quit`）时不取消排队任务，正在压的那个标为 `interrupted`；下次启动全部未完成任务显示为「待继续」，用户点「继续处理」（`/queue/continue`）才开始，已完成的按 `.dji.json` 跳过，被打断的从头压。编码本身不支持从中间续。
- SD 导入断点续拷：复制写到目标盘 `<盘根>/.qingsave/partials/<key>.part`，每 64 MB 记录一次分块 SHA-256（`.json`）。中断后再次导入时先校验已有分块，从第一个不对的分块接着复制；读回时逐块比对。校验不一致才删掉 part，取消 / 拔卡 / 退出都保留。
- 编码器：`/hardware` 启动时试编码检测可用编码器和显卡型号（Windows 用 nvidia-smi），页面显示「自动选择」会用哪个。压缩进度里有 `engine_name/engine_kind/engine_skipped`，显卡没用上会写原因。NVIDIA 路线同时用 `-hwaccel cuda` 解码；完整播放检查仍用 CPU 解码（为了校验严格）。
- `tools/selftest.py` 会把 applog 指到临时目录，不会污染真实日志。

## 验证

```
python tools/selftest.py
```

会在临时目录里造一块假素材盘（各种混乱命名、重复、冲突、坏文件）和一张假 SD 卡，检查所有规则，最后输出「全部通过」。改了 `library.py` 或 `sd_import.py` 必须跑一遍。真实素材只做只读扫描验证。

## 文件

| 文件 | 作用 |
|---|---|
| `web_app.py` | 本机 HTTP 服务（只监听 127.0.0.1，校验 Host 头和 `X-App-Token`），压缩队列，路由 `/sd/*`、`/library/*` |
| `compressor.py` | FFmpeg 调用、码率规则（`encoding_args`、`QUALITY`）、校验、删除原片 |
| `sd_import.py` | SD 卡检测（`diskutil`）、分盘计划、复制+读回校验、损坏视频处理 |
| `library.py` | 素材库扫描：日期识别、原片判断、冲突检测、`_ORG` 改名与撤销、导出网页 |
| `界面.html` | 压缩主界面（含 SD 卡面板） |
| `素材日历.html` | 素材日历页面；在轻存里是实时模式，导出到硬盘根目录时是内嵌数据的静态模式（`/*__MODE__*/` 占位符） |
| `osv_pipeline.py`、`dji_studio_export.py`、`spherical.py` | OSV 全景拼接（实验） |
| `tools/selftest.py` | 端到端自测 |

## 规则（不要破坏）

- **删除前必须校验**：压缩删原片要求完整解码 + SHA-256；SD 卡删原片要求复制时算的 SHA-256 与绕过缓存读回的一致，并且 FFprobe 能读出视频。
- **坏视频不自动删**：读不出的视频把同名 LRF 存到固态硬盘（`<名字>_LRF.mp4`），等用户在页面确认后才清卡。用户倾向保留一份损坏原片以后修复（untrunc）。
- **导入去重**：`~/Library/Application Support/QingSave/sd-imports.jsonl` 按「卡内相对路径 + 大小 + 修改时间」识别，不要再加卷 UUID（有的卡每次插入 UUID 都变，曾导致重复复制）。
- **改名可撤销**：`library.mark_originals` 每次改名写 `<根目录>/.qingsave/rename-log.jsonl`，同时更新 `*.dji.json` 压缩记录里的 `source`，`undo_last_mark` 反向恢复。
- **路径一律用相对根目录**：同一块盘在 Windows（`H:\`）和 macOS（`/Volumes/...`）上结果一致；`.dji.json` 里的绝对路径是压缩时那台机器的，只按文件名匹配。
- **平台**：所有模块在 Windows 便携 Python（无 tkinter）下必须能 import。不要在模块顶层用平台专属库；Linux 上 SD 面板隐藏（`SUPPORTED=False`）。
- **编号一路沿用**：相机 `CAM_…_0002_D` → 导入 `…_ORG_002` → 压缩 `…_CMP_002`（`compressor.clip_number`）。没有编号的文件才用页面上的起始序号依次编；同名冲突时导入改用下一个空号，压缩加 `-2`。
- **一个接一个**：用户要求移动和压缩都严格逐个文件处理（处理一个、校验一个、删一个），不要改回多张卡并行复制。
- **码率**：Apple VideoToolbox 保持按目标码率（实测它的 `-q:v` 没有优势）；CPU 用 x265 CRF 28/25/22，NVIDIA 用 CQ 30/27/24 + lookahead + AQ，两者都以档位码率 1.5 倍为峰值上限；NVIDIA 参数被驱动拒绝时退回普通 VBR。

## 素材库识别规则（library.py）

- 拍摄时间优先级：文件名里的日期时间（`CAM_20261015160907_…`、`20261015_160907_…`、`2026-01-13_23-24-57`）> 文件名里的日期 > 文件夹日期 > 视频元数据 `creation_time` > 文件修改时间（会标注「推断」）。
- 文件夹：`2025` / `2025年` / `25年`（年），`2025.10` / `2025年10月` / `2026.08Done`（月），`2025年10月21日 xx` / `2025-10-21 xx` / `20251021`（日），`12.5 第一次见 rebelo` / `10月21日` / `03.14-学校`（月.日，年份从上级文件夹继承，没有就按文件时间推断），`21日`（在月文件夹里）。日期后面的文字是当天描述。
- 原片 / 压缩版：文件名带 `CMP` / `compressed` → 压缩版；带 `ORG` / `original` → 已标记原片；`_LRF` → 代理；出现在 `.dji.json` 的 output → 压缩版；否则按每像素每帧码率（HEVC ≥0.20 / H.264 ≥0.23 为原片，≤0.18 / ≤0.20 为压缩过），OSV 一律原片。
- 冲突：(a) 同一秒开始，或时间段重叠超过 2 秒（相机分段首尾相接不算）；(b) 大小相同且开头/中间/结尾各 1 MB 的哈希相同（页面可点「完整校验」读全文件）；(c) 压缩记录配对，或同一拍摄时间 + 时长一致的原片/压缩版。
- 缓存：`<根目录>/.qingsave/library-cache.json`，按「相对路径|大小|mtime_ns」缓存 FFprobe 结果。

## 当前状态与待办

- 用户 24–26 年的素材盘接在另一台电脑（很可能是 Windows + RTX 3080）上，还没在真实大盘上扫过。接手后：在那台电脑拉代码 → 跑 `tools/selftest.py` → 打开轻存 `/library` 扫那块盘 → 把扫描摘要和可疑项告诉用户，**改名前让用户在页面上确认**。
- NVIDIA 新参数（cq / AQ / lookahead）还没在 3080 上实测；压一段后看日志是否显示「画质值 27」。
- 可选后续：坏视频修复（untrunc，需同相机好文件做参照）。
