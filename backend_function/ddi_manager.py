"""DeveloperDiskImage（个性化 DDI）加载逻辑。

对齐 AutoPilot ``autopilot/mobile/ios_bootstrap.py``：
  1) 按设备 ApChipID + ApBoardID 匹配本地 ``devimages/**/Restore``；
  2) 只硬挂匹配项（避免旧 ddi-15F31d 对 iPad/新 SoC findIdentity 失败）；
  3) 本地无匹配时，从 doronz88/DeveloperDiskImage 的 main 拉取当前个性化镜像，
     确认 BuildManifest 含本机芯片身份后再下载对应载荷；
  4) 仍失败 → go-ios ``image auto``（旧版仍会拉 ddi-15F31d）；
  5) 从 auto 报错解析 ChipID/BoardId，再按身份补上游镜像并重试；
  6) 再失败 → 可选 pymobiledevice3 ``mounter auto-mount``。

iOS 17+ 的个性化镜像是一份多芯片身份包，文件名在上游保持稳定
（BuildManifest.plist / Image.dmg / Image.dmg.trustcache），内容随 main 更新。
"""

from __future__ import annotations

import json
import logging
import plistlib
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

AGENT_ENV = {"ENABLE_GO_IOS_AGENT": "user"}

# 上游按固定文件名发布「当前」个性化 DDI，不绑定某个构建号（如 17E5179g）。
# https://github.com/doronz88/DeveloperDiskImage
PERSONALIZED_IMAGE_DIR = "Xcode_iOS_DDI_Personalized"
PERSONALIZED_RAW_BASE = (
    "https://raw.githubusercontent.com/doronz88/DeveloperDiskImage/main/"
    "PersonalizedImages/Xcode_iOS_DDI_Personalized/"
)
_DDI_FETCH_LOCK = threading.Lock()

_RE_AP_IDS = re.compile(
    r"ApBoardId\s+(0x[0-9a-fA-F]+)\s+and\s+ApChipId\s+(0x[0-9a-fA-F]+)",
    re.IGNORECASE,
)
_RE_DEC_IDS = re.compile(
    r"BoardId\s*:\s*(\d+)\b.*?ChipID\s*:\s*(\d+)\b",
    re.IGNORECASE | re.DOTALL,
)

_SUBPROCESS_ERRORS = (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired)
_PLIST_ERRORS = (OSError, ValueError, TypeError, plistlib.InvalidFileException)
_JSON_ERRORS = (json.JSONDecodeError, TypeError, ValueError, AttributeError)
_GOIOS_CALL_ERRORS = (AttributeError, TypeError, RuntimeError, OSError, ValueError)


def _plist_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip().lower()
    if not s:
        return None
    try:
        return int(s, 16) if s.startswith("0x") else int(s)
    except (ValueError, TypeError):
        return None


def list_local_ddi_restore_dirs(basedir: str | Path) -> list[Path]:
    """枚举 basedir 下所有含 Restore/BuildManifest.plist 的个性化 DDI。"""
    root = Path(basedir)
    out: list[Path] = []
    seen: set[str] = set()
    if not root.is_dir():
        return out
    for manifest in sorted(root.rglob("BuildManifest.plist")):
        restore = manifest.parent
        if restore.name != "Restore":
            continue
        key = str(restore.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(restore)

    def _rank(p: Path) -> tuple[int, str]:
        name = p.parent.name.lower()
        if name == "ddi-17e5179g":
            return 0, name
        if name.startswith("ddi-") and name != "ddi-15f31d":
            return 1, name
        if name == "ddi-15f31d":
            return 2, name
        return 3, name

    out.sort(key=_rank)
    return out


def ddi_identity_matches(restore_dir: Path, chip_id: int, board_id: int) -> bool:
    """BuildManifest 是否含与本机 ApChipID / ApBoardID 一致的 BuildIdentity。"""
    manifest = Path(restore_dir) / "BuildManifest.plist"
    if not manifest.is_file():
        return False
    try:
        data = plistlib.loads(manifest.read_bytes())
    except _PLIST_ERRORS:
        return False
    for bi in data.get("BuildIdentities", []) or []:
        if not isinstance(bi, dict):
            continue
        info = bi.get("Info") if isinstance(bi.get("Info"), dict) else {}
        chip = _plist_int(bi.get("ApChipID") or info.get("ApChipID"))
        board = _plist_int(bi.get("ApBoardID") or info.get("ApBoardID"))
        if chip == chip_id and board == board_id:
            return True
    return False


def parse_personalization_ids(text: str) -> Optional[tuple[int, int]]:
    """从 go-ios findIdentity 报错里取出 (ChipID, BoardId)。

    旧版 ``image auto`` 读不到芯片身份（未装 pymobiledevice3）时，
    报错本身仍带 ``ApChipId`` / ``BoardId``。
    """
    raw = text or ""
    match = _RE_AP_IDS.search(raw)
    if match:
        board = _plist_int(match.group(1))
        chip = _plist_int(match.group(2))
        if chip is not None and board is not None:
            return chip, board
    match = _RE_DEC_IDS.search(raw)
    if match:
        board = _plist_int(match.group(1))
        chip = _plist_int(match.group(2))
        if chip is not None and board is not None:
            return chip, board
    return None


def _bundled_devimages_dir() -> Optional[Path]:
    """仓库自带镜像目录。包导入失败时再按脚本直跑方式导入。"""
    import importlib

    for module_name in ("backend_function.config", "config"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        else:
            raw = getattr(getattr(module, "Config", None), "BUNDLED_DEVIMAGES_DIR", None)
            if raw:
                return Path(raw)
    return None


def _restore_search_dirs(basedir: Path) -> list[Path]:
    """下载目录在前，仓库自带的 IOSPrechecker/devimages 在后。"""
    roots = [basedir]
    bundled = _bundled_devimages_dir()
    if bundled is None:
        return roots
    try:
        same = bundled.resolve() == basedir.resolve()
    except OSError:
        same = str(bundled) == str(basedir)
    if not same:
        roots.append(bundled)
    return roots


def _matched_restore_dirs(basedir: Path, ids: Optional[tuple[int, int]]) -> list[Path]:
    if not ids:
        return []
    chip, board = ids
    found: list[Path] = []
    seen: set[str] = set()
    for root in _restore_search_dirs(basedir):
        for restore in list_local_ddi_restore_dirs(root):
            key = str(restore).lower()
            if key in seen or not ddi_identity_matches(restore, chip, board):
                continue
            seen.add(key)
            found.append(restore)
    return found


def _ids_desc(ids: Optional[tuple[int, int]]) -> str:
    if not ids:
        return "ChipID=未知"
    chip, board = ids
    return f"ChipID=0x{chip:x} BoardId=0x{board:x}"


def identity_payload_paths(manifest_data: dict, chip_id: int, board_id: int) -> list[str]:
    """取出与本机芯片身份对应的载荷相对路径（如 Image.dmg）。"""
    paths: list[str] = []
    for bi in manifest_data.get("BuildIdentities", []) or []:
        if not isinstance(bi, dict):
            continue
        info = bi.get("Info") if isinstance(bi.get("Info"), dict) else {}
        chip = _plist_int(bi.get("ApChipID") or info.get("ApChipID"))
        board = _plist_int(bi.get("ApBoardID") or info.get("ApBoardID"))
        if chip != chip_id or board != board_id:
            continue
        entries = bi.get("Manifest") if isinstance(bi.get("Manifest"), dict) else {}
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            einfo = entry.get("Info") if isinstance(entry.get("Info"), dict) else {}
            raw = einfo.get("Path")
            if not isinstance(raw, str) or not raw.strip():
                continue
            rel = raw.replace("\\", "/").lstrip("/")
            parts = Path(rel).parts
            if not rel or Path(rel).is_absolute() or ".." in parts:
                continue
            if rel not in paths:
                paths.append(rel)
        break
    return paths


def _download_bytes(url: str, timeout: int = 180) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        blob = resp.read()
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        raise ValueError(f"下载内容为空: {url}")
    return bytes(blob)


def ensure_upstream_personalized_ddi(
    basedir: str | Path,
    chip_id: int,
    board_id: int,
) -> Tuple[bool, str]:
    """按芯片身份拉取上游当前个性化镜像。

    先下 BuildManifest，确认含本机 ApChipID/ApBoardID 后，再下该身份引用的载荷。
    清单与本地一致且文件已在时不重复下载。
    """
    restore = Path(basedir) / PERSONALIZED_IMAGE_DIR / "Restore"
    manifest_path = restore / "BuildManifest.plist"
    id_desc = f"ChipID=0x{chip_id:x} BoardId=0x{board_id:x}"

    with _DDI_FETCH_LOCK:
        try:
            manifest_bytes = _download_bytes(PERSONALIZED_RAW_BASE + "BuildManifest.plist", timeout=60)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            return False, f"下载上游 BuildManifest 失败: {exc}"

        try:
            data = plistlib.loads(manifest_bytes)
        except _PLIST_ERRORS as exc:
            return False, f"解析上游 BuildManifest 失败: {exc}"
        if not isinstance(data, dict):
            return False, "上游 BuildManifest 格式无效"

        payloads = identity_payload_paths(data, chip_id, board_id)
        if not payloads:
            return False, f"上游当前个性化镜像不含 {id_desc}"

        same_manifest = manifest_path.is_file() and manifest_path.read_bytes() == manifest_bytes
        missing = [rel for rel in payloads if not (restore / rel).is_file()]
        if same_manifest and not missing:
            return True, str(restore)

        try:
            restore.mkdir(parents=True, exist_ok=True)
            manifest_path.write_bytes(manifest_bytes)
        except OSError as exc:
            return False, f"写入 BuildManifest 失败: {exc}"

        for rel in payloads:
            dest = restore / rel
            if same_manifest and dest.is_file():
                continue
            url = PERSONALIZED_RAW_BASE + rel
            logger.info("按 %s 下载个性化载荷: %s", id_desc, rel)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(_download_bytes(url))
            except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
                return False, f"下载 {rel} 失败: {exc}"

    if not ddi_identity_matches(restore, chip_id, board_id):
        return False, f"上游镜像落盘后仍不匹配 {id_desc}"
    return True, str(restore)


def _try_mount_restores(goios, udid: str, restores: list[Path], opts: dict) -> Tuple[bool, str]:
    last_err = ""
    for restore in restores:
        logger.info("尝试挂载匹配 DDI: %s", restore)
        call_opts = dict(opts)
        extra_env = call_opts.get("extra_env")
        if isinstance(extra_env, dict):
            call_opts["extra_env"] = dict(extra_env)
        try:
            ok, out = goios.image_mount(udid, path=str(restore), **call_opts)
        except _GOIOS_CALL_ERRORS as exc:
            last_err = str(exc)
            logger.warning("挂载异常 %s: %s", restore, exc)
            continue
        text = out or ""
        if _mount_output_ok(text) or (ok and _image_already_mounted(goios, udid, **dict(opts))):
            return True, f"已挂载匹配镜像: {restore.parent.name}"
        last_err = text.strip().splitlines()[-1] if text.strip() else last_err
        logger.warning("匹配镜像挂载未成功 (%s): %s", restore.parent.name, last_err[:200])
    return False, last_err


def query_personalization_ids(udid: str) -> Optional[tuple[int, int]]:
    """读设备个性化标识（ChipID / BoardId）。优先 pymobiledevice3，失败返回 None。"""
    udid = (udid or "").strip()
    if not udid:
        return None
    try:
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "pymobiledevice3",
                "mounter",
                "query-personalization-identifiers",
                "--udid",
                udid,
            ],
            capture_output=True,
            timeout=45,
        )
    except _SUBPROCESS_ERRORS as exc:
        logger.debug("query-personalization-identifiers 不可用: %s", exc)
        return None
    text = (r.stdout or b"").decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except _JSON_ERRORS:
        return None
    chip = _plist_int(data.get("ChipID"))
    board = _plist_int(data.get("BoardId"))
    if chip is None or board is None:
        return None
    return chip, board


def _mount_output_ok(output: str) -> bool:
    low = (output or "").lower()
    if "findidentity" in low and ("failed" in low or "error" in low):
        return False
    if "success mounting" in low:
        return True
    if "already" in low and "mounted" in low:
        return True
    if "error" in low or "failed" in low:
        return False
    return False


def _image_already_mounted(goios, udid: str, **opts: Any) -> bool:
    try:
        ok, out = goios.image_list(udid, **opts)
    except _GOIOS_CALL_ERRORS:
        return False
    if not ok or not out:
        return False
    for line in str(out).splitlines():
        line = line.strip()
        if not line:
            continue
        msg = line
        try:
            msg = str(json.loads(line).get("msg", "")).strip()
        except _JSON_ERRORS:
            pass
        if msg and msg.lower() != "none":
            return True
    # 非 JSON：只要不是明显 none
    low = str(out).lower()
    return "none" not in low and bool(out.strip())


def _pymobiledevice3_auto_mount(udid: str) -> Tuple[bool, str]:
    try:
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "pymobiledevice3",
                "mounter",
                "auto-mount",
                "--udid",
                udid,
                "--userspace",
            ],
            capture_output=True,
            timeout=300,
        )
    except FileNotFoundError:
        return False, "未安装 pymobiledevice3"
    except _SUBPROCESS_ERRORS as exc:
        return False, str(exc)
    out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace").strip()
    if r.returncode == 0:
        return True, out or "pymobiledevice3 auto-mount ok"
    return False, out or f"auto-mount exit {r.returncode}"


def ensure_developer_image(
    goios,
    udid: str,
    *,
    basedir: str | Path,
    tunnel_opts: Optional[dict] = None,
    require_success: bool = False,
) -> Tuple[bool, str]:
    """确保设备已挂载匹配的 DeveloperDiskImage。

    :param goios: GoIOSManager 实例
    :param udid: 设备 UDID
    :param basedir: DDI 缓存根目录（如 IOSPrechecker/devimages）
    :param tunnel_opts: 透传给 go-ios 的隧道参数（如 tunnel-info-port）
    :param require_success: True 时挂载失败返回 False（供连接关键路径使用）
    :return: (ok, message)
    """
    udid = (udid or "").strip()
    if not udid:
        return False, "缺少 udid"

    basedir = Path(basedir)
    try:
        basedir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("创建 DEVIMAGES_DIR 失败: %s", exc)

    opts = dict(tunnel_opts or {})
    extra_env = dict(opts.pop("extra_env", {}) or {})
    extra_env.setdefault("ENABLE_GO_IOS_AGENT", "user")
    opts["extra_env"] = extra_env

    probe_opts = dict(opts)
    if isinstance(probe_opts.get("extra_env"), dict):
        probe_opts["extra_env"] = dict(probe_opts["extra_env"])
    if _image_already_mounted(goios, udid, **probe_opts):
        return True, "开发者镜像已挂载"

    ids = query_personalization_ids(udid)
    if ids and not _matched_restore_dirs(basedir, ids):
        # 已知芯片但本地没有对应身份：按身份拉上游当前镜像，避免 image auto 去挂 ddi-15F31d
        chip, board = ids
        dl_ok, dl_msg = ensure_upstream_personalized_ddi(basedir, chip, board)
        if not dl_ok:
            logger.warning("补齐上游个性化镜像失败: %s", dl_msg)

    matched = _matched_restore_dirs(basedir, ids)
    id_desc = _ids_desc(ids)
    if ids:
        logger.info(
            "DDI：%s，本地匹配 %s/%s",
            id_desc,
            len(matched),
            len(list_local_ddi_restore_dirs(basedir)),
        )
    else:
        logger.info("DDI：未能读取 ChipID，跳过本地硬挂，直接尝试在线下载")

    last_err = ""
    if matched:
        mounted, mount_msg = _try_mount_restores(goios, udid, matched, opts)
        if mounted:
            return True, mount_msg
        last_err = mount_msg

    # 在线：go-ios image auto（旧二进制仍硬编码 ddi-15F31d）
    logger.info("本地无可用匹配镜像，尝试 go-ios image auto…")
    try:
        auto_opts = dict(opts)
        extra_env = auto_opts.get("extra_env")
        if isinstance(extra_env, dict):
            auto_opts["extra_env"] = dict(extra_env)
        ok, out = goios.image_auto(udid, basedir=str(basedir), **auto_opts)
        auto_text = out or ""
        if ok and (_mount_output_ok(auto_text) or _image_already_mounted(goios, udid, **dict(opts))):
            return True, "image auto 挂载成功"
        last_err = auto_text.strip().splitlines()[-1] if auto_text.strip() else last_err
        logger.warning("image auto 未成功: %s", (last_err or auto_text)[:240])
    except _GOIOS_CALL_ERRORS as exc:
        last_err = str(exc)
        auto_text = last_err
        logger.warning("image auto 异常: %s", exc)

    # 旧 go-ios 读不到 ChipID，但 findIdentity 报错里带有 ApChipId / BoardId
    if ids is None:
        ids = parse_personalization_ids(auto_text) or parse_personalization_ids(last_err)
        if ids:
            id_desc = _ids_desc(ids)
            logger.info("从 image auto 报错解析到 %s", id_desc)

    if ids:
        chip, board = ids
        if not _matched_restore_dirs(basedir, ids):
            dl_ok, dl_msg = ensure_upstream_personalized_ddi(basedir, chip, board)
            if not dl_ok:
                last_err = dl_msg
                logger.warning("补齐上游个性化镜像失败: %s", dl_msg)
        rematch = [p for p in _matched_restore_dirs(basedir, ids) if p not in matched]
        if rematch:
            mounted, mount_msg = _try_mount_restores(goios, udid, rematch, opts)
            if mounted:
                return True, mount_msg
            if mount_msg:
                last_err = mount_msg

    # 兜底 pymobiledevice3
    logger.info("尝试 pymobiledevice3 mounter auto-mount…")
    ok, out = _pymobiledevice3_auto_mount(udid)
    if ok:
        return True, out
    if out:
        last_err = out.strip().splitlines()[-1]

    hint = (
        f"开发者镜像挂载失败（{id_desc}）。"
        f"上游当前个性化镜像也不含该芯片身份。"
        f"可将含该身份的 Restore/BuildManifest.plist 放入 {basedir}，"
        f"或安装 pymobiledevice3 后重试。最后错误: {last_err or 'unknown'}"
    )
    logger.error(hint)
    if require_success:
        return False, hint
    return False, hint


def local_ddi_summary(basedir: str | Path) -> dict:
    """供诊断 API：列出本地 DDI 与可选芯片覆盖概况。"""
    dirs = list_local_ddi_restore_dirs(basedir)
    items = []
    for restore in dirs:
        chips: set[str] = set()
        manifest = restore / "BuildManifest.plist"
        try:
            data = plistlib.loads(manifest.read_bytes())
            for bi in data.get("BuildIdentities", []) or []:
                if not isinstance(bi, dict):
                    continue
                info = bi.get("Info") if isinstance(bi.get("Info"), dict) else {}
                chip = _plist_int(bi.get("ApChipID") or info.get("ApChipID"))
                if chip is not None:
                    chips.add(f"0x{chip:x}")
        except _PLIST_ERRORS:
            pass
        items.append(
            {
                "name": restore.parent.name,
                "restore": str(restore),
                "chip_count": len(chips),
                "sample_chips": sorted(chips)[:12],
            }
        )
    return {"basedir": str(basedir), "count": len(items), "images": items}
