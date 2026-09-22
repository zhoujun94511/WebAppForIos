"""
文件上传处理工具
从WebForWebsocketCommunication移植的文件传输功能
"""

import os
import shutil
import time
import uuid
import logging
from typing import Tuple, Optional, Dict, Any
from werkzeug.utils import secure_filename
from flask import current_app, jsonify, request as flask_request

# 统一使用模块级 logger
logger = logging.getLogger(__name__)

# ===== 允许的扩展名（与前端一致）=====
ALLOWED_EXTENSIONS = {
    # 图片
    'jpeg', 'jpg', 'png', 'gif', 'bmp', 'webp', 'svg', 'tiff', 'tif', 'ico', 'heic', 'heif',  # noqa: E501
    # 视频
    'mp4', 'webm', 'mov', 'mkv', 'avi', 'flv', 'wmv', 'mpeg', 'mpg', 'm4v', '3gp',
    # 音频
    'mp3', 'wav', 'ogg', 'flac', 'aac', 'm4a', 'wma',
    # 文档
    'txt', 'pdf', 'cert', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'csv', 'json', 'rtf',
    'odt', 'ods', 'odp', 'xml', 'yaml', 'yml', 'md', 'log', 'ini', 'conf', 'toml', 'xmind',
    # 压缩/打包
    'zip', 'rar', '7z', 'tar', 'gz', 'bz2', 'xz', 'tgz', 'lz', 'lzma', 'zst',
    # 代码/配置/脚本
    'js', 'jsx', 'ts', 'tsx', 'py', 'java', 'cpp', 'c', 'cc', 'h', 'hpp', 'cs', 'go', 'rs', 'rb', 'php', 'swift', 'kt',
    'sh', 'bash', 'bat', 'cmd', 'pl', 'lua', 'vue', 'svelte', 'scss', 'less', 'sql',
    'properties', 'gradle', 'dockerfile', 'makefile', 'cmake', 'env', 'cfg', 'ini', 'toml',
    # 安装包/可执行/镜像（生产可按需关闭）
    'exe', 'msi', 'apk', 'ipa', 'dmg', 'pkg', 'deb', 'rpm', 'bin', 'run', 'appimage', 'iso', 'img',
    # 设计/3D/工程（可选）
    'psd', 'ai', 'xd', 'fig', 'sketch', 'blend', 'fbx', 'obj', 'stl', 'dwg', 'dxf',
    # 数据/模型（可选）
    'pickle', 'pkl', 'h5', 'hdf5', 'onnx', 'pt', 'pth', 'ckpt', 'npz', 'npy', 'parquet'
}

# 可执行/脚本类黑名单（当 ALLOW_EXECUTABLES=False 时生效）
DANGEROUS_EXT = {
    'exe', 'msi', 'apk', 'ipa', 'bat', 'cmd', 'sh', 'bash', 'ps1', 'bin', 'run', 'appimage'
}

# ===== 上传指标（简单全局计数器）=====
METRICS = {
    'total_uploads': 0,
    'total_chunks': 0,
    'chunk_failures': 0,
    'direct_failures': 0,
    'bytes_uploaded': 0,
    'chunk_bytes_uploaded': 0,
    'direct_bytes_uploaded': 0,
    'total_chunk_duration_s': 0.0,
    'total_direct_duration_s': 0.0,
}

# 高精度计时器
hpc = time.perf_counter


def _ext_from_raw_filename(raw: str) -> str:
    """从原始文件名取扩展（不经过 secure_filename），返回不带点的小写扩展；取不到返回空"""
    if not raw or '.' not in raw:
        return ''
    return raw.rsplit('.', 1)[1].lower()


def allowed_file_from_raw(raw_filename: str) -> bool:
    """
    取消原有的白名单限制，放行所有扩展名，仅在显式禁用可执行文件时拦截危险扩展。
    这样可以支持 cert 等此前被拒绝的文件类型。
    """
    ext = _ext_from_raw_filename(raw_filename)
    allow_exec = current_app.config.get('ALLOW_EXECUTABLES', True)
    if not allow_exec and ext and ext in DANGEROUS_EXT:
        return False
    return True


def make_safe_filename(raw_filename: str) -> str:
    """
    生成安全文件名并保留扩展名：
    - 先从原始文件名抽取扩展 ext（小写）
    - 再对"文件名主体"做 secure_filename
    - 如主体被清洗成空字符串，则兜底 file_<ts>
    """
    base, ext = os.path.splitext(raw_filename or '')
    ext = (ext or '').lower()  # 包含点，如 ".pdf"
    if not ext and '.' in (raw_filename or ''):
        ext = '.' + raw_filename.rsplit('.', 1)[1].lower()
    safe_base = secure_filename(base) or f'file_{int(time.time())}'
    return safe_base + (ext or '')


def _uploads_dir() -> str:
    """获取上传目录路径"""
    base_dir = current_app.config.get('UPLOAD_FOLDER', './uploads')
    # 使用即时传输专用目录
    instant_dir = os.path.join(base_dir, 'instant_transfer')
    os.makedirs(instant_dir, exist_ok=True)
    return instant_dir


def _move_into_random_subdir(filename: str) -> str:
    """将文件移动到随机前缀子目录，返回一次性短链"""
    uploads_dir = _uploads_dir()
    random_prefix = uuid.uuid4().hex[:8]
    subdir = os.path.join(uploads_dir, random_prefix)
    os.makedirs(subdir, exist_ok=True)
    
    src_path = os.path.join(uploads_dir, filename)
    dst_path = os.path.join(subdir, filename)
    
    if os.path.exists(src_path):
        shutil.move(src_path, dst_path)
        logger.info(f"File moved to subdirectory: {random_prefix}/{filename}")
        return f"/api/instant_download/{random_prefix}/{filename}"
    else:
        logger.warning(f"Source file not found: {src_path}")
        return f"/api/instant_download/{random_prefix}/{filename}"


def _normalize_chunk_indices(form) -> Tuple[Optional[int], Optional[int], bool]:
    """解析分片索引，返回 (chunk_idx, total_chunks, is_chunk)"""
    chunk_idx = None
    total_chunks = None
    is_chunk = False
    
    # 尝试多种分片索引字段名
    for field in ['chunk', 'chunkIndex', 'chunkNumber']:
        if field in form:
            try:
                chunk_idx = int(form[field])
                is_chunk = True
                break
            except (ValueError, TypeError):
                continue
    
    # 尝试多种总片数字段名
    for field in ['totalChunks', 'total_chunks', 'chunks']:
        if field in form:
            try:
                total_chunks = int(form[field])
                break
            except (ValueError, TypeError):
                continue
    
    if is_chunk and total_chunks is None:
        raise ValueError("chunk index provided but total chunks missing")
    
    if is_chunk and (chunk_idx < 0 or chunk_idx >= total_chunks):
        raise ValueError("chunk index out of range")
    
    return chunk_idx, total_chunks, is_chunk


def save_chunk(file, chunk_idx: int, filename: str):
    """保存分片文件"""
    uploads_dir = _uploads_dir()
    chunk_dir = os.path.join(uploads_dir, f"{filename}.chunks")
    os.makedirs(chunk_dir, exist_ok=True)
    
    chunk_path = os.path.join(chunk_dir, f"chunk_{chunk_idx}")
    file.save(chunk_path)
    logger.debug(f"Saved chunk {chunk_idx} for {filename}")


def merge_chunks(filename: str, total_chunks: int):
    """合并分片文件"""
    uploads_dir = _uploads_dir()
    chunk_dir = os.path.join(uploads_dir, f"{filename}.chunks")
    final_path = os.path.join(uploads_dir, filename)
    
    with open(final_path, 'wb') as outfile:
        for i in range(total_chunks):
            chunk_path = os.path.join(chunk_dir, f"chunk_{i}")
            if os.path.exists(chunk_path):
                with open(chunk_path, 'rb') as chunk_file:
                    # 使用类型转换来避免类型检查器警告
                    shutil.copyfileobj(chunk_file, outfile)  # type: ignore
            else:
                logger.warning(f"Missing chunk {i} for {filename}")
    
    # 清理分片目录
    shutil.rmtree(chunk_dir, ignore_errors=True)
    logger.info(f"Merged {total_chunks} chunks for {filename}")


def handle_upload(req: flask_request) -> Tuple[Any, int]:
    """
    上传处理入口：供 app.py 的 /api/instant_upload 路由直接调用
    统一返回 (Response, status_code)
    """
    try:
        file = req.files.get('file')
        
        # 文件名来源：表单字段或 fileStorage.filename（用于判扩展）
        raw_filename = req.form.get('filename') or (file.filename if file else None)
        if not raw_filename:
            return jsonify({"message": "Missing filename"}), 400
        
        # 先用"原始文件名"判断是否允许（避免中文清洗丢扩展）
        if not allowed_file_from_raw(raw_filename):
            bad_ext = _ext_from_raw_filename(raw_filename) or 'unknown'
            return jsonify({"message": f"File type not allowed: .{bad_ext}"}), 400
        
        # 再生成用于落盘的安全文件名（保留扩展）
        safe_filename = make_safe_filename(raw_filename)
        
        # 解析是否分片请求
        try:
            chunk_idx, total_chunks, is_chunk = _normalize_chunk_indices(req.form)
        except ValueError as ve:
            return jsonify({"message": str(ve)}), 400
        
        # 非分片直传
        if not is_chunk:
            if not file:
                return jsonify({"message": "No file part"}), 400
            
            dst = os.path.join(_uploads_dir(), secure_filename(safe_filename))
            t0 = hpc()
            file.save(dst)
            dur = hpc() - t0
            logger.info(f"File {safe_filename} has been uploaded directly.")
            try:
                size_bytes = (
                    int(req.form.get('size') or 0)
                    or int(getattr(file, 'content_length', 0) or 0)
                    or int(req.content_length or 0)
                )
            except (ValueError, TypeError, AttributeError):
                size_bytes = 0
            METRICS['total_uploads'] += 1
            METRICS['bytes_uploaded'] += max(0, size_bytes)
            METRICS['direct_bytes_uploaded'] += max(0, size_bytes)
            METRICS['total_direct_duration_s'] += max(0.0, dur)
            # 移动到随机前缀子目录，返回一次性短链
            file_url = _move_into_random_subdir(safe_filename)
            return jsonify({
                "message": "File uploaded successfully!",
                "completed": True,
                "fileUrl": file_url
            }), 200
        
        # 分片上传
        if not file:
            return jsonify({"message": "No file part for chunk"}), 400
        
        try:
            start_pos = int(req.form.get('start') or -1)
            end_pos = int(req.form.get('end') or -1)
            chunk_bytes = (end_pos - start_pos + 1) if (0 <= start_pos <= end_pos) else 0
        except (ValueError, TypeError):
            chunk_bytes = 0
        
        t0 = hpc()
        save_chunk(file, chunk_idx, safe_filename)
        dur = hpc() - t0
        METRICS['total_chunks'] += 1
        METRICS['bytes_uploaded'] += max(0, chunk_bytes)
        METRICS['chunk_bytes_uploaded'] += max(0, chunk_bytes)
        METRICS['total_chunk_duration_s'] += max(0.0, dur)
        
        if (chunk_idx + 1) == total_chunks:
            merge_chunks(safe_filename, total_chunks)
            # 合并完成后移动到随机前缀子目录，返回一次性短链
            file_url = _move_into_random_subdir(safe_filename)
            logger.info(f"Merged file {safe_filename}, totalChunks={total_chunks}")
            return jsonify({
                "message": "File uploaded successfully!",
                "completed": True,
                "fileUrl": file_url,
                "receivedChunk": chunk_idx,
                "totalChunks": total_chunks,
                "progress": 100
            }), 200
        else:
            progress = int(((chunk_idx + 1) / total_chunks) * 100)
            return jsonify({
                "message": "Chunk received",
                "completed": False,
                "receivedChunk": chunk_idx,
                "totalChunks": total_chunks,
                "progress": progress
            }), 200
    
    except Exception as e:
        logger.exception("File upload failed")
        _is_chunk = (
            (req.form.get('chunk') is not None)
            or (req.form.get('chunkIndex') is not None)
            or (req.form.get('chunkNumber') is not None)
        )
        if _is_chunk:
            METRICS['chunk_failures'] += 1
        else:
            METRICS['direct_failures'] += 1
        return jsonify({"message": f"File upload failed: {str(e)}"}), 500


def get_metrics_snapshot() -> Dict[str, Any]:
    """返回上传指标快照和派生统计"""
    total_uploads = METRICS['total_uploads']
    total_chunks = METRICS['total_chunks']
    chunk_failures = METRICS['chunk_failures']
    direct_failures = METRICS['direct_failures']
    bytes_uploaded = METRICS['bytes_uploaded']
    chunk_bytes = METRICS['chunk_bytes_uploaded']
    direct_bytes = METRICS['direct_bytes_uploaded']
    chunk_duration = METRICS['total_chunk_duration_s']
    direct_duration = METRICS['total_direct_duration_s']
    
    # 计算平均速度
    total_duration = chunk_duration + direct_duration
    avg_speed_mbps = (bytes_uploaded / (1024 * 1024)) / max(total_duration, 0.001) if total_duration > 0 else 0
    
    # 计算成功率
    total_attempts = total_uploads + chunk_failures + direct_failures
    success_rate = (total_uploads / max(total_attempts, 1)) * 100
    
    return {
        'total_uploads': total_uploads,
        'total_chunks': total_chunks,
        'chunk_failures': chunk_failures,
        'direct_failures': direct_failures,
        'bytes_uploaded': bytes_uploaded,
        'chunk_bytes_uploaded': chunk_bytes,
        'direct_bytes_uploaded': direct_bytes,
        'total_chunk_duration_s': chunk_duration,
        'total_direct_duration_s': direct_duration,
        'avg_speed_mbps': round(avg_speed_mbps, 2),
        'success_rate': round(success_rate, 2)
    }
