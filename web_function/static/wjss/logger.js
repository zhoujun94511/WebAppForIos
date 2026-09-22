/**
 * 统一日志管理器
 * 提供统一的日志输出格式和级别控制
 */
class Logger {
    constructor() {
        this.levels = {
            DEBUG: 0,
            INFO: 1,
            WARN: 2,
            ERROR: 3
        };
        
        this.currentLevel = this.levels.INFO;
        this.debugMode = window.__IOSAPP_DEBUG__ || false;
        this.logHistory = [];
        this.maxHistorySize = 1000;
        
        this.setupGlobalMethods();
    }

    /**
     * 设置全局方法
     */
    setupGlobalMethods() {
        // 兼容旧版本方法
        window.logDebug = (...args) => this.log('DEBUG', ...args);
        window.logWarn = (...args) => this.log('WARN', ...args);
        window.logInfo = (...args) => this.log('INFO', ...args);
        window.logError = (...args) => this.log('ERROR', ...args);
    }

    /**
     * 设置日志级别
     */
    setLevel(level) {
        if (typeof level === 'string') {
            this.currentLevel = this.levels[level.toUpperCase()] || this.levels.INFO;
        } else {
            this.currentLevel = level;
        }
    }

    /**
     * 设置调试模式
     */
    setDebugMode(enabled) {
        this.debugMode = enabled;
        if (enabled) {
            this.setLevel('DEBUG');
        }
    }

    /**
     * 格式化时间戳
     */
    formatTimestamp() {
        const now = new Date();
        return now.toISOString().replace('T', ' ').replace('Z', '');
    }

    /**
     * 格式化日志消息
     */
    formatMessage(level, args) {
        const timestamp = this.formatTimestamp();
        const levelStr = level.padEnd(5);
        const message = args.map(arg => 
            typeof arg === 'object' ? JSON.stringify(arg, null, 2) : String(arg)
        ).join(' ');
        
        return `[${timestamp}] ${levelStr} ${message}`;
    }

    /**
     * 记录日志
     */
    log(level, ...args) {
        const levelValue = this.levels[level];
        
        // 检查是否应该输出此级别的日志
        if (levelValue < this.currentLevel) {
            return;
        }

        // 特殊处理 DEBUG 级别
        if (level === 'DEBUG' && !this.debugMode) {
            return;
        }

        const formattedMessage = this.formatMessage(level, args);
        
        // 添加到历史记录
        this.addToHistory(level, formattedMessage, args);
        
        // 输出到控制台
        this.outputToConsole(level, formattedMessage, args);
    }

    /**
     * 添加到历史记录
     */
    addToHistory(level, formattedMessage, originalArgs) {
        this.logHistory.push({
            timestamp: Date.now(),
            level: level,
            message: formattedMessage,
            originalArgs: originalArgs
        });
        
        // 限制历史记录大小
        if (this.logHistory.length > this.maxHistorySize) {
            this.logHistory.shift();
        }
    }

    /**
     * 输出到控制台
     */
    outputToConsole(level, formattedMessage, args) {
        try {
            switch (level) {
                case 'DEBUG':
                    console.debug(formattedMessage, ...args);
                    break;
                case 'INFO':
                    console.info(formattedMessage, ...args);
                    break;
                case 'WARN':
                    console.warn(formattedMessage, ...args);
                    break;
                case 'ERROR':
                    console.error(formattedMessage, ...args);
                    break;
                default:
                    console.log(formattedMessage, ...args);
            }
        } catch (e) {
            // 防止日志输出本身出错
            console.error('Logger output error:', e);
        }
    }

    /**
     * 获取日志历史
     */
    getHistory(level = null, limit = null) {
        let history = this.logHistory;
        
        if (level) {
            history = history.filter(log => log.level === level);
        }
        
        if (limit) {
            history = history.slice(-limit);
        }
        
        return history;
    }

    /**
     * 清空日志历史
     */
    clearHistory() {
        this.logHistory = [];
    }

    /**
     * 导出日志
     */
    exportLogs(format = 'text') {
        const logs = this.getHistory();
        
        if (format === 'json') {
            return JSON.stringify(logs, null, 2);
        } else {
            return logs.map(log => log.message).join('\n');
        }
    }

    /**
     * 下载日志文件
     */
    downloadLogs(filename = null) {
        const logs = this.exportLogs('text');
        const blob = new Blob([logs], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        
        const link = document.createElement('a');
        link.href = url;
        link.download = filename || `logs_${new Date().toISOString().replace(/[:.]/g, '-')}.txt`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
    }

    /**
     * 性能监控
     */
    time(label) {
        console.time(label);
    }

    timeEnd(label) {
        console.timeEnd(label);
    }

    /**
     * 性能测量
     */
    measure(name, fn) {
        const start = performance.now();
        const result = fn();
        const end = performance.now();
        this.log('INFO', `Performance: ${name} took ${(end - start).toFixed(2)}ms`);
        return result;
    }

    /**
     * 异步性能测量
     */
    async measureAsync(name, fn) {
        const start = performance.now();
        const result = await fn();
        const end = performance.now();
        this.log('INFO', `Performance: ${name} took ${(end - start).toFixed(2)}ms`);
        return result;
    }
}

// 创建全局实例
window.logger = new Logger();

// 设置调试模式
if (window.__IOSAPP_DEBUG__ && window.logger && typeof window.logger.setDebugMode === 'function') {
    window.logger.setDebugMode(true);
}
