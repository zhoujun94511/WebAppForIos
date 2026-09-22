"""
API 业务逻辑处理器
封装所有 API 相关的业务逻辑，从 app.py 中分离出来
"""

import os
import subprocess
import uuid
import json
import threading
import time
from typing import Any, Dict, Tuple
from werkzeug.utils import secure_filename
from urllib.parse import urlsplit, urlunsplit
from flask import request, jsonify, send_from_directory, abort, Response, stream_with_context
from backend_function.common_utils import (
    get_local_ip,
    normalize_ios_info,
    build_ordered_ios_info,
    normalize_ios_list_output,
    parse_ios_apps,
    extract_goios_opts,
    terminate_process,
    get_device_model,
    now_timestamp_str,
    create_signed_download_token,
    consume_signed_download_token,
    parse_ps_apps_raw,
    parse_crash_ls_items,
    crash_export_collect,
    crash_zip_dir,
    crash_remove_many,
    listen_event_stream,
    syslog_start_session,
    run_with_quick_check_and_escalate,
    stream_syslog_sse,
    start_mjpeg_to_mp4,
    stop_recorder,
    check_port_available,
    find_free_port,
    wait_for_mjpeg_stream,
)


class APIHandlers:
    """API 业务逻辑处理器"""
    
    def __init__(self, app, goios, tunnel, prechecker):
        self.app = app
        self.goios = goios
        self.tunnel = tunnel
        self.prechecker = prechecker
        
        # 会话存储
        self.forwards: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.streams: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.syslogs: Dict[str, Dict[str, Dict[str, Any]]] = {}
    
    # ===== 设备管理 =====
    def get_devices(self, details: bool = False) -> Dict[str, Any]:
        """获取设备列表"""
        opts = extract_goios_opts(request.args)
        success, out_text = self.goios.list_devices(details=details, **opts)
        if not success:
            return {"ok": False, "msg": "获取设备列表失败", "raw": out_text}
        
        devices = normalize_ios_list_output(out_text or "")
        return {"ok": True, "devices": devices, "raw": out_text}
    
    def get_device_info(self, udid: str) -> Dict[str, Any]:
        """获取设备详细信息"""
        if not udid:
            return {"ok": False, "msg": "缺少 udid"}
        
        # 使用快速检查而不是完整检查
        is_ready, check_msg = self.prechecker.quick_check(udid)
        if not is_ready:
            return {"ok": False, "msg": check_msg}
        
        opts = extract_goios_opts(request.args)
        got_info, detail = self.goios.device_info(udid, **opts)
        
        info_list = []
        if got_info and detail:
            try:
                parsed = json.loads(detail) if isinstance(detail, str) else detail
                info_map = normalize_ios_info(parsed)
                info_list = build_ordered_ios_info(info_map)
            except json.JSONDecodeError as exc:
                self.app.logger.warning("device_info JSON 解析失败: %s", exc)
            except (KeyError, TypeError, ValueError) as exc:
                self.app.logger.warning("device_info 数据结构异常: %s", exc)
            except Exception as exc:
                self.app.logger.exception("device_info 未知异常: %s", exc)
        
        return {"ok": got_info, "info": info_list, "raw": detail}
    
    def take_screenshot(self, udid: str) -> Tuple[bool, Any]:
        """截屏功能"""
        if not udid:
            return False, jsonify({"ok": False, "msg": "缺少 udid"})
        
        # 尝试快速路径：不启动 tunnel，直接截屏；失败再完整检查后重试
        is_ready, check_msg = self.prechecker.quick_check(udid)
        if not is_ready:
            return False, jsonify({"ok": False, "msg": check_msg})
        
        model = get_device_model(udid, self.goios)
        ts = now_timestamp_str()
        display_name = f"{model}_{udid}_screenshot_{ts}.png"
        
        fname = f"{udid}_screenshot_{int(time.time())}.png"
        # 使用截图专用目录
        screenshots_dir = os.path.join(self.app.config["UPLOAD_FOLDER"], "screenshots")
        os.makedirs(screenshots_dir, exist_ok=True)
        save_path = os.path.join(screenshots_dir, fname)
        
        # 第一次尝试
        first_ok, first_out = self.goios.screenshot(udid, save_path)
        if not first_ok:
            self.app.logger.warning("screenshot 首次失败，将执行完整检查后重试: %s", first_out)
            
            # 第二次尝试：按芯片身份挂载开发者镜像（对齐 AutoPilot，勿硬挂 16.6/旧 ddi）
            try:
                # 先确保tunnel运行
                tunnel_ok, tunnel_msg = self.tunnel.status()
                if not tunnel_ok:
                    tunnel_ok, tunnel_msg = self.prechecker.ensure_tunnel_running()
                    if not tunnel_ok:
                        return False, jsonify({"ok": False, "msg": f"Tunnel启动失败: {tunnel_msg}"})
                
                extra_opts = self.tunnel.get_goios_opts(udid) or {}
                from backend_function.ddi_manager import ensure_developer_image

                self.app.logger.info("按芯片身份挂载开发者镜像…")
                image_ok, image_out = ensure_developer_image(
                    self.goios,
                    udid,
                    basedir=self.app.config["DEVIMAGES_DIR"],
                    tunnel_opts=extra_opts,
                    require_success=True,
                )
                if not image_ok:
                    error_msg = f"""开发者镜像挂载失败，截图也失败: {first_out}

{image_out}

可能的解决方案:
1. 将含本机芯片身份的个性化 DDI（Restore/BuildManifest.plist）放入 DEVIMAGES_DIR
2. 可从 AutoPilot resources/re_go_ios/devimages 复制较新的 ddi-*（如 ddi-17E5179g）
3. 可选安装 pymobiledevice3 以启用 auto-mount 兜底
4. 确保设备已开启开发者模式并信任此电脑

错误详情: {image_out}"""
                    return False, jsonify({"ok": False, "msg": error_msg})

                self.app.logger.info("开发者镜像挂载成功: %s", image_out)
                retry_ok, retry_out = self.goios.screenshot(
                    udid, save_path, extra_env={"ENABLE_GO_IOS_AGENT": "user"}, **extra_opts
                )
                if not retry_ok:
                    self.app.logger.warning("带隧道参数截图失败，直接尝试: %s", retry_out)
                    retry_ok, retry_out = self.goios.screenshot(
                        udid, save_path, extra_env={"ENABLE_GO_IOS_AGENT": "user"}
                    )
                if not retry_ok:
                    error_msg = f"""开发者镜像已挂载，但截图失败: {retry_out}

可能的解决方案:
1. 确保设备已开启开发者模式
2. 重新连接设备
3. 重启设备
4. 检查设备是否信任此电脑

错误详情: {retry_out}"""
                    return False, jsonify({"ok": False, "msg": error_msg})
                first_ok, first_out = retry_ok, retry_out
                    
            except Exception as exc:
                self.app.logger.exception("截图重试过程中异常: %s", exc)
                return False, jsonify({"ok": False, "msg": f"截图重试失败: {exc}"})
        
        try:
            return True, send_from_directory(
                screenshots_dir,
                os.path.basename(first_out),
                mimetype="image/png",
                as_attachment=True,
                download_name=display_name,
            )
        except Exception as exc:
            self.app.logger.exception("发送截图文件失败: %s", exc)
            return False, jsonify({"ok": False, "msg": f"发送文件失败: {exc}"})
    
    # ===== 应用管理 =====
    def get_apps(self, udid: str, only_list: bool = False) -> Dict[str, Any]:
        """获取应用列表"""
        if not udid:
            return {"ok": False, "msg": "缺少 udid"}
        
        opts = extract_goios_opts(request.args)
        apps_ok, apps_raw = run_with_quick_check_and_escalate(
            self.prechecker,
            udid,
            lambda: self.goios.apps_list(udid, only_list=only_list, **opts)
        )
        apps = parse_ios_apps(apps_raw or "", third_party_only=True)
        return {"ok": apps_ok, "apps": apps, "raw": apps_raw}
    
    def install_app(self, udid: str, file) -> Dict[str, Any]:
        """安装应用"""
        if not udid or not file:
            return {"ok": False, "msg": "缺少 udid 或 IPA 文件"}
        
        install_ready, install_msg = self.prechecker.check_all(udid)
        if not install_ready:
            return {"ok": False, "msg": install_msg}
        
        fname_lower = (file.filename or "").lower()
        if not fname_lower.endswith(".ipa"):
            return {"ok": False, "msg": "仅支持 .ipa 文件"}
        
        filename = secure_filename(file.filename)
        unique_name = f"{uuid.uuid4().hex}_{filename}"
        save_path = os.path.join(self.app.config["UPLOAD_FOLDER"], unique_name)
        file.save(save_path)
        
        opts = extract_goios_opts(request.args or request.form or {})
        install_ok, install_raw = self.goios.install_ipa(udid, save_path, **opts)
        return {"ok": install_ok, "raw": install_raw, "filename": unique_name}
    
    def launch_app(self, udid: str, bundle_id: str, wait: bool = False) -> Dict[str, Any]:
        """启动应用"""
        if not udid or not bundle_id:
            return {"ok": False, "msg": "缺少 udid 或 bundle_id"}
        
        data = request.get_json(force=True, silent=True) or {}
        opts = extract_goios_opts(data)
        launch_ok, launch_raw = run_with_quick_check_and_escalate(
            self.prechecker,
            udid,
            lambda: self.goios.launch_app(udid, bundle_id, wait=wait, **opts)
        )
        return {"ok": launch_ok, "raw": launch_raw}
    
    def kill_app(self, udid: str, bundle_id: str) -> Dict[str, Any]:
        """终止应用"""
        if not udid or not bundle_id:
            return {"ok": False, "msg": "缺少 udid 或 bundle_id"}
        
        data = request.get_json(silent=True) or {}
        opts = extract_goios_opts(data)
        kill_ok, kill_msg = run_with_quick_check_and_escalate(
            self.prechecker,
            udid,
            lambda: self.goios.kill_app(udid=udid, bundle_id=bundle_id, **opts)
        )
        return {"ok": kill_ok, "msg": kill_msg}
    
    # ===== 设备控制 =====
    def reboot_device(self, udid: str) -> Dict[str, Any]:
        """重启设备"""
        if not udid:
            return {"ok": False, "msg": "缺少 udid"}
        
        data = request.get_json(silent=True) or {}
        opts = extract_goios_opts(data)
        reboot_ok, reboot_raw = run_with_quick_check_and_escalate(
            self.prechecker,
            udid,
            lambda: self.goios.reboot(udid, **opts)
        )
        return {"ok": reboot_ok, "raw": reboot_raw}
    
    def get_battery_info(self, udid: str) -> Dict[str, Any]:
        """获取电池信息"""
        if not udid:
            return {"ok": False, "msg": "缺少 udid"}
        
        opts = extract_goios_opts(request.args)
        battery_ok, battery_raw = run_with_quick_check_and_escalate(
            self.prechecker,
            udid,
            lambda: self.goios.battery_info(udid, **opts)
        )
        return {"ok": battery_ok, "raw": battery_raw}
    
    def get_battery_detail(self, udid: str) -> Dict[str, Any]:
        """获取详细电池信息"""
        if not udid:
            return {"ok": False, "msg": "缺少 udid"}
        
        opts = extract_goios_opts(request.args)
        b_ok, b_raw = run_with_quick_check_and_escalate(self.prechecker, udid, lambda: self.goios.battery_info(udid, **opts))
        r_ok, r_raw = run_with_quick_check_and_escalate(self.prechecker, udid, lambda: self.goios.battery_registry(udid, **opts))
        
        detail = {"batterycheck": None, "batteryregistry": None}
        try:
            detail["batterycheck"] = json.loads(b_raw) if b_ok and b_raw else None
        except json.JSONDecodeError:
            detail["batterycheck"] = None
        try:
            detail["batteryregistry"] = json.loads(r_raw) if r_ok and r_raw else None
        except json.JSONDecodeError:
            detail["batteryregistry"] = None
        
        return {"ok": True, "detail": detail, "raw": {"batterycheck": b_raw, "batteryregistry": r_raw}}
    
    def get_diskspace(self, udid: str) -> Dict[str, Any]:
        """获取磁盘空间信息"""
        if not udid:
            return {"ok": False, "msg": "缺少 udid"}
        
        opts = extract_goios_opts(request.args)
        disk_ok, disk_raw = run_with_quick_check_and_escalate(
            self.prechecker,
            udid,
            lambda: self.goios.diskspace(udid, **opts)
        )
        return {"ok": disk_ok, "raw": disk_raw}
    
    # ===== 截屏流管理 =====
    def start_screenshot_stream(self, udid: str, port: int = 3333) -> Dict[str, Any]:
        """启动截屏流"""
        if not udid:
            return {"ok": False, "msg": "缺少 udid"}
        
        # 为提高成功率：在启动截图流前执行完整检查
        ready_ok, ready_msg = self.prechecker.check_all(udid)
        if not ready_ok:
            return {"ok": False, "msg": ready_msg}
        
        # 检查端口是否已被占用
        if not check_port_available(port):
            new_port = find_free_port(start_port=port + 1, max_tries=10)
            if new_port:
                self.app.logger.warning("端口 %d 已占用，使用端口 %d", port, new_port)
                port = new_port
            else:
                return {"ok": False, "msg": f"端口 {port} 已占用且无可用端口"}
        
        opts = extract_goios_opts(request.get_json(silent=True) or {})
        
        # 添加 tunnel 参数以提升连接稳定性
        extra_opts = self.tunnel.get_goios_opts(udid) if hasattr(self.tunnel, 'get_goios_opts') else {}
        opts.update(extra_opts)
        
        # 注入环境变量提升兼容性
        opts["extra_env"] = {"ENABLE_GO_IOS_AGENT": "user"}
        
        self.app.logger.debug("启动 MJPEG 流: port=%d, opts=%s", port, opts)
        p = self.goios.screenshot_stream_popen(udid=udid, port=port, **opts)
        if p is None:
            return {"ok": False, "msg": "启动 MJPEG 流失败"}
        
        # 等待 MJPEG 服务器启动
        server_ip = get_local_ip() or "127.0.0.1"
        mjpeg_url = f"http://{server_ip}:{port}"
        
        if not wait_for_mjpeg_stream(mjpeg_url, p, max_wait_seconds=15, logger=self.app.logger):
            # 如果使用服务器 IP 失败，尝试 localhost 作为回退
            if server_ip != "127.0.0.1":
                self.app.logger.warning("使用服务器 IP %s 连接失败，尝试 localhost 回退", server_ip)
                mjpeg_url_fallback = f"http://127.0.0.1:{port}"
                if wait_for_mjpeg_stream(mjpeg_url_fallback, p, max_wait_seconds=10, logger=self.app.logger):
                    mjpeg_url = mjpeg_url_fallback
                else:
                    try:
                        p.terminate()
                        p.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    return {"ok": False, "msg": "MJPEG 流超时未就绪 (多种地址尝试失败)"}
            else:
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                return {"ok": False, "msg": "MJPEG 流超时未就绪 (15秒)"}
        
        sid = uuid.uuid4().hex
        self.streams.setdefault(udid, {})[sid] = {"p": p, "port": port}
        
        parts = urlsplit(request.host_url)
        url = urlunsplit((parts.scheme, f"{parts.hostname}:{port}", "", "", ""))
        
        # 开启服务端录制
        model = get_device_model(udid, self.goios)
        ts = now_timestamp_str()
        base = f"{model}_{udid}_record_{ts}"
        
        # 启动进程监控线程
        def monitor_process():
            while True:
                if udid in self.streams and sid in self.streams[udid]:
                    current_p = self.streams[udid][sid].get("p")
                    if current_p and current_p.poll() is not None:
                        self.app.logger.error("MJPEG 流进程意外退出 (退出码: %s)", current_p.returncode)
                        if udid in self.streams and sid in self.streams[udid]:
                            self.app.logger.debug("尝试重启 MJPEG 流...")
                            new_p = self.goios.screenshot_stream_popen(udid=udid, port=port, **opts)
                            if new_p:
                                self.streams[udid][sid]["p"] = new_p
                                self.app.logger.debug("MJPEG 流已重启")
                            else:
                                self.app.logger.error("MJPEG 流重启失败")
                                break
                        else:
                            self.app.logger.debug("检测到会话已停止，退出监控线程")
                            break
                else:
                    self.app.logger.debug("流会话已被清理，退出监控线程")
                    break
                time.sleep(5)
        
        monitor_thread = threading.Thread(target=monitor_process, daemon=True, name=f"Monitor-{sid}")
        monitor_thread.start()
        
        # 延迟启动录制，确保 MJPEG 流完全稳定
        time.sleep(2.0)
        
        # 使用录屏专用目录
        recordings_dir = os.path.join(self.app.config["UPLOAD_FOLDER"], "recordings")
        os.makedirs(recordings_dir, exist_ok=True)
        rec_ctx = start_mjpeg_to_mp4(mjpeg_url=mjpeg_url, out_dir=recordings_dir, basename=base, logger=self.app.logger)
        
        self.streams[udid][sid].update({
            "rec_ctx": rec_ctx,
            "rec_path": rec_ctx.get("path"),
            "rec_name": rec_ctx.get("name"),
            "monitor_thread": monitor_thread
        })
        
        return {
            "ok": True,
            "id": sid,
            "url": url,
            "port": port,
            "msg": f"MJPEG 流已启动 (端口: {port})"
        }
    
    def stop_screenshot_stream(self, udid: str, stream_id: str) -> Dict[str, Any]:
        """停止截屏流"""
        if not udid or not stream_id:
            return {"ok": False, "msg": "缺少 udid/id"}
        
        # 先查找流信息
        udid_streams = self.streams.get(udid, {})
        if stream_id not in udid_streams:
            return {"ok": False, "msg": "无此会话"}
        
        # 获取流信息
        info = udid_streams[stream_id]
        if not isinstance(info, dict) or "p" not in info:
            return {"ok": False, "msg": "会话信息无效"}
        
        # 停止进程
        terminate_process(info["p"])
        
        # 停止录制
        rec_ctx = info.get("rec_ctx")
        if rec_ctx:
            stop_recorder(rec_ctx)
        
        # 清理监控线程
        monitor_thread = info.get("monitor_thread")
        if monitor_thread and monitor_thread.is_alive():
            self.app.logger.debug("清理监控线程")
        
        # 删除流信息
        del udid_streams[stream_id]
        if not udid_streams:
            self.streams.pop(udid, None)
        
        rec_path = (rec_ctx or {}).get("path")
        rec_name = (rec_ctx or {}).get("name")
        
        # 生成签名下载链接
        download_url = None
        if rec_name and os.path.exists(rec_path or ""):
            # 计算相对于UPLOAD_FOLDER的相对路径
            rel = str(os.path.relpath(rec_path, self.app.config["UPLOAD_FOLDER"]).replace("\\", "/"))
            token = create_signed_download_token(rel, ttl_seconds=600)
            download_url = f"/api/download_secure/{token}"
        
        return {"ok": True, "file": rec_name, "download_url": download_url}
    
    # ===== 端口转发管理 =====
    def start_port_forward(self, udid: str, host_port: int, target_port: int) -> Dict[str, Any]:
        """启动端口转发"""
        if not udid:
            return {"ok": False, "msg": "缺少 udid"}
        
        fwd_ready, fwd_msg = self.prechecker.check_all(udid)
        if not fwd_ready:
            return {"ok": False, "msg": fwd_msg}
        
        if not host_port or not target_port:
            return {"ok": False, "msg": "缺少 host_port/target_port"}
        
        data = request.get_json(force=True, silent=True) or {}
        opts = extract_goios_opts(data)
        p = self.goios.forward_popen(udid=udid, host_port=host_port, target_port=target_port, **opts)
        if p is None:
            return {"ok": False, "msg": "启动失败"}
        
        fid = uuid.uuid4().hex
        self.forwards.setdefault(udid, {})[fid] = {"p": p, "host_port": host_port, "target_port": target_port}
        return {"ok": True, "id": fid, "udid": udid}
    
    def stop_port_forward(self, udid: str, fid: str) -> Dict[str, Any]:
        """停止端口转发"""
        if not udid or not fid:
            return {"ok": False, "msg": "缺少 udid/id"}
        
        info = (self.forwards.get(udid) or {}).pop(fid, None)
        if not isinstance(info, dict) or "p" not in info:
            return {"ok": False, "msg": "无此转发会话"}
        
        terminate_process(info["p"])
        return {"ok": True}
    
    # ===== 文件下载 =====
    def download_file(self, fname: str):
        """下载文件"""
        safe_name = os.path.basename(fname)
        full_path = os.path.join(self.app.config["UPLOAD_FOLDER"], safe_name)
        if not os.path.exists(full_path):
            return jsonify({"ok": False, "msg": "文件不存在"}), 404
        return send_from_directory(self.app.config["UPLOAD_FOLDER"], safe_name, as_attachment=True)
    
    def download_secure_file(self, token: str):
        """安全下载文件"""
        try:
            fname = consume_signed_download_token(token)
        except KeyError:
            return abort(403)
        
        safe_rel = fname.replace("\\", "/").lstrip("/")
        full_path = os.path.normpath(os.path.join(self.app.config["UPLOAD_FOLDER"], safe_rel))
        if not full_path.startswith(os.path.normpath(self.app.config["UPLOAD_FOLDER"])):
            return abort(403)
        if not os.path.exists(full_path):
            return jsonify({"ok": False, "msg": "文件不存在"}), 404
        
        directory = os.path.dirname(os.path.relpath(full_path, self.app.config["UPLOAD_FOLDER"]))
        filename = os.path.basename(full_path)
        base_dir = str(self.app.config["UPLOAD_FOLDER"])
        dir_part = str(directory)
        send_dir = base_dir if dir_part == "." or dir_part == "" else os.path.join(base_dir, dir_part)
        resp = send_from_directory(send_dir, str(filename), as_attachment=True)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    
    # ===== 运行中应用 =====
    def get_running_apps(self, udid: str) -> Dict[str, Any]:
        """获取运行中的应用"""
        if not udid:
            return {"ok": False, "msg": "缺少 udid"}
        
        opts = extract_goios_opts(request.args)
        ps_ok, ps_raw = run_with_quick_check_and_escalate(self.prechecker, udid, lambda: self.goios.ps_apps(udid, **opts))
        processes = parse_ps_apps_raw(ps_raw or "") if ps_ok and ps_raw else []
        return {"ok": ps_ok, "list": processes, "raw": ps_raw}
    
    # ===== Crash 日志管理 =====
    def list_crash_logs(self, udid: str, pattern: str = "", limit: int = 100) -> Dict[str, Any]:
        """列出 Crash 日志"""
        if not udid:
            return {"ok": False, "msg": "缺少 udid"}
        
        ok_pre, pre_msg = self.prechecker.quick_check(udid)
        if not ok_pre:
            return {"ok": False, "msg": pre_msg}
        
        # 模式：含通配符(*, ?)走 go-ios 侧过滤；否则后端全量+不区分大小写包含匹配
        has_glob = any(ch in pattern for ch in ("*", "?")) if pattern else False
        go_pattern = pattern if has_glob else None
        
        ok_list, raw = run_with_quick_check_and_escalate(
            self.prechecker, udid, lambda: self.goios.crash_ls(udid, go_pattern)
        )
        items = parse_crash_ls_items(raw) if ok_list and raw else []
        
        # 如果是全量列出（无 pattern），则尝试递归列出一些大目录 (DiagnosticLogs, Retired, Assistant)
        if ok_list and not pattern:
            std_subdirs = ["DiagnosticLogs", "Retired", "Assistant"]
            found_subdirs = [x for x in items if x in std_subdirs]
            
            for subdir in found_subdirs:
                # 尝试列出子目录内容
                ok_sub, raw_sub = self.goios.crash_ls(udid, f"{subdir}/*")
                if ok_sub and raw_sub:
                    sub_items = parse_crash_ls_items(raw_sub)
                    # 将子目录下的文件加入列表（包含子目录前缀以便下载/导出）
                    for si in sub_items:
                        items.append(f"{subdir}/{si}")

        if ok_list and items and pattern and not has_glob:
            kw = pattern.lower()
            items = [x for x in items if kw in x.lower()]
        
        # 统计总数（过滤后，截断前）
        total_count = len(items)
        
        # 应用数量限制（0 或负数表示不限制）
        if 0 < limit < len(items):
            items = items[:limit]
        
        return {
            "ok": ok_list,
            "items": items,
            "total": total_count,
            "limit": limit,
            "raw": raw
        }
    
    def export_crash_logs(self, udid: str, pattern: str = "*", patterns: list = None) -> Dict[str, Any]:
        """导出 Crash 日志"""
        if not udid:
            return {"ok": False, "msg": "缺少 udid"}
        
        crash_root = os.path.join(self.app.config["UPLOAD_FOLDER"], "crashes")
        os.makedirs(crash_root, exist_ok=True)
        
        def _do_export():
            res = crash_export_collect(
                self.goios, udid,
                patterns if patterns else [pattern],
                crash_root,
                logger=self.app.logger
            )
            return bool(res.get("ok")), res
        
        ok_flag, result = run_with_quick_check_and_escalate(self.prechecker, udid, _do_export)
        files = (result or {}).get("files") or []
        self.app.logger.info("Crash 导出: udid=%s, ok=%s, files=%s", udid, ok_flag, files)
        download_url = None
        zip_name = ""
        
        if ok_flag:
            count = len(files)
            if count == 1:
                fp = files[0]
                if os.path.exists(fp):
                    rel = str(os.path.relpath(fp, self.app.config["UPLOAD_FOLDER"]).replace("\\", "/"))
                    token = create_signed_download_token(rel, ttl_seconds=1800)
                    download_url = f"/api/download_secure/{token}"
            elif count >= 2:
                zip_path = crash_zip_dir((result or {}).get("batch_dir"), crash_root, logger=self.app.logger)
                if zip_path and os.path.exists(zip_path):
                    zip_name = os.path.basename(zip_path)
                    rel = str(os.path.relpath(zip_path, self.app.config["UPLOAD_FOLDER"]).replace("\\", "/"))
                    token = create_signed_download_token(rel, ttl_seconds=1800)
                    download_url = f"/api/download_secure/{token}"
        
        return {
            "ok": ok_flag,
            "zip": zip_name,
            "download_url": download_url,
            "download_urls": None,
            "raw": (result or {}).get("raw")
        }
    
    def remove_crash_logs(self, udid: str, pattern: str = "*", patterns: list = None, cwd: str = ".", recursive: bool = True) -> Dict[str, Any]:
        """删除 Crash 日志"""
        if not udid:
            return {"ok": False, "msg": "缺少 udid"}
        
        def _do_rm():
            res = crash_remove_many(
                self.goios, udid,
                patterns if patterns else [pattern],
                cwd=cwd,
                recursive=recursive
            )
            return bool(res.get("ok")), res
        
        ok_rm, res_rm = run_with_quick_check_and_escalate(self.prechecker, udid, _do_rm)
        
        raw_output = (res_rm or {}).get("raw", "")
        success = bool((res_rm or {}).get("ok"))
        
        # 提取删除的文件名
        deleted_files = []
        try:
            for line in raw_output.strip().splitlines():
                if line.strip().startswith("{"):
                    obj = json.loads(line)
                    if obj.get("msg") == "delete" and "path" in obj:
                        path = obj["path"]
                        if "." in os.path.basename(path):
                            deleted_files.append(path)
        except Exception as e:
            self.app.logger.warning("Crash 删除日志解析失败: %s", e)
        
        if deleted_files:
            self.app.logger.info("Crash 删除: udid=%s, success=%s, deleted_files=%s",
                                udid, success, deleted_files)
        else:
            snippet = raw_output[:200] + "..." if len(raw_output) > 200 else raw_output
            self.app.logger.info("Crash 删除: udid=%s, success=%s, raw_snippet=%s",
                                udid, success, snippet)
        
        if not success and raw_output:
            if "permission denied" in raw_output.lower() or "access denied" in raw_output.lower():
                error_msg = "权限不足，无法删除某些 crash 文件。可能需要管理员权限或设备解锁。"
            elif "no such file" in raw_output.lower() or "file not found" in raw_output.lower():
                error_msg = "部分文件不存在或已被删除。"
            elif "usage:" in raw_output.lower() or "help" in raw_output.lower():
                error_msg = "删除命令参数不正确，请检查 pattern 和 cwd 参数。"
            else:
                error_msg = f"删除失败: {raw_output}"
            
            return {
                "ok": False,
                "msg": error_msg,
                "raw": raw_output,
                "diagnostic": {
                    "udid": udid,
                    "cwd": cwd,
                    "pattern": pattern,
                    "patterns": patterns,
                    "recursive": recursive,
                    "suggestion": "尝试使用更具体的 pattern 或检查文件权限"
                }
            }
        
        return {
            "ok": success,
            "msg": "删除操作完成" if success else "删除操作失败",
            "raw": raw_output,
            "deleted_files": deleted_files
        }
    
    # ===== 系统日志管理 =====
    def start_syslog(self, udid: str, parse: bool = True) -> Dict[str, Any]:
        """启动系统日志记录"""
        if not udid:
            return {"ok": False, "msg": "缺少 udid"}
        
        ok_pre, pre_msg = self.prechecker.quick_check(udid)
        if not ok_pre:
            return {"ok": False, "msg": pre_msg}
        
        sid = uuid.uuid4().hex
        # 使用原有的iOS日志目录
        ctx = syslog_start_session(self.goios, udid, self.app.config["LOG_DIR"], parse=parse, logger=self.app.logger)
        if not ctx.get("ok"):
            return {"ok": False, "msg": ctx.get("msg") or "启动失败"}
        
        self.syslogs.setdefault(udid, {})[sid] = {"p": ctx.get("p"), "path": ctx.get("path"), "name": ctx.get("name")}
        return {"ok": True, "id": sid, "file": ctx.get("name")}
    
    def stream_syslog(self, udid: str, parse: bool = True, keywords: list = None, levels: list = None):
        """流式获取系统日志"""
        if not udid:
            return jsonify({"ok": False, "msg": "缺少 udid"})
        
        ok_pre, pre_msg = self.prechecker.quick_check(udid)
        if not ok_pre:
            return jsonify({"ok": False, "msg": pre_msg})
        
        def _gen():
            # 查找现有的 syslog 进程
            existing_process = None
            for session_info in self.syslogs.get(udid, {}).values():
                if session_info.get("p"):
                    existing_process = session_info["p"]
                    break
                    
            for chunk in stream_syslog_sse(self.goios, udid, parse=parse, logger=self.app.logger,
                                           keywords=keywords, levels=levels, 
                                           existing_process=existing_process):
                yield chunk
        
        resp = Response(stream_with_context(_gen()), mimetype='text/event-stream')
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Connection"] = "keep-alive"
        return resp
    
    def stop_syslog(self, udid: str, sid: str) -> Dict[str, Any]:
        """停止系统日志记录"""
        if not udid or not sid:
            return {"ok": False, "msg": "缺少 udid/id"}
        
        info = (self.syslogs.get(udid) or {}).pop(sid, None)
        if not info or "p" not in info:
            return {"ok": False, "msg": "无此会话"}
        
        terminate_process(info["p"])
        log_name = info.get("name")
        syslog_file_path = info.get("path")
        download_url = None
        if log_name and syslog_file_path and os.path.exists(syslog_file_path):
            # 使用原有的iOS日志目录
            rel = str(os.path.relpath(syslog_file_path, self.app.config["LOG_DIR"]).replace("\\", "/"))
            token = create_signed_download_token(rel, ttl_seconds=1800)
            download_url = f"/api/download_secure/{token}"
        return {"ok": True, "download_url": download_url}
    
    # ===== 设备事件流 =====
    def get_device_events(self):
        """获取设备事件流"""
        def _gen():
            for chunk in listen_event_stream(self.goios):
                yield chunk
        return Response(stream_with_context(_gen()), mimetype='text/event-stream')
    
    # ===== 调试功能 =====
    def cleanup_processes(self) -> Dict[str, Any]:
        """清理所有ios.exe进程"""
        try:
            from backend_function.common_utils import cleanup_all_ios_processes
            cleanup_all_ios_processes(self.app.logger)
            return {"ok": True, "msg": "ios.exe进程清理完成"}
        except Exception as e:
            self.app.logger.exception("手动清理ios.exe进程异常: %s", e)
            return {"ok": False, "msg": f"清理失败: {str(e)}"}
    
    def export_debug_logs(self) -> Dict[str, Any]:
        """导出应用日志文件用于调试分析"""
        try:
            # 使用固定的日志文件
            source_log_path = os.path.join(self.app.config["LOG_DIR"], "ios_app.log")
            
            self.app.logger.info("Debug日志导出 - 日志文件: %s", source_log_path)
            
            if not os.path.exists(source_log_path):
                return {"ok": False, "msg": "日志文件不存在"}
            
            timestamp = now_timestamp_str()
            copy_filename = f"debug_logs_{timestamp}.log"
            copy_path = os.path.join(self.app.config["UPLOAD_FOLDER"], copy_filename)
            
            try:
                # 直接复制日志文件
                with open(source_log_path, 'r', encoding='utf-8') as src_file:
                    with open(copy_path, 'w', encoding='utf-8') as dst_file:
                        chunk_size = 8192
                        while True:
                            chunk = src_file.read(chunk_size)
                            if not chunk:
                                break
                            dst_file.write(chunk)
                
                rel = str(os.path.relpath(copy_path, self.app.config["UPLOAD_FOLDER"]).replace("\\", "/"))
                token = create_signed_download_token(rel, ttl_seconds=3600)
                download_url = f"/api/download_secure/{token}"
                
                self.app.logger.info("Debug日志导出成功: %s", copy_filename)
                return {
                    "ok": True, 
                    "filename": copy_filename,
                    "download_url": download_url,
                    "msg": "日志导出成功"
                }
                
            except (OSError, IOError) as e:
                self.app.logger.error("复制日志文件失败: %s", e)
                return {"ok": False, "msg": f"复制文件失败: {str(e)}"}
                
        except Exception as e:
            self.app.logger.exception("Debug日志导出异常: %s", e)
            return {"ok": False, "msg": f"导出失败: {str(e)}"}
