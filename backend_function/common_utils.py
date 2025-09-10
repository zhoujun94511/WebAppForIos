import re
import os
import cv2
import json
import time
import uuid
import socket
import signal
import zipfile
import logging
import datetime
import requests
import threading
import subprocess

# 条件导入
try:
    import psutil
except ImportError:
    psutil = None

try:
    import numpy as np
except ImportError:
    np = None
from typing import Any, Dict, List, Optional
from requests.exceptions import RequestException


def is_tunnel_error(text: Optional[str]) -> bool:
    """粗略判断输出是否与 tunnel/agent 未就绪相关，以触发升级检查。
    不依赖精确文案，尽量匹配常见错误关键词。
    """
    s = (text or "").lower()
    if not s:
        return False
    patterns = [
        "agent is not running",
        "failed to get tunnel",
        "serve tunnel",
        "tunnel server",
        "connectex",
        "connection refused",
        "actively refused",
        ":60105",
        "bind",
        "failed to start tunnel",
    ]
    return any(p in s for p in patterns)


def run_with_quick_check_and_escalate(prechecker, udid: str, exec_fn):
    """
    先执行 quick_check（不启动 tunnel），执行命令；
    若失败且判断为隧道相关，再执行 check_all 启动/修复隧道后重试。

    :param prechecker: IOSPrechecker 实例
    :param udid: 设备 UDID
    :param exec_fn: 可调用，无参，返回 (ok: bool, raw: Any)
    :return: (ok, raw)
    """
    try:
        ok, msg = prechecker.quick_check(udid)
        if not ok:
            return False, msg
    except (AttributeError, ValueError, TypeError, RuntimeError, OSError):
        # quick_check 异常时也不阻断，继续尝试执行
        pass

    try:
        ok1, out1 = exec_fn()
    except (AttributeError, ValueError, TypeError, RuntimeError, OSError) as e:
        # 将异常文案视作 raw 以便后续判断
        ok1, out1 = False, str(e)

    if ok1:
        return True, out1

    if is_tunnel_error(str(out1)):
        try:
            ok_t, _ = prechecker.check_all(udid)
        except (AttributeError, ValueError, TypeError, RuntimeError, OSError):
            ok_t = False
        if ok_t:
            try:
                return exec_fn()
            except (AttributeError, ValueError, TypeError, RuntimeError, OSError) as e:
                return False, str(e)

    return ok1, out1


def get_local_ip() -> Optional[str]:
    """获取本地IP地址；失败返回 None。"""
    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception as e:
        logging.info("获取本地IP失败: %s", e)
        return None
    finally:
        if sock is not None:
            sock.close()


# === iOS 设备信息规范化 ===

UNKNOWN = "UNKNOWN"


def _pick(d: Dict[str, Any], keys: List[str], default: Optional[str] = UNKNOWN):
    """从字典 d 里按顺序取第一个有值(key 存在且值不为空)的键。"""
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] not in (None, "", []):
            return d[k]
    return default


# 展示顺序
ORDERED_IOS_INFO_LABELS: List[str] = [
    "设备品牌",
    "产品型号",
    "内部型号",
    "系统版本",
    "CPU 架构",
    "激活状态",
    "设备区域",
    "设备时区",
    "UDID",
    "IMEI信息①",
    "IMEI信息②",
    "Wi-Fi地址",
    "蓝牙地址",
    "以太网地址",
]


def format_product_model(model: Optional[str]) -> str:
    s = str(model or "").strip()
    return s.split(",")[0] if s else s


def normalize_ios_info(raw: Dict[str, Any]) -> Dict[str, Any]:
    """规范化 go-ios JSON 输出"""
    info_map: Dict[str, Any] = {
        "设备品牌": _pick(raw, ["ProductName"]),
        "产品型号": format_product_model(_pick(raw, ["ProductType"])),
        "内部型号": _pick(raw, ["ModelNumber"]),
        "系统版本": _pick(raw, ["HumanReadableProductVersionString", "ProductVersion"]),
        "CPU 架构": _pick(raw, ["CPUArchitecture"]),
        "激活状态": _pick(raw, ["ActivationState"]),
        "设备区域": _pick(raw, ["RegionInfo"]),
        "设备时区": _pick(raw, ["TimeZone"]),
        "UDID": _pick(raw, ["UniqueDeviceID", "UDID"]),
        "IMEI信息①": _pick(raw, ["InternationalMobileEquipmentIdentity"]),
        "IMEI信息②": _pick(raw, ["InternationalMobileEquipmentIdentity2"]),
        "Wi-Fi地址": _pick(raw, ["WiFiAddress"]),
        "蓝牙地址": _pick(raw, ["BluetoothAddress"]),
        "以太网地址": _pick(raw, ["EthernetAddress"]),
    }
    for k, v in list(info_map.items()):
        if v in (None, "", []):
            info_map[k] = UNKNOWN
    return info_map


def build_ordered_ios_info(info_map: Dict[str, Any]) -> List[Dict[str, str]]:
    """转成有序数组"""
    return [{"label": lab, "value": str(info_map.get(lab, UNKNOWN))} for lab in ORDERED_IOS_INFO_LABELS]


# === 设备列表规范化 ===

def normalize_ios_list_output(raw: str) -> List[Dict[str, Any]]:
    """规整 go-ios list 输出"""
    devices: List[Dict[str, Any]] = []
    raw = (raw or "").strip()
    if not raw:
        return devices
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None

    def pick(rec: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "udid": rec.get("udid") or rec.get("UDID") or rec.get("Udid") or rec.get("UniqueDeviceID") or rec.get(
                "serialNumber"),
            "name": rec.get("name") or rec.get("DeviceName") or rec.get("ProductName"),
            "model": format_product_model(rec.get("model") or rec.get("ProductType") or rec.get("productType")),
            "version": rec.get("version") or rec.get("ProductVersion") or rec.get("productVersion"),
        }

    if isinstance(data, dict):
        arr = data.get("deviceList") or data.get("devices") or []
        for obj in arr:
            if isinstance(obj, dict):
                nd = pick(obj)
                if nd["udid"]:
                    devices.append(nd)
    elif isinstance(data, list):
        for obj in data:
            if isinstance(obj, str):
                devices.append({"udid": obj, "name": None, "model": None, "version": None})
            elif isinstance(obj, dict):
                nd = pick(obj)
                if nd["udid"]:
                    devices.append(nd)
    else:
        for line in raw.splitlines():
            val = line.strip()
            if val and len(val) >= 16:
                devices.append({"udid": val, "name": None, "model": None, "version": None})

    seen = set()
    uniq: List[Dict[str, Any]] = []
    for dev in devices:
        uid = dev.get("udid")
        if uid and uid not in seen:
            seen.add(uid)
            uniq.append(dev)
    return uniq


# === 应用列表解析 ===

def _is_system_bundle(bundle_id: Optional[str], app_type: Optional[str]) -> bool:
    if app_type and isinstance(app_type, str) and "system" in app_type.lower():
        return True
    return bool(bundle_id) and str(bundle_id).startswith("com.apple.")


def parse_ios_apps(raw: str, third_party_only: bool = True) -> List[Dict[str, str]]:
    """解析 go-ios apps 输出"""
    result: List[Dict[str, str]] = []
    raw = raw or ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None

    def push(bundle_id: str, title: str = "", version: str = "", app_type: str = ""):
        if not bundle_id:
            return
        if third_party_only and _is_system_bundle(bundle_id, app_type):
            return
        result.append({"bundleId": bundle_id, "name": title, "version": version})

    if isinstance(data, list):
        for obj in data:
            if isinstance(obj, dict):
                bid = obj.get("CFBundleIdentifier") or obj.get("bundleID") or obj.get("id")
                app_name = obj.get("CFBundleDisplayName") or obj.get("CFBundleName") or obj.get("name") or ""
                ver = obj.get("CFBundleShortVersionString") or obj.get("version") or ""
                typ = obj.get("ApplicationType") or obj.get("appType") or ""
                push(str(bid or ""), str(app_name or ""), str(ver or ""), str(typ or ""))
    elif isinstance(data, dict):
        possible = data.get("apps") or data.get("applications") or []
        for obj in possible:
            if isinstance(obj, dict):
                bid = obj.get("CFBundleIdentifier") or obj.get("bundleID") or obj.get("id")
                app_name = obj.get("CFBundleDisplayName") or obj.get("CFBundleName") or obj.get("name") or ""
                ver = obj.get("CFBundleShortVersionString") or obj.get("version") or ""
                typ = obj.get("ApplicationType") or obj.get("appType") or ""
                push(str(bid or ""), str(app_name or ""), str(ver or ""), str(typ or ""))
    else:
        for line in raw.splitlines():
            m = re.match(r"^\s*([a-zA-Z0-9._\-]+)\s*(?:-\s*([^-\n]+))?(?:-\s*([^\n]+))?", line)
            if not m:
                continue
            bid = (m.group(1) or "").strip()
            if not bid:
                continue
            if third_party_only and bid.startswith("com.apple."):
                continue
            app_name = (m.group(2) or "").strip()
            ver = (m.group(3) or "").strip()
            result.append({"bundleId": bid, "name": app_name, "version": ver})

    seen = set()
    uniq: List[Dict[str, str]] = []
    for app in result:
        bid = app.get("bundleId")
        if bid and bid not in seen:
            seen.add(bid)
            uniq.append(app)
    return uniq


# === 工具函数 ===

def find_free_port(start_port=60105, max_tries=20):
    for port in range(start_port, start_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return None


def check_port_available(port: int) -> bool:
    """检查端口是否可用"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            result = s.connect_ex(('127.0.0.1', port))
            return result != 0  # 连接失败说明端口可用
    except (OSError, socket.error):
        return False


def wait_for_mjpeg_stream(mjpeg_url: str, process, max_wait_seconds: int = 15, logger=None) -> bool:
    """等待 MJPEG 流就绪"""
    if logger:
        logger.debug("等待 MJPEG 流就绪: %s", mjpeg_url)

    for attempt in range(max_wait_seconds):
        try:
            response = requests.get(mjpeg_url, timeout=2, stream=True)
            if response.status_code == 200:
                response.close()
                if logger:
                    logger.debug("MJPEG 流已就绪 (尝试 %d/%d)", attempt + 1, max_wait_seconds)
                return True
        except (requests.RequestException, OSError, IOError):
            pass

        if process.poll() is not None:
            if logger:
                error_msg = f"MJPEG 流进程异常退出 (code: {process.returncode})"
                try:
                    stdout, stderr = process.communicate(timeout=1)
                    if stdout:
                        error_msg += f"\nstdout: {stdout}"
                    if stderr:
                        error_msg += f"\nstderr: {stderr}"
                except (ValueError, OSError, subprocess.TimeoutExpired):
                    pass
                logger.error(error_msg)
            return False

        time.sleep(1)
        if attempt % 3 == 0 and logger:
            logger.debug("等待 MJPEG 流启动 (%d/%d)...", attempt + 1, max_wait_seconds)

    return False


def cleanup_old_tunnel_processes(tunnel_manager=None, logger=None) -> None:
    """清理旧 tunnel 进程"""
    try:
        if tunnel_manager:
            try:
                tunnel_manager.stop()
                if logger:
                    logger.debug("已停止现有 tunnel 进程")
            except Exception as e:
                if logger:
                    logger.debug("停止 tunnel 时异常: %s", e)

        if psutil is None:
            if logger:
                logger.debug("psutil 不可用，跳过进程清理")
            return

        killed = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = ' '.join(proc.info.get('cmdline') or []).lower()
                if 'ios' in proc.info.get('name', '').lower() and 'tunnel' in cmdline:
                    if logger:
                        logger.warning("发现旧 tunnel 进程 PID %d: %s", proc.info['pid'], cmdline)
                    proc.terminate()
                    proc.wait(timeout=5)
                    killed.append(proc.info['pid'])
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                continue
        if killed and logger:
            logger.debug("已清理旧 tunnel 进程: %s", killed)

    except Exception as e:
        if logger:
            logger.warning("清理旧 tunnel 进程异常: %s", e)


def cleanup_all_ios_processes(logger=None) -> None:
    """清理所有ios.exe进程（包括可能遗留的进程）"""
    try:
        import psutil
        killed = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if proc.info['name'] and proc.info['name'].lower() in ['ios.exe', 'ios']:
                    # 强制终止所有ios.exe进程
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except psutil.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
                    killed.append(proc.info['pid'])
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                continue
        if killed and logger:
            logger.info("已清理 %d 个ios.exe进程: %s", len(killed), killed)
        elif logger:
            logger.debug("未发现需要清理的ios.exe进程")

    except ImportError:
        if logger:
            logger.warning("psutil未安装，无法清理ios.exe进程")
    except Exception as e:
        if logger:
            logger.warning("清理ios.exe进程异常: %s", e)


def create_required_directories(config, logger=None) -> None:
    """创建必要的目录"""
    dirs = [
        getattr(config, "UPLOAD_FOLDER", None),
        getattr(config, "LOG_DIR", None),
        getattr(config, "GOIOS_DIR", None),
        getattr(config, "GOIOS_EXECUTABLE_DIR", None),
        getattr(config, "DEVIMAGES_DIR", None),
        os.path.join(getattr(config, "IOS_HOME", ""), 'wintun', 'amd64'),
        os.path.join(getattr(config, "IOS_HOME", ""), 'wintun', 'arm'),
        os.path.join(getattr(config, "IOS_HOME", ""), 'wintun', 'arm64'),
        os.path.join(getattr(config, "IOS_HOME", ""), 'wintun', 'x86'),
    ]
    for d in dirs:
        if d:
            try:
                os.makedirs(d, exist_ok=True)
                if logger:
                    logger.debug("确保目录存在: %s", d)
            except OSError as e:
                if logger:
                    logger.warning("创建目录失败 %s: %s", d, e)

    # 启动下载目录清理守护线程（仅启动一次）
    try:
        download_dir = getattr(config, "UPLOAD_FOLDER", None)
        if download_dir:
            _start_downloads_janitor(download_dir, logger=logger)
    except Exception as e:
        if logger:
            logger.debug("启动下载目录清理线程失败: %s", e)


def to_int(value, default: Optional[int]) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_goios_opts(src: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "address": (str(src.get("address") or "").strip() or None),
        "rsd_port": to_int(src.get("rsd_port"), None),
        "userspace_port": to_int(src.get("userspace_port"), None),
        "proxyurl": (str(src.get("proxyurl") or "").strip() or None),
        "tunnel_info_port": to_int(src.get("tunnel_info_port"), None),
        "verbose": bool(src.get("verbose", False)),
        "trace": bool(src.get("trace", False)),
        "nojson": bool(src.get("nojson", False)),
        "pretty": bool(src.get("pretty", False)),
    }


def terminate_process(process) -> None:
    """强制终止进程，包括子进程"""
    try:
        if process and hasattr(process, "poll") and process.poll() is None:
            # 使用psutil强制终止进程及其子进程
            try:
                import psutil
                parent = psutil.Process(process.pid)
                children = parent.children(recursive=True)

                # 先终止子进程
                for child in children:
                    try:
                        child.terminate()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

                # 等待子进程终止
                try:
                    psutil.wait_procs(children, timeout=3)
                except psutil.TimeoutExpired:
                    # 强制杀死未终止的子进程
                    for child in children:
                        try:
                            if child.is_running():
                                child.kill()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass

                # 终止父进程
                try:
                    parent.terminate()
                    parent.wait(timeout=5)
                except (psutil.TimeoutExpired, psutil.NoSuchProcess):
                    try:
                        parent.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

            except ImportError:
                # 如果没有psutil，使用传统方法
                if os.name == "nt":
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                else:
                    os.kill(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.kill(process.pid, signal.SIGKILL)

    except Exception as e:
        logging.warning("终止子进程失败: %s", e)


def safe_filename(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fa5.-]", "", str(text or ""))


def get_device_model(udid: str, goios_manager) -> str:
    ok, out = goios_manager.list_devices(details=True)
    if not ok or not out:
        return "iOSDevice"
    try:
        devices = normalize_ios_list_output(out)
        for d in devices:
            if d.get("udid") == udid:
                return safe_filename(format_product_model(d.get("model") or "iOSDevice"))
    except (ValueError, KeyError, TypeError) as e:
        logging.debug("解析设备信息失败: %s", e)
    return "iOSDevice"


def now_timestamp_str() -> str:
    return datetime.datetime.now().strftime("%Y%m%d%H%M%S")


# === MJPEG 录屏 ===

def _start_manual_mjpeg_recording(
        mjpeg_url: str,
        out_dir: str,
        basename: str,
        stop_evt: threading.Event,
        logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """
    手动解析 MJPEG 流并保存为 MP4 文件。
    """
    mp4_name = f"{basename}.mp4"
    mp4_path = os.path.join(out_dir, mp4_name)

    def _worker():
        try:
            with requests.get(mjpeg_url, stream=True, timeout=10) as r:
                r.raise_for_status()

                buffer = b""
                writer: Optional[Any] = None
                frame: Optional[Any] = None
                target_w: Optional[int] = None
                target_h: Optional[int] = None
                # 目标帧率（可通过环境变量 RECORD_FPS 配置），用于还原真实时长
                try:
                    target_fps = float(os.environ.get("RECORD_FPS", "30"))
                    if not (1.0 <= target_fps <= 60.0):
                        target_fps = 12.0
                except (ValueError, TypeError):
                    target_fps = 12.0
                last_ts = None
                frame_acc = 0.0
                max_dup_per_tick = 5  # 一次最多重复写入帧数，限制卡顿时的爆发
                for chunk in r.iter_content(chunk_size=4096):
                    if stop_evt.is_set():
                        break
                    if not chunk:
                        continue

                    buffer += chunk

                    # 尝试从 buffer 中提取尽可能多的完整 JPEG 帧
                    while True:
                        start = buffer.find(b"\xff\xd8")
                        end = buffer.find(b"\xff\xd9")
                        if start != -1 and end != -1 and end > start:
                            jpg_bytes = buffer[start:end + 2]
                            buffer = buffer[end + 2:]
                        else:
                            break

                        arr = np.frombuffer(jpg_bytes, dtype=np.uint8)
                        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        if frame is None:
                            continue

                        h, w = frame.shape[:2]

                        # 初始化写入器，统一视频尺寸（强制偶数，提升兼容性）
                        if writer is None:
                            target_w = w - (w % 2)
                            target_h = h - (h % 2)
                            if target_w <= 0 or target_h <= 0:
                                target_w, target_h = w, h
                            if target_w != w or target_h != h:
                                frame = cv2.resize(frame, (target_w, target_h))
                                h, w = frame.shape[:2]

                            fourcc = _safe_fourcc("mp4v")
                            writer = cv2.VideoWriter(mp4_path, fourcc, target_fps, (w, h))
                            last_ts = time.time()
                            if logger:
                                logger.debug("初始化 VideoWriter: %s [%dx%d @ %.1f fps]",
                                             mp4_path, w, h, target_fps)
                        else:
                            # 若帧尺寸变化（如旋转），统一到初始尺寸
                            if (w != target_w) or (h != target_h):
                                frame = cv2.resize(frame, (target_w, target_h))

                        # 按时间间隔平滑计算应写入帧数，保持生成视频时长 ≈ 实际时长
                        now_ts = time.time()
                        if last_ts is None:
                            frame_acc += 1.0
                        else:
                            dt = max(0.0, now_ts - last_ts)
                            frame_acc += dt * target_fps
                        last_ts = now_ts

                        # 平滑写入，限幅避免卡顿时爆量重复
                        frames_to_write = int(frame_acc)
                        if frames_to_write > 0:
                            frames_to_write = min(frames_to_write, max_dup_per_tick)
                            frame_acc -= frames_to_write
                            for _ in range(frames_to_write):
                                writer.write(frame)

        except requests.RequestException as e:
            if logger:
                if stop_evt.is_set():
                    # 停止录制导致的连接中断，视为正常结束
                    logger.debug("MJPEG 流已结束")
                else:
                    logger.error("MJPEG 请求失败: %s", e)
        except (OSError, IOError) as e:
            if logger:
                logger.error("文件写入失败: %s", e)
        except Exception as e:  # 更具体的兜底
            if logger:
                logger.error("MJPEG 手动录制异常: %s", e, exc_info=True)
        finally:
            # 确保写入器被正确释放，写入 moov 等元数据
            try:
                if 'writer' in locals() and writer is not None:
                    writer.release()
                    if logger:
                        logger.debug("已关闭视频写入器: %s", mp4_path)
            except Exception as e:
                if logger:
                    logger.warning("释放 VideoWriter 失败: %s", e)

    th = threading.Thread(target=_worker, daemon=True, name=f"ManualMJPEG-{basename}")
    th.start()
    return {"stop_evt": stop_evt, "thread": th, "path": mp4_path, "name": mp4_name, "format": "mp4"}


def _record_mjpeg_fallback(mjpeg_url: str, out_path: str, stop_event: threading.Event,
                           logger: Optional[logging.Logger] = None) -> None:
    """回退：直接把 MJPEG 流保存为 .mjpeg"""
    try:
        with requests.get(mjpeg_url, stream=True, timeout=15) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if stop_event.is_set():
                        return
                    if chunk:
                        f.write(chunk)
        if logger:
            logger.debug("MJPEG 录制完成: %s", out_path)
    except RequestException as e:
        if logger:
            logger.warning("MJPEG 请求错误: %s", e)
    except (OSError, IOError) as e:
        if logger:
            logger.error("MJPEG 文件写入错误: %s", e)


def _safe_fourcc(codec_str: str):
    try:
        if hasattr(cv2, "VideoWriter_fourcc"):
            return cv2.VideoWriter_fourcc(*codec_str)
        return sum(ord(c) << (8 * i) for i, c in enumerate(codec_str[:4]))
    except (AttributeError, TypeError, ValueError) as e:
        logging.debug("生成 fourcc 失败: %s, 使用默认 mp4v", e)
        return 0x34766D70  # 默认 mp4v


def _start_mjpeg_fallback(mjpeg_url: str, out_dir: str, basename: str,
                          logger: Optional[logging.Logger] = None) -> Dict[str, Any]:
    stop_evt = threading.Event()
    mjpeg_name = f"{basename}.mjpeg"
    mjpeg_path = os.path.join(out_dir, mjpeg_name)

    def _worker():
        _record_mjpeg_fallback(mjpeg_url, mjpeg_path, stop_evt, logger)

    th = threading.Thread(target=_worker, daemon=True, name=f"MJPEG-{basename}")
    th.start()
    return {"stop_evt": stop_evt, "thread": th, "path": mjpeg_path, "name": mjpeg_name, "format": "mjpeg"}


def start_mjpeg_to_mp4(mjpeg_url: str, out_dir: str, basename: str,
                       logger: Optional[logging.Logger] = None) -> Dict[str, Any]:
    """
    启动录屏：默认使用手动 MJPEG 解析转 MP4。
    如果 numpy/cv2 不可用，则退回保存为 .mjpeg。
    """
    os.makedirs(out_dir, exist_ok=True)

    if np is None or cv2 is None:
        if logger:
            logger.warning("缺少 numpy 或 OpenCV，回退到 .mjpeg 保存方案")
        return _start_mjpeg_fallback(mjpeg_url, out_dir, basename, logger)

    stop_evt = threading.Event()
    return _start_manual_mjpeg_recording(mjpeg_url, out_dir, basename, stop_evt, logger)


def stop_recorder(rec_ctx: Dict[str, Any], join_timeout: float = 5.0) -> None:
    if not isinstance(rec_ctx, dict):
        return
    stop_evt = rec_ctx.get("stop_evt")
    th = rec_ctx.get("thread")
    if isinstance(stop_evt, threading.Event):
        stop_evt.set()
    if isinstance(th, threading.Thread) and th.is_alive():
        th.join(timeout=join_timeout)
        if th.is_alive():
            logging.warning("录制线程 %s 在 %.1f 秒后仍在运行", th.name, join_timeout)


# === 下载目录自动清理 ===
_downloads_janitor_started = False
_downloads_janitor_lock = threading.Lock()


def _downloads_cleanup_once(download_dir: str, max_age_seconds: int = 1800,
                            logger: Optional[logging.Logger] = None) -> None:
    """执行一次清理：删除下载目录内超过 max_age_seconds 的普通文件。
    - 忽略子目录
    - 文件被占用/权限错误时跳过
    - 仅输出 debug 日志，避免噪声
    """
    now = time.time()
    try:
        for name in os.listdir(download_dir):
            path = os.path.join(download_dir, name)
            try:
                if not os.path.isfile(path):
                    continue
                # 计算文件年龄（按 mtime）
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if (now - float(mtime)) <= max_age_seconds:
                    continue

                # 尝试删除；被占用或无权限时跳过
                try:
                    os.remove(path)
                    if logger:
                        logger.debug("已清理过期文件: %s", path)
                except PermissionError:
                    # 可能仍在写入/占用，跳过
                    if logger:
                        logger.debug("文件占用，跳过清理: %s", path)
                except OSError as e:
                    if logger:
                        logger.debug("删除失败，跳过 %s: %s", path, e)
            except (OSError, PermissionError) as e:
                if logger:
                    logger.debug("处理文件异常，跳过 %s: %s", path, e)
                continue
    except FileNotFoundError:
        # 目录被外部删除
        if logger:
            logger.debug("下载目录不存在，跳过清理: %s", download_dir)
    except Exception as e:
        if logger:
            logger.debug("清理任务异常: %s", e)


def _start_downloads_janitor(download_dir: str,
                             logger: Optional[logging.Logger] = None,
                             max_age_seconds: int = 300,
                             scan_interval_seconds: int = 60) -> None:
    """启动后台清理线程（仅启动一次）。"""
    global _downloads_janitor_started
    if not download_dir:
        return
    with _downloads_janitor_lock:
        if _downloads_janitor_started:
            return
        _downloads_janitor_started = True

    def _worker():
        # 首次延迟，避免与启动阶段 I/O 冲突
        time.sleep(5)
        while True:
            _downloads_cleanup_once(download_dir, max_age_seconds=max_age_seconds, logger=logger)
            time.sleep(max(15, scan_interval_seconds))

    th = threading.Thread(target=_worker, daemon=True, name="DownloadsJanitor")
    th.start()


# === 签名下载令牌（通用） ===
_SIGNED_DOWNLOADS_STORE: Dict[str, Dict[str, Any]] = {}


def create_signed_download_token(filename: str, ttl_seconds: int = 600) -> str:
    token = uuid.uuid4().hex if 'uuid' in globals() else str(int(time.time() * 1000))
    expire_at = time.time() + max(30, ttl_seconds)
    rel = str(filename).replace("\\", "/").lstrip("/")
    _SIGNED_DOWNLOADS_STORE[token] = {"file": rel, "expire": expire_at}
    return token


def consume_signed_download_token(token: str) -> str:
    info = _SIGNED_DOWNLOADS_STORE.pop(token, None)
    if not info:
        raise KeyError("invalid token")
    if time.time() > float(info.get("expire", 0)):
        raise KeyError("expired token")
    return str(info.get("file"))


# === ps --apps 解析 ===

def parse_ps_apps_raw(raw: str) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    try:
        data = json.loads(raw or "")
        if isinstance(data, list):
            for p in data:
                if not isinstance(p, dict):
                    continue
                result.append({
                    "Name": p.get("Name"),
                    "Pid": p.get("Pid") or p.get("PID") or p.get("pid"),
                    "RealAppName": p.get("RealAppName"),
                })
    except json.JSONDecodeError:
        pass
    return result


# === 电池详情合并 ===

def build_battery_detail(goios_manager, udid: str, **opts) -> Dict[str, Any]:
    ok1, raw1 = goios_manager.battery_info(udid, **opts)
    ok2, raw2 = getattr(goios_manager, 'battery_registry', lambda *a, **k: (False, None))(udid, **opts)
    detail = {"batterycheck": None, "batteryregistry": None}
    try:
        if ok1 and isinstance(raw1, str) and raw1.strip():
            detail["batterycheck"] = json.loads(raw1)
    except json.JSONDecodeError:
        detail["batterycheck"] = None
    try:
        if ok2 and isinstance(raw2, str) and raw2.strip():
            detail["batteryregistry"] = json.loads(raw2)
    except json.JSONDecodeError:
        detail["batteryregistry"] = None
    return detail


# === 磁盘信息解析（支持 JSON 或文本） ===

def parse_diskspace_summary(raw: str) -> Dict[str, Any]:
    summary = {"BlockSize": None, "FreeSpace": None, "UsedSpace": None, "TotalSpace": None}
    if not raw:
        return summary
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            summary["BlockSize"] = data.get("BlockSize")
            summary["FreeSpace"] = data.get("FreeSpace") or data.get("free")
            summary["UsedSpace"] = data.get("UsedSpace") or data.get("used")
            summary["TotalSpace"] = data.get("TotalSpace") or data.get("total")
            return summary
    except json.JSONDecodeError:
        pass
    text = str(raw)
    import re as _re
    mb = _re.search(r"BlockSize:\s*([0-9.]+)", text, _re.I)
    mf = _re.search(r"FreeSpace:\s*([^\n\r]+)", text, _re.I)
    mu = _re.search(r"UsedSpace:\s*([^\n\r]+)", text, _re.I)
    mt = _re.search(r"TotalSpace:\s*([^\n\r]+)", text, _re.I)
    summary["BlockSize"] = (mb.group(1) + "KB") if mb else None
    summary["FreeSpace"] = mf.group(1).strip() if mf else None
    summary["UsedSpace"] = mu.group(1).strip() if mu else None
    summary["TotalSpace"] = mt.group(1).strip() if mt else None
    return summary


# === Crash 导出/删除 ===

def crash_export_zip(goios_manager, udid: str, patterns: List[str], crash_root: str,
                     logger: Optional[logging.Logger] = None) -> Dict[str, Any]:
    os.makedirs(crash_root, exist_ok=True)
    batch_dir = os.path.join(crash_root, uuid.uuid4().hex if 'uuid' in globals() else str(int(time.time())))
    os.makedirs(batch_dir, exist_ok=True)
    ok_all = True
    raws: List[str] = []
    pats = patterns or ["*"]
    for pat in pats:
        ok_one, raw_one = goios_manager.crash_cp(udid, str(pat), batch_dir)
        ok_all = ok_all and ok_one
        raws.append(raw_one)
    zip_name = os.path.basename(batch_dir) + ".zip"
    zip_path = os.path.join(crash_root, zip_name)
    try:
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(batch_dir):
                for f in files:
                    full = os.path.join(root, f)
                    arc = os.path.relpath(full, batch_dir)
                    z.write(full, arcname=arc)
    except Exception as exc:
        if logger:
            logger.warning("打包 crash 目录失败: %s", exc)
    return {"ok": ok_all, "zip_path": zip_path, "raw": "\n".join(raws)}


def crash_remove_many(goios_manager, udid: str, patterns: List[str], cwd: str = ".", recursive: bool = False) -> Dict[
    str, Any]:
    pats = patterns or ["*"]
    all_ok = True
    raws: List[str] = []
    results: List[Dict[str, Any]] = []

    for pat in pats:
        try:
            ok_rm, raw_rm = goios_manager.crash_rm(udid, cwd, str(pat), recursive=recursive)
            all_ok = all_ok and ok_rm
            raws.append(raw_rm)

            # 记录每个pattern的删除结果
            result = {
                "pattern": pat,
                "success": ok_rm,
                "output": raw_rm,
                "cwd": cwd,
                "recursive": recursive
            }

            # 分析失败原因
            if not ok_rm:
                if "permission denied" in raw_rm.lower() or "access denied" in raw_rm.lower():
                    result["error_type"] = "permission_denied"
                    result["suggestion"] = "检查文件权限或设备锁定状态"
                elif "no such file" in raw_rm.lower() or "file not found" in raw_rm.lower():
                    result["error_type"] = "file_not_found"
                    result["suggestion"] = "文件可能已被删除或路径不正确"
                elif "usage:" in raw_rm.lower() or "help" in raw_rm.lower():
                    result["error_type"] = "invalid_parameters"
                    result["suggestion"] = "检查pattern和cwd参数格式"
                else:
                    result["error_type"] = "unknown_error"
                    result["suggestion"] = "检查go-ios命令输出获取更多信息"

            results.append(result)

        except Exception as e:
            all_ok = False
            error_msg = f"删除pattern '{pat}'时发生异常: {str(e)}"
            raws.append(error_msg)
            results.append({
                "pattern": pat,
                "success": False,
                "output": error_msg,
                "error_type": "exception",
                "suggestion": "检查go-ios进程状态和网络连接"
            })

    return {
        "ok": all_ok,
        "raw": "\n".join(raws),
        "results": results,
        "summary": {
            "total_patterns": len(pats),
            "successful_deletions": sum(1 for r in results if r["success"]),
            "failed_deletions": sum(1 for r in results if not r["success"]),
            "cwd": cwd,
            "recursive": recursive
        }
    }


def crash_export_collect(goios_manager, udid: str, patterns: List[str], crash_root: str,
                         logger: Optional[logging.Logger] = None) -> Dict[str, Any]:
    os.makedirs(crash_root, exist_ok=True)
    batch_dir = os.path.join(crash_root, uuid.uuid4().hex if 'uuid' in globals() else str(int(time.time())))
    os.makedirs(batch_dir, exist_ok=True)
    ok_all = True
    raws: List[str] = []
    pats = patterns or ["*"]
    for pat in pats:
        pat_str = str(pat)
        tried = []
        # 先按原样
        ok_one, raw_one = goios_manager.crash_cp(udid, pat_str, batch_dir)
        tried.append((pat_str, ok_one, raw_one))
        # 若像目录名（不含通配符且不含点后缀），追加 "/*" 重试
        if not ok_one:
            if all(ch not in pat_str for ch in ("*", "?")) and "." not in pat_str:
                pat_dir = pat_str.rstrip("/") + "/*"
                ok_two, raw_two = goios_manager.crash_cp(udid, pat_dir, batch_dir)
                tried.append((pat_dir, ok_two, raw_two))
                ok_one = ok_two
                raw_one = (raw_one or "") + ("\n" + (raw_two or ""))
        ok_all = ok_all and ok_one
        raws.append(raw_one)
    # 列出 batch_dir 下的所有文件（扁平化相对路径）
    files: List[str] = []
    for root, _, fnames in os.walk(batch_dir):
        for f in fnames:
            full = os.path.join(root, f)
            files.append(full)
    if logger:
        logger.debug("Crash 导出收集：udid=%s, patterns=%s, files=%d, dir=%s", udid, pats, len(files), batch_dir)
    return {"ok": ok_all, "batch_dir": batch_dir, "files": files, "raw": "\n".join(raws)}


def crash_zip_dir(batch_dir: str, crash_root: str, logger: Optional[logging.Logger] = None) -> Optional[str]:
    try:
        name = os.path.basename(batch_dir.rstrip(os.sep)) + ".zip"
        zip_path = os.path.join(crash_root, name)
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(batch_dir):
                for f in files:
                    full = os.path.join(root, f)
                    arc = os.path.relpath(full, batch_dir)
                    z.write(full, arcname=arc)
        return zip_path
    except Exception as exc:
        if logger:
            logger.warning("打包 crash 目录失败: %s", exc)
        return None


# === 系统日志会话（写文件） ===

def _drain_syslog_to_file(proc, path: str, logger: Optional[logging.Logger] = None) -> None:
    try:
        if not proc or not getattr(proc, 'stdout', None):
            return
        with open(path, 'a', encoding='utf-8', errors='replace') as f:
            for line in iter(proc.stdout.readline, ''):
                if not line:
                    break
                f.write(line)
    except (OSError, IOError) as exc:
        if logger:
            logger.debug("syslog 写入失败: %s", exc)
    except Exception as exc:
        if logger:
            logger.debug("syslog 写入未知异常: %s", exc)


def syslog_start_session(goios_manager, udid: str, out_dir: str, parse: bool = True,
                         logger: Optional[logging.Logger] = None) -> Dict[str, Any]:
    p = goios_manager.syslog_stream_popen_parsed(udid=udid, parse=parse)
    if p is None:
        return {"ok": False, "msg": "启动 syslog 失败"}
    name = f"{udid}_syslog_{now_timestamp_str()}.log"
    path = os.path.join(out_dir, name)
    t = threading.Thread(target=_drain_syslog_to_file, args=(p, path, logger), daemon=True, name=f"Syslog-{udid}")
    t.start()
    return {"ok": True, "p": p, "name": name, "path": path}


# === 设备事件监听（SSE生成器） ===

def listen_event_stream(goios_manager):
    p = goios_manager.listen_popen()
    try:
        if not p or not p.stdout:
            yield "event: error\ndata: failed to start listen\n\n"
            return
        for line in p.stdout:
            if not line:
                break
            data = line.strip()
            yield f"data: {data}\n\n"
    finally:
        try:
            if p:
                p.terminate()
        except (OSError, AttributeError) as exc:
            logging.debug("listen_popen 终止异常: %s", exc)


def parse_crash_ls_items(raw: str) -> List[str]:
    items: List[str] = []
    raw = raw or ""

    # 1) 先尝试整体 JSON
    def _push_files(files_in):
        for x in files_in or []:
            if not isinstance(x, str):
                continue
            name_in = x.strip()
            if not name_in or name_in in (".", ".."):
                continue
            items.append(name_in)

    data = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        pass
    if isinstance(data, dict):
        files_found = data.get("files") or data.get("list")
        if isinstance(files_found, list):
            _push_files(files_found)
    elif isinstance(data, list) and all(isinstance(x, str) for x in data):
        _push_files(data)

    # 2) 若未取到，逐行解析（应对多段 JSON / 告警 JSON + 结果 JSON）
    if not items:
        for line in str(raw).splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                # 不是 JSON，跳过（避免把整段 JSON 文本当成文件项）
                continue
            if isinstance(obj, dict):
                files_found = obj.get("files") or obj.get("list")
                if isinstance(files_found, list):
                    _push_files(files_found)
        # 逐行提取后可能仍为空，则再按纯文本回退
        if not items:
            for line in str(raw).splitlines():
                line_name = line.strip()
                if not line_name or line_name in (".", ".."):
                    continue
                # 仅接受看起来像文件名的项（.ips 或 非 JSON 格式）
                if line_name.startswith("{") and line_name.endswith("}"):
                    continue
                items.append(line_name)

    # 去重
    seen = set()
    uniq: List[str] = []
    for unique_name in items:
        if unique_name not in seen:
            seen.add(unique_name)
            uniq.append(unique_name)
    return uniq


def stream_syslog_sse(goios_manager, udid: str, parse: bool = True, logger: Optional[logging.Logger] = None,
                      keywords: Optional[List[str]] = None,
                      levels: Optional[List[str]] = None,
                      existing_process=None):
    """基于 go-ios syslog 的 SSE 生成器。客户端断开后终止进程。"""
    # 如果提供了现有进程，使用它；否则创建新的
    p = existing_process
    if not p:
        if logger:
            logger.warning("没有提供现有进程，创建新的syslog进程: udid=%s", udid)
        p = goios_manager.syslog_stream_popen_parsed(udid=udid, parse=parse)

    kw_list = [str(k or "").lower() for k in (keywords or []) if str(k or "").strip()]
    lv_set = set([str(l or "").lower() for l in (levels or []) if str(l or "").strip()])

    def _bucketize_level(val: str) -> str:
        s = str(val or "").strip().lower()
        # 数字优先级：0=emerg ... 7=debug
        try:
            n = int(s)
            if n <= 3:
                return "error"
            if n == 4:
                return "warning"
            return "info"
        except (ValueError, TypeError):
            pass
        if not s:
            return "info"
        if "warn" in s:
            return "warning"
        if s in ("error", "err", "fault", "critical", "crit", "emergency", "emerg", "alert", "fatal"):
            return "error"
        # 常见信息级别：info/notice/default/debug
        return "info"

    try:
        if not p or not getattr(p, 'stdout', None):
            yield "event: error\ndata: failed to start syslog\n\n"
            return

        # 立即发送一次注释/心跳，触发浏览器接收响应头并建立 EventSource
        yield ": heartbeat\n\n"

        for line in p.stdout:
            if not line:
                break
            data = line.rstrip("\n\r")
            include = True
            if parse:
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    obj = None
                if isinstance(obj, dict):
                    msg_val = str(obj.get("Message") or obj.get("message") or obj.get("msg") or "")
                    proc_val = str(
                        obj.get("Process") or obj.get("process") or obj.get("Sender") or obj.get("Image") or obj.get(
                            "Program") or "")
                    raw_level = obj.get("Level") or obj.get("level") or obj.get("Priority") or ""
                    bucket = _bucketize_level(raw_level)
                    low_msg = msg_val.lower()
                    low_proc = proc_val.lower()
                    if kw_list:
                        include = any((k in low_msg) or (k in low_proc) for k in kw_list)
                    if include and lv_set:
                        include = (bucket in lv_set)
                else:
                    # 解析失败时按原始文本关键字
                    if kw_list:
                        include = any(k in data.lower() for k in kw_list)
            else:
                # 非 parse 模式仅做关键字过滤
                if kw_list:
                    include = any(k in data.lower() for k in kw_list)
            if not include:
                continue
            # 简单限长，避免超长行撑爆前端
            if len(data) > 8000:
                data = data[:8000] + " …"
            yield f"data: {data}\n\n"
    finally:
        # 只有当进程是我们创建的（不是传入的现有进程）时才终止
        if not existing_process and p:
            try:
                if logger:
                    logger.debug("终止新创建的syslog进程: udid=%s", udid)
                p.terminate()
            except (OSError, AttributeError) as exc:
                if logger:
                    logger.debug("syslog_sse 终止异常: %s", exc)
