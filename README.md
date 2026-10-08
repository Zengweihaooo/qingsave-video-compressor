# 轻存 · QingSave Video Compressor

本地视频批量压缩工具，提供中文网页界面、按拍摄日期扫描与归档、每个任务独立的画质设置，以及 OSV 全景拼接实验功能。素材在本机处理。

## 功能

- 拖拽或选择视频，按天选择素材，双击日期批量加入。
- 视频与音频独立档位，入队时保存参数；支持硬件编码与 CPU 编码。
- 当前视频和整体队列进度，运行中添加任务、取消、重试和清除历史。
- 按 `MM.DD` 归档，时间戳命名，默认 `CMP` 标记。
- 成品校验及完成记录；支持验证后将原片送入回收站或永久删除。建议先用保留原片模式验证自己的素材。
- 已拼接 360° MP4 的全景信息保留；OSV 可经 OpenOSV 拼接后压缩。

## 快速开始

需要 Python 3.10+（含 tkinter）及 FFmpeg / ffprobe。将 FFmpeg 加入 PATH，或在项目 `bin` 目录放置对应平台的可执行文件。

Windows 双击 `启动-Windows.cmd`；macOS 在终端执行 `bash 启动-macOS.command`。也可手动运行：

```sh
python -m venv .venv
# 激活虚拟环境后：
python -m pip install -r requirements.txt
python web_app.py --port 3912
```

按终端显示的本地地址访问。仅用于可信本机环境，请勿通过代理暴露到公网。NVIDIA、Intel、Apple 编码能否启用取决于系统、驱动及 FFmpeg 构建；跨平台硬件组合尚未全部实测。

## OSV 可选依赖

普通 MP4 压缩不需要以下依赖。仓库不包含大型运行时或 DJI 软件。

### OpenOSV（已完成 Windows 实片流程验证）

从 [OpenOSV Releases](https://github.com/Kemerd/OpenOSV/releases) 获取适合平台的运行时。本项目验证版本为 v0.4.2。保留其配套文件，将 `osvtool.exe` 放在 `vendor/openosv/`，或设置环境变量 `QINGSAVE_OSVTOOL` 指向可执行文件。

支持 4K / 6K / 8K 等距柱状全景输出。Windows 临时路径需为英文路径，建议留足拼接中间片空间。OSV 拼接会重新编码，“保留原片质量”并不代表与 OSV 原始数据逐位无损。拼接接缝效果仍需按场景判断。

### DJI Studio（实验适配，尚未通过真实界面端到端验证）

基于 [exodustw/dji-osv2mp4](https://github.com/exodustw/dji-osv2mp4) 的 SikuliX 图像自动化，并非 DJI 官方 CLI 或 MCP。当前仅面向 Windows 英文界面和 25/30 fps；显示缩放或软件界面变更可能导致失败。

需要自行安装 DJI Studio，并准备：

- Java 17 JRE：解压到 `vendor/java/`，下层包含 `bin/java.exe`。
- SikuliX 2.0.5 Windows：`vendor/sikulixide-2.0.5.jar`。
- Jython standalone 2.7.3：`vendor/jython-standalone-2.7.3.jar`。

该路线需要独占 DJI Studio 新窗口。建议优先使用已验证的 OpenOSV 路线，DJI 适配仍需实机验证。

## 验证与限制

OpenOSV 已用 25 fps 的 OSV 实片验证拼接、压缩、全景元数据、完整解码校验及取消清理。DJI Studio 路线仅完成代码适配与依赖检查，不能视为可用性承诺。取消后的文件重试会从头编码，不支持视频内部断点续编码。

仓库不包含私人视频、素材扫描清单、历史任务、运行日志或校验记录。运行生成的 JSON 是防重与删除验证依据，不应随意清理。

## 第三方来源

`vendor/exporter.sikuli` 的脚本及界面模板源自 MIT 许可的 dji-osv2mp4，并做了超时、帧率与退出处理修改，许可见 `vendor/DJI-automation-LICENSE`。
OpenOSV 使用 Apache-2.0 许可，许可见 `vendor/OpenOSV-LICENSE`，其运行时需单独下载。其他依赖遵循各自许可。本项目未给原创代码指定开源许可证。
