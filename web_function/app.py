import io
import os
import sys
import time
import signal
import atexit
from typing import Literal, cast
from qrcode.main import QRCode
from qrcode.constants import ERROR_CORRECT_L
import logging
import logging.handlers
import webbrowser
import threading
from concurrent_log_handler import ConcurrentRotatingFileHandler
from flask_socketio import SocketIO
from flask import Flask, request, jsonify, send_from_directory, send_file
from backend_function.common_utils import (
    get_local_ip,
    create_required_directories,
    cleanup_old_tunnel_processes,
    cleanup_all_ios_processes,
)
from backend_function.config import Config
from backend_function.goios_wrapper import GoIOSManager
from backend_function.ios_prechecker import IOSPrechecker
from backend_function.tunnel_manager import TunnelManager
from backend_function.api_handlers import APIHandlers
from backend_function.route_handlers import RouteHandlers
from backend_function.upload_utils import handle_upload, get_metrics_snapshot
from backend_function.websocket_handler import (
    handle_connect, handle_disconnect, handle_join_room, handle_leave_room,
    handle_start_transfer, handle_file_uploaded, handle_transfer_cancel,
    handle_get_transfer_status, handle_get_room_info, handle_ping, handle_message
)

app = Flask(__name__)

# ===== 配置与日志 =====
app.config.from_object(Config)
app.config['ENV'] = 'development'
app.config['DEBUG'] = True

# ===== SocketIO 配置 =====
app.config.update({
    'GOIOS_DIR': os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'IOSPrechecker', 'utils'),
    'GOIOS_EXECUTABLE_DIR': os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'IOSPrechecker', 'executable'),
    'GOIOS_BIN_PATH': os.environ.get("GOIOS_BIN_PATH", ""),
    'ALLOW_EXECUTABLES': True,
    'MAX_CONTENT_LENGTH': 200 * 1024 * 1024,  # 200MB
    'FRONTEND_UPLOAD_MAX_MB': 200,
})

# 确保上传目录存在
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# 初始化 SocketIO（日志级别将在 setup_logging 中统一设置）
socketio = SocketIO(app, cors_allowed_origins="*", logger=False, engineio_logger=False)

os.makedirs(app.config["LOG_DIR"], exist_ok=True)
log_path = os.path.join(app.config["LOG_DIR"], "ios_app.log")

# 改进的日志配置 - 使用线程安全的日志轮转
def setup_logging():
    """
    设置线程安全的日志配置，解决 Windows 多线程环境下的文件锁定问题。
    本函数是幂等的，确保在 Flask reloader 场景下只初始化一次。
    """
    # 幂等性保护：检查是否已经配置过
    root_logger = logging.getLogger()
    if getattr(root_logger, "_configured_by_app", False):
        # 已配置，直接返回现有日志文件路径
        return os.path.join(app.config["LOG_DIR"], "ios_app.log")
    
    # 使用固定的日志文件路径
    fixed_log_path = os.path.join(app.config["LOG_DIR"], "ios_app.log")
    
    # 使用 ConcurrentRotatingFileHandler 解决 Windows 多线程文件锁定问题
    handler = ConcurrentRotatingFileHandler(
        fixed_log_path, 
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,  # 保留5个备份文件
        encoding="utf-8",
        use_gzip=False  # 不压缩备份文件，保持与原有行为一致
    )
    
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s - %(message)s")
    handler.setFormatter(fmt)
    
    # 配置根日志记录器
    root_logger.setLevel(logging.INFO)
    
    # 清除现有的文件处理器，避免重复
    for h in root_logger.handlers[:]:
        if isinstance(h, (logging.FileHandler, logging.handlers.RotatingFileHandler, 
                         logging.handlers.TimedRotatingFileHandler)):
            # 兼容检查所有可能的文件处理器类型
            root_logger.removeHandler(h)
            try:
                h.close()
            except (OSError, IOError, AttributeError, RuntimeError):
                # 忽略关闭文件 handler 时可能出现的文件操作、属性或运行时错误
                pass
    
    # 添加新的处理器
    root_logger.addHandler(handler)
    
    # 配置应用日志记录器
    app.logger.setLevel(logging.INFO)
    app.logger.propagate = True
    
    # 设置 engineio 和 socketio 的日志级别为 WARNING，减少高频心跳日志污染
    logging.getLogger('engineio').setLevel(logging.WARNING)
    logging.getLogger('socketio').setLevel(logging.WARNING)
    
    # 标记已配置，确保幂等性
    root_logger._configured_by_app = True
    
    return fixed_log_path

# 设置日志（幂等函数，确保只初始化一次）
log_file_path = setup_logging()

# 注意：不再需要手动清理日志文件
# ConcurrentRotatingFileHandler 已内建日志轮转和备份数量控制（backupCount=5）
# 日志生命周期完全由 handler 管理，避免职责冲突和竞态条件

# ===== 初始化 go-ios 管理器与检查器 =====
goios = GoIOSManager(
    goios_root=app.config["GOIOS_DIR"],
    bin_dir=app.config["GOIOS_EXECUTABLE_DIR"],
    bin_path_override=app.config["GOIOS_BIN_PATH"],
)
tunnel = TunnelManager(goios)
prechecker = IOSPrechecker(goios, tunnel)

# === 创建必要目录 ===
create_required_directories(app.config, app.logger)

# === Windows tunnel 依赖检查（一次） ===
if os.name == 'nt' and hasattr(tunnel, 'check_windows_wintun'):
    wintun_ok, wintun_msg = tunnel.check_windows_wintun()
    if wintun_ok:
        app.logger.debug("Windows tunnel 依赖检查: %s", wintun_msg)
    else:
        app.logger.warning("Windows tunnel 依赖检查失败：%s", wintun_msg)

# === 清理旧的 tunnel 进程 ===
cleanup_old_tunnel_processes(tunnel, app.logger)

# === 清理遗留的 ios.exe 进程 ===
cleanup_all_ios_processes(app.logger)

# ===== 初始化处理器 =====
api_handlers = APIHandlers(app, goios, tunnel, prechecker)
route_handlers = RouteHandlers(app, api_handlers)

# ===== 应用停止时清理逻辑 =====
def cleanup_tunnel():
    """应用停止时清理隧道连接和遗留进程"""
    try:
        app.logger.debug("应用停止，清理隧道连接...")
        tunnel.stop()
        app.logger.debug("隧道清理完成")
    except (AttributeError, OSError) as exc:
        app.logger.warning("隧道清理失败: %s", exc)
    
    try:
        app.logger.debug("应用停止，清理遗留的ios.exe进程...")
        cleanup_all_ios_processes(app.logger)
        app.logger.debug("ios.exe进程清理完成")
    except Exception as exc:
        app.logger.warning("ios.exe进程清理失败: %s", exc)

def signal_handler(signum, _frame):
    """信号处理器"""
    app.logger.debug("接收到停止信号 %s，开始清理...", signum)
    cleanup_tunnel()
    sys.exit(0)

# 注册清理函数
atexit.register(cleanup_tunnel)
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ===== WebSocket 事件处理器 =====
@socketio.on('connect')
def on_connect():
    handle_connect()

@socketio.on('disconnect')
def on_disconnect():
    handle_disconnect()

@socketio.on('join_room')
def on_join_room(data):
    handle_join_room(data)

@socketio.on('leave_room')
def on_leave_room(data):
    handle_leave_room(data)

@socketio.on('start_transfer')
def on_start_transfer(data):
    handle_start_transfer(data)

@socketio.on('file_uploaded')
def on_file_uploaded(data):
    handle_file_uploaded(data)

@socketio.on('transfer_cancel')
def on_transfer_cancel(data):
    handle_transfer_cancel(data)

@socketio.on('get_transfer_status')
def on_get_transfer_status(data):
    handle_get_transfer_status(data)

@socketio.on('get_room_info')
def on_get_room_info(data):
    handle_get_room_info(data)

@socketio.on('message')
def on_message(data):
    handle_message(data)

@socketio.on('ping')
def on_ping():
    handle_ping()

# ===== 即时传输相关路由 =====
@app.route("/api/instant_upload", methods=["POST"])
def api_instant_upload():
    """即时传输文件上传"""
    return handle_upload(request)

@app.route("/api/instant_download/<path:filename>")
def api_instant_download(filename):
    """即时传输文件下载"""
    try:
        # 使用即时传输专用目录
        instant_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'instant_transfer')
        app.logger.info(f"Instant download request: filename={filename}, instant_dir={instant_dir}")
        
        # 检查文件是否存在
        full_path = os.path.join(instant_dir, filename)
        if not os.path.exists(full_path):
            app.logger.warning(f"File not found: {full_path}")
            return jsonify({"error": "文件不存在"}), 404
            
        return send_from_directory(instant_dir, filename)
    except Exception as e:
        app.logger.error(f"Download error: {e}")
        return jsonify({"error": "下载失败"}), 500

@app.route("/api/instant_metrics")
def api_instant_metrics():
    """获取传输指标"""
    return jsonify(get_metrics_snapshot())

@app.route("/api/instant_qr")
def api_instant_qr():
    """生成即时传输二维码"""
    url = request.url_root
    app.logger.info(f'Generated QR code for URL: {url}')

    qr = QRCode(
        version=1,
        error_correction=cast(Literal[0, 1, 2, 3], ERROR_CORRECT_L),
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill='black', back_color='white')

    img_io = io.BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)
    return send_file(img_io, mimetype='image/png')

@app.route("/api/instant_config")
def api_instant_config():
    """获取即时传输配置 - 内网环境允许可执行文件"""
    from backend_function.upload_utils import ALLOWED_EXTENSIONS
    from backend_function.config import Config
    
    return {
        'upload': {
            'maxSizeMB': Config.UPLOAD_MAX_SIZE_MB,
            'allowExecutables': Config.ALLOW_EXECUTABLES,  # 内网环境默认允许
            'chunkSizeMB': 1,
            'allowedExtensions': sorted(list(ALLOWED_EXTENSIONS)),
            'dangerousExtensions': sorted(list(Config.DANGEROUS_EXTENSIONS)),
        }
    }, 200

# ===== 路由定义 =====
@app.route("/")
def index():
    return route_handlers.index()

@app.route("/favicon.ico")
def favicon():
    return route_handlers.favicon()

@app.route("/guide")
def guide():
    """引导文档页面"""
    return route_handlers.guide()

@app.route("/health")
def health():
    """健康检查"""
    return route_handlers.health()

# ---------- 设备管理 ----------
@app.route("/api/devices")
def api_devices():
    return route_handlers.api_devices()

@app.route("/api/device_info")
def api_device_info():
    return route_handlers.api_device_info()

@app.route("/api/screenshot")
def api_screenshot():
    return route_handlers.api_screenshot()

# ---------- 应用管理 ----------
@app.route("/api/apps")
def api_apps():
    return route_handlers.api_apps()

@app.route("/api/install", methods=["POST"])
def api_install():
    return route_handlers.api_install()

@app.route("/api/launch", methods=["POST"])
def api_launch():
    return route_handlers.api_launch()

@app.route("/api/kill", methods=["POST"])
def api_kill():
    return route_handlers.api_kill()

# ---------- 设备重启 ----------
@app.route("/api/reboot", methods=["POST"])
def api_reboot():
    return route_handlers.api_reboot()

# ---------- 电量 ----------
@app.route("/api/battery")
def api_battery():
    return route_handlers.api_battery()

@app.route("/api/battery/detail")
def api_battery_detail():
    return route_handlers.api_battery_detail()

@app.route("/api/diskspace")
def api_diskspace():
    return route_handlers.api_diskspace()

@app.route("/api/diskspace/detail")
def api_diskspace_detail():
    return route_handlers.api_diskspace_detail()

# ---------- 开发者镜像 ----------
@app.route("/api/image/auto", methods=["POST"])
def api_image_auto():
    return route_handlers.api_image_auto()

# ---------- 设备状态 ----------
@app.route("/api/devicestate/list")
def api_devicestate_list():
    return route_handlers.api_devicestate_list()

@app.route("/api/devicestate/enable", methods=["POST"])
def api_devicestate_enable():
    return route_handlers.api_devicestate_enable()

# ---------- 模拟位置 ----------
@app.route("/api/setlocation", methods=["POST"])
def api_setlocation():
    return route_handlers.api_setlocation()

# ---------- 截屏流 ----------
@app.route("/api/screenshot/stream/start", methods=["POST"])
def api_ss_start():
    return route_handlers.api_ss_start()

@app.route("/api/screenshot/stream/stop", methods=["POST"])
def api_ss_stop():
    return route_handlers.api_ss_stop()

# ---------- 端口转发 ----------
@app.route("/api/forward/start", methods=["POST"])
def api_forward_start():
    return route_handlers.api_forward_start()

@app.route("/api/forward/stop", methods=["POST"])
def api_forward_stop():
    return route_handlers.api_forward_stop()

# ---------- 文件下载 ----------
@app.route("/api/download/<path:fname>")
def api_download_file(fname: str):
    return route_handlers.api_download_file(fname)

@app.route("/api/download_secure/<token>")
def api_download_secure(token: str):
    return route_handlers.api_download_secure(token)

# ---------- 运行中应用（基于 ps --apps） ----------
@app.route("/api/apps/running")
def api_apps_running():
    return route_handlers.api_apps_running()

# ---------- Crash 日志 ----------
@app.route("/api/crash/ls")
def api_crash_ls():
    return route_handlers.api_crash_ls()

@app.route("/api/crash/cp", methods=["POST"])
def api_crash_cp():
    return route_handlers.api_crash_cp()

@app.route("/api/crash/rm", methods=["POST"])
def api_crash_rm():
    return route_handlers.api_crash_rm()

# ---------- 配置文件管理 ----------
@app.route("/api/profile/list")
def api_profile_list():
    return route_handlers.api_profile_list()

@app.route("/api/profile/remove", methods=["POST"])
def api_profile_remove():
    return route_handlers.api_profile_remove()

# ---------- 开发者模式 ----------
@app.route("/api/devmode/get")
def api_devmode_get():
    return route_handlers.api_devmode_get()

@app.route("/api/devmode/check")
def api_devmode_check():
    return route_handlers.api_devmode_check()

@app.route("/api/devmode/enable", methods=["POST"])
def api_devmode_enable():
    return route_handlers.api_devmode_enable()

# ---------- 辅助功能 ----------
@app.route("/api/assistive/<feature>/<action>", methods=["POST"]) 
def api_assistive(feature: str, action: str):
    return route_handlers.api_assistive(feature, action)

# ---------- 设备事件（SSE） ----------
@app.route("/api/devices/events")
def api_devices_events():
    return route_handlers.api_devices_events()

# ---------- 系统日志（启动/停止，保存为文件供下载） ----------
@app.route("/api/syslog/start", methods=["POST"])
def api_syslog_start():
    return route_handlers.api_syslog_start()

@app.route("/api/syslog/stream")
def api_syslog_stream():
    return route_handlers.api_syslog_stream()

@app.route("/api/syslog/stop", methods=["POST"])
def api_syslog_stop():
    return route_handlers.api_syslog_stop()

# ========== Debug功能 ==========
@app.route("/api/debug/cleanup-processes", methods=["POST"])
def api_debug_cleanup_processes():
    """手动清理所有ios.exe进程"""
    return route_handlers.api_debug_cleanup_processes()

@app.route("/api/debug/export-logs", methods=["POST"])
def api_debug_export_logs():
    """导出应用日志文件用于调试分析"""
    return route_handlers.api_debug_export_logs()

# ===== 自启动浏览器 =====
use_local_ip = get_local_ip()
def open_browser():
    time.sleep(1)
    cert = app.config.get("SSL_CERT_FILE")
    key = app.config.get("SSL_KEY_FILE")
    scheme = "https" if (cert and key and os.path.exists(cert) and os.path.exists(key)) else "http"
    webbrowser.open_new_tab(f'{scheme}://{use_local_ip}:5001')

if __name__ == '__main__':
    # 使用环境变量防止debug模式重复打开浏览器
    if not os.environ.get('BROWSER_OPENED'):
        os.environ['BROWSER_OPENED'] = '1'
        threading.Thread(target=open_browser, daemon=True).start()
    app.logger.info("Web 控制台启动中： http://%s:%s", use_local_ip, 5001)
    
    ssl_cert = app.config.get("SSL_CERT_FILE")
    ssl_key = app.config.get("SSL_KEY_FILE")
    ssl_ctx = None
    if ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        ssl_ctx = (ssl_cert, ssl_key)
        app.logger.info("以 HTTPS 启动: https://%s:%s", use_local_ip, 5001)
    
    # 使用 SocketIO 启动应用
    socketio.run(app, host=use_local_ip, port=5001, debug=True, ssl_context=ssl_ctx, allow_unsafe_werkzeug=True)
