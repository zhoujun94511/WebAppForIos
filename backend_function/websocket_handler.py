"""
WebSocket处理器
处理即时传输的WebSocket通信逻辑
"""

import time
import logging
from typing import Dict, Any
from flask_socketio import emit, join_room, leave_room
from flask import request

# 统一使用模块级 logger
logger = logging.getLogger(__name__)

# 存储连接的客户端信息
connected_clients: Dict[str, Dict[str, Any]] = {}
# 存储传输会话信息
transfer_sessions: Dict[str, Dict[str, Any]] = {}


def handle_connect():
    """处理客户端连接"""
    client_id = getattr(request, 'sid', None)
    if not client_id:
        logger.warning("无法获取客户端ID")
        return
    
    # 自动加入默认房间
    join_room('default')
    
    connected_clients[client_id] = {
        'connected_at': time.time(),
        'user_agent': request.headers.get('User-Agent', ''),
        'ip': request.remote_addr,
        'room': 'default'
    }
    logger.info(f"客户端连接: {client_id}，已加入默认房间")
    emit('connected', {'message': '连接成功', 'client_id': client_id})


def handle_disconnect():
    """处理客户端断开连接"""
    client_id = getattr(request, 'sid', None)
    if not client_id:
        return
    if client_id in connected_clients:
        # 清理该客户端的传输会话
        for session_id, session in list(transfer_sessions.items()):
            if session.get('client_id') == client_id:
                del transfer_sessions[session_id]
                logger.info(f"清理传输会话: {session_id}")
        
        del connected_clients[client_id]
        logger.info(f"客户端断开: {client_id}")


def handle_join_room(data: Dict[str, Any]):
    """处理加入房间"""
    client_id = getattr(request, 'sid', None)
    if not client_id:
        return
    room = data.get('room', 'default')
    
    if client_id in connected_clients:
        # 离开之前的房间
        old_room = connected_clients[client_id].get('room')
        if old_room:
            leave_room(old_room)
        
        # 加入新房间
        join_room(room)
        connected_clients[client_id]['room'] = room
        logger.info(f"客户端 {client_id} 加入房间: {room}")
        emit('joined_room', {'room': room, 'message': f'成功加入房间 {room}'})
        
        # 通知房间内其他用户
        emit('user_joined', {
            'client_id': client_id,
            'room': room,
            'timestamp': time.time()
        }, to=room, include_self=False)


def handle_leave_room(data: Dict[str, Any]):
    """处理离开房间"""
    client_id = getattr(request, 'sid', None)
    if not client_id:
        return
    room = data.get('room', 'default')
    
    if client_id in connected_clients:
        leave_room(room)
        connected_clients[client_id]['room'] = None
        logger.info(f"客户端 {client_id} 离开房间: {room}")
        emit('left_room', {'room': room, 'message': f'已离开房间 {room}'})
        
        # 通知房间内其他用户
        emit('user_left', {
            'client_id': client_id,
            'room': room,
            'timestamp': time.time()
        }, to=room, include_self=False)


def handle_start_transfer(data: Dict[str, Any]):
    """处理开始传输"""
    client_id = getattr(request, 'sid', None)
    if not client_id:
        emit('transfer_error', {'message': '无法获取客户端ID'})
        return
    files = data.get('files', [])
    room = data.get('room', 'default')
    
    if not files:
        emit('transfer_error', {'message': '没有选择文件'})
        return
    
    # 创建传输会话
    session_id = f"transfer_{int(time.time())}_{client_id[:8]}"
    transfer_sessions[session_id] = {
        'client_id': client_id,
        'room': room,
        'files': files,
        'status': 'preparing',
        'created_at': time.time(),
        'progress': 0,
        'completed_files': 0,
        'total_files': len(files)
    }
    
    logger.info(f"开始传输会话: {session_id}, 文件数: {len(files)}")
    
    # 通知房间内所有用户
    emit('transfer_started', {
        'session_id': session_id,
        'files': files,
        'client_id': client_id,
        'timestamp': time.time()
    }, to=room)
    
    # 发送会话信息给发起者
    emit('transfer_session_created', {
        'session_id': session_id,
        'files': files,
        'message': '传输会话已创建'
    })
    
    # 同时发送 transfer_started 给发起者，触发文件上传
    emit('transfer_started', {
        'session_id': session_id,
        'files': files,
        'client_id': client_id,
        'timestamp': time.time()
    })


def handle_file_uploaded(data: Dict[str, Any]):
    """处理文件上传完成"""
    client_id = getattr(request, 'sid', None)
    if not client_id:
        return
    session_id = data.get('session_id')
    file_info = data.get('file_info', {})
    
    if session_id not in transfer_sessions:
        emit('transfer_error', {'message': '传输会话不存在'})
        return
    
    session = transfer_sessions[session_id]
    if session['client_id'] != client_id:
        emit('transfer_error', {'message': '无权限操作此传输会话'})
        return
    
    # 更新会话状态
    session['completed_files'] += 1
    session['progress'] = int((session['completed_files'] / session['total_files']) * 100)
    
    logger.info(f"文件上传完成: {file_info.get('filename', 'unknown')}, 进度: {session['progress']}%")
    
    # 通知房间内所有用户
    emit('file_uploaded', {
        'session_id': session_id,
        'file_info': file_info,
        'progress': session['progress'],
        'completed_files': session['completed_files'],
        'total_files': session['total_files'],
        'timestamp': time.time()
    }, to=session['room'])
    
    # 检查是否所有文件都上传完成
    if session['completed_files'] >= session['total_files']:
        session['status'] = 'completed'
        emit('transfer_completed', {
            'session_id': session_id,
            'message': '所有文件传输完成',
            'timestamp': time.time()
        }, to=session['room'])
        
        # 清理会话（延迟清理，给客户端时间处理）
        def cleanup_session():
            time.sleep(30)  # 30秒后清理
            if session_id in transfer_sessions:
                del transfer_sessions[session_id]
                logger.info(f"清理传输会话: {session_id}")
        
        import threading
        threading.Thread(target=cleanup_session, daemon=True).start()


def handle_transfer_cancel(data: Dict[str, Any]):
    """处理取消传输"""
    client_id = getattr(request, 'sid', None)
    if not client_id:
        return
    session_id = data.get('session_id')
    
    if session_id not in transfer_sessions:
        emit('transfer_error', {'message': '传输会话不存在'})
        return
    
    session = transfer_sessions[session_id]
    if session['client_id'] != client_id:
        emit('transfer_error', {'message': '无权限操作此传输会话'})
        return
    
    # 更新会话状态
    session['status'] = 'cancelled'
    logger.info(f"传输会话取消: {session_id}")
    
    # 通知房间内所有用户
    emit('transfer_cancelled', {
        'session_id': session_id,
        'message': '传输已取消',
        'timestamp': time.time()
    }, to=session['room'])
    
    # 清理会话
    del transfer_sessions[session_id]


def handle_get_transfer_status(data: Dict[str, Any]):
    """处理获取传输状态"""
    session_id = data.get('session_id')
    
    if session_id not in transfer_sessions:
        emit('transfer_error', {'message': '传输会话不存在'})
        return
    
    session = transfer_sessions[session_id]
    
    # 发送当前状态
    emit('transfer_status', {
        'session_id': session_id,
        'status': session['status'],
        'progress': session['progress'],
        'completed_files': session['completed_files'],
        'total_files': session['total_files'],
        'files': session['files'],
        'created_at': session['created_at']
    })


def handle_get_room_info(data: Dict[str, Any]):
    """处理获取房间信息"""
    room = data.get('room', 'default')
    
    # 统计房间内用户数
    room_users = [cid for cid, info in connected_clients.items() 
                  if info.get('room') == room]
    
    # 统计房间内活跃传输会话
    active_sessions = [session for session in transfer_sessions.values() 
                      if session.get('room') == room and session.get('status') in ['preparing', 'uploading']]
    
    emit('room_info', {
        'room': room,
        'user_count': len(room_users),
        'active_sessions': len(active_sessions),
        'connected_users': room_users,
        'timestamp': time.time()
    })


def handle_message(data: Dict[str, Any]):
    """处理普通消息 - 修复消息广播逻辑"""
    client_id = getattr(request, 'sid', None)
    if not client_id:
        logger.warning("无法获取客户端ID，忽略消息")
        return
    
    # 验证消息内容 - 支持文本消息和文件消息
    if not data:
        logger.warning(f"客户端 {client_id} 发送了空数据，忽略")
        return
    
    # 检查是否为有效消息（文本消息或文件消息）
    is_text_message = data.get('text', '').strip()
    is_file_message = data.get('type') == 'file' and data.get('filename')
    
    if not is_text_message and not is_file_message:
        logger.warning(f"客户端 {client_id} 发送了无效消息，忽略")
        return
    
    # 获取客户端房间
    room = connected_clients.get(client_id, {}).get('room', 'default')
    
    # 广播消息到房间内所有用户（包括发送者）
    emit('message', data, to=room)
    
    if is_file_message:
        logger.info(f"文件消息已广播到房间 {room}: {data.get('filename', 'unknown')}")
    else:
        logger.info(f"文本消息已广播到房间 {room}: {data.get('text', '')[:50]}...")

def handle_ping():
    """处理心跳检测"""
    emit('pong', {'timestamp': time.time()})


def get_connected_clients_count() -> int:
    """获取连接的客户端数量"""
    return len(connected_clients)


def get_active_sessions_count() -> int:
    """获取活跃传输会话数量"""
    return len([s for s in transfer_sessions.values() 
                if s.get('status') in ['preparing', 'uploading']])


def get_transfer_metrics() -> Dict[str, Any]:
    """获取传输指标"""
    total_sessions = len(transfer_sessions)
    completed_sessions = len([s for s in transfer_sessions.values() 
                            if s.get('status') == 'completed'])
    cancelled_sessions = len([s for s in transfer_sessions.values() 
                             if s.get('status') == 'cancelled'])
    
    return {
        'connected_clients': get_connected_clients_count(),
        'active_sessions': get_active_sessions_count(),
        'total_sessions': total_sessions,
        'completed_sessions': completed_sessions,
        'cancelled_sessions': cancelled_sessions,
        'completion_rate': (completed_sessions / max(total_sessions, 1)) * 100
    }
