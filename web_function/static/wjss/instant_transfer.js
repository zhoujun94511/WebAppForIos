/**
 * 即时传输功能JavaScript - 完全基于WebForWebsocketCommunication的聊天界面
 * 处理文件选择、上传、WebSocket通信等功能
 */

class InstantTransfer {
    constructor() {
        this.socket = io();
        this.selectedFiles = [];
        this.currentSession = null;
        this.receivedSession = null;
        this.receivedFiles = [];
        this.currentTransferSession = null;
        
        // 移植自 WebForWebsocketCommunication 的配置
        this.runtimeConfig = { 
            upload: { 
                maxSizeMB: 200, 
                allowExecutables: false, 
                chunkSizeMB: 1, 
                allowedExtensions: null, 
                dangerousExtensions: null 
            } 
        };
        
        this.CHUNK_SIZE = 1024 * 1024; // 1MB chunks
        
        this.init();
    }
    
    init() {
        this.bindEvents();
        this.initSocket();
        this.loadRuntimeConfig();
        this.initPasteHandler();
        this.initDragDrop();
    }
    
    // 加载运行时配置（移植自原项目）
    loadRuntimeConfig() {
        try {
            fetch('/api/instant_config').then(r => r.ok ? r.json() : null).then(cfg => {
                if (cfg && cfg.upload) {
                    if (typeof cfg.upload.maxSizeMB === 'number') this.runtimeConfig.upload.maxSizeMB = cfg.upload.maxSizeMB;
                    if (typeof cfg.upload.allowExecutables === 'boolean') this.runtimeConfig.upload.allowExecutables = cfg.upload.allowExecutables;
                    if (typeof cfg.upload.chunkSizeMB === 'number' && cfg.upload.chunkSizeMB > 0) this.runtimeConfig.upload.chunkSizeMB = cfg.upload.chunkSizeMB;
                    if (Array.isArray(cfg.upload.allowedExtensions)) this.runtimeConfig.upload.allowedExtensions = new Set(cfg.upload.allowedExtensions.map(s => String(s).toLowerCase()));
                    if (Array.isArray(cfg.upload.dangerousExtensions)) this.runtimeConfig.upload.dangerousExtensions = new Set(cfg.upload.dangerousExtensions.map(s => String(s).toLowerCase()));
                }
                // 动态设置切片大小
                this.CHUNK_SIZE = Math.max(256 * 1024, Math.floor(this.runtimeConfig.upload.chunkSizeMB * 1024 * 1024));
            }).catch(() => {});
        } catch (_) { /* ignore */ }
    }
    
    bindEvents() {
        // 文件选择
        const fileInput = document.getElementById('instant-file-input');
        if (fileInput) {
            fileInput.addEventListener('change', (e) => this.handleFileSelect(e));
        }
        
        // 发送消息
        const sendButton = document.getElementById('send-button');
        if (sendButton) {
            sendButton.addEventListener('click', () => this.sendMessage());
        }
        
        // 输入框回车发送
        const inputText = document.getElementById('input-text');
        if (inputText) {
            inputText.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    this.sendMessage();
                }
            });
        }
    }
    
    initSocket() {
        // 连接事件
        this.socket.on('connect', () => {
            if (window.logger && typeof window.logger.info === 'function') {
                window.logger.info('WebSocket连接成功');
            }
            this.updateConnectionStatus('online');
            this.showToast('连接成功', 'success', 2000);
        });
        
        this.socket.on('disconnect', () => {
            if (window.logger && typeof window.logger.warn === 'function') {
                window.logger.warn('WebSocket连接断开');
            }
            this.updateConnectionStatus('offline');
            this.showToast('连接断开', 'warn', 3000);
        });
        
        // 消息事件（移植自原项目）
        this.socket.on('message', (data) => {
            this.displayMessage(data);
        });
        
        // 传输相关事件
        this.socket.on('transfer_session_created', (data) => {
            if (window.logger && typeof window.logger.info === 'function') {
                window.logger.info('传输会话已创建:', data);
            }
            // 只有发送端才设置 currentSession
            if (this.selectedFiles.length > 0) {
                this.currentSession = data.session_id;
            }
        });
        
        this.socket.on('transfer_started', (data) => {
            if (window.logger && typeof window.logger.info === 'function') {
                window.logger.info('传输开始:', data);
            }
            this.handleTransferStarted(data);
        });
        
        this.socket.on('file_uploaded', (data) => {
            if (window.logger && typeof window.logger.info === 'function') {
                window.logger.info('文件上传完成:', data);
            }
            this.handleFileUploaded(data);
        });
        
        this.socket.on('transfer_completed', (data) => {
            if (window.logger && typeof window.logger.info === 'function') {
                window.logger.info('传输完成:', data);
            }
            this.handleTransferCompleted(data);
        });
        
        this.socket.on('transfer_error', (data) => {
            if (window.logger && typeof window.logger.error === 'function') {
                window.logger.error('传输错误:', data);
            }
            this.showToast(`传输错误: ${data.message}`, 'error', 5000);
        });
    }
    
    handleFileSelect(event) {
        const files = Array.from(event.target.files);
        files.forEach(file => {
            if (this.validateFile(file)) {
                this.sendFileInChunks(file);
            }
        });
        // 清理文件选择，便于再次选择同一文件
        event.target.value = '';
    }
    
    // 文件验证（移植自原项目）
    validateFile(file, opts = {}) {
        const maxSizeMB = Number.isFinite(opts.maxSizeMB) ? opts.maxSizeMB : this.runtimeConfig.upload.maxSizeMB;
        const allowExecutables = typeof opts.allowExecutables === 'boolean' ? opts.allowExecutables : this.runtimeConfig.upload.allowExecutables;
        
        const allowedExtensions = this.runtimeConfig.upload.allowedExtensions;
        const dangerousExt = this.runtimeConfig.upload.dangerousExtensions || new Set(['exe','msi','apk','ipa','bat','cmd','sh','bash','ps1','bin','run','appimage']);
        
        const name = file?.name || '';
        const ext = (name.includes('.') ? name.split('.').pop() : '').toLowerCase();
        
        const maxSizeBytes = Math.max(1, maxSizeMB) * 1024 * 1024;
        if (!Number.isFinite(file?.size)) {
            this.showToast('无法读取文件大小', 'error');
            return false;
        }
        if (file.size > maxSizeBytes) {
            this.showToast(`文件过大（上限 ${maxSizeMB}MB）`, 'warn');
            return false;
        }
        
        if (!allowExecutables && dangerousExt.has(ext)) {
            this.showToast(`为安全起见，禁止上传此类可执行/脚本文件（.${ext}）`, 'warn');
            return false;
        }
        
        return true;
    }
    
    sendMessage() {
        const inputText = document.getElementById('input-text');
        if (!inputText) {
            console.error('找不到输入框元素');
            this.showToast('找不到输入框', 'error');
            return;
        }
        
        const message = inputText.value.trim();
        
        // 修复：空消息时静默忽略，不显示任何提示
        if (!message) {
            return;
        }
        
        const time = this.getCurrentTime();
        const sender = 'user_' + Math.random().toString(36).substr(2, 9);
        const messageData = { 
            text: message, 
            time: time, 
            sender: sender 
        };
        
        // 发送到服务器（服务器会广播给所有客户端，包括发送者）
        this.socket.emit('message', messageData);
        
        // 清空输入框
        inputText.value = '';
    }
    
    // 分片上传文件（完全移植自原项目）
    sendFileInChunks(file) {
        const totalChunks = Math.ceil(file.size / this.CHUNK_SIZE);
        let currentChunk = 0;

        const sendNextChunk = () => {
            if (currentChunk >= totalChunks) return;

            const chunkStart = currentChunk * this.CHUNK_SIZE;
            const chunkEndExclusive = Math.min(file.size, chunkStart + this.CHUNK_SIZE);
            const chunkEndInclusive = chunkEndExclusive - 1; // 闭区间
            const chunk = file.slice(chunkStart, chunkEndExclusive);

            const uploadId = `user_${Math.random().toString(36).substr(2, 9)}:${file.size}`;

            const formData = new FormData();
            formData.append('file', chunk);

            // 兼容字段
            formData.append('chunk', String(currentChunk));           // 0-based
            formData.append('chunkIndex', String(currentChunk));      // 0-based 别名
            formData.append('chunkNumber', String(currentChunk + 1)); // 1-based
            formData.append('totalChunks', String(totalChunks));
            formData.append('chunks', String(totalChunks));

            // 基本信息（FormData 支持中文）
            formData.append('filename', file.name);
            formData.append('size', String(file.size));
            formData.append('mime', file.type || 'application/octet-stream');
            formData.append('uploadId', uploadId);

            // 断点信息
            formData.append('start', String(chunkStart));
            formData.append('end', String(chunkEndInclusive));

            fetch('/api/instant_upload', { method: 'POST', body: formData })
                .then(async (response) => {
                    if (!response.ok) {
                        const errText = await response.text().catch(() => '');
                        console.error('Upload HTTP error:', response.status, errText);
                        this.showToast(`上传失败：${response.status} ${errText || ''}`.trim(), 'error', 3600);
                        throw new Error(`Upload failed: ${response.status} ${errText}`);
                    }
                    let data = {};
                    try { data = await response.json(); }
                    catch { data = { progress: Math.floor(((currentChunk + 1) / totalChunks) * 100) }; }

                    const completed = !!(data.completed || data.done || (currentChunk + 1 === totalChunks));
                    const fileUrl = data.shortUrl || data.fileUrl || data.url || data.path;

                    if (completed && fileUrl) {
                        // 只存储相对路径，不暴露IP地址
                        const relativeUrl = fileUrl.startsWith('http') ? fileUrl.replace(/^https?:\/\/[^\/]+/, '') : fileUrl;
                        
                        const fileMessage = {
                            type: 'file',
                            filename: file.name,
                            content: relativeUrl, // 只存储相对路径
                            size: file.size,
                            mime: file.type || '',
                            time: this.getCurrentTime(),
                            sender: 'user_' + Math.random().toString(36).substr(2, 9)
                        };
                        
                        // 立即在本地显示文件消息
                        // 发送到服务器（服务器会广播给所有客户端，包括发送者）
                        this.socket.emit('message', fileMessage);
                    }

                    currentChunk++;
                    sendNextChunk();
                })
                .catch(error => {
                    console.error('Chunk upload failed', error);
                    this.showToast(`上传失败：${error && error.message ? error.message : '网络或服务器错误'}`, 'error', 3600);
                });
        };

        sendNextChunk();
    }

    displayMessage(data) {
        
        const chatContainer = document.getElementById('chat-container');
        const messagesList = document.getElementById('messages-list');
        const emptyState = document.getElementById('empty-state');
        
        if (!chatContainer || !messagesList) {
            console.log('找不到聊天容器');
            return;
        }
        
        // 隐藏空状态提示
        if (emptyState) {
            emptyState.style.display = 'none';
        }
        
        // 确保消息列表可见
        messagesList.style.display = 'block';
        
        // 创建消息元素
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message mb-3 p-3 bg-white rounded shadow-sm';
        
        if (data.type === 'file') {
            // 文件消息 - 参考WebForWebsocketCommunication的展示效果
            const fileType = this.classifyFileTypeByExt(data.filename);
            const isImage = fileType === 'image';
            const isVideo = fileType === 'video';
            
            // 统一使用文件卡片样式
            const fileEmoji = this.getFileTypeEmoji(fileType);
            const buttonText = isImage ? '预览' : (isVideo ? '播放' : '预览');
            const buttonIcon = isImage ? 'fa-eye' : (isVideo ? 'fa-play' : 'fa-eye');
            
            messageDiv.innerHTML = `
                <div class="instant-file-card" data-filetype="${fileType}">
                    <div class="instant-file-icon">${fileEmoji}</div>
                    <div class="instant-file-body">
                        <div class="instant-file-name" title="${data.filename}">${data.filename}</div>
                        <div class="instant-file-meta">${this.formatBytes(data.size)}</div>
                    </div>
                    <div class="instant-file-actions">
                        <button class="btn btn-sm btn-outline-primary instant-preview-btn" data-url="${data.content}" data-filename="${data.filename}">
                            <i class="fas ${buttonIcon}"></i> ${buttonText}
                        </button>
                        <button class="btn btn-sm btn-outline-secondary instant-download-btn" data-url="${data.content}" data-filename="${data.filename}">
                            <i class="fas fa-download"></i> 下载
                        </button>
                    </div>
                    <div class="text-muted small mt-2">${data.time}</div>
                </div>
            `;
        } else {
            // 文本消息 - 支持URL自动识别
            const textContent = data.text || '';
            const linkifiedContent = this.linkifyText(textContent);
            
            messageDiv.innerHTML = `
                <div class="mb-2">${linkifiedContent}</div>
                <div class="text-muted small">${data.time}</div>
            `;
        }
        
        // 添加到消息列表
        messagesList.appendChild(messageDiv);
        
        // 绑定事件监听器
        this.bindFileActions(messageDiv);
        
        // 滚动到底部
        chatContainer.scrollTop = chatContainer.scrollHeight;
    }
    
    // 绑定文件操作事件
    bindFileActions(messageDiv) {
        // 预览按钮事件
        const previewBtns = messageDiv.querySelectorAll('.instant-preview-btn, .instant-preview-trigger');
        previewBtns.forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                const url = btn.getAttribute('data-url');
                const filename = btn.getAttribute('data-filename');
                this.previewFile(url, filename);
            });
        });
        
        // 下载按钮事件
        const downloadBtns = messageDiv.querySelectorAll('.instant-download-btn');
        downloadBtns.forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                const url = btn.getAttribute('data-url');
                const filename = btn.getAttribute('data-filename');
                this.downloadFile(url, filename);
            });
        });
    }
    
    // URL识别和链接化功能
    linkifyText(text) {
        if (!text) return '';
        
        // URL正则表达式
        const URL_REGEX = /(?:(?:https?:\/\/)|(?:www\.))[\w\-]+(?:\.[\w\-.]+)+(?:\:\d+)?(?:\/[^\s<>()\[\]{}"']*)?/gi;
        
        // 转义HTML特殊字符
        const escapeHtml = (str) => {
            return str.replace(/[&<>"']/g, c => ({
                '&': '&amp;',
                '<': '&lt;',
                '>': '&gt;',
                '"': '&quot;',
                "'": '&#39;'
            }[c]));
        };
        
        // 规范化URL
        const normalizeUrl = (url) => {
            if (!/^https?:\/\//i.test(url)) return 'http://' + url;
            return url;
        };
        
        let result = '';
        let lastIndex = 0;
        
        text.replace(URL_REGEX, (match, offset) => {
            // 添加URL前的文本
            if (offset > lastIndex) {
                result += escapeHtml(text.slice(lastIndex, offset));
            }
            
            // 处理URL
            let url = match.trim();
            // 去掉右侧常见尾随标点
            while (/[)\].,!?;:]+$/.test(url)) url = url.slice(0, -1);
            
            const normalizedUrl = normalizeUrl(url);
            result += `<a href="${escapeHtml(normalizedUrl)}" target="_blank" rel="noopener noreferrer nofollow ugc" class="text-primary">${escapeHtml(url)}</a>`;
            
            lastIndex = offset + match.length;
            return match;
        });
        
        // 添加剩余的文本
        if (lastIndex < text.length) {
            result += escapeHtml(text.slice(lastIndex));
        }
        
        return result;
    }
    
    // 文件类型分类 - 参考WebForWebsocketCommunication
    classifyFileTypeByExt(filename = '') {
        const ext = (filename.split('.').pop() || '').toLowerCase();
        if (['zip','rar','7z','tar','gz','bz2'].includes(ext)) return 'archive';
        if (['pdf'].includes(ext)) return 'pdf';
        if (['doc','docx','rtf'].includes(ext)) return 'doc';
        if (['xls','xlsx','csv'].includes(ext)) return 'sheet';
        if (['ppt','pptx'].includes(ext)) return 'ppt';
        if (['txt','log','md'].includes(ext)) return 'text';
        if (['json'].includes(ext)) return 'json';
        if (['xmind'].includes(ext)) return 'xmind';
        if (['jpg','jpeg','png','gif','webp','bmp','svg'].includes(ext)) return 'image';
        if (['mp4','webm','mov','mkv','avi','flv','wmv','mpeg','mpg','m4v','3gp'].includes(ext)) return 'video';
        if (['mp3','wav','flac','aac','ogg','m4a'].includes(ext)) return 'audio';
        return 'other';
    }
    
    // 文件类型表情符号
    getFileTypeEmoji(type) {
        switch (type) {
            case 'archive': return '📦';
            case 'pdf': return '📄';
            case 'doc': return '📝';
            case 'sheet': return '📊';
            case 'ppt': return '📽️';
            case 'text': return '📄';
            case 'json': return '🔧';
            case 'xmind': return '🧠';
            case 'image': return '🖼️';
            case 'video': return '🎬';
            case 'audio': return '🎵';
            default: return '📦';
        }
    }
    
    // 检查文件是否支持预览
    isPreviewable(filename) {
        const previewableExtensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg', '.mp4', '.webm', '.ogg', '.pdf', '.txt', '.md'];
        const ext = filename.toLowerCase().substring(filename.lastIndexOf('.'));
        return previewableExtensions.includes(ext);
    }
    
    // 预览文件方法
    previewFile(url, filename) {
        if (!this.isPreviewable(filename)) {
            this.showToast('此文件类型不支持预览', 'warning', 2000);
            return;
        }
        
        // 构造完整URL，但不暴露在HTML中
        const fullUrl = url.startsWith('http') ? url : 
                        url.startsWith('/') ? window.location.origin + url : 
                        window.location.origin + '/' + url;
        
        window.open(fullUrl, '_blank');
    }
    
    // 下载文件方法
    downloadFile(url, filename) {
        // 构造完整URL，但不暴露在HTML中
        const fullUrl = url.startsWith('http') ? url : 
                        url.startsWith('/') ? window.location.origin + url : 
                        window.location.origin + '/' + url;
        
        const a = document.createElement('a');
        a.href = fullUrl;
        a.setAttribute('download', filename || 'download');
        document.body.appendChild(a);
        a.click();
        a.remove();
    }
    
    handleTransferStarted(data) {
        console.log('传输开始:', data);
        // 这里可以添加传输开始的UI反馈
    }
    
    handleFileUploaded(data) {
        console.log('文件上传完成:', data);
        // 这里可以添加文件上传完成的UI反馈
    }
    
    handleTransferCompleted(data) {
        console.log('传输完成:', data);
        // 这里可以添加传输完成的UI反馈
    }
    
    // 初始化粘贴处理
    initPasteHandler() {
        const messageInput = document.getElementById('message-input');
        if (!messageInput) return;
        
        messageInput.addEventListener('paste', (e) => {
            this.handlePaste(e);
        });
        
        // 添加粘贴提示
        this.createPasteHint();
    }
    
    // 处理粘贴事件
    async handlePaste(e) {
        const clipboardData = e.clipboardData || window.clipboardData;
        if (!clipboardData) return;
        
        const items = clipboardData.items;
        const files = [];
        
        // 检查是否有文件
        for (let i = 0; i < items.length; i++) {
            const item = items[i];
            if (item.kind === 'file') {
                const file = item.getAsFile();
                if (file) {
                    files.push(file);
                }
            }
        }
        
        if (files.length > 0) {
            e.preventDefault();
            this.showPasteHint(`检测到 ${files.length} 个文件，正在上传...`);
            
            try {
                // 处理粘贴的文件
                for (const file of files) {
                    await this.uploadPastedFile(file);
                }
                this.showPasteHint(`成功上传 ${files.length} 个文件`);
            } catch (error) {
                console.error('粘贴文件上传失败:', error);
                this.showPasteHint('文件上传失败，请重试');
            }
        }
    }
    
    // 上传粘贴的文件
    async uploadPastedFile(file) {
        try {
            // 检查文件大小
            const maxSizeMB = this.runtimeConfig?.upload?.maxSizeMB || 200;
            if (file.size > maxSizeMB * 1024 * 1024) {
                throw new Error(`文件 ${file.name} 超过大小限制 ${maxSizeMB}MB`);
            }
            
            // 内网环境允许可执行文件传输
            const allowExecutables = this.runtimeConfig?.upload?.allowExecutables ?? true;
            if (!allowExecutables) {
                const ext = file.name.split('.').pop().toLowerCase();
                const dangerousExts = Array.isArray(this.runtimeConfig.upload.dangerousExtensions) 
                    ? this.runtimeConfig.upload.dangerousExtensions 
                    : ['exe', 'bat', 'cmd', 'scr', 'pif', 'com', 'msi'];
                if (dangerousExts.includes(ext)) {
                    throw new Error(`不允许上传可执行文件: ${file.name}`);
                }
            }
            
            // 使用现有的文件上传逻辑
            await this.sendFileInChunks(file);
            
        } catch (error) {
            console.error('上传粘贴文件失败:', error);
            this.showToast(`上传失败: ${error.message}`, 'error', 3000);
        }
    }
    
    // 显示粘贴提示
    showPasteHint(message) {
        const hint = document.getElementById('paste-hint');
        if (hint) {
            hint.textContent = message;
            hint.classList.add('show');
            setTimeout(() => {
                hint.classList.remove('show');
            }, 2000);
        }
    }
    
    // 创建粘贴提示元素
    createPasteHint() {
        const hint = document.createElement('div');
        hint.id = 'paste-hint';
        hint.className = 'paste-hint';
        hint.textContent = '支持粘贴文件';
        document.getElementById('message-input').appendChild(hint);
    }
    
    // 初始化拖拽上传
    initDragDrop() {
        const messageInput = document.getElementById('message-input');
        if (!messageInput) return;
        
        // 防止默认拖拽行为
        ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
            messageInput.addEventListener(eventName, (e) => {
                e.preventDefault();
                e.stopPropagation();
            });
        });
        
        // 拖拽进入
        messageInput.addEventListener('dragenter', () => {
            messageInput.classList.add('drag-over');
        });
        
        // 拖拽离开
        messageInput.addEventListener('dragleave', (e) => {
            if (!messageInput.contains(e.relatedTarget)) {
                messageInput.classList.remove('drag-over');
            }
        });
        
        // 拖拽悬停
        messageInput.addEventListener('dragover', () => {
            messageInput.classList.add('drag-over');
        });
        
        // 拖拽放下
        messageInput.addEventListener('drop', (e) => {
            messageInput.classList.remove('drag-over');
            this.handleDrop(e);
        });
    }
    
    // 处理拖拽放下
    async handleDrop(e) {
        const files = Array.from(e.dataTransfer.files);
        if (files.length === 0) return;
        
        this.showPasteHint(`检测到 ${files.length} 个文件，正在上传...`);
        
        try {
            for (const file of files) {
                await this.uploadPastedFile(file);
            }
            this.showPasteHint(`成功上传 ${files.length} 个文件`);
        } catch (error) {
            console.error('拖拽文件上传失败:', error);
            this.showPasteHint('文件上传失败，请重试');
        }
    }
    
    // 工具方法
    getCurrentTime() {
        const now = new Date();
        const year = now.getFullYear();
        const month = String(now.getMonth() + 1).padStart(2, '0');
        const day = String(now.getDate()).padStart(2, '0');
        const hours = String(now.getHours()).padStart(2, '0');
        const minutes = String(now.getMinutes()).padStart(2, '0');
        const seconds = String(now.getSeconds()).padStart(2, '0');
        return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
    }
    
    formatBytes(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    }
    
    updateConnectionStatus(status) {
        const statusElement = document.getElementById('connection-status');
        if (statusElement) {
            if (status === 'online') {
                statusElement.textContent = '在线';
                statusElement.className = 'badge bg-success rounded-pill';
            } else {
                statusElement.textContent = '离线';
                statusElement.className = 'badge bg-danger rounded-pill offline';
            }
        }
    }
    
    showToast(message, type = 'info', duration = 3000) {
        // 使用统一的通知管理器
        if (window.notificationManager) {
            window.notificationManager.show(type, message, {
                duration: duration,
                useToast: true
            });
        } else {
            // 降级到原有实现
            this.showToastLegacy(message, type, duration);
        }
    }
    
    showToastLegacy(message, type = 'info', duration = 3000) {
        // 原有的toast实现作为降级方案
        const toast = document.createElement('div');
        toast.className = `alert alert-${type === 'success' ? 'success' : type === 'error' ? 'danger' : type === 'warn' ? 'warning' : 'info'} alert-dismissible fade show position-fixed`;
        toast.style.cssText = 'top: 20px; right: 20px; z-index: 9999; min-width: 300px; border-radius: 12px; box-shadow: 0 8px 25px rgba(0,0,0,0.15);';
        toast.innerHTML = `
            <div class="d-flex align-items-center">
                <i class="fas fa-${type === 'success' ? 'check-circle' : type === 'error' ? 'exclamation-circle' : type === 'warn' ? 'exclamation-triangle' : 'info-circle'} me-2"></i>
                <span>${message}</span>
                <button type="button" class="btn-close ms-auto" data-bs-dismiss="alert"></button>
            </div>
        `;
        
        document.body.appendChild(toast);
        
        setTimeout(() => {
            if (toast.parentNode) {
                toast.classList.add('fade');
                setTimeout(() => {
                    if (toast.parentNode) {
                        toast.parentNode.removeChild(toast);
                    }
                }, 300);
            }
        }, duration);
    }
}

// 全局实例
let instantTransfer;

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', function() {
    instantTransfer = new InstantTransfer();
});