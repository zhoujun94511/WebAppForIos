"""
路由处理器
只负责路由定义和参数解析，业务逻辑委托给 APIHandlers
"""

import os
import json
from flask import request, jsonify, send_from_directory, render_template
from backend_function.common_utils import extract_goios_opts, run_with_quick_check_and_escalate


class RouteHandlers:
    """路由处理器"""
    
    def __init__(self, app, api_handlers):
        self.app = app
        self.api = api_handlers
    
    # ===== 基础路由 =====
    def index(self):
        """首页"""
        success, out = self.api.goios.list_devices(details=False)
        devices = []
        if success and out:
            for line in out.splitlines():
                val = line.strip()
                if val and len(val) >= 16:
                    devices.append(val)
        return render_template("index.html", devices=devices)
    
    def favicon(self):
        """网站图标"""
        return send_from_directory(os.path.join(self.app.root_path, 'static', 'wresource'), 'favicon.ico')
    
    @staticmethod
    def guide():
        """引导文档页面"""
        return render_template("ios_introduce_guide_index.html")
    
    @staticmethod
    def health():
        """健康检查"""
        return jsonify({"ok": True}), 200
    
    # ===== 设备管理路由 =====
    def api_devices(self):
        """获取设备列表"""
        details = request.args.get("details", "0") == "1"
        result = self.api.get_devices(details=details)
        if not result["ok"]:
            return jsonify(result), 500
        return jsonify(result)
    
    def api_device_info(self):
        """获取设备信息"""
        udid = request.args.get("udid", "").strip()
        result = self.api.get_device_info(udid)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    def api_screenshot(self):
        """截屏"""
        udid = request.args.get("udid", "").strip()
        success, response = self.api.take_screenshot(udid)
        if not success:
            return response
        return response
    
    # ===== 应用管理路由 =====
    def api_apps(self):
        """获取应用列表"""
        udid = request.args.get("udid", "").strip()
        only_list = request.args.get("list", "0") == "1"
        result = self.api.get_apps(udid, only_list)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    def api_install(self):
        """安装应用"""
        udid = (request.form.get("udid") or "").strip()
        file = request.files.get("file")
        result = self.api.install_app(udid, file)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    def api_launch(self):
        """启动应用"""
        data = request.get_json(force=True, silent=True) or {}
        udid = (data.get("udid") or "").strip()
        bundle_id = (data.get("bundle_id") or "").strip()
        wait = bool(data.get("wait", False))
        result = self.api.launch_app(udid, bundle_id, wait)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    def api_kill(self):
        """终止应用"""
        data = request.get_json(silent=True) or {}
        udid = (data.get("udid") or "").strip()
        bundle_id = (data.get("bundle_id") or "").strip()
        result = self.api.kill_app(udid, bundle_id)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    # ===== 设备控制路由 =====
    def api_reboot(self):
        """重启设备"""
        data = request.get_json(silent=True) or {}
        udid = (data.get("udid") or "").strip()
        result = self.api.reboot_device(udid)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    def api_battery(self):
        """获取电池信息"""
        udid = request.args.get("udid", "").strip()
        result = self.api.get_battery_info(udid)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    def api_battery_detail(self):
        """获取详细电池信息"""
        udid = request.args.get("udid", "").strip()
        result = self.api.get_battery_detail(udid)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    def api_diskspace(self):
        """获取磁盘空间信息"""
        udid = request.args.get("udid", "").strip()
        result = self.api.get_diskspace(udid)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    def api_diskspace_detail(self):
        """获取详细磁盘空间信息"""
        udid = request.args.get("udid", "").strip()
        result = self.api.get_diskspace(udid)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    # ===== 开发者镜像路由 =====
    def api_image_auto(self):
        """按芯片身份自动挂载开发者镜像（对齐 AutoPilot）。"""
        data = request.get_json(silent=True) or {}
        udid = (data.get("udid") or "").strip()
        if not udid:
            return jsonify({"ok": False, "msg": "缺少 udid"}), 400

        from backend_function.config import Config
        from backend_function.ddi_manager import ensure_developer_image, local_ddi_summary

        basedir = (data.get("basedir") or "").strip() or Config.DEVIMAGES_DIR
        opts = extract_goios_opts(data)

        def _do():
            return ensure_developer_image(
                self.api.goios,
                udid,
                basedir=basedir,
                tunnel_opts=opts,
                require_success=True,
            )

        image_ok, image_raw = run_with_quick_check_and_escalate(
            self.api.prechecker,
            udid,
            _do,
        )
        payload = {
            "ok": image_ok,
            "msg": image_raw,
            "raw": image_raw,
            "basedir": basedir,
            "local_ddi": local_ddi_summary(basedir),
        }
        return jsonify(payload), (200 if image_ok else 500)    
    # ===== 设备状态路由 =====
    def api_devicestate_list(self):
        """获取设备状态列表"""
        udid = request.args.get("udid", "").strip() or None
        if not udid:
            return jsonify({"ok": False, "msg": "缺少 udid"}), 400
        

        opts = extract_goios_opts(request.args)
        state_ok, state_raw = run_with_quick_check_and_escalate(
            self.api.prechecker, udid, 
            lambda: self.api.goios.devicestate_list(udid=udid, **opts)
        )
        return jsonify({"ok": state_ok, "raw": state_raw})
    
    def api_devicestate_enable(self):
        """启用设备状态"""
        data = request.get_json(force=True, silent=True) or {}
        udid = (data.get("udid") or "").strip()
        t = (data.get("profile_type_id") or "").strip()
        p = (data.get("profile_id") or "").strip()
        if not (udid and t and p):
            return jsonify({"ok": False, "msg": "缺少 udid / profile_type_id / profile_id"}), 400
        

        opts = extract_goios_opts(data)
        state_enable_ok, state_enable_raw = run_with_quick_check_and_escalate(
            self.api.prechecker, udid, 
            lambda: self.api.goios.devicestate_enable(udid, t, p, **opts)
        )
        return jsonify({"ok": state_enable_ok, "raw": state_enable_raw})
    
    # ===== 模拟位置路由 =====
    def api_setlocation(self):
        """设置位置"""
        data = request.get_json(force=True, silent=True) or {}
        udid = (data.get("udid") or "").strip()
        lat = data.get("lat")
        lon = data.get("lon")
        if not udid or lat is None or lon is None:
            return jsonify({"ok": False, "msg": "缺少 udid/lat/lon"}), 400
        

        opts = extract_goios_opts(data)
        loc_ok, loc_raw = run_with_quick_check_and_escalate(
            self.api.prechecker, udid, 
            lambda: self.api.goios.set_location(udid, float(lat), float(lon), **opts)
        )
        return jsonify({"ok": loc_ok, "raw": loc_raw})
    
    # ===== 截屏流路由 =====
    def api_ss_start(self):
        """启动截屏流"""
        payload = request.get_json(silent=True) or {}
        udid = (payload.get("udid") or "").strip()
        if not udid:
            return jsonify({"ok": False, "msg": "缺少 udid"}), 400
        
        port = int(payload.get("port", 3333)) or 3333
        result = self.api.start_screenshot_stream(udid, port)
        if not result["ok"]:
            return jsonify(result), 500
        return jsonify(result)
    
    def api_ss_stop(self):
        """停止截屏流"""
        payload = request.get_json(silent=True) or {}
        udid = (payload.get("udid") or "").strip()
        stream_id = (payload.get("id") or "").strip()
        
        result = self.api.stop_screenshot_stream(udid, stream_id)
        if not result["ok"]:
            return jsonify(result), 404
        return jsonify(result)
    
    # ===== 端口转发路由 =====
    def api_forward_start(self):
        """启动端口转发"""
        data = request.get_json(force=True, silent=True) or {}
        udid = (data.get("udid") or "").strip()
        host_port = int(data.get("host_port", 0)) or 0
        target_port = int(data.get("target_port", 0)) or 0
        
        result = self.api.start_port_forward(udid, host_port, target_port)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    def api_forward_stop(self):
        """停止端口转发"""
        payload = request.get_json(silent=True) or {}
        udid = (payload.get("udid") or "").strip()
        fid = (payload.get("id") or "").strip()
        
        result = self.api.stop_port_forward(udid, fid)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    # ===== 文件下载路由 =====
    def api_download_file(self, fname: str):
        """下载文件"""
        return self.api.download_file(fname)
    
    def api_download_secure(self, token: str):
        """安全下载文件"""
        return self.api.download_secure_file(token)
    
    # ===== 运行中应用路由 =====
    def api_apps_running(self):
        """获取运行中的应用"""
        udid = request.args.get("udid", "").strip()
        result = self.api.get_running_apps(udid)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    # ===== Crash 日志路由 =====
    def api_crash_ls(self):
        """列出 Crash 日志"""
        udid = request.args.get("udid", "").strip()
        pattern_input = (request.args.get("pattern") or "").strip()
        limit_val = request.args.get("limit", type=int)
        
        if limit_val is not None:
            result = self.api.list_crash_logs(udid, pattern_input, limit=limit_val)
        else:
            result = self.api.list_crash_logs(udid, pattern_input)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    def api_crash_cp(self):
        """复制 Crash 日志"""
        data = request.get_json(silent=True) or {}
        udid = (data.get("udid") or "").strip()
        pattern = (data.get("pattern") or "*").strip() or "*"
        patterns = data.get("patterns") if isinstance(data.get("patterns"), list) else None
        
        result = self.api.export_crash_logs(udid, pattern, patterns)
        return jsonify(result)
    
    def api_crash_rm(self):
        """删除 Crash 日志"""
        data = request.get_json(silent=True) or {}
        udid = (data.get("udid") or "").strip()
        cwd = (data.get("cwd") or ".").strip() or "."
        pattern = (data.get("pattern") or "*").strip() or "*"
        patterns = data.get("patterns") if isinstance(data.get("patterns"), list) else None
        recursive = bool(data.get("recursive", True))
        
        result = self.api.remove_crash_logs(udid, pattern, patterns, cwd, recursive)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    # ===== 配置文件管理路由 =====
    def api_profile_list(self):
        """获取配置文件列表"""
        udid = request.args.get("udid", "").strip()
        if not udid:
            return jsonify({"ok": False, "msg": "缺少 udid"}), 400

        ok_l, raw_l = run_with_quick_check_and_escalate(
            self.api.prechecker, udid, 
            lambda: self.api.goios.profile_list(udid)
        )
        items = []
        if ok_l and raw_l:
            try:
                data = json.loads(raw_l)
                if isinstance(data, list):
                    items = data
            except (json.JSONDecodeError, ValueError):
                items = [s.strip() for s in str(raw_l).splitlines() if s.strip()]
        return jsonify({"ok": ok_l, "items": items, "raw": raw_l})
    
    def api_profile_remove(self):
        """移除配置文件"""
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
            ok_r, raw_r = run_with_quick_check_and_escalate(
                self.api.prechecker, udid, 
                lambda pn=profile_name: self.api.goios.profile_remove(udid, str(pn))
            )
            results.append({"name": profile_name, "ok": ok_r, "raw": raw_r})
            all_ok = all_ok and ok_r
        return jsonify({"ok": all_ok, "results": results})
    
    # ===== 开发者模式路由 =====
    def api_devmode_get(self):
        """获取开发者模式状态"""
        udid = request.args.get("udid", "").strip()
        if not udid:
            return jsonify({"ok": False, "msg": "缺少 udid"}), 400
        

        opts = extract_goios_opts(request.args)
        devmode_ok, devmode_raw = run_with_quick_check_and_escalate(
            self.api.prechecker,
            udid,
            lambda: self.api.goios.devmode_get(udid, **opts)
        )
        return jsonify({"ok": devmode_ok, "raw": devmode_raw})
    
    def api_devmode_check(self):
        """检查开发者模式"""
        udid = request.args.get("udid", "").strip()
        if not udid:
            return jsonify({"ok": False, "msg": "缺少 udid"}), 400
        
        try:
            ok_pre, pre_msg = self.api.prechecker.check_all(udid)
            if not ok_pre:
                return jsonify({"ok": False, "raw": f"设备检查失败: {pre_msg}"}), 500
            
            tunnel_opts = self.api.tunnel.get_goios_opts(udid) if hasattr(self.api.tunnel, 'get_goios_opts') else {}
            devmode_ok, devmode_raw = self.api.goios.devmode_get(udid, **tunnel_opts)
            return jsonify({"ok": devmode_ok, "raw": devmode_raw})
        except Exception as e:
            self.app.logger.exception("开发者模式检测异常: %s", e)
            return jsonify({"ok": False, "raw": str(e)}), 500
    
    def api_devmode_enable(self):
        """启用开发者模式"""
        data = request.get_json(silent=True) or {}
        udid = (data.get("udid") or "").strip()
        enable_post_restart = bool(data.get("enable_post_restart", True))
        if not udid:
            return jsonify({"ok": False, "msg": "缺少 udid"}), 400
        

        opts = extract_goios_opts(data)
        devmode_ok, devmode_raw = run_with_quick_check_and_escalate(
            self.api.prechecker,
            udid,
            lambda: self.api.goios.devmode_enable(udid, enable_post_restart=enable_post_restart, **opts)
        )
        return jsonify({"ok": devmode_ok, "raw": devmode_raw})
    
    # ===== 辅助功能路由 =====
    def api_assistive(self, feature: str, action: str):
        """辅助功能控制"""
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

        ok_a, raw_a = run_with_quick_check_and_escalate(
            self.api.prechecker, udid, 
            lambda: self.api.goios.assistive(udid, feature, action, force=force)
        )
        return jsonify({"ok": ok_a, "raw": raw_a})
    
    # ===== 设备事件路由 =====
    def api_devices_events(self):
        """设备事件流"""
        return self.api.get_device_events()
    
    # ===== 系统日志路由 =====
    def api_syslog_start(self):
        """启动系统日志记录"""
        data = request.get_json(silent=True) or {}
        udid = (data.get("udid") or "").strip()
        parse = bool(data.get("parse", True))
        
        result = self.api.start_syslog(udid, parse)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    def api_syslog_stream(self):
        """流式获取系统日志"""
        udid = (request.args.get("udid") or "").strip()
        parse = request.args.get("parse", "1") != "0"
        keywords = [s.strip() for s in (request.args.get("kw") or "").split(",") if s.strip()]
        levels = [s.strip().lower() for s in (request.args.get("lv") or "").split(",") if s.strip()]
        
        return self.api.stream_syslog(udid, parse, keywords, levels)
    
    def api_syslog_stop(self):
        """停止系统日志记录"""
        data = request.get_json(silent=True) or {}
        udid = (data.get("udid") or "").strip()
        sid = (data.get("id") or "").strip()
        
        result = self.api.stop_syslog(udid, sid)
        if not result["ok"]:
            return jsonify(result), 400
        return jsonify(result)
    
    # ===== 调试功能路由 =====
    def api_debug_cleanup_processes(self):
        """手动清理所有ios.exe进程"""
        result = self.api.cleanup_processes()
        if not result["ok"]:
            return jsonify(result), 500
        return jsonify(result)
    
    def api_debug_export_logs(self):
        """导出应用日志文件用于调试分析"""
        result = self.api.export_debug_logs()
        if not result["ok"]:
            return jsonify(result), 500
        return jsonify(result)
