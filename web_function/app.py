import os
import sys
import time
import uuid
import json
import signal
import atexit
import logging
import webbrowser
import threading
import subprocess

from logging.handlers import RotatingFileHandler
from typing import Any, Dict
from urllib.parse import urlsplit, urlunsplit

from flask import (
    Flask, render_template, request, jsonify,
    send_from_directory, abort, Response, stream_with_context
)
from werkzeug.utils import secure_filename

from backend_function.common_utils import (
    get_local_ip,
    normalize_ios_info,
    build_ordered_ios_info,
    normalize_ios_list_output,
    parse_ios_apps,
    to_int,
    extract_goios_opts,
    terminate_process,
    get_device_model,
    now_timestamp_str,
    create_required_directories,
    cleanup_old_tunnel_processes,
    cleanup_all_ios_processes,
    create_signed_download_token,
    consume_signed_download_token,
    parse_ps_apps_raw,
    parse_crash_ls_items,
    crash_export_collect,
    crash_zip_dir,
    crash_remove_many, listen_event_stream, syslog_start_session,
    run_with_quick_check_and_escalate,
    stream_syslog_sse,
)
from backend_function.config import Config
from backend_function.goios_wrapper import GoIOSManager
from backend_function.ios_prechecker import IOSPrechecker
from backend_function.tunnel_manager import TunnelManager
from backend_function.common_utils import start_mjpeg_to_mp4, stop_recorder

app = Flask(__name__)

# 简单签名/令牌存储（内存，短时有效）
_SIGNED_DOWNLOADS: Dict[str, Dict[str, Any]] = {}

# ===== 配置与日志 =====
app.config.from_object(Config)
app.config['ENV'] = 'development'
app.config['DEBUG'] = True

os.makedirs(app.config["LOG_DIR"], exist_ok=True)
log_path = os.path.join(app.config["LOG_DIR"], "ios_app.log")
handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s - %(message)s")
handler.setFormatter(fmt)
app.logger.setLevel(logging.INFO)
app.logger.propagate = True
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
# 防重复：仅将文件句柄挂到根记录器
if not any(isinstance(h, RotatingFileHandler) and getattr(h, 'baseFilename', None) == log_path for h in root_logger.handlers):
    root_logger.addHandler(handler)

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

# 目录已在上面的 create_directories() 中创建

FORWARDS: Dict[str, Dict[str, Dict[str, Any]]] = {}
STREAMS: Dict[str, Dict[str, Dict[str, Any]]] = {}
SYSLOGS: Dict[str, Dict[str, Dict[str, Any]]] = {}

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

# ===== 工具函数 =====
# 已迁移到 backend_function.common_utils

# ===== 路由 =====
@app.route("/")
def index():
    success, out = goios.list_devices(details=False)
    devices = []
    if success and out:
        for line in out.splitlines():
            val = line.strip()
            if val and len(val) >= 16:
                devices.append(val)
    return render_template("index.html", devices=devices)

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static', 'wresource'), 'favicon.ico')

@app.route("/guide")
def guide():
    """引导文档页面"""
    return render_template("ios_introduce_guide_index.html")

# ---------- 设备管理 ----------
@app.route("/api/devices")
def api_devices():
    # 设备列表不需要tunnel
    details = request.args.get("details", "0") == "1"
    opts = extract_goios_opts(request.args)
    success, out_text = goios.list_devices(details=details, **opts)
    if not success:
        return jsonify({"ok": False, "msg": "获取设备列表失败", "raw": out_text}), 500
    devices = normalize_ios_list_output(out_text or "")
    return jsonify({"ok": True, "devices": devices, "raw": out_text})

@app.route("/api/device_info")
def api_device_info():
    udid = request.args.get("udid", "").strip()
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400

    # 使用快速检查而不是完整检查
    is_ready, check_msg = prechecker.quick_check(udid)
    if not is_ready:
        return jsonify({"ok": False, "msg": check_msg}), 400

    opts = extract_goios_opts(request.args)
    got_info, detail = goios.device_info(udid, **opts)

    info_list = []
    if got_info and detail:
        try:
            parsed = json.loads(detail) if isinstance(detail, str) else detail
            info_map = normalize_ios_info(parsed)
            info_list = build_ordered_ios_info(info_map)
        except json.JSONDecodeError as exc:
            app.logger.warning("device_info JSON 解析失败: %s", exc)
        except (KeyError, TypeError, ValueError) as exc:
            app.logger.warning("device_info 数据结构异常: %s", exc)
        except Exception as exc:
            app.logger.exception("device_info 未知异常: %s", exc)

    return jsonify({"ok": got_info, "info": info_list, "raw": detail})

# 新增：单次截屏（返回图片二进制）
@app.route("/api/screenshot")
def api_screenshot():
    udid = request.args.get("udid", "").strip()
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400

    # 尝试快速路径：不启动 tunnel，直接截屏；失败再完整检查后重试
    is_ready, check_msg = prechecker.quick_check(udid)
    if not is_ready:
        return jsonify({"ok": False, "msg": check_msg}), 400

    model = get_device_model(udid, goios)
    ts = now_timestamp_str()
    display_name = f"{model}_{udid}_screenshot_{ts}.png"

    import time
    fname = f"{udid}_screenshot_{int(time.time())}.png"  # 实际存储名
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], fname)

    # 第一次尝试
    first_ok, first_out = goios.screenshot(udid, save_path)
    if not first_ok:
        app.logger.warning("screenshot 首次失败，将执行完整检查后重试: %s", first_out)
        
        # 第二次尝试：强制挂载开发者镜像
        try:
            # 先确保tunnel运行
            tunnel_ok, tunnel_msg = tunnel.status()
            if not tunnel_ok:
                return jsonify({"ok": False, "msg": f"Tunnel启动失败: {tunnel_msg}"}), 500
            
            # 智能挂载开发者镜像
            extra_opts = tunnel.get_goios_opts(udid) or {}
            
            # 先尝试自动下载和挂载（go-ios会自动检测设备版本并下载对应镜像）
            app.logger.info("尝试自动下载和挂载开发者镜像...")
            image_ok, image_out = goios.image_auto(udid, basedir=app.config["DEVIMAGES_DIR"], extra_env={"ENABLE_GO_IOS_AGENT": "user"}, **extra_opts)
            
            # 验证镜像是否真正挂载成功
            if image_ok:
                app.logger.info("自动挂载命令成功，验证镜像状态...")
                # 检查镜像列表，确认是否真正挂载
                list_ok, list_out = goios.image_list(udid, **extra_opts)
                if list_ok and "none" not in list_out.lower():
                    app.logger.info("镜像验证成功: %s", list_out)
                    image_ok = True
                else:
                    app.logger.warning("镜像验证失败，实际未挂载: %s", list_out)
                    image_ok = False
            
            if not image_ok:
                app.logger.warning("自动挂载开发者镜像失败: %s", image_out)
                
                # 如果自动挂载失败，尝试手动挂载现有镜像
                app.logger.info("尝试手动挂载现有开发者镜像...")
                
                # 查找可用的开发者镜像
                devimages_dir = app.config["DEVIMAGES_DIR"]
                available_images = []
                
                # 扫描所有子目录中的DeveloperDiskImage.dmg文件
                for root, dirs, files in os.walk(devimages_dir):
                    for file in files:
                        if file == "DeveloperDiskImage.dmg":
                            available_images.append(os.path.join(root, file))
                
                if available_images:
                    # 尝试挂载第一个可用的镜像
                    image_path = available_images[0]
                    app.logger.info("尝试挂载镜像: %s", image_path)
                    
                    # 尝试多种挂载方式
                    mount_attempts = [
                        # 方式1: 标准挂载
                        lambda: goios.image_mount(udid, path=image_path, extra_env={"ENABLE_GO_IOS_AGENT": "user"}, **extra_opts),
                        # 方式2: 跳过签名验证（如果支持）
                        lambda: goios.image_mount(udid, path=image_path, extra_env={"ENABLE_GO_IOS_AGENT": "user", "SKIP_SIGNATURE_VERIFICATION": "1"}, **extra_opts),
                        # 方式3: 使用不同的环境变量
                        lambda: goios.image_mount(udid, path=image_path, extra_env={"ENABLE_GO_IOS_AGENT": "user", "GO_IOS_SKIP_SIGNATURE": "1"}, **extra_opts)
                    ]
                    
                    image_ok = False
                    for i, mount_attempt in enumerate(mount_attempts):
                        try:
                            app.logger.info("尝试挂载方式 %d", i + 1)
                            image_ok, image_out = mount_attempt()
                            if image_ok:
                                app.logger.info("挂载方式 %d 命令成功，验证镜像状态...", i + 1)
                                # 验证镜像是否真正挂载成功
                                list_ok, list_out = goios.image_list(udid, **extra_opts)
                                if list_ok and "none" not in list_out.lower():
                                    app.logger.info("挂载方式 %d 验证成功: %s", i + 1, list_out)
                                    image_ok = True
                                    break
                                else:
                                    app.logger.warning("挂载方式 %d 验证失败，实际未挂载: %s", i + 1, list_out)
                                    image_ok = False
                            else:
                                app.logger.warning("挂载方式 %d 失败: %s", i + 1, image_out)
                        except Exception as e:
                            app.logger.warning("挂载方式 %d 异常: %s", i + 1, e)
                    
                    if not image_ok:
                        app.logger.warning("所有挂载方式都失败，尝试直接截图")
                        # 即使挂载失败，也尝试直接截图（作为兜底）
                        retry_ok, retry_out = goios.screenshot(udid, save_path, extra_env={"ENABLE_GO_IOS_AGENT": "user"}, **extra_opts)
                        if not retry_ok:
                            error_msg = f"""开发者镜像挂载失败，截图也失败: {retry_out}

可能的解决方案:
1. 确保设备已开启开发者模式
2. 重新连接设备
3. 重启设备
4. 检查设备是否信任此电脑
5. 如果问题持续，请尝试在设备上手动安装开发者镜像

错误详情: {retry_out}"""
                            return jsonify({"ok": False, "msg": error_msg}), 500
                        else:
                            # 直接截图成功
                            first_ok, first_out = retry_ok, retry_out
                else:
                    app.logger.error("未找到可用的开发者镜像文件，尝试直接截图")
                    # 没有镜像文件，尝试直接截图
                    retry_ok, retry_out = goios.screenshot(udid, save_path, extra_env={"ENABLE_GO_IOS_AGENT": "user"}, **extra_opts)
                    if not retry_ok:
                        error_msg = f"""未找到可用的开发者镜像文件，截图也失败: {retry_out}

可能的解决方案:
1. 确保设备已开启开发者模式
2. 重新连接设备
3. 重启设备
4. 检查设备是否信任此电脑
5. 如果问题持续，请尝试在设备上手动安装开发者镜像

错误详情: {retry_out}"""
                        return jsonify({"ok": False, "msg": error_msg}), 500
                    else:
                        # 直接截图成功
                        first_ok, first_out = retry_ok, retry_out
            else:
                app.logger.info("开发者镜像挂载成功")
            
            # 如果镜像挂载成功，重试截图
            if image_ok:
                app.logger.info("开发者镜像挂载成功，重试截图")
                retry_ok, retry_out = goios.screenshot(udid, save_path, extra_env={"ENABLE_GO_IOS_AGENT": "user"}, **extra_opts)
                
                if not retry_ok:
                    # 兜底尝试：直接截图
                    app.logger.warning("带隧道参数截图失败，直接尝试: %s", retry_out)
                    retry_ok, retry_out = goios.screenshot(udid, save_path, extra_env={"ENABLE_GO_IOS_AGENT": "user"})
                
                if not retry_ok:
                    error_msg = f"""开发者镜像已挂载，但截图失败: {retry_out}

可能的解决方案:
1. 确保设备已开启开发者模式
2. 重新连接设备
3. 重启设备
4. 检查设备是否信任此电脑
5. 如果问题持续，请尝试在设备上手动安装开发者镜像

错误详情: {retry_out}"""
                    return jsonify({"ok": False, "msg": error_msg}), 500
                else:
                    # 将成功结果赋回
                    first_ok, first_out = retry_ok, retry_out
                
        except Exception as exc:
            app.logger.exception("截图重试过程中异常: %s", exc)
            return jsonify({"ok": False, "msg": f"截图重试失败: {exc}"}), 500

    try:
        return send_from_directory(
            app.config["UPLOAD_FOLDER"],
            os.path.basename(first_out),
            mimetype="image/png",
            as_attachment=True,
            download_name=display_name,
        )
    except Exception as exc:
        app.logger.exception("发送截图文件失败: %s", exc)
        return jsonify({"ok": False, "msg": f"发送文件失败: {exc}"}), 500

# ---------- 应用管理 ----------
@app.route("/api/apps")
def api_apps():
    udid = request.args.get("udid", "").strip()
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400

    only_list = request.args.get("list", "0") == "1"
    opts = extract_goios_opts(request.args)
    apps_ok, apps_raw = run_with_quick_check_and_escalate(
        prechecker,
        udid,
        lambda: goios.apps_list(udid, only_list=only_list, **opts)
    )
    apps = parse_ios_apps(apps_raw or "", third_party_only=True)
    return jsonify({"ok": apps_ok, "apps": apps, "raw": apps_raw})

@app.route("/api/install", methods=["POST"])
def api_install():
    udid = (request.form.get("udid") or "").strip()
    file = request.files.get("file")
    if not udid or not file:
        return jsonify({"ok": False, "msg": "缺少 udid 或 IPA 文件"}), 400

    install_ready, install_msg = prechecker.check_all(udid)
    if not install_ready:
        return jsonify({"ok": False, "msg": install_msg}), 400

    fname_lower = (file.filename or "").lower()
    if not fname_lower.endswith(".ipa"):
        return jsonify({"ok": False, "msg": "仅支持 .ipa 文件"}), 400

    filename = secure_filename(file.filename)
    unique_name = f"{uuid.uuid4().hex}_{filename}"
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
    file.save(save_path)

    opts = extract_goios_opts(request.args or request.form or {})
    install_ok, install_raw = goios.install_ipa(udid, save_path, **opts)
    return jsonify({"ok": install_ok, "raw": install_raw, "filename": unique_name})

@app.route("/api/launch", methods=["POST"])
def api_launch():
    data = request.get_json(force=True, silent=True) or {}
    udid = (data.get("udid") or "").strip()
    bundle_id = (data.get("bundle_id") or "").strip()
    wait = bool(data.get("wait", False))
    if not udid or not bundle_id:
        return jsonify({"ok": False, "msg": "缺少 udid 或 bundle_id"}), 400

    opts = extract_goios_opts(data)
    launch_ok, launch_raw = run_with_quick_check_and_escalate(
        prechecker,
        udid,
        lambda: goios.launch_app(udid, bundle_id, wait=wait, **opts)
    )
    return jsonify({"ok": launch_ok, "raw": launch_raw})

@app.route("/api/kill", methods=["POST"])
def api_kill():
    data = request.get_json(silent=True) or {}
    udid = (data.get("udid") or "").strip()
    bundle_id = (data.get("bundle_id") or "").strip()
    if not udid or not bundle_id:
        return jsonify({"ok": False, "msg": "缺少 udid 或 bundle_id"}), 400

    opts = extract_goios_opts(data)
    kill_ok, kill_msg2 = run_with_quick_check_and_escalate(
        prechecker,
        udid,
        lambda: goios.kill_app(udid=udid, bundle_id=bundle_id, **opts)
    )
    return jsonify({"ok": kill_ok, "msg": kill_msg2})

# ---------- 设备重启 ----------
@app.route("/api/reboot", methods=["POST"])
def api_reboot():
    data = request.get_json(silent=True) or {}
    udid = (data.get("udid") or "").strip()
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400

    opts = extract_goios_opts(data)
    reboot_ok, reboot_raw = run_with_quick_check_and_escalate(
        prechecker,
        udid,
        lambda: goios.reboot(udid, **opts)
    )
    return jsonify({"ok": reboot_ok, "raw": reboot_raw})


# ---------- 电量 ----------
@app.route("/api/battery")
def api_battery():
    udid = request.args.get("udid", "").strip()
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400

    opts = extract_goios_opts(request.args)
    battery_ok, battery_raw = run_with_quick_check_and_escalate(
        prechecker,
        udid,
        lambda: goios.battery_info(udid, **opts)
    )
    return jsonify({"ok": battery_ok, "raw": battery_raw})

@app.route("/api/battery/detail")
def api_battery_detail():
    udid = request.args.get("udid", "").strip()
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400

    opts = extract_goios_opts(request.args)
    b_ok, b_raw = run_with_quick_check_and_escalate(prechecker, udid, lambda: goios.battery_info(udid, **opts))
    r_ok, r_raw = run_with_quick_check_and_escalate(prechecker, udid, lambda: goios.battery_registry(udid, **opts))

    detail = {"batterycheck": None, "batteryregistry": None}
    try:
        detail["batterycheck"] = json.loads(b_raw) if b_ok and b_raw else None
    except json.JSONDecodeError:
        detail["batterycheck"] = None
    try:
        detail["batteryregistry"] = json.loads(r_raw) if r_ok and r_raw else None
    except json.JSONDecodeError:
        detail["batteryregistry"] = None

    return jsonify({"ok": True, "detail": detail, "raw": {"batterycheck": b_raw, "batteryregistry": r_raw}})

@app.route("/api/diskspace")
def api_diskspace():
    udid = request.args.get("udid", "").strip()
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400

    opts = extract_goios_opts(request.args)
    disk_ok, disk_raw = run_with_quick_check_and_escalate(
        prechecker,
        udid,
        lambda: goios.diskspace(udid, **opts)
    )
    return jsonify({"ok": disk_ok, "raw": disk_raw})

@app.route("/api/diskspace/detail")
def api_diskspace_detail():
    udid = request.args.get("udid", "").strip()
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400
    opts = extract_goios_opts(request.args)
    disk_ok, disk_raw = run_with_quick_check_and_escalate(prechecker, udid, lambda: goios.diskspace(udid, **opts))
    return jsonify({"ok": disk_ok, "raw": disk_raw})


# ---------- 开发者镜像 ----------
@app.route("/api/image/auto", methods=["POST"])
def api_image_auto():
    data = request.get_json(silent=True) or {}
    udid = (data.get("udid") or "").strip()
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400

    basedir = (data.get("basedir") or "").strip() or None
    opts = extract_goios_opts(data)
    image_ok, image_raw = run_with_quick_check_and_escalate(
        prechecker,
        udid,
        lambda: goios.image_auto(udid, basedir, **opts)
    )
    return jsonify({"ok": image_ok, "raw": image_raw})


# ---------- 设备状态 ----------
@app.route("/api/devicestate/list")
def api_devicestate_list():
    udid = request.args.get("udid", "").strip() or None
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400

    opts = extract_goios_opts(request.args)
    state_ok, state_raw = run_with_quick_check_and_escalate(prechecker, udid, lambda: goios.devicestate_list(udid=udid, **opts))
    return jsonify({"ok": state_ok, "raw": state_raw})


@app.route("/api/devicestate/enable", methods=["POST"])
def api_devicestate_enable():
    data = request.get_json(force=True, silent=True) or {}
    udid = (data.get("udid") or "").strip()
    t = (data.get("profile_type_id") or "").strip()
    p = (data.get("profile_id") or "").strip()
    if not (udid and t and p):
        return jsonify({"ok": False, "msg": "缺少 udid / profile_type_id / profile_id"}), 400

    opts = extract_goios_opts(data)
    state_enable_ok, state_enable_raw = run_with_quick_check_and_escalate(prechecker, udid, lambda: goios.devicestate_enable(udid, t, p, **opts))
    return jsonify({"ok": state_enable_ok, "raw": state_enable_raw})


# ---------- 模拟位置 ----------
@app.route("/api/setlocation", methods=["POST"])
def api_setlocation():
    data = request.get_json(force=True, silent=True) or {}
    udid = (data.get("udid") or "").strip()
    lat = data.get("lat"); lon = data.get("lon")
    if not udid or lat is None or lon is None:
        return jsonify({"ok": False, "msg": "缺少 udid/lat/lon"}), 400

    opts = extract_goios_opts(data)
    loc_ok, loc_raw = run_with_quick_check_and_escalate(prechecker, udid, lambda: goios.set_location(udid, float(lat), float(lon), **opts))
    return jsonify({"ok": loc_ok, "raw": loc_raw})


# ---------- 截屏流 ----------
@app.route("/api/screenshot/stream/start", methods=["POST"])
def api_ss_start():
    from backend_function.common_utils import check_port_available, find_free_port, wait_for_mjpeg_stream
    
    payload = request.get_json(silent=True) or {}
    udid = (payload.get("udid") or "").strip()
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400
    # 为提高成功率：在启动截图流前执行完整检查（包含隧道与开发者镜像挂载）
    ready_ok, ready_msg = prechecker.check_all(udid)
    if not ready_ok:
        return jsonify({"ok": False, "msg": ready_msg}), 400

    port = to_int(payload.get("port"), 3333) or 3333
    
    # 检查端口是否已被占用
    if not check_port_available(port):
        new_port = find_free_port(start_port=port + 1, max_tries=10)
        if new_port:
            app.logger.warning("端口 %d 已占用，使用端口 %d", port, new_port)
            port = new_port
        else:
            return jsonify({"ok": False, "msg": f"端口 {port} 已占用且无可用端口"}), 500

    opts = extract_goios_opts(payload)
    
    # 添加 tunnel 参数以提升连接稳定性
    extra_opts = tunnel.get_goios_opts(udid) if hasattr(tunnel, 'get_goios_opts') else {}
    opts.update(extra_opts)
    
    # 注入环境变量提升兼容性
    opts["extra_env"] = {"ENABLE_GO_IOS_AGENT": "user"}
    
    app.logger.debug("启动 MJPEG 流: port=%d, opts=%s", port, opts)
    p = goios.screenshot_stream_popen(udid=udid, port=port, **opts)
    if p is None:
        return jsonify({"ok": False, "msg": "启动 MJPEG 流失败"}), 500

    # 等待 MJPEG 服务器启动
    # 使用实际的服务器 IP 地址，而不是 localhost
    server_ip = get_local_ip() or "127.0.0.1"
    mjpeg_url = f"http://{server_ip}:{port}"
    
    if not wait_for_mjpeg_stream(mjpeg_url, p, max_wait_seconds=15, logger=app.logger):
        # 如果使用服务器 IP 失败，尝试 localhost 作为回退
        if server_ip != "127.0.0.1":
            app.logger.warning("使用服务器 IP %s 连接失败，尝试 localhost 回退", server_ip)
            mjpeg_url_fallback = f"http://127.0.0.1:{port}"
            if wait_for_mjpeg_stream(mjpeg_url_fallback, p, max_wait_seconds=10, logger=app.logger):
                mjpeg_url = mjpeg_url_fallback
            else:
                # 两种方式都失败，终止进程
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                return jsonify({"ok": False, "msg": "MJPEG 流超时未就绪 (多种地址尝试失败)"}), 500
        else:
            # 超时，终止进程
            try:
                p.terminate()
                p.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return jsonify({"ok": False, "msg": "MJPEG 流超时未就绪 (15秒)"}), 500

    sid = uuid.uuid4().hex
    STREAMS.setdefault(udid, {})[sid] = {"p": p, "port": port}

    parts = urlsplit(request.host_url)
    url = urlunsplit((parts.scheme, f"{parts.hostname}:{port}", "", "", ""))

    # 开启服务端录制
    model = get_device_model(udid, goios)
    ts = now_timestamp_str()
    base = f"{model}_{udid}_record_{ts}"

    # 启动进程监控线程
    def monitor_process():
        import time
        while True:
            # 从 STREAMS 中获取当前进程引用，避免闭包问题
            if udid in STREAMS and sid in STREAMS[udid]:
                current_p = STREAMS[udid][sid].get("p")
                if current_p and current_p.poll() is not None:
                    app.logger.error("MJPEG 流进程意外退出 (退出码: %s)", current_p.returncode)
                    # 检查会话是否仍然有效（可能已被停止）
                    if udid in STREAMS and sid in STREAMS[udid]:
                        # 尝试重启进程
                        app.logger.debug("尝试重启 MJPEG 流...")
                        new_p = goios.screenshot_stream_popen(udid=udid, port=port, **opts)
                        if new_p:
                            STREAMS[udid][sid]["p"] = new_p
                            app.logger.debug("MJPEG 流已重启")
                        else:
                            app.logger.error("MJPEG 流重启失败")
                            break
                    else:
                        # 会话已被停止，退出监控
                        app.logger.debug("检测到会话已停止，退出监控线程")
                        break
            else:
                # 流会话已被清理，退出监控
                app.logger.debug("流会话已被清理，退出监控线程")
                break
            time.sleep(5)  # 每5秒检查一次
    
    monitor_thread = threading.Thread(target=monitor_process, daemon=True, name=f"Monitor-{sid}")
    monitor_thread.start()
    
    # 延迟启动录制，确保 MJPEG 流完全稳定
    import time
    time.sleep(2.0)  # 增加等待时间
    
    rec_ctx = start_mjpeg_to_mp4(mjpeg_url=mjpeg_url, out_dir=app.config["UPLOAD_FOLDER"], basename=base, logger=app.logger)

    STREAMS[udid][sid].update({
        "rec_ctx": rec_ctx,
        "rec_path": rec_ctx.get("path"),
        "rec_name": rec_ctx.get("name"),
        "monitor_thread": monitor_thread
    })

    return jsonify({
        "ok": True,
        "id": sid,  # 前端期望 id 字段
        "url": url,
        "port": port,
        "msg": f"MJPEG 流已启动 (端口: {port})"
    })


# 安全下载令牌生成
# 已迁移至 common_utils.create_signed_download_token/consume_signed_download_token

@app.route("/api/screenshot/stream/stop", methods=["POST"])
def api_ss_stop():
    payload = request.get_json(silent=True) or {}
    app.logger.debug("停止录屏请求: %s", payload)
    
    udid = (payload.get("udid") or "").strip()
    stream_id = (payload.get("id") or "").strip()  # 前端传递的是 id
    
    app.logger.debug("解析参数: udid=%s, stream_id=%s", udid, stream_id)
    app.logger.debug("当前活动流: %s", STREAMS)
    
    if not udid or not stream_id:
        app.logger.warning("缺少必要参数: udid=%s, stream_id=%s", udid, stream_id)
        return jsonify({"ok": False, "msg": "缺少 udid/id"}), 400

    # 先查找流信息
    udid_streams = STREAMS.get(udid, {})
    if stream_id not in udid_streams:
        app.logger.warning("未找到流会话: udid=%s, stream_id=%s, 可用流: %s", udid, stream_id, list(udid_streams.keys()))
        return jsonify({"ok": False, "msg": "无此会话"}), 404
    
    # 获取流信息
    info = udid_streams[stream_id]
    if not isinstance(info, dict) or "p" not in info:
        app.logger.warning("流信息格式错误: %s", info)
        return jsonify({"ok": False, "msg": "会话信息无效"}), 404
    
    # 停止进程
    terminate_process(info["p"])
    
    # 停止录制
    rec_ctx = info.get("rec_ctx")
    if rec_ctx:
        stop_recorder(rec_ctx)
    
    # 清理监控线程（如果存在）
    monitor_thread = info.get("monitor_thread")
    if monitor_thread and monitor_thread.is_alive():
        app.logger.debug("清理监控线程")
        # 监控线程是 daemon 线程，会在主线程退出时自动结束
        # 这里主要是为了日志记录

    # 删除流信息
    del udid_streams[stream_id]
    if not udid_streams:  # 如果该设备没有其他流，删除设备条目
        STREAMS.pop(udid, None)

    rec_path = (rec_ctx or {}).get("path")
    rec_name = (rec_ctx or {}).get("name")

    # 生成签名下载链接
    download_url = None
    if rec_name and os.path.exists(rec_path or ""):
        token = create_signed_download_token(rec_name, ttl_seconds=600)
        download_url = f"/api/download_secure/{token}"

    app.logger.debug("停止录屏成功: file=%s, download_url=%s", rec_name, download_url)
    return jsonify({"ok": True, "file": rec_name, "download_url": download_url})


# ---------- 端口转发 ----------
@app.route("/api/forward/start", methods=["POST"])
def api_forward_start():
    data = request.get_json(force=True, silent=True) or {}
    udid = (data.get("udid") or "").strip()
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400

    fwd_ready, fwd_msg = prechecker.check_all(udid)
    if not fwd_ready:
        return jsonify({"ok": False, "msg": fwd_msg}), 400

    host_port = to_int(data.get("host_port"), 0) or 0
    target_port = to_int(data.get("target_port"), 0) or 0
    if not host_port or not target_port:
        return jsonify({"ok": False, "msg": "缺少 host_port/target_port"}), 400

    opts = extract_goios_opts(data)
    p = goios.forward_popen(udid=udid, host_port=host_port, target_port=target_port, **opts)
    if p is None:
        return jsonify({"ok": False, "msg": "启动失败"}), 500

    fid = uuid.uuid4().hex
    FORWARDS.setdefault(udid, {})[fid] = {"p": p, "host_port": host_port, "target_port": target_port}
    return jsonify({"ok": True, "id": fid, "udid": udid})


@app.route("/api/forward/stop", methods=["POST"])
def api_forward_stop():
    payload = request.get_json(silent=True) or {}
    udid = (payload.get("udid") or "").strip()
    fid = (payload.get("id") or "").strip()
    if not udid or not fid:
        return jsonify({"ok": False, "msg": "缺少 udid/id"}), 400

    info = (FORWARDS.get(udid) or {}).pop(fid, None)
    if not isinstance(info, dict) or "p" not in info:
        return jsonify({"ok": False, "msg": "无此转发会话"}), 404
    terminate_process(info["p"])
    return jsonify({"ok": True})

# ---------- 文件下载 ----------
@app.route("/api/download/<path:fname>")
def api_download_file(fname: str):
    # 仅允许从上传目录下载，并作为附件返回
    safe_name = os.path.basename(fname)
    full_path = os.path.join(app.config["UPLOAD_FOLDER"], safe_name)
    if not os.path.exists(full_path):
        return jsonify({"ok": False, "msg": "文件不存在"}), 404
    return send_from_directory(app.config["UPLOAD_FOLDER"], safe_name, as_attachment=True)

# 新增：带签名的安全下载，短时有效
@app.route("/api/download_secure/<token>")
def api_download_secure(token: str):
    try:
        fname = consume_signed_download_token(token)
    except KeyError:
        return abort(403)
    # 仅允许相对路径并限制在 UPLOAD_FOLDER 内
    safe_rel = fname.replace("\\", "/").lstrip("/")
    full_path = os.path.normpath(os.path.join(app.config["UPLOAD_FOLDER"], safe_rel))
    if not full_path.startswith(os.path.normpath(app.config["UPLOAD_FOLDER"])):
        return abort(403)
    if not os.path.exists(full_path):
        return jsonify({"ok": False, "msg": "文件不存在"}), 404
    directory = os.path.dirname(os.path.relpath(full_path, app.config["UPLOAD_FOLDER"]))
    filename = os.path.basename(full_path)
    base_dir = str(app.config["UPLOAD_FOLDER"])  # ensure str
    dir_part = str(directory)
    send_dir = base_dir if dir_part == "." or dir_part == "" else os.path.join(base_dir, dir_part)
    resp = send_from_directory(send_dir, str(filename), as_attachment=True)
    # 防缓存
    resp.headers["Cache-Control"] = "no-store"
    return resp

# ---------- 运行中应用（基于 ps --apps） ----------
@app.route("/api/apps/running")
def api_apps_running():
    udid = request.args.get("udid", "").strip()
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400

    opts = extract_goios_opts(request.args)
    ps_ok, ps_raw = run_with_quick_check_and_escalate(prechecker, udid, lambda: goios.ps_apps(udid, **opts))
    # 容错解析
    processes = parse_ps_apps_raw(ps_raw or "") if ps_ok and ps_raw else []
    return jsonify({"ok": ps_ok, "list": processes, "raw": ps_raw})

# ---------- Crash 日志 ----------
@app.route("/api/crash/ls")
def api_crash_ls():
    udid = request.args.get("udid", "").strip()
    pattern_input = (request.args.get("pattern") or "").strip()
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400
    ok_pre, pre_msg = prechecker.quick_check(udid)
    if not ok_pre:
        return jsonify({"ok": False, "msg": pre_msg}), 400
    # 模式：含通配符(*, ?)走 go-ios 侧过滤；否则后端全量+不区分大小写包含匹配
    has_glob = any(ch in pattern_input for ch in ("*", "?")) if pattern_input else False
    go_pattern = pattern_input if has_glob else None
    ok_list, raw = run_with_quick_check_and_escalate(prechecker, udid, lambda: goios.crash_ls(udid, go_pattern))
    items = parse_crash_ls_items(raw) if ok_list and raw else []
    if ok_list and items and pattern_input and not has_glob:
        kw = pattern_input.lower()
        items = [x for x in items if kw in x.lower()]
    return jsonify({"ok": ok_list, "items": items, "raw": raw})

@app.route("/api/crash/cp", methods=["POST"])
def api_crash_cp():
    data = request.get_json(silent=True) or {}
    udid = (data.get("udid") or "").strip()
    pattern = (data.get("pattern") or "*").strip() or "*"
    patterns = data.get("patterns") if isinstance(data.get("patterns"), list) else None
    # 仅允许复制到服务端的 UPLOAD_FOLDER/crashes/<uuid>/
    crash_root = os.path.join(app.config["UPLOAD_FOLDER"], "crashes")
    os.makedirs(crash_root, exist_ok=True)
    batch_dir = os.path.join(crash_root, uuid.uuid4().hex)
    os.makedirs(batch_dir, exist_ok=True)
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400
    # 收集导出文件（执行前 quick_check，失败升级）
    def _do_export():
        ok_, raw_ = True, None
        res = crash_export_collect(goios, udid, patterns if patterns else [pattern], crash_root, logger=app.logger)
        # 用 ok/raw 表示整体结果
        return bool(res.get("ok")), res
    ok_flag, result = run_with_quick_check_and_escalate(prechecker, udid, _do_export)
    files = (result or {}).get("files") or []
    download_url = None
    # 保留变量名称占位，当前逻辑不再使用多链接下载
    zip_name = ""
    if ok_flag:
        count = len(files)
        if count == 1:
            fp = files[0]
            if os.path.exists(fp):
                rel = str(os.path.relpath(fp, app.config["UPLOAD_FOLDER"]).replace("\\", "/"))
                token = create_signed_download_token(rel, ttl_seconds=1800)
                download_url = f"/api/download_secure/{token}"
        elif count >= 2:
            # 2+ 文件统一打包
            zip_path = crash_zip_dir((result or {}).get("batch_dir"), crash_root, logger=app.logger)
            if zip_path and os.path.exists(zip_path):
                zip_name = os.path.basename(zip_path)
                rel = str(os.path.relpath(zip_path, app.config["UPLOAD_FOLDER"]).replace("\\", "/"))
                token = create_signed_download_token(rel, ttl_seconds=1800)
                download_url = f"/api/download_secure/{token}"

    return jsonify({
        "ok": ok_flag,
        "zip": zip_name,
        "download_url": download_url,
        "download_urls": None,
        "raw": (result or {}).get("raw")
    })

@app.route("/api/crash/rm", methods=["POST"])
def api_crash_rm():
    data = request.get_json(silent=True) or {}
    udid = (data.get("udid") or "").strip()
    cwd = (data.get("cwd") or ".").strip() or "."
    pattern = (data.get("pattern") or "*").strip() or "*"
    patterns = data.get("patterns") if isinstance(data.get("patterns"), list) else None
    recursive = bool(data.get("recursive", True))
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400
    def _do_rm():
        ok_, raw_ = True, None
        res = crash_remove_many(goios, udid, patterns if patterns else [pattern], cwd=cwd, recursive=recursive)
        return bool(res.get("ok")), res
    ok_rm, res_rm = run_with_quick_check_and_escalate(prechecker, udid, _do_rm)
    return jsonify({"ok": bool((res_rm or {}).get("ok")), "raw": (res_rm or {}).get("raw")})

# ---------- 配置文件管理 ----------
@app.route("/api/profile/list")
def api_profile_list():
    udid = request.args.get("udid", "").strip()
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400
    ok_l, raw_l = run_with_quick_check_and_escalate(prechecker, udid, lambda: goios.profile_list(udid))
    items = []
    if ok_l and raw_l:
        try:
            data = json.loads(raw_l)
            if isinstance(data, list):
                items = data  # 保持原始结构（对象/字符串），交由前端格式化展示
        except json.JSONDecodeError:
            items = [s.strip() for s in str(raw_l).splitlines() if s.strip()]
    return jsonify({"ok": ok_l, "items": items, "raw": raw_l})

@app.route("/api/profile/remove", methods=["POST"])
def api_profile_remove():
    data = request.get_json(silent=True) or {}
    udid = (data.get("udid") or "").strip()
    names = data.get("names") or []
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400
    if not isinstance(names, list) or not names:
        return jsonify({"ok": False, "msg": "缺少要移除的配置文件名"}), 400
    results = []
    all_ok = True
    for profile_name in names:
        ok_r, raw_r = run_with_quick_check_and_escalate(prechecker, udid, lambda pn=profile_name: goios.profile_remove(udid, str(pn)))
        results.append({"name": profile_name, "ok": ok_r, "raw": raw_r})
        all_ok = all_ok and ok_r
    return jsonify({"ok": all_ok, "results": results})

# ---------- 开发者模式 ----------
@app.route("/api/devmode/get")
def api_devmode_get():
    udid = request.args.get("udid", "").strip()
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400

    opts = extract_goios_opts(request.args)
    devmode_ok, devmode_raw = run_with_quick_check_and_escalate(
        prechecker,
        udid,
        lambda: goios.devmode_get(udid, **opts)
    )
    return jsonify({"ok": devmode_ok, "raw": devmode_raw})

@app.route("/api/devmode/check")
def api_devmode_check():
    """
    开发者模式检测，确保tunnel状态正常
    """
    udid = request.args.get("udid", "").strip()
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400

    try:
        # 使用prechecker的check_all方法，它会自动处理tunnel状态
        ok_pre, pre_msg = prechecker.check_all(udid)
        if not ok_pre:
            return jsonify({"ok": False, "raw": f"设备检查失败: {pre_msg}"}), 500

        # 获取tunnel参数
        tunnel_opts = tunnel.get_goios_opts(udid) if hasattr(tunnel, 'get_goios_opts') else {}
        
        # 调用go-ios命令检测开发者模式
        devmode_ok, devmode_raw = goios.devmode_get(udid, **tunnel_opts)
        return jsonify({"ok": devmode_ok, "raw": devmode_raw})
    except Exception as e:
        app.logger.exception("开发者模式检测异常: %s", e)
        return jsonify({"ok": False, "raw": str(e)}), 500

@app.route("/api/devmode/enable", methods=["POST"])
def api_devmode_enable():
    data = request.get_json(silent=True) or {}
    udid = (data.get("udid") or "").strip()
    enable_post_restart = bool(data.get("enable_post_restart", True))
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400

    opts = extract_goios_opts(data)
    devmode_ok, devmode_raw = run_with_quick_check_and_escalate(
        prechecker,
        udid,
        lambda: goios.devmode_enable(udid, enable_post_restart=enable_post_restart, **opts)
    )
    return jsonify({"ok": devmode_ok, "raw": devmode_raw})

# ---------- 辅助功能 ----------
@app.route("/api/assistive/<feature>/<action>", methods=["POST"]) 
def api_assistive(feature: str, action: str):
    data = request.get_json(silent=True) or {}
    udid = (data.get("udid") or "").strip()
    force = bool(data.get("force", False))
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400
    feature = feature.strip().lower()
    action = action.strip().lower()
    if feature not in ("assistivetouch", "voiceover", "zoom"):
        return jsonify({"ok": False, "msg": "不支持的功能"}), 400
    if action not in ("enable", "disable", "toggle", "get"):
        return jsonify({"ok": False, "msg": "不支持的操作"}), 400
    ok_a, raw_a = run_with_quick_check_and_escalate(prechecker, udid, lambda: goios.assistive(udid, feature, action, force=force))
    return jsonify({"ok": ok_a, "raw": raw_a})

# ---------- 设备事件（SSE） ----------
@app.route("/api/devices/events")
def api_devices_events():
    def _gen():
        for chunk in listen_event_stream(goios):
            yield chunk
    return Response(stream_with_context(_gen()), mimetype='text/event-stream')

# ---------- 系统日志（启动/停止，保存为文件供下载） ----------
@app.route("/api/syslog/start", methods=["POST"])
def api_syslog_start():
    data = request.get_json(silent=True) or {}
    udid = (data.get("udid") or "").strip()
    parse = bool(data.get("parse", True))
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400
    ok_pre, pre_msg = prechecker.quick_check(udid)
    if not ok_pre:
        return jsonify({"ok": False, "msg": pre_msg}), 400

    sid = uuid.uuid4().hex
    # 使用通用工具启动写文件会话
    ctx = syslog_start_session(goios, udid, app.config["UPLOAD_FOLDER"], parse=parse, logger=app.logger)
    if not ctx.get("ok"):
        return jsonify({"ok": False, "msg": ctx.get("msg") or "启动失败"}), 500

    SYSLOGS.setdefault(udid, {})[sid] = {"p": ctx.get("p"), "path": ctx.get("path"), "name": ctx.get("name")}
    return jsonify({"ok": True, "id": sid, "file": ctx.get("name")})

@app.route("/api/syslog/stream")
def api_syslog_stream():
    udid = (request.args.get("udid") or "").strip()
    parse = request.args.get("parse", "1") != "0"
    keywords = [s.strip() for s in (request.args.get("kw") or "").split(",") if s.strip()]
    levels = [s.strip().lower() for s in (request.args.get("lv") or "").split(",") if s.strip()]
    if not udid:
        return jsonify({"ok": False, "msg": "缺少 udid"}), 400
    # 使用 quick_check，必要时前端根据报错提示
    ok_pre, pre_msg = prechecker.quick_check(udid)
    if not ok_pre:
        return jsonify({"ok": False, "msg": pre_msg}), 400
    def _gen():
        # 查找现有的 syslog 进程
        existing_process = None
        for session_info in SYSLOGS.get(udid, {}).values():
            if session_info.get("p"):
                existing_process = session_info["p"]
                break
                
        for chunk in stream_syslog_sse(goios, udid, parse=parse, logger=app.logger,
                                       keywords=keywords, levels=levels, 
                                       existing_process=existing_process):
            yield chunk
    resp = Response(stream_with_context(_gen()), mimetype='text/event-stream')
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"  # 兼容反代
    resp.headers["Connection"] = "keep-alive"
    return resp

@app.route("/api/syslog/stop", methods=["POST"])
def api_syslog_stop():
    data = request.get_json(silent=True) or {}
    udid = (data.get("udid") or "").strip()
    sid = (data.get("id") or "").strip()
    if not udid or not sid:
        return jsonify({"ok": False, "msg": "缺少 udid/id"}), 400
    info = (SYSLOGS.get(udid) or {}).pop(sid, None)
    if not info or "p" not in info:
        return jsonify({"ok": False, "msg": "无此会话"}), 404
    terminate_process(info["p"])
    log_name = info.get("name")
    syslog_file_path = info.get("path")
    download_url = None
    if log_name and syslog_file_path and os.path.exists(syslog_file_path):
        rel = str(os.path.relpath(syslog_file_path, app.config["UPLOAD_FOLDER"]).replace("\\", "/"))
        token = create_signed_download_token(rel, ttl_seconds=1800)
        download_url = f"/api/download_secure/{token}"
    return jsonify({"ok": True, "download_url": download_url})


# ========== 健康检查 ==========
@app.route("/health")
def health():
    return jsonify({"ok": True}), 200

# ========== Debug功能 ==========
@app.route("/api/debug/cleanup-processes", methods=["POST"])
def api_debug_cleanup_processes():
    """手动清理所有ios.exe进程"""
    try:
        cleanup_all_ios_processes(app.logger)
        return jsonify({"ok": True, "msg": "ios.exe进程清理完成"})
    except Exception as e:
        app.logger.exception("手动清理ios.exe进程异常: %s", e)
        return jsonify({"ok": False, "msg": f"清理失败: {str(e)}"}), 500

@app.route("/api/debug/export-logs", methods=["POST"])
def api_debug_export_logs():
    """导出应用日志文件用于调试分析"""
    try:
        # 源日志文件路径
        source_log_path = os.path.join(app.config["LOG_DIR"], "ios_app.log")
        
        # 检查源文件是否存在
        if not os.path.exists(source_log_path):
            return jsonify({"ok": False, "msg": "日志文件不存在"}), 404
        
        # 生成副本文件名（带时间戳）
        timestamp = now_timestamp_str()
        copy_filename = f"debug_logs_{timestamp}.log"
        copy_path = os.path.join(app.config["UPLOAD_FOLDER"], copy_filename)
        
        # 复制文件内容（流式读取，避免大文件内存问题）
        try:
            with open(source_log_path, 'r', encoding='utf-8') as src_file:
                with open(copy_path, 'w', encoding='utf-8') as dst_file:
                    # 分块读取和写入，避免内存占用过大
                    chunk_size = 8192  # 8KB chunks
                    while True:
                        chunk = src_file.read(chunk_size)
                        if not chunk:
                            break
                        dst_file.write(chunk)
            
            # 生成下载链接
            rel = str(os.path.relpath(copy_path, app.config["UPLOAD_FOLDER"]).replace("\\", "/"))
            token = create_signed_download_token(rel, ttl_seconds=3600)  # 1小时有效期
            download_url = f"/api/download_secure/{token}"
            
            app.logger.info("Debug日志导出成功: %s", copy_filename)
            return jsonify({
                "ok": True, 
                "filename": copy_filename,
                "download_url": download_url,
                "msg": "日志导出成功"
            })
            
        except (OSError, IOError) as e:
            app.logger.error("复制日志文件失败: %s", e)
            return jsonify({"ok": False, "msg": f"复制文件失败: {str(e)}"}), 500
            
    except Exception as e:
        app.logger.exception("Debug日志导出异常: %s", e)
        return jsonify({"ok": False, "msg": f"导出失败: {str(e)}"}), 500

# ===== 自启动浏览器 =====
use_local_ip = get_local_ip()
def open_browser():
    time.sleep(1)
    cert = app.config.get("SSL_CERT_FILE")
    key = app.config.get("SSL_KEY_FILE")
    scheme = "https" if (cert and key and os.path.exists(cert) and os.path.exists(key)) else "http"
    webbrowser.open_new_tab(f'{scheme}://{use_local_ip}:5001')

if __name__ == '__main__':

    
    if not hasattr(app, 'browser_opened') or not app.browser_opened:
        threading.Thread(target=open_browser, daemon=True).start()
        app.browser_opened = True
    app.logger.info("Web 控制台启动中： http://%s:%s", use_local_ip, 5001)
    ssl_cert = app.config.get("SSL_CERT_FILE")
    ssl_key = app.config.get("SSL_KEY_FILE")
    ssl_ctx = None
    if ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        ssl_ctx = (ssl_cert, ssl_key)
        app.logger.info("以 HTTPS 启动: https://%s:%s", use_local_ip, 5001)
    app.run(host=use_local_ip, port=5001, debug=True, use_reloader=False, ssl_context=ssl_ctx)
