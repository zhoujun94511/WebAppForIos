# WebAppForIOS - iOS设备Web调试工具

基于 Python + Flask + go-ios 的跨平台 Web 工具，通过浏览器实现对 iPhone 设备的全面管理和调试功能。

## 🚀 项目概述

WebAppForIOS 是一个功能完整的 iOS 设备 Web 调试平台，提供设备管理、应用管理、系统日志、录屏截图、崩溃日志分析等核心功能。支持 iOS 17+ 的 Tunnel 模式，确保与最新 iOS 设备的兼容性。

## 项目架构
```
### 前端架构 HTML5 + CSS3 + JavaScript + Bootstrap + jQuery
web_function/                    # Flask Web 应用
├── app.py                      # Flask 主应用，API 路由定义
├── templates/                  # HTML 模板目录
│   └── index.html             # 主页面模板
└── static/                    # 静态资源目录
    ├── wcss/                  # CSS 样式文件
    │   ├── bootstrap.min.css  # Bootstrap 框架样式
    │   ├── style.css          # 自定义样式
    │   └── sweetalert2.min.css # SweetAlert2 弹窗样式
    ├── wjss/                  # JavaScript 文件
    │   ├── app_ios.js         # 主要应用逻辑
    │   ├── bootstrap.min.js   # Bootstrap 框架脚本
    │   ├── jquery.min.js      # jQuery 库
    │   └── sweetalert2.all.min.js # SweetAlert2 弹窗脚本
    └── wresource/             # 静态资源文件
        ├── background.jpg     # 背景图片
        ├── favicon.ico        # 网站图标
        └── wicon.ico          # 应用图标

### 后端架构 (Python + Flask)
backend_function/               # 后端功能模块
├── __init__.py               # 模块初始化文件
├── config.py                 # 配置文件，包含路径、端口等设置
├── goios_wrapper.py          # go-ios 命令封装，提供设备操作接口
├── tunnel_manager.py         # Tunnel 管理，处理设备连接隧道
├── ios_prechecker.py         # iOS 设备检查器，验证设备状态
└── common_utils.py           # 通用工具函数，包含数据处理、文件操作等

IOSPrechecker/                # iOS 工具和资源目录
├── executable/               # go-ios 可执行文件
│   └── win/                 # Windows 平台可执行文件
│       ├── ios.exe          # go-ios 主程序
│       └── selfIdentity.plist # 身份配置文件
├── utils/                   # go-ios 压缩包
│   ├── go-ios-linux.zip     # Linux 版本
│   ├── go-ios-mac.zip       # macOS 版本
│   └── go-ios-win.zip       # Windows 版本
├── devimages/               # 开发者镜像
│   ├── 16.6/               # iOS 16.6 开发者镜像
│   │   ├── DeveloperDiskImage.dmg
│   │   └── DeveloperDiskImage.dmg.signature
│   └── ddi-15F31d/         # iOS 15F31d 开发者镜像
│       ├── Restore/         # 恢复文件
│       │   ├── 022-16988-038.dmg
│       │   ├── BuildManifest.plist
│       │   └── Firmware/    # 固件文件
│       └── version.plist    # 版本信息
└── wintun/                  # Windows Tunnel 驱动
    ├── amd64/               # AMD64 架构驱动
    ├── arm/                 # ARM 架构驱动
    ├── arm64/               # ARM64 架构驱动
    └── x86/                 # x86 架构驱动

ios_downloads/                # 下载文件目录
└── crashes/                 # 崩溃日志文件

ios_logs/                    # 日志文件目录

requirements.txt             # Python 依赖包列表
```

## 📱 使用指南

### 设备连接
1. 开启开发者模式，
2. 使用 USB 线连接 iOS 设备
3. 在设备上选择"信任此电脑"
4. 点击"刷新设备"按钮
5. 从下拉列表中选择设备

## 📋 功能特性

### 🔧 设备管理
- **设备检测与连接**：自动检测已连接的 iOS 设备
- **设备信息获取**：获取设备型号、系统版本、序列号等详细信息
- **设备重启**：远程重启 iOS 设备
- **开发者模式检测**：自动检测并提示开发者模式状态
- **设备状态监控**：实时监控设备连接状态

### 📱 应用管理
- **应用列表获取**：获取设备上已安装的第三方应用
- **应用搜索**：支持按名称或 Bundle ID 搜索应用
- **应用启动/停止**：远程启动和停止应用
- **IPA 安装**：支持上传并安装 IPA 文件
- **运行中应用查看**：查看当前运行的应用进程

### 📸 截图与录屏
- **单次截图**：快速获取设备屏幕截图
- **实时录屏**：支持 MJPEG 流式录屏，可录制为 MP4 格式
- **录屏管理**：开始/停止录屏，自动保存录制文件
- **文件下载**：支持录屏文件的直接下载

### 📊 系统日志
- **实时日志流**：通过 SSE 实时获取系统日志
- **日志过滤**：支持按关键字和日志级别过滤
- **日志导出**：将日志保存为文件供下载
- **日志解析**：支持原始日志和解析后日志两种模式

### 🚨 崩溃日志管理
- **崩溃日志列表**：获取设备上的崩溃日志文件
- **日志搜索**：支持通配符和关键字搜索
- **日志导出**：批量导出崩溃日志文件
- **日志清理**：删除指定的崩溃日志文件

### ⚙️ 系统功能
- **电池信息**：获取详细的电池状态信息
- **磁盘空间**：查看设备存储空间使用情况
- **位置模拟**：模拟设备 GPS 位置
- **辅助功能**：管理 AssistiveTouch、VoiceOver、Zoom 等辅助功能
- **配置文件管理**：管理设备配置文件

### 🛠️ 调试功能
- **Debug 模式**：三连击版权文字激活调试功能
- **日志导出**：导出应用运行日志用于问题分析
- **进程清理**：清理遗留的 ios.exe 进程
- **自动清理**：应用启动/退出时自动清理资源



## 📦 安装与配置

### 系统要求
- **Python 3.8+**
- **Windows/macOS/Linux**
- **USB 驱动**：确保 iPhone 已"信任此电脑"
- **iOS 17+**：如需使用 Tunnel 功能

# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境
# Windows:
.\.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

## 🚀 快速启动

### 1. 基础启动
bash
# 启动 Web 服务
python web_function/app.py

### 2. 访问应用
- **本地访问**：http://localhost:5001
- **网络访问**：http://[本机IP]:5001
- **自动启动**：应用会自动打开默认浏览器

### 3. 首次使用
1. 连接 iOS 设备到电脑
2. 在设备上选择"信任此电脑"
3. 刷新设备列表
4. 选择设备开始使用

## 🔧 高级配置
### 日志配置
- **日志文件**：`ios_logs/ios_app.log`
- **日志轮转**：10MB 文件大小，保留 5 个备份
- **日志级别**：INFO 级别，包含详细的操作记录

### 文件清理
- **自动清理**：下载文件 30 分钟后自动删除
- **手动清理**：通过 Debug 功能手动清理进程
- **进程管理**：应用退出时自动清理遗留进程

## 🔍 故障排除

### 常见问题

#### 1. 设备连接失败
- 检查 USB 线连接
- 确认设备已"信任此电脑"
- 重启设备和应用

#### 2. Tunnel 启动失败
- **Windows**：确认 wintun.dll 已安装
- **macOS/Linux**：使用 sudo 权限运行
- 检查防火墙设置

#### 3. 截图/录屏失败
- 确认开发者模式已启用
- 检查开发者镜像是否正确挂载
- 重新连接设备

#### 4. ios.exe 进程累积
- 使用 Debug 功能清理进程
- 重启应用
- 检查任务管理器手动终止进程

### 日志分析
- 查看 `ios_logs/ios_app.log` 获取详细错误信息
- 使用 Debug 功能导出日志进行分析
- 检查 go-ios 命令输出

## 🛡️ 安全说明

### 文件安全
- 所有下载文件使用签名令牌保护
- 文件链接 1 小时内有效
- 自动清理过期文件

### 网络安全
- 支持 HTTPS 配置
- 本地网络访问控制
- 无敏感信息传输

### 进程安全
- 自动清理遗留进程
- 防止进程累积
- 安全的进程终止机制

## 📦 Windows .exe 打包

### 自动打包
项目提供了完整的 PyInstaller 打包配置，支持生成带任务栏图标的 Windows 可执行文件。

#### 快速打包
```bash
# 方法1: 使用批处理脚本（推荐）
双击运行 build.bat

# 方法2: 使用 Python 脚本
python build_exe.py
```

#### 手动打包
```bash
# 安装 PyInstaller
pip install pyinstaller

# 执行打包
pyinstaller --clean --noconfirm WebAppForIOS.spec
```

### 打包配置说明

#### 任务栏图标配置
- **图标文件**: `web_function/static/wresource/wicon.ico`
- **版本信息**: `version_info.txt` 包含应用程序元数据
- **控制台隐藏**: `console=False` 隐藏命令行窗口
- **应用程序类型**: 设置为 Windows 应用程序

#### 包含的文件
- Flask 模板和静态文件
- 后端功能模块
- iOS 工具和资源
- go-ios 可执行文件
- 开发者镜像文件
- Windows Tunnel 驱动

#### 生成的文件
- `dist/WebAppForIOS.exe` - 主程序文件
- `启动WebAppForIOS.bat` - 启动脚本

### 使用打包后的程序

#### 启动方式
1. **双击启动脚本**: `启动WebAppForIOS.bat`
2. **直接运行**: `dist/WebAppForIOS.exe`
3. **命令行启动**: `WebAppForIOS.exe`

#### 任务栏显示
- 程序运行时会自动在任务栏显示图标
- 图标使用项目自定义的 `wicon.ico`
- 鼠标悬停显示 "WebAppForIOS iOS设备调试工具"

#### 注意事项
- 首次运行可能需要管理员权限（用于安装 wintun.dll）
- 确保已连接 iOS 设备并信任此电脑
- 程序会自动打开浏览器访问 http://localhost:5001

## 📄 许可证

本项目采用 MIT 许可证，详见 LICENSE 文件。

## 🤝 贡献

欢迎提交 Issue 和 Pull Request 来改进项目。

## 📞 支持

如有问题或建议，请通过以下方式联系：
- 提交 GitHub Issue
- 查看项目 Wiki
- 参考代码注释和文档

---

**注意**：本工具仅用于开发和调试目的，请遵守相关法律法规和 Apple 开发者协议。