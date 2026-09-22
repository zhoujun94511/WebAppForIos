<div align="center">

<img src="resources/branding/logo.png" width="64" height="64" alt="WebAppForIOS logo">

# WebAppForIOS

**面向测试与调试的 iOS 设备 Web 控制台**

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://python.org)
[![Flask](https://img.shields.io/badge/Web-Flask%203-000000.svg)](https://flask.palletsprojects.com/)
[![go-ios](https://img.shields.io/badge/Device-go--ios-111111.svg)](https://github.com/danielpaulus/go-ios)

[中文](README.md) · [English](README_en.md)

**[开发者镜像](docs/developer-image.md)** · **[使用指南](web_function/templates/ios_introduce_guide_index.html)**

</div>

WebAppForIOS 供研发人员通过浏览器操作已用 USB 连接的 iPhone / iPad，支持在 Windows、Linux、macOS 三端运行。在页面上即可查看设备、管理应用、截图录屏，并采集系统日志与崩溃日志。

同一局域网内的浏览器均可打开控制台。启动后访问 `http://<本机IP>:5001`，页眉 **使用指南** 对应 `/guide`。

![控制台界面](resources/interface.png)

---

## 产品亮点

* **浏览器即控制台** — 连接、刷新和日常操作都在网页完成，无需安装 Xcode 或额外桌面客户端。
* **覆盖真机调试主路径** — 设备信息、应用安装与启停、截图、录屏、系统日志、崩溃日志和描述文件集中在同一页面。
* **iOS 17+ 可直接接入** — Windows 使用 userspace tunnel 与 wintun；挂载镜像前按 `ApChipID` 与 `ApBoardID` 匹配，避免旧镜像导致 `findIdentity` 失败。
* **局域网即时传输** — 页面内收发文本与文件，并生成二维码，便于同一网络上的手机打开控制台传文件。
* **镜像按身份获取** — 本地已有匹配镜像则直接使用；没有匹配项时，从 [DeveloperDiskImage](https://github.com/doronz88/DeveloperDiskImage) 的 `main` 拉取当前个性化镜像，确认清单含本机身份后再下载载荷。

---

## 快速开始

**环境：** Python 3.10+ · Windows 10 / macOS / Linux · 已开启开发者模式并信任本机的 USB 真机。

### 1. 安装依赖

```powershell
# Windows
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
# Linux / macOS
python3.12 -m venv .venv
./.venv/bin/python -m pip install -U pip
./.venv/bin/python -m pip install -r requirements.txt
```

可选兜底（读取芯片身份、镜像自动挂载）：`pip install "pymobiledevice3>=4.0.0"`。说明见 [开发者镜像](docs/developer-image.md)。

### 2. 启动控制台

```powershell
# Windows
.\.venv\Scripts\python.exe web_function\app.py
```

```bash
# Linux / macOS
./.venv/bin/python web_function/app.py
```

服务监听本机局域网地址的 **5001** 端口，并尝试打开浏览器。健康检查：`GET /health`。日志：`runtime/logs/ios_app.log`（首次运行会创建 `runtime/`）。

### 3. 连接设备并开始使用

1. 在设备上开启开发者模式（设置 → 隐私与安全性 → 开发者模式），并信任此电脑。
2. 用数据线连接，在页面点击 **刷新设备**，选择列表中的设备。
3. 按页面分区操作：查看设备信息、管理应用、截图或录屏、查看日志，或使用即时传输。
4. 不确定按钮含义时，打开页眉 **使用指南**（`/guide`）。

离线检查镜像匹配逻辑：

```bash
python -m unittest tests.test_ddi_manager
```

---

## 能力范围

| 区域 | 能力 | 说明 |
|:-----|:-----|:-----|
| 设备 | 列表、详情、重启、开发者模式 | 详情含电池与磁盘；辅助功能含 AssistiveTouch、VoiceOver、Zoom |
| 应用 | 已安装列表、运行中进程、启动 / 停止、安装 IPA | 可按名称或 Bundle ID 搜索 |
| 画面 | 单张截图、MJPEG 录屏 | 截图写入 `runtime/uploads/screenshots/`，录屏写入 `runtime/uploads/recordings/` |
| 日志 | 系统日志实时流、Crash `.ips` | 可按关键字与级别筛选；Crash 支持导出与删除 |
| 配置 | 描述文件列表与移除 | 通过 go-ios `profile` |
| 传输 | 即时文本与文件、二维码 | WebSocket；上传上限见运行配置（默认 512MB） |
| 连接 | Tunnel、个性化开发者镜像、端口转发 | iOS 17+ 需要 tunnel 与匹配的开发者镜像 |

---

## 核心能力

**设备与应用** — 刷新已连接设备，查看型号、系统版本、电池与磁盘，切换辅助功能，安装 IPA，启动或结束应用。

**截图与录屏** — 单张截图走快速检查；录屏在 tunnel 就绪后输出 MJPEG，并在服务端保存为 MP4。

**日志与描述文件** — 系统日志可筛选后下载；崩溃日志支持通配搜索、导出和删除；描述文件可列出并移除。

**即时传输** — 在控制台发送文本和文件。二维码指向本机服务，同一网络上的手机扫码即可打开页面传文件。

**开发者镜像** — iOS 17+ 按芯片身份匹配 `Restore/BuildManifest.plist`。本地优先使用 `runtime/devimages`；无匹配且可访问 GitHub 时，自动补齐当前个性化镜像。步骤与诊断见 [docs/developer-image.md](docs/developer-image.md)。

---

## 目录结构

```
web_function/           Web 入口、模板与静态资源（app.py 监听 5001）
backend_function/       go-ios 封装、tunnel、开发者镜像与 API
IOSPrechecker/
  utils/                go-ios 压缩包（随仓库提交）
  wintun/               Windows tunnel 驱动（随仓库提交）
  executable/           运行时从 utils 解压（不提交）
  devimages/            本机镜像目录（不提交）
runtime/                日志、上传、下载的镜像（不提交）
tests/                  开发者镜像匹配单测
docs/                   开发者镜像说明
```

路由定义在 `web_function/app.py`，业务实现在 `backend_function/route_handlers.py` 与 `api_handlers.py`。

---

## 文档

| 文档 | 适合谁 | 说明 |
|:-----|:-------|:-----|
| 本文 | 新用户 | 安装、启动、能力范围 |
| [docs/developer-image.md](docs/developer-image.md) | 排障 | 个性化镜像目录、挂载顺序、诊断接口 |
| 控制台使用指南 | 操作时 | 启动后打开 `/guide` |
| [README_en.md](README_en.md) | English | 英文说明 |

---

## 常见问题

**列表里没有设备** — 确认数据线和「信任此电脑」，然后点刷新。服务日志在 `runtime/logs/ios_app.log`。

**开发者模式检测或录屏返回 500** — 多为 tunnel 或开发者镜像未就绪。在日志中查看 `ApChipId` / `ApBoardId`，并阅读 [开发者镜像说明](docs/developer-image.md)。

**Tunnel 启动失败** — Windows 需要仓库中的 `IOSPrechecker/wintun`。若日志提示端口 `60105` 被占用，先结束残留的 `ios.exe` 再启动。

**截图成功但录屏失败** — 单张截图可以在镜像未挂载时完成；录屏会做完整检查，需要 tunnel、匹配的开发者镜像，以及设备上的开发者模式。

---

## 许可

本项目采用 [MIT License](LICENSE)。

## 致谢

本项目的设备侧能力基于开源项目 [go-ios](https://github.com/danielpaulus/go-ios)，向 Daniel Paulus 及 go-ios 社区维护者致谢。

---

本工具用于开发与测试调试。使用时请遵守所在地法律与 Apple 开发者协议。
