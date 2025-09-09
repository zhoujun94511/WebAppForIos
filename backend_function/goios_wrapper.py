import os
import json
import stat
import zipfile
import logging
import platform
import threading
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any

logger = logging.getLogger(__name__)


class GoIOSManager:
    """
    负责：
    1) 按操作系统自动解压 go-ios 压缩包到 bin 目录；
    2) 找到可执行文件（可能叫 ios 或 ios.exe）；
    3) 提供统一的 run() 封装与常用功能方法（list/info/screenshot/apps/install/launch/syslog 等）；
    4) 多设备并发安全：对同一 UDID 的命令串行化执行。
    """

    def __init__(self, goios_root: str, bin_dir: str, bin_path_override: str = ""):
        self.goios_root = goios_root
        self.bin_dir = bin_dir
        self.bin_path_override = bin_path_override
        self._ensure_dirs()
        self.ios_bin = self._ensure_ios_binary()
        self._locks: Dict[str, threading.Lock] = {}

    # ---------------- 基础能力：目录、可执行文件 ----------------

    def _ensure_dirs(self) -> None:
        for d in (self.goios_root, self.bin_dir):
            os.makedirs(d, exist_ok=True)

    @staticmethod
    def _zip_map_for_os() -> Tuple[str, str]:
        """
        根据宿主系统返回 (zip 文件名, 解压到的子目录名)
        """
        sys_name = platform.system().lower()
        if sys_name.startswith("win"):
            return "go-ios-win.zip", "win"
        if sys_name == "darwin":
            return "go-ios-mac.zip", "mac"
        return "go-ios-linux.zip", "linux"

    @staticmethod
    def _arch_preferred_names() -> list:
        """
        根据宿主 CPU 架构给出优先候选名（Linux 包里常见：ios-amd64 / ios-arm64）。
        同时包含跨平台常见命名：ios / ios.exe / go-ios / go-ios.exe。
        """
        m = (platform.machine() or "").lower()
        preferred = []
        if any(x in m for x in ("aarch64", "arm64")):
            preferred += ["ios-arm64", "ios"]   # Apple Silicon / ARM Linux
        elif any(x in m for x in ("x86_64", "amd64", "x64")):
            preferred += ["ios-amd64", "ios"]  # x86_64 Linux
        else:
            preferred += ["ios"]                # 兜底

        # 跨平台常见别名一并加入（用于 Mac/Win 或历史包）
        preferred += ["go-ios", "ios.exe", "go-ios.exe", "ios-arm64.exe", "ios-amd64.exe"]
        # 去重但保留顺序
        seen = set()
        ordered = []
        for n in preferred:
            if n not in seen:
                seen.add(n)
                ordered.append(n)
        return ordered

    @staticmethod
    def _find_executable(search_dir: str) -> Optional[str]:
        """
        在 search_dir 下递归寻找 go-ios 可执行文件。
        兼容：
          - Linux:   ios-amd64 / ios-arm64  （无扩展名）
          - macOS:   ios
          - Windows: ios.exe
          - 旧名：   go-ios / go-ios.exe
        优先选择与宿主架构匹配的名称。
        """
        # 先收集所有文件 -> 路径
        file_map: Dict[str, str] = {}
        for root, _, files in os.walk(search_dir):
            for f in files:
                file_map[f] = os.path.join(root, f)

        # 按优先顺序挑选
        for name in GoIOSManager._arch_preferred_names():
            if name in file_map:
                path = file_map[name]
                logger.debug("发现 go-ios 可执行文件: %s", path)
                return path

        # 兜底：找名为 'ios'（无扩展）的文件
        if "ios" in file_map:
            path = file_map["ios"]
            logger.debug("发现 go-ios 可执行文件（fallback）: %s", path)
            return path

        return None

    @staticmethod
    def _ensure_executable_perm(file_path: str) -> None:
        """
        为 *nix 系统赋予执行权限；Windows 忽略
        """
        try:
            if os.name != "nt":
                st_mode = os.stat(file_path).st_mode
                os.chmod(file_path, st_mode | stat.S_IEXEC)
        except OSError as exc:
            logger.warning("为 %s 赋予执行权限失败：%s", file_path, exc)

    def _ensure_ios_binary(self) -> str:
        """
        确保 go-ios 可执行文件可用：优先使用覆盖路径，否则从 zip 解压/复用已解压文件。
        """
        # 优先使用外部覆盖路径（如果指定）
        if self.bin_path_override:
            if not os.path.exists(self.bin_path_override):
                raise FileNotFoundError(f"GOIOS_BIN_PATH 不存在: {self.bin_path_override}")
            logger.debug("Using go-ios binary (override): %s", self.bin_path_override)
            return self.bin_path_override

        zip_name, os_dir = self._zip_map_for_os()
        target_dir = os.path.join(self.bin_dir, os_dir)
        os.makedirs(target_dir, exist_ok=True)

        # 若目录中已存在可执行文件，直接使用
        existing = self._find_executable(target_dir)
        if existing:
            self._ensure_executable_perm(existing)
            return existing

        # 否则尝试从 zip 解压
        zip_path = os.path.join(self.goios_root, zip_name)
        if not os.path.exists(zip_path):
            raise FileNotFoundError(f"未找到 go-ios 压缩包: {zip_path}")

        logger.debug("首次使用：正在解压 %s 到 %s ...", zip_path, target_dir)
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(target_dir)
        except zipfile.BadZipFile as exc:
            logger.exception("go-ios 压缩包损坏：%s", exc)
            raise

        exe = self._find_executable(target_dir)
        if not exe:
            raise FileNotFoundError(
                f"解压后仍未找到 ios 可执行文件，请检查压缩包内容: {zip_path}"
            )
        self._ensure_executable_perm(exe)
        logger.debug("Using go-ios binary at: %s", exe)
        return exe

    # ---------------- 命令拼接与执行 ----------------

    def _lock_for(self, udid: Optional[str]) -> threading.Lock:
        """
        返回某 UDID 对应的锁；未指定 UDID 的命令共享“匿名锁”
        """
        key = udid or "_default_"
        if key not in self._locks:
            self._locks[key] = threading.Lock()
        return self._locks[key]

    @staticmethod
    def _build_common_opts(
        udid: Optional[str] = None,
        address: Optional[str] = None,
        rsd_port: Optional[int] = None,
        userspace_port: Optional[int] = None,
        proxyurl: Optional[str] = None,
        tunnel_info_port: Optional[int] = None,
        verbose: bool = False,
        trace: bool = False,
        nojson: bool = False,
        pretty: bool = False,
    ) -> List[str]:
        """
        将 go-ios 全局 options（--udid 等）转成参数列表
        """
        opts: List[str] = []
        if verbose:
            opts.append("-v")
        if trace:
            opts.append("--trace")
        if nojson:
            opts.append("--nojson")
        if pretty:
            opts.append("--pretty")
        if udid:
            opts += ["--udid", udid]
        if address:
            opts += ["--address", address]
        if rsd_port:
            opts += ["--rsd-port", str(rsd_port)]
        if userspace_port:
            opts += ["--userspace-port", str(userspace_port)]
        if proxyurl:
            opts += ["--proxyurl", proxyurl]
        if tunnel_info_port:
            opts += ["--tunnel-info-port", str(tunnel_info_port)]
        return opts

    def run(
        self,
        args: List[str],
        timeout: int = 120,
        **opts: Any,
    ) -> Tuple[int, str, str]:
        """
        通用执行器：支持 go-iOS 全局 options（--udid/--address/...）。
        所有“针对具体设备”的方法都应把 udid 传进来（用于加锁与选中目标设备）。
        返回 (returncode, stdout, stderr)；不抛异常，让上层决定如何提示。
        """
        # 允许调用方注入额外环境变量（例如 ENABLE_GO_IOS_AGENT）。
        # 注意：需先从 opts 中弹出，避免传入 _build_common_opts。
        extra_env_raw = opts.get("extra_env")
        if isinstance(extra_env_raw, dict):
            extra_env: Dict[str, str] = extra_env_raw
            del opts["extra_env"]
        else:
            extra_env = {}

        common = self._build_common_opts(**opts)
        cmd = [self.ios_bin] + common + args
        udid = opts.get("udid")  # 仅用于加锁

        env = os.environ.copy()
        env.update({k: str(v) for k, v in extra_env.items()})

        logger.debug("[GoIOSManager] 执行命令: %s", " ".join(cmd))
        lock = self._lock_for(udid)
        with lock:
            try:
                cp = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0),
                )
                if cp.returncode != 0:
                    logger.warning(
                        "命令失败（返回码 %s）：stderr=%s",
                        cp.returncode,
                        (cp.stderr or "").strip(),
                    )
                return cp.returncode, cp.stdout, cp.stderr
            except subprocess.TimeoutExpired:
                logger.error("命令超时：%s", " ".join(cmd))
                return 124, "", "命令执行超时"
            except (FileNotFoundError, OSError, ValueError) as exc:
                # FileNotFoundError: 可执行文件不存在；OSError/ValueError: 参数或环境错误
                logger.exception("命令执行异常：%s", exc)
                return 125, "", f"命令执行异常: {exc}"

    # ---------------- 常用操作（全部支持 **opts 透传） ----------------
    def device_pair(
            self,
            udid: Optional[str] = None,
            p12file: Optional[str] = None,
            password: Optional[str] = None,
            **opts: Any,
    ) -> Tuple[bool, str]:
        """
        go-ios 配对：
          - 普通设备：直接 `ios pair`
          - 受监督设备：传 p12 与密码可静默配对
        """
        args = ["pair"]

        if p12file:
            path = Path(p12file)
            try:
                # 仅做“存在且是文件”的健壮性检查，避免把路径错误带到 go-ios
                if not path.is_file():
                    return False, f"p12 文件不存在: {path}"
            except (OSError, PermissionError) as exc:
                # 只捕获文件系统相关异常；记录后继续让 go-ios 尝试
                logger.warning("检查 p12 文件时出错（%s）：%s，将继续尝试 go-ios。",
                               type(exc).__name__, exc)
            args += ["--p12file", str(path)]

        if password:
            # 注意：不要把密码写入日志
            args += ["--password", password]

        code, out, err = self.run(args, udid=udid, timeout=120, **opts)
        return code == 0, (out.strip() if (out or "").strip() else err)

    def list_devices(self, details: bool = False, **opts: Any) -> Tuple[bool, str]:
        args = ["list"]
        if details:
            args.append("--details")
        code, out, err = self.run(args, **opts)
        return code == 0, out if out.strip() else err

    def device_info(self, udid: str, **opts: Any) -> Tuple[bool, str]:
        code, out, err = self.run(["info"], udid=udid, **opts)
        out_str = out.strip() or err
        try:
            data = json.loads(out_str)
            return code == 0, json.dumps(data, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            return code == 0, out_str

    def screenshot(self, udid: str, save_path: str, **opts: Any) -> Tuple[bool, str]:
        args = ["screenshot", f"--output={save_path}"]  # <- 改这里
        code, out, err = self.run(args, udid=udid, timeout=180, **opts)
        if code == 0 and os.path.exists(save_path):
            return True, save_path
        return False, (out.strip() or err or "截屏失败")

    def apps_list(self, udid: str, only_list: bool = True, **opts: Any) -> Tuple[bool, str]:
        # ios apps [--list]
        args = ["apps"]
        if only_list:
            args.append("--list")
        code, out, err = self.run(args, udid=udid, **opts)
        return code == 0, out if out.strip() else err

    def install_ipa(self, udid: str, ipa_path: str, **opts: Any) -> Tuple[bool, str]:
        # ios install --path=<ipaOrAppFolder>
        code, out, err = self.run(
            ["install", "--path", ipa_path],
            udid=udid,
            timeout=1800,
            **opts,
        )
        return code == 0, out if out.strip() else err

    def launch_app(self, udid: str, bundle_id: str, wait: bool = False, **opts: Any) -> Tuple[bool, str]:
        # ios launch <bundleID> [--wait]
        args = ["launch", bundle_id]
        if wait:
            args.append("--wait")
        code, out, err = self.run(args, udid=udid, timeout=300, **opts)
        return code == 0, out if out.strip() else err

    def kill_app(self, udid: str, bundle_id: str, **opts: Any) -> Tuple[bool, str]:
        """
        更稳健的停止逻辑：
        先尝试按文档直接使用 bundleID；若失败，再回退到 ps/apps 映射进程名方案。
        文档参考：ios kill (<bundleID> | --pid | --process=<processName>)
        """
        # 0. 首先直接按 bundleID 尝试（与官方文档一致）
        code0, out0, err0 = self.run(["kill", bundle_id], udid=udid, timeout=60, **opts)
        if code0 == 0:
            return True, (out0.strip() or "停止应用成功")
        # 继续回退到进程名匹配方案

        """
        1) 先 ios ps --apps (JSON) 获取进程列表；
        2) 直接用 RealAppName/Name 匹配；
        3) 若未匹配到，则 ios apps 获取 CFBundleExecutable/Name，映射到进程 Name 或路径；
        4) 命中后使用 --process 结束。
        """
        # 1. ps 检查（JSON）
        code, out, err = self.run(["ps", "--apps"], udid=udid, timeout=60, **opts)
        if code != 0:
            return False, (out.strip() or err or "无法获取进程列表")

        try:
            processes = json.loads(out)
            if not isinstance(processes, list):
                return False, f"进程列表格式异常: {out or err}"
        except json.JSONDecodeError:
            return False, f"进程列表解析失败: {out or err}"

        def pick_target(proc_list, bundle: str, exec_names: list[str]) -> Optional[Dict[str, Any]]:
            b = (bundle or "").strip()
            for p in proc_list:
                real = str(p.get("RealAppName") or "")
                name = str(p.get("Name") or "")
                if b and b in real:
                    return p
                for en in exec_names:
                    if not en:
                        continue
                    if name == en:
                        return p
                    if real.endswith(f"/{en}") or f"/{en}.app/{en}" in real:
                        return p
            return None

        # 2. 直接尝试用 bundle_id 在 RealAppName 中匹配
        target = pick_target(processes, bundle_id, exec_names=[])

        # 3. 若失败，从 apps 列表推断可执行名再匹配
        if not target:
            code_apps, out_apps, err_apps = self.run(["apps"], udid=udid, timeout=90, **opts)
            exec_candidates: list[str] = []
            if code_apps == 0 and out_apps:
                try:
                    data = json.loads(out_apps)
                except json.JSONDecodeError:
                    data = None
                # data 可以是 list 或包含 apps 的 dict
                app_list = []
                if isinstance(data, list):
                    app_list = data
                elif isinstance(data, dict):
                    maybe = data.get("apps") or data.get("Apps") or data.get("applications") or data.get("list")
                    if isinstance(maybe, list):
                        app_list = maybe
                # 提取可执行名
                for obj in app_list:
                    if not isinstance(obj, dict):
                        continue
                    bid = str(obj.get("CFBundleIdentifier") or obj.get("bundleID") or obj.get("bundleId") or obj.get("Bundle") or obj.get("id") or "")
                    if bid == bundle_id:
                        exec_candidates.append(str(obj.get("CFBundleExecutable") or "").strip())
                        # 兜底：名称也作为候选
                        name_candidate = str(obj.get("CFBundleDisplayName") or obj.get("CFBundleName") or obj.get("BundleName") or obj.get("name") or obj.get("Name") or "").strip()
                        if name_candidate:
                            exec_candidates.append(name_candidate)
                        break
            # 去重并清理
            exec_candidates = [e for e in {e for e in exec_candidates if e}]
            target = pick_target(processes, bundle_id, exec_candidates)

        if not target:
            return False, f"{bundle_id} 未运行"

        process_name = target.get("Name")
        if not process_name:
            return False, f"未找到 {bundle_id} 对应的进程名"

        # 4. kill by process name
        code, out, err = self.run(["kill", "--process", process_name], udid=udid, timeout=60, **opts)
        if code != 0:
            return False, (out.strip() or err or "停止应用失败")

        return True, out.strip() or "停止应用成功"

    def reboot(self, udid: str, **opts: Any) -> Tuple[bool, str]:
        code, out, err = self.run(["reboot"], udid=udid, timeout=60, **opts)
        return code == 0, out if out.strip() else err

    def battery_info(self, udid: str, **opts: Any) -> Tuple[bool, str]:
        code, out, err = self.run(["batterycheck"], udid=udid, timeout=30, **opts)
        return code == 0, out if out.strip() else err

    def battery_registry(self, udid: str, **opts: Any) -> Tuple[bool, str]:
        code, out, err = self.run(["batteryregistry"], udid=udid, timeout=30, **opts)
        return code == 0, out if out.strip() else err

    def diskspace(self, udid: str, **opts: Any) -> Tuple[bool, str]:
        code, out, err = self.run(["diskspace"], udid=udid, timeout=30, **opts)
        return code == 0, out if out.strip() else err

    def image_auto(self, udid: str, basedir: Optional[str] = None, **opts: Any) -> Tuple[bool, str]:
        # ios image auto [--basedir=<where_dev_images_are_stored>]
        args = ["image", "auto"]
        if basedir:
            args.append(f"--basedir={basedir}")
        code, out, err = self.run(args, udid=udid, timeout=300, **opts)
        
        # 检查是否有错误信息，即使退出码为0
        output = out if out.strip() else err
        if "error" in output.lower() or "failed" in output.lower():
            return False, output
        
        return code == 0, output

    def image_mount(self, udid: str, path: str, **opts: Any) -> Tuple[bool, str]:
        # ios image mount [--path=<imagepath>]
        args = ["image", "mount", f"--path={path}"]
        code, out, err = self.run(args, udid=udid, timeout=300, **opts)
        return code == 0, out if out.strip() else err

    def image_list(self, udid: Optional[str] = None, **opts: Any) -> Tuple[bool, str]:
        # ios image list
        args = ["image", "list"]
        code, out, err = self.run(args, udid=udid, timeout=30, **opts)
        return code == 0, out if out.strip() else err

    def devicestate_list(self, udid: Optional[str] = None, **opts: Any) -> Tuple[bool, str]:
        # ios devicestate list
        code, out, err = self.run(["devicestate", "list"], udid=udid, timeout=30, **opts)
        return code == 0, out if out.strip() else err

    def devicestate_enable(self, udid: str, profile_type_id: str, profile_id: str, **opts: Any) -> Tuple[bool, str]:
        # ios devicestate enable <profileTypeId> <profileId>
        code, out, err = self.run(
            ["devicestate", "enable", profile_type_id, profile_id],
            udid=udid,
            timeout=30,
            **opts,
        )
        return code == 0, out if out.strip() else err

    def set_location(self, udid: str, lat: float, lon: float, **opts: Any) -> Tuple[bool, str]:
        # ios setlocation --lat=.. --lon=..
        args = ["setlocation", f"--lat={lat}", f"--lon={lon}"]
        code, out, err = self.run(args, udid=udid, timeout=30, **opts)
        return code == 0, out if out.strip() else err

    # ---------------- 长进程（Popen）：日志/流/转发 ----------------

    def syslog_stream_popen(self, udid: str, **opts: Any):
        """
        启动 syslog 流；返回 Popen。调用方需在断连时自行终止进程。
        """
        cmd = [self.ios_bin] + self._build_common_opts(udid=udid, **opts) + ["syslog"]
        try:
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0,
            )
        except (OSError, ValueError) as exc:
            logger.exception("启动 syslog 失败：%s", exc)
            return None

    def screenshot_stream_popen(self, udid: str, port: int = 3333, **opts: Any):
        """
        启动 MJPEG 截屏流到 0.0.0.0:<port>；返回 Popen。
        """
        # 提取环境变量，避免传递给 _build_common_opts
        extra_env = opts.pop("extra_env", {})
        
        cmd = (
            [self.ios_bin]
            + self._build_common_opts(udid=udid, **opts)
            + ["screenshot", "--stream", "--port", str(port)]
        )
        
        # 设置环境变量
        env = os.environ.copy()
        env.update({k: str(v) for k, v in extra_env.items()})
        
        try:
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,  # 分离 stderr 和 stdout
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                bufsize=0,  # 无缓冲，减少延迟
                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0,
            )
        except (OSError, ValueError) as exc:
            logger.exception("启动 screenshot --stream 失败：%s", exc)
            return None

    def forward_popen(self, udid: str, host_port: int, target_port: int, **opts: Any):
        """
        启动端口转发（host_port -> target_port）；返回 Popen。
        """
        cmd = (
            [self.ios_bin]
            + self._build_common_opts(udid=udid, **opts)
            + ["forward", str(host_port), str(target_port)]
        )
        try:
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, ValueError) as exc:
            logger.exception("启动 forward 失败：%s", exc)
            return None

    def crash_ls(self, udid: str, pattern: Optional[str] = None, **opts: Any) -> Tuple[bool, str]:
        args = ["crash", "ls"]
        if pattern:
            args.append(pattern)
        code, out, err = self.run(args, udid=udid, timeout=120, **opts)
        return code == 0, out if out.strip() else err

    def crash_cp(self, udid: str, srcpattern: str, target_dir: str, **opts: Any) -> Tuple[bool, str]:
        args = ["crash", "cp", srcpattern, target_dir]
        code, out, err = self.run(args, udid=udid, timeout=300, **opts)
        return code == 0, out if out.strip() else err

    def crash_rm(self, udid: str, cwd: str, pattern: str, recursive: bool = False, **opts: Any) -> Tuple[bool, str]:
        candidates: List[List[str]] = []
        # 优先尝试包含 cwd 的形式
        if recursive:
            candidates.append(["crash", "rm", "-r", cwd, pattern])
            candidates.append(["crash", "rm", "--r", cwd, pattern])
            candidates.append(["crash", "rm", "--recursive", cwd, pattern])
        candidates.append(["crash", "rm", cwd, pattern])
        # 再尝试不含 cwd 的形式
        if recursive:
            candidates.append(["crash", "rm", "-r", pattern])
            candidates.append(["crash", "rm", "--r", pattern])
            candidates.append(["crash", "rm", "--recursive", pattern])
        candidates.append(["crash", "rm", pattern])

        last_msg = ""
        for args in candidates:
            try:
                logger.debug("尝试 crash_rm 命令: %s", " ".join(args))
                code, out, err = self.run(args, udid=udid, timeout=120, **opts)
                msg = out if (out and out.strip()) else (err or "")
                if code == 0:
                    return True, msg
                last_msg = msg
            except (OSError, ValueError) as exc:
                last_msg = str(exc)
                continue
        return False, last_msg

    def devmode_get(self, udid: str, **opts: Any) -> Tuple[bool, str]:
        code, out, err = self.run(["devmode", "get"], udid=udid, timeout=60, **opts)
        return code == 0, out if out.strip() else err

    def devmode_enable(self, udid: str, enable_post_restart: bool = True, **opts: Any) -> Tuple[bool, str]:
        args = ["devmode", "enable"]
        if enable_post_restart:
            args.append("--enable-post-restart")
        code, out, err = self.run(args, udid=udid, timeout=120, **opts)
        return code == 0, out if out.strip() else err

    def profile_list(self, udid: str, **opts: Any) -> Tuple[bool, str]:
        code, out, err = self.run(["profile", "list"], udid=udid, timeout=60, **opts)
        return code == 0, out if out.strip() else err

    def profile_remove(self, udid: str, profile_name: str, **opts: Any) -> Tuple[bool, str]:
        code, out, err = self.run(["profile", "remove", profile_name], udid=udid, timeout=60, **opts)
        return code == 0, out if out.strip() else err

    def ps_apps(self, udid: str, **opts: Any) -> Tuple[bool, str]:
        code, out, err = self.run(["ps", "--apps"], udid=udid, timeout=60, **opts)
        return code == 0, out if out.strip() else err

    def syslog_stream_popen_parsed(self, udid: str, parse: bool = True, **opts: Any):
        cmd = [self.ios_bin] + self._build_common_opts(udid=udid, **opts) + ["syslog"]
        if parse:
            cmd.append("--parse")
        try:
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0,
            )
        except (OSError, ValueError) as exc:
            logger.exception("启动 syslog 失败：%s", exc)
            return None

    def listen_popen(self, **opts: Any):
        cmd = [self.ios_bin] + self._build_common_opts(**opts) + ["listen"]
        try:
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0,
            )
        except (OSError, ValueError) as exc:
            logger.exception("启动 listen 失败：%s", exc)
            return None

    def assistive(self, udid: str, feature: str, action: str, force: bool = False, **opts: Any) -> Tuple[bool, str]:
        # feature: assistivetouch | voiceover | zoom  ; action: enable|disable|toggle|get
        args = [feature, action]
        if force:
            args.append("--force")
        code, out, err = self.run(args, udid=udid, timeout=60, **opts)
        return code == 0, out if out.strip() else err
