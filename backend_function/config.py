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

    # 统一到项目根目录
    IOS_HOME = os.path.join(PROJECT_ROOT, 'IOSPrechecker')
    UPLOAD_FOLDER = os.path.join(PROJECT_ROOT, "ios_downloads")
    LOG_DIR = os.path.join(PROJECT_ROOT, "ios_logs")

    # go-ios 资源与镜像统一归档
    GOIOS_DIR = os.path.join(IOS_HOME, 'utils')                    # 放置 go-ios 压缩包 (go-ios-*.zip)
    GOIOS_EXECUTABLE_DIR = os.path.join(IOS_HOME, 'executable')    # 解压后的可执行文件存放处（避免与wintun/bin冲突）
    DEVIMAGES_DIR = os.environ.get('DEVIMAGES_DIR', os.path.join(IOS_HOME, 'devimages'))

    # 可选：若你想使用系统里已安装的 ios 可执行文件，直接设置此环境变量即可覆盖
    GOIOS_BIN_PATH = os.environ.get("GOIOS_BIN_PATH", "")
    
    # Flask 上传文件大小限制
    MAX_CONTENT_LENGTH = 512 * 1024 * 1024  # 512MB

    # 可选：HTTPS 证书与私钥（若同时提供则启用 HTTPS）
    SSL_CERT_FILE = os.environ.get("SSL_CERT_FILE", "")
    SSL_KEY_FILE = os.environ.get("SSL_KEY_FILE", "")
