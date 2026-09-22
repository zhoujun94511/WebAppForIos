# -*- coding: utf-8 -*-
# config.py
import os

class Config:
    # Flask
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret")
    DEBUG = os.environ.get("FLASK_DEBUG", "1") == "1"
    HOST = os.environ.get("FLASK_HOST", "0.0.0.0")
    PORT = int(os.environ.get("FLASK_PORT", "5000"))

    # 路径
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, '..'))

    # 工具链随源码；日志、上传和下载的镜像放在 runtime/，不进入版本库
    IOS_HOME = os.path.join(PROJECT_ROOT, 'IOSPrechecker')
    RUNTIME_DIR = os.path.join(PROJECT_ROOT, "runtime")
    UPLOAD_FOLDER = os.path.join(RUNTIME_DIR, "uploads")
    LOG_DIR = os.path.join(RUNTIME_DIR, "logs")

    # 按功能分类的 uploads 子目录
    UPLOADS_ROOT = UPLOAD_FOLDER
    SCREENSHOTS_DIR = os.path.join(UPLOADS_ROOT, "screenshots")      # 截图目录
    RECORDINGS_DIR = os.path.join(UPLOADS_ROOT, "recordings")        # 录屏目录
    INSTANT_TRANSFER_DIR = os.path.join(UPLOADS_ROOT, "instant_transfer")  # 即时传输目录
    OTHER_UPLOADS_DIR = os.path.join(UPLOADS_ROOT, "other")          # 其他上传文件目录

    # go-ios 资源与镜像统一归档
    GOIOS_DIR = os.path.join(IOS_HOME, 'utils')
    GOIOS_EXECUTABLE_DIR = os.path.join(IOS_HOME, 'executable')
    # 本地解压或下载的镜像，不提交；运行时下载的个性化镜像写入 runtime/devimages
    BUNDLED_DEVIMAGES_DIR = os.path.join(IOS_HOME, 'devimages')
    DEVIMAGES_DIR = os.environ.get('DEVIMAGES_DIR', os.path.join(RUNTIME_DIR, 'devimages'))

    # 可选：若你想使用系统里已安装的 ios 可执行文件，直接设置此环境变量即可覆盖
    GOIOS_BIN_PATH = os.environ.get("GOIOS_BIN_PATH", "")
    
    # Flask 上传文件大小限制
    MAX_CONTENT_LENGTH = 512 * 1024 * 1024  # 512MB
    UPLOAD_MAX_SIZE_MB = 512  # 上传文件最大大小（MB）
    ALLOW_EXECUTABLES = True  # 是否允许可执行文件上传
    
    # 危险文件扩展名
    DANGEROUS_EXTENSIONS = {'.exe', '.bat', '.cmd', '.scr', '.pif', '.com', '.vbs', '.js', '.jar', '.sh'}

    # 可选：HTTPS 证书与私钥（若同时提供则启用 HTTPS）
    SSL_CERT_FILE = os.environ.get("SSL_CERT_FILE", "")
    SSL_KEY_FILE = os.environ.get("SSL_KEY_FILE", "")
