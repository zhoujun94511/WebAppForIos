/**
 * 统一通知管理器
 * 整合 SweetAlert2 和 Toast 通知系统
 */
class NotificationManager {
    constructor() {
        this.toastContainer = null;
        this.initToastContainer();
        this.setupGlobalMethods();
    }

    /**
     * 初始化 Toast 容器
     */
    initToastContainer() {
        if (!this.toastContainer) {
            this.toastContainer = document.createElement('div');
            this.toastContainer.id = 'notification-toast-container';
            this.toastContainer.style.cssText = `
                position: fixed;
                top: 20px;
                right: 20px;
                z-index: 10000;
                pointer-events: none;
            `;
            document.body.appendChild(this.toastContainer);
        }
    }

    /**
     * 设置全局方法
     */
    setupGlobalMethods() {
        // 全局通知方法
        window.notify = {
            success: (message, options = {}) => this.show('success', message, options),
            error: (message, options = {}) => this.show('error', message, options),
            warning: (message, options = {}) => this.show('warning', message, options),
            info: (message, options = {}) => this.show('info', message, options)
        };

        // 兼容旧版本方法
        window.notifySuccess = (title, text, timer) => this.alert('success', title, text, timer);
        window.notifyError = (title, text, timer) => this.alert('error', title, text, timer);
        window.notifyWarning = (title, text, timer) => this.alert('warning', title, text, timer);
        window.notifyInfo = (title, text, timer) => this.alert('info', title, text, timer);
    }

    /**
     * 显示通知
     * @param {string} type - 通知类型: success, error, warning, info
     * @param {string} message - 通知消息
     * @param {object} options - 配置选项
     */
    show(type, message, options = {}) {
        const {
            title = '',
            duration = this.getDefaultDuration(type),
            position = 'top-right',
            showCloseButton = true,
            useToast = true
        } = options;

        if (useToast) {
            this.showToast(type, message, duration, title, showCloseButton);
        } else {
            this.showAlert(type, title || message, '', duration);
        }
    }

    /**
     * 显示 Toast 通知
     */
    showToast(type, message, duration, title = '', showCloseButton = true) {
        const toast = document.createElement('div');
        toast.className = `notification-toast alert alert-${this.getBootstrapClass(type)} alert-dismissible fade show`;
        toast.style.cssText = `
            margin-bottom: 10px;
            min-width: 300px;
            max-width: 500px;
            border-radius: 12px;
            box-shadow: 0 8px 25px rgba(0,0,0,0.15);
            backdrop-filter: blur(10px);
            animation: toastSlideIn 0.4s ease-out;
            pointer-events: auto;
        `;

        const icon = this.getIcon(type);
        const titleHtml = title ? `<strong>${title}</strong><br>` : '';
        
        toast.innerHTML = `
            <div class="d-flex align-items-start">
                <i class="fas fa-${icon} me-2 mt-1" style="color: ${this.getIconColor(type)};"></i>
                <div class="flex-grow-1">
                    ${titleHtml}${message}
                </div>
                ${showCloseButton ? '<button type="button" class="btn-close ms-auto" data-bs-dismiss="alert"></button>' : ''}
            </div>
        `;

        this.toastContainer.appendChild(toast);

        // 自动移除
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

    /**
     * 显示 SweetAlert2 弹窗
     */
    showAlert(type, title, text, timer) {
        if (typeof Swal !== 'undefined') {
            Swal.fire({
                icon: type,
                title: title,
                text: text,
                timer: timer,
                showConfirmButton: false,
                toast: true,
                position: 'top-end'
            });
        } else {
            // 降级到原生 alert
            alert(`${title}: ${text}`);
        }
    }

    /**
     * 兼容旧版本 alertAndHide 方法
     */
    alert(type, title, text, timer) {
        return this.showAlert(type, title, text, timer);
    }

    /**
     * 获取默认持续时间
     */
    getDefaultDuration(type) {
        const durations = {
            success: 2000,
            error: 4000,
            warning: 3000,
            info: 2500
        };
        return durations[type] || 3000;
    }

    /**
     * 获取 Bootstrap 样式类
     */
    getBootstrapClass(type) {
        const classes = {
            success: 'success',
            error: 'danger',
            warning: 'warning',
            info: 'info'
        };
        return classes[type] || 'info';
    }

    /**
     * 获取图标
     */
    getIcon(type) {
        const icons = {
            success: 'check-circle',
            error: 'exclamation-circle',
            warning: 'exclamation-triangle',
            info: 'info-circle'
        };
        return icons[type] || 'info-circle';
    }

    /**
     * 获取图标颜色
     */
    getIconColor(type) {
        const colors = {
            success: '#28a745',
            error: '#dc3545',
            warning: '#ffc107',
            info: '#17a2b8'
        };
        return colors[type] || '#17a2b8';
    }

    /**
     * 显示加载状态
     */
    showLoading(message = '加载中...') {
        if (typeof Swal !== 'undefined') {
            Swal.fire({
                title: message,
                allowOutsideClick: false,
                allowEscapeKey: false,
                showConfirmButton: false,
                didOpen: () => {
                    Swal.showLoading();
                }
            });
        }
    }

    /**
     * 隐藏加载状态
     */
    hideLoading() {
        if (typeof Swal !== 'undefined') {
            Swal.close();
        }
    }

    /**
     * 显示确认对话框
     */
    confirm(title, text, options = {}) {
        if (typeof Swal !== 'undefined') {
            return Swal.fire({
                title: title,
                text: text,
                icon: 'warning',
                showCancelButton: true,
                confirmButtonText: options.confirmText || '确认',
                cancelButtonText: options.cancelText || '取消',
                confirmButtonColor: '#3085d6',
                cancelButtonColor: '#d33'
            });
        } else {
            return Promise.resolve({ isConfirmed: confirm(`${title}: ${text}`) });
        }
    }
}

// 创建全局实例
window.notificationManager = new NotificationManager();

// 添加 CSS 动画
const style = document.createElement('style');
style.textContent = `
    @keyframes toastSlideIn {
        from {
            opacity: 0;
            transform: translateX(100px);
        }
        to {
            opacity: 1;
            transform: translateX(0);
        }
    }
    
    .notification-toast {
        animation: toastSlideIn 0.4s ease-out;
    }
`;
document.head.appendChild(style);


