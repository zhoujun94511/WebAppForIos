# -*- coding: utf-8 -*-
import os
import json
import time
import logging
import platform
from typing import Tuple, Dict, Any, Optional

from .goios_wrapper import GoIOSManager
from .config import Config

logger = logging.getLogger(__name__)


class TunnelManager:
    """
    管理 go-ios tunnel（隧道）的启动、停止和状态查询。
    主要用于 iOS 17+ 设备的通信。
    """

    def __init__(self, goios_manager: GoIOSManager):
        self.goios = goios_manager
        self.info_port: Optional[int] = None
        self._device_opts: Dict[str, Any] = {}

    @staticmethod
    def _check_system_requirements() -> Tuple[bool, str]:
        """检查系统要求"""
        system = platform.system().lower()
        
        if system == "windows":
            # 检查wintun.dll
            wintun_path = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'system32', 'wintun.dll')
            if not os.path.exists(wintun_path):
                return False, (
                    "Windows系统缺少wintun.dll依赖\n"
                    "tunnel需要wintun.dll才能正常工作\n"
                    "请检查文件是否存在: C:/Windows/system32/wintun.dll"
                )
            
            # 检查管理员权限（可选，因为可以使用userspace模式）
            try:
                import ctypes
                is_admin = ctypes.windll.shell32.IsUserAnAdmin()
                if not is_admin:
                    logger.debug("未检测到管理员权限，将使用userspace模式")
            except (ImportError, AttributeError, OSError):
                logger.warning("无法检查Windows管理员权限")
                
        elif system in ["linux", "darwin"]:
            # 检查是否为root或sudo权限
            if os.geteuid() != 0:
                return False, (
                    f"{system.title()}系统需要root权限启动tunnel\n"
                    "请使用 sudo 权限运行应用"
                )
        
        return True, "系统要求检查通过"

    @staticmethod
    def _get_windows_arch() -> str:
        """获取Windows系统架构"""
        arch = platform.machine().lower()
        
        # 映射架构名称
        arch_mapping = {
            'amd64': 'amd64',
            'x86_64': 'amd64', 
            'x86': 'x86',
            'i386': 'x86',
            'i686': 'x86',
            'arm64': 'arm64',
            'aarch64': 'arm64',
            'arm': 'arm'
        }
        
        return arch_mapping.get(arch, 'amd64')  # 默认amd64

    def _auto_install_wintun(self) -> Tuple[bool, str]:
        """自动安装 wintun.dll 到系统目录"""
        try:
            arch = self._get_windows_arch()
            logger.debug("开始安装 wintun.dll (架构: %s)", arch)
            
            # 构建源文件路径（新结构：IOSPrechecker/wintun/{arch}/wintun.dll）
            wintun_source = os.path.join(Config.IOS_HOME, 'wintun', arch, 'wintun.dll')
            wintun_target = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'system32', 'wintun.dll')
            
            if not os.path.exists(wintun_source):
                return False, (
                    f"未找到适合当前架构({arch})的 wintun.dll\n"
                    f"源文件路径: {wintun_source}\n"
                    "请检查 IOSPrechecker/wintun/{amd64|x86|arm|arm64} 目录结构"
                )
            
            # 检查管理员权限
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                return False, (
                    "需要管理员权限才能安装 wintun.dll\n"
                    "请以管理员身份重新启动应用"
                )
            
            # 复制文件
            import shutil
            shutil.copy2(wintun_source, wintun_target)
            
            # 验证复制成功
            if os.path.exists(wintun_target):
                logger.debug("成功复制 wintun.dll: %s -> %s", wintun_source, wintun_target)
                return True, f"wintun.dll 已自动安装 (架构: {arch})"
            else:
                return False, "wintun.dll 复制失败，目标文件不存在"
                
        except ImportError as e:
            return False, f"缺少必要模块: {e}"
        except PermissionError:
            return False, (
                "权限不足，无法复制到系统目录\n"
                "请确保以管理员权限运行应用"
            )
        except Exception as e:
            logger.exception("自动安装 wintun.dll 失败")
            return False, f"安装 wintun.dll 时出错: {e}"

    def start(self, userspace: bool = True, tunnel_info_port: Optional[int] = None, retry_count: int = 3) -> Tuple[
        bool, str]:
        """
        启动 tunnel，支持重试机制
        :param userspace: 是否使用 --userspace 模式（推荐Windows使用）
        :param tunnel_info_port: 可选，指定 tunnel_info_port
        :param retry_count: 重试次数
        """
        # 检查系统要求
        system = platform.system().lower()
        if not userspace and system != "windows":
            # 非用户态模式需要检查权限
            ok, msg = TunnelManager._check_system_requirements()
            if not ok:
                logger.warning("系统要求检查失败: %s", msg)
                # 不直接返回失败，而是强制使用用户态模式
                logger.debug("自动切换到用户态模式(--userspace)")
                userspace = True
        
        cmd = ["tunnel", "start"]
        if userspace:
            cmd.append("--userspace")
        if tunnel_info_port:
            cmd.append(f"--tunnel-info-port={tunnel_info_port}")

        logger.debug("TunnelManager.start(): cmd=%s (系统: %s)", " ".join(cmd), system)

        for attempt in range(retry_count):
            logger.debug("尝试启动tunnel (第%d次)...", attempt + 1)

            # 先检查是否已经在运行
            if attempt > 0:
                is_running, status_msg = self.status(_tunnel_info_port=tunnel_info_port)
                if is_running:
                    logger.debug("Tunnel 已在运行: %s", status_msg)
                    # 若调用方传入端口，记住它；否则保留原值
                    if tunnel_info_port:
                        self.info_port = tunnel_info_port
                    return True, "Tunnel 已启动"

            # 增加超时时间，Windows下tunnel启动可能比较慢
            timeout = 60 if attempt == 0 else 90
            # 在 Windows 用户态模式下注入 agent 变量以提升稳定性
            extra_env = {"ENABLE_GO_IOS_AGENT": "user"} if userspace else {}
            code, out, err = self.goios.run(cmd, timeout=timeout, extra_env=extra_env)

            if code == 0:
                logger.debug("Tunnel 启动命令返回成功，等待就绪...")
                # 启动后等待就绪：轮询 ls 最多 10 秒（仅打印首尾日志）
                ready = False
                for i in range(10):
                    ok, _msg = self.status(_tunnel_info_port=tunnel_info_port)
                    if ok:
                        ready = True
                        break
                    time.sleep(1)
                if ready:
                    logger.debug("Go-iOS Agent is ready")
                    # 记录端口（若未传入，则保留现有值或 None）
                    if tunnel_info_port:
                        self.info_port = tunnel_info_port
                    return True, "Tunnel 启动成功"
                logger.warning("Tunnel 启动命令成功但状态检查失败，继续重试...")
            else:
                error_msg = err.strip() or out.strip() or "未知错误"
                logger.warning("Tunnel 启动失败 (第%d次): %s", attempt + 1, error_msg)

                # 如果是端口冲突，尝试不同端口
                if "address already in use" in error_msg.lower() or "端口" in error_msg:
                    if tunnel_info_port:
                        tunnel_info_port += 1
                        cmd = ["tunnel", "start"]
                        if userspace:
                            cmd.append("--userspace")
                        cmd.append(f"--tunnel-info-port={tunnel_info_port}")
                        logger.debug("端口冲突，尝试新端口: %d", tunnel_info_port)

                if attempt < retry_count - 1:
                    time.sleep(3)  # 重试前等待

        return False, f"Tunnel 启动失败，已重试{retry_count}次"

    def _cache_from_ls_raw(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            maybe = data.get("tunnels") or data.get("list") or []
            if isinstance(maybe, list):
                items = maybe
        for obj in items:
            if not isinstance(obj, dict):
                continue
            # 兼容不同大小写/命名
            udid = obj.get("udid") or obj.get("UDID") or obj.get("Udid")
            if not udid:
                continue
            address = obj.get("address") or obj.get("Address")
            rsd_port = obj.get("rsdPort") or obj.get("rsd_port") or obj.get("RsdPort")
            userspace_port = (obj.get("userspaceTunPort") or obj.get("userspacePort") or 
                             obj.get("userspace_port") or obj.get("UserspacePort"))
            opts: Dict[str, Any] = {}
            if address:
                opts["address"] = address
            if rsd_port:
                try:
                    opts["rsd_port"] = int(rsd_port)
                except (ValueError, TypeError):
                    pass
            if userspace_port:
                try:
                    opts["userspace_port"] = int(userspace_port)
                except (ValueError, TypeError):
                    pass
            if opts:
                self._device_opts[udid] = opts

    def status(self, _tunnel_info_port: Optional[int] = None) -> Tuple[bool, str]:
        """
        查询 tunnel 状态
        使用 `ios tunnel ls`，并解析输出判断是否 agent 已运行
        """
        args = ["tunnel", "ls"]
        # 为了兼容 go-ios 使用的同一环境，用户态时也注入 agent 环境变量
        extra_env = {"ENABLE_GO_IOS_AGENT": "user"}
        code, out, err = self.goios.run(args, timeout=15, extra_env=extra_env)
        raw = out.strip() or err or ""
        logger.debug("Tunnel 状态输出: %s", raw)

        # 缓存 JSON 信息
        if raw.startswith("[") or raw.startswith("{"):
            self._cache_from_ls_raw(raw)

        # 判断 agent 是否未运行
        if "not running" in raw.lower():
            return False, "agent 未运行"
        if "failed to get tunnel info" in raw.lower():
            return False, "tunnel server 未响应"
        if "connectex: No connection could be made" in raw:
            return False, "tunnel服务连接失败"

        # 返回码 0 且包含 JSON 列表，说明 agent 已运行
        if code == 0:
            # 检查是否包含有效的tunnel信息
            if raw.startswith("[") or raw.startswith("{"):
                try:
                    data = json.loads(raw)
                    # 空数组表示没有活动的 tunnel
                    if isinstance(data, list) and len(data) == 0:
                        return False, "没有活动的tunnel"
                    # 非空数组或有效对象表示有活动的 tunnel
                    if isinstance(data, list) and len(data) > 0:
                        logger.debug("Tunnel 状态: 已运行")
                        return True, raw
                    if isinstance(data, dict) and data:
                        logger.debug("Tunnel 状态: 已运行")
                        return True, raw
                except json.JSONDecodeError:
                    pass
            elif "no tunnels running" in raw.lower():
                return False, "没有活动的tunnel"

        # 默认兜底
        return code == 0, raw or "未知 tunnel 状态"

    def stop(self) -> Tuple[bool, str]:
        """
        停止 tunnel
        """
        code, out, err = self.goios.run(["tunnel", "stopagent"], timeout=30)
        ok = (code == 0)
        msg = out.strip() or err or ("停止成功" if ok else "停止失败")
        if ok:
            logger.debug("Tunnel 停止成功: %s", msg)
        else:
            logger.warning("Tunnel 停止失败: %s", msg)
        return ok, msg

    def get_info_port(self) -> Optional[int]:
        return self.info_port

    def check_windows_wintun(self) -> Tuple[bool, str]:
        """检查Windows系统的wintun.dll是否存在，缺失时尝试自动安装"""
        if platform.system().lower() != "windows":
            return True, "非Windows系统，无需检查wintun.dll"
        
        wintun_path = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'system32', 'wintun.dll')
        if os.path.exists(wintun_path):
            return True, "wintun.dll 已存在"
        else:
            # 尝试自动安装
            logger.debug("检测到 wintun.dll 缺失，尝试自动安装...")
            install_ok, install_msg = self._auto_install_wintun()
            if install_ok:
                return True, f"wintun.dll 自动安装成功: {install_msg}"
            else:
                return False, (
                    "检测到Windows系统缺少 wintun.dll\n\n"
                    f"自动安装失败: {install_msg}\n\n"
                    "手动安装步骤：\n"
                    "1. 确保以管理员权限运行应用\n"
                    "2. 检查 IOSPrechecker/wintun/{amd64|x86|arm|arm64} 目录结构\n"
                    "3. 或访问 https://git.zx2c4.com/wintun 手动下载"
                )

    def get_goios_opts(self, udid: str) -> Dict[str, Any]:
        opts = dict(self._device_opts.get(udid, {}))
        if self.info_port:
            opts["tunnel_info_port"] = self.info_port
        return opts