// Toast Notification System
class ToastManager {
    constructor() {
        this.container = null;
        this.stack = null;
        this.toasts = new Map();
        this.toastQueue = [];
        this.toastCounter = 0;
        this.removalTimers = new Map();
        this.toastDuration = 2000;
        this.removalDelay = 500;
        this.maxToasts = 5;
        this.init();
    }

    init() {
        this.container = document.createElement('div');
        this.container.className = 'toast-container';

        this.stack = document.createElement('div');
        this.stack.className = 'toast-stack';

        const stackHeader = document.createElement('div');
        stackHeader.className = 'toast-stack-header';
        stackHeader.style.display = 'none';
        stackHeader.innerHTML = `
            <span class="toast-stack-count">0 notifications</span>
            <button class="toast-stack-clear" onclick="toastManager.clearAll()">×</button>
        `;

        this.stack.appendChild(stackHeader);
        this.container.appendChild(this.stack);
        document.body.appendChild(this.container);

        if (window.innerWidth > 768) {
            this.container.style.display = 'block';
        }

        if (document.title && document.title.includes('Debug Functions')) {
            this.container.style.display = 'none';
        }
    }

    show(title, message, type = 'info', duration = 0) {
        if (localStorage.getItem('toastsDisabled') === 'true') {
            return;
        }

        if (window.innerWidth <= 768) {
            return;
        }

        if (this.toasts.size >= this.maxToasts) {
            this.toastQueue.push({ title, message, type });
            return;
        }

        this.createAndShowToast(title, message, type);
    }

    createAndShowToast(title, message, type) {
        const toastId = `toast-${++this.toastCounter}`;
        const toast = this.createToast(toastId, title, message, type);

        this.stack.insertBefore(toast, this.stack.children[1]);
        this.toasts.set(toastId, toast);

        this.updateStackCount();

        requestAnimationFrame(() => {
            toast.classList.add('show', 'new-notification');
            setTimeout(() => {
                toast.classList.remove('new-notification');
            }, 300);
        });

        this.rescheduleAllToasts();

        return toastId;
    }

    scheduleRemoval(toastId) {
        const toastArray = Array.from(this.toasts.keys());
        const toastIndex = toastArray.indexOf(toastId);

        const baseDelay = this.toastDuration;
        const additionalDelay = (toastArray.length - 1 - toastIndex) * this.removalDelay;
        const totalDelay = baseDelay + additionalDelay;

        if (this.removalTimers.has(toastId)) {
            clearTimeout(this.removalTimers.get(toastId));
        }

        const timer = setTimeout(() => {
            this.hide(toastId);
            this.removalTimers.delete(toastId);
        }, totalDelay);

        this.removalTimers.set(toastId, timer);
    }

    rescheduleAllToasts() {
        this.removalTimers.forEach(timer => clearTimeout(timer));
        this.removalTimers.clear();

        const allToasts = Array.from(this.toasts.keys());

        allToasts.forEach((toastId, index) => {
            const position = allToasts.length - 1 - index;
            const delay = this.toastDuration + (position * this.removalDelay);

            const timer = setTimeout(() => {
                this.hide(toastId);
                this.removalTimers.delete(toastId);
            }, delay);
            this.removalTimers.set(toastId, timer);
        });
    }

    rescheduleRemainingToasts() {
        this.removalTimers.forEach(timer => clearTimeout(timer));
        this.removalTimers.clear();

        const remainingToasts = Array.from(this.toasts.keys());
        remainingToasts.forEach((toastId, index) => {
            const position = remainingToasts.length - 1 - index;
            const delay = this.toastDuration + (position * this.removalDelay);
            const timer = setTimeout(() => {
                this.hide(toastId);
                this.removalTimers.delete(toastId);
            }, delay);
            this.removalTimers.set(toastId, timer);
        });
    }

    createToast(id, title, message, type) {
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.id = id;

        const time = new Date().toLocaleTimeString();

        toast.innerHTML = `
            <div class="toast-header">
                <div class="toast-title">${this.escapeHtml(title)}</div>
                <button class="toast-close" onclick="toastManager.hide('${id}')">&times;</button>
            </div>
            <div class="toast-body">
                ${this.escapeHtml(message)}
                <div class="toast-time">${time}</div>
            </div>
        `;

        return toast;
    }

    hide(toastId) {
        const toast = this.toasts.get(toastId);
        if (!toast) return;

        if (this.removalTimers.has(toastId)) {
            clearTimeout(this.removalTimers.get(toastId));
            this.removalTimers.delete(toastId);
        }

        toast.classList.add('slide-out');

        setTimeout(() => {
            if (toast.parentNode) {
                toast.parentNode.removeChild(toast);
            }
            this.toasts.delete(toastId);
            this.updateStackCount();

            if (this.toasts.size === 0) {
                this.slideOutHeader();
            } else {
                this.rescheduleRemainingToasts();
            }

            this.processQueue();
        }, 400);
    }

    slideOutHeader() {
        const stackHeader = this.stack.querySelector('.toast-stack-header');
        if (stackHeader) {
            stackHeader.classList.add('slide-out');
            setTimeout(() => {
                stackHeader.style.display = 'none';
                stackHeader.classList.remove('slide-out');
            }, 400);
        }
    }

    clearAll() {
        this.removalTimers.forEach(timer => clearTimeout(timer));
        this.removalTimers.clear();

        this.toastQueue = [];

        this.toasts.forEach((toast, id) => {
            toast.classList.add('slide-out');
        });

        setTimeout(() => {
            this.toasts.forEach((toast, id) => {
                if (toast.parentNode) {
                    toast.parentNode.removeChild(toast);
                }
            });
            this.toasts.clear();
            this.updateStackCount();
        }, 400);
    }

    updateStackCount() {
        const countElement = this.stack.querySelector('.toast-stack-count');
        const stackHeader = this.stack.querySelector('.toast-stack-header');

        if (countElement) {
            const count = this.toasts.size;
            const queueCount = this.toastQueue.length;
            const totalCount = count + queueCount;

            countElement.textContent = `${totalCount} notification${totalCount !== 1 ? 's' : ''}`;

            if (stackHeader) {
                if (totalCount === 0) {
                    stackHeader.style.display = 'none';
                } else {
                    stackHeader.style.display = 'flex';
                }
            }
        }
    }

    processQueue() {
        while (this.toasts.size < this.maxToasts && this.toastQueue.length > 0) {
            const queuedToast = this.toastQueue.shift();
            this.createAndShowToast(queuedToast.title, queuedToast.message, queuedToast.type);
        }
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Initialize toast manager when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() {
        window.toastManager = new ToastManager();
    });
} else {
    window.toastManager = new ToastManager();
}
