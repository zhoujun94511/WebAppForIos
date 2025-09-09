# -*- coding: utf-8 -*-
import time
import logging
import threading
from typing import Tuple
from .tunnel_manager import TunnelManager
from .goios_wrapper import GoIOSManager
from .common_utils import find_free_port
from .config import Config

logger = logging.getLogger(__name__)


class IOSPrechecker:
    """
    iOS 操作前的统一检查器：
    1. UDID 是否存在
    2. go-ios tunnel 是否已启动（iOS17+ 必需）
    3. 是否挂载了 Developer Image
    """

    def __init__(self, manager: GoIOSManager, tunnel: TunnelManager):
        self.m = manager
        self.tunnel = tunnel
        self._tunnel_lock = threading.Lock()
        self._tunnel_start_time = 0

    def check_all(self, udid: str, skip_tunnel_check: bool = False) -> Tuple[bool, str]:
        """
        执行全量检查
        :param udid: 设备UDID
        :param skip_tunnel_check: 是否跳过tunnel检查（用于某些不需要tunnel的操作）
        返回: (ok, msg)
        """
        # 1. 检查 UDID 是否存在
        ok, out = self.m.list_devices(details=False)
        if not ok:
            return False, f"无法获取设备列表，请检查:\n1. iOS设备是否已连接\n2. 是否已信任此电脑\n3. go-ios是否正确安装"

        if not out or udid not in out:
            return False, f"设备 {udid} 未连接或未被识别"

        # 2. 检查 tunnel 状态（可选跳过）
        if not skip_tunnel_check:
            ok, tunnel_msg = self._ensure_tunnel_running()
            if not ok:
                return False, tunnel_msg

        # 3. 检查是否挂载开发者镜像（对于某些操作是必需的）
        try:
            # 先获取隧道参数再挂载镜像
            extra_opts = self.tunnel.get_goios_opts(udid) if hasattr(self.tunnel, 'get_goios_opts') else {}
            ok, out = self.m.image_auto(udid, basedir=Config.DEVIMAGES_DIR, extra_env={"ENABLE_GO_IOS_AGENT": "user"}, **extra_opts)
            if not ok:
                logger.warning("开发者镜像挂载失败，某些功能可能受限: %s", out)
                # 不作为致命错误，某些操作不需要开发者镜像
        except Exception as exc:
            logger.warning("检查开发者镜像时异常: %s", exc)

        return True, "设备检查通过"

    def _ensure_tunnel_running(self) -> Tuple[bool, str]:
        """
        确保tunnel正在运行，包含智能重试逻辑
        """
        with self._tunnel_lock:
            # 首先检查tunnel状态
            is_running, status_msg = self.tunnel.status()
            if is_running:
                logger.info("Tunnel 已在运行")
                return True, "Tunnel 正常运行"

            # 如果最近刚尝试启动过，避免频繁重试
            current_time = time.time()
            if current_time - self._tunnel_start_time < 60:  # 60秒内不重复启动
                return False, "Tunnel 启动中或最近启动失败，请稍后重试"

            logger.warning("Tunnel 未运行，尝试启动...")
            self._tunnel_start_time = current_time

            # 寻找可用端口
            port = find_free_port(start_port=60105, max_tries=20)
            if not port:
                return False, "无可用端口，无法启动 tunnel"

            # 启动tunnel
            ok, msg = self.tunnel.start(userspace=True, tunnel_info_port=port, retry_count=2)
            if not ok:
                return False, f"Tunnel 启动失败: {msg}\n\n可能的解决方案:\n1. 重新连接设备\n2. 重启应用\n3. 检查设备是否为iOS17+"

            logger.info("Tunnel 启动成功，监听端口 %s", port)
            return True, f"Tunnel 启动成功 (端口: {port})"

    def check_device_only(self, udid: str) -> Tuple[bool, str]:
        """
        仅检查设备连接状态，不检查tunnel
        """
        return self.check_all(udid, skip_tunnel_check=True)

    def quick_check(self, udid: str) -> Tuple[bool, str]:
        """
        快速检查：只验证设备连接，不启动tunnel
        """
        ok, out = self.m.list_devices(details=False)
        if not ok or not out:
            return False, "无法获取设备列表"

        if udid not in out:
            return False, f"设备 {udid} 未连接"

        return True, "设备连接正常"