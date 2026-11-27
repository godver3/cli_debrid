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
                ${this.formatBody(message)}
                <div class="toast-time">${time}</div>
            </div>
        `;

        // Hovering keeps a toast open; leaving restarts the usual countdown
        toast.addEventListener('mouseenter', () => {
            if (this.removalTimers.has(id)) {
                clearTimeout(this.removalTimers.get(id));
                this.removalTimers.delete(id);
            }
        });
        toast.addEventListener('mouseleave', () => {
            if (this.toasts.has(id)) this.scheduleRemoval(id);
        });

        return toast;
    }

    // Message lines (state notifications send "Successfully collected X\n
    // Source: ...\nFile: ...") each get their own row: the first line is the
    // headline, "Key: value" lines are small meta rows, and a "File:" line
    // shows the release's scene tags as chips above the wrapped file name.
    formatBody(message) {
        const lines = String(message ?? '').split('\n').map(l => l.trim()).filter(Boolean);
        if (lines.length <= 1) return this.escapeHtml(lines[0] || '');
        const out = [`<div class="toast-main">${this.escapeHtml(lines[0])}</div>`];
        const meta = [];
        for (const line of lines.slice(1)) {
            const m = line.match(/^([A-Za-z][A-Za-z ]{0,24}):\s*(.*)$/);
            if (m && m[1].toLowerCase() === 'file') {
                const tags = this.sceneTags(m[2]);
                if (tags.length) {
                    out.push(`<div class="toast-tags">${tags.map(t => `<span class="toast-tag">${this.escapeHtml(t)}</span>`).join('')}</div>`);
                }
                out.push(`<div class="toast-file">${this.escapeHtml(m[2])}</div>`);
            } else if (m) {
                meta.push(`<span class="toast-meta-key">${this.escapeHtml(m[1])}:</span> ${this.escapeHtml(m[2])}`);
            } else {
                out.push(`<div class="toast-line">${this.escapeHtml(line)}</div>`);
            }
        }
        if (meta.length) out.push(`<div class="toast-meta">${meta.join('<br>')}</div>`);
        return out.join('');
    }

    // The common scene tags of a release name, in a fixed order:
    // resolution, source, HDR, video codec, audio, group.
    sceneTags(name) {
        const n = String(name || '').replace(/\.(mkv|mp4|avi|m4v|ts|m2ts|mov|wmv|webm|mpe?g|iso)$/i, '');
        const s = n.replace(/[._-]/g, ' ');
        const pick = (re, fmt) => { const m = s.match(re); return m ? (fmt ? fmt(m) : m[0]) : null; };
        const tags = [
            pick(/\b(2160p|1080p|1080i|720p|576p|480p|4K)\b/i, m => m[1].toUpperCase() === '4K' ? '4K' : m[1].toLowerCase()),
            pick(/\b(REMUX|BluRay|Blu Ray|BDRip|BRRip|WEB DL|WEBDL|WEBRip|WEB|HDTV|DVDRip|DVD)\b/i, m => {
                const v = m[1].toUpperCase().replace(' ', '-');
                return ({'BLURAY': 'BluRay', 'BLU-RAY': 'BluRay', 'WEB-DL': 'WEB-DL', 'WEBDL': 'WEB-DL', 'WEBRIP': 'WEBRip',
                         'BDRIP': 'BDRip', 'BRRIP': 'BRRip', 'DVDRIP': 'DVDRip', 'REMUX': 'REMUX'})[v] || v;
            }),
            pick(/\b(DV|DoVi|Dolby Vision|HDR10\+|HDR10Plus|HDR10|HDR)\b/i, m => /^(dv|dovi|dolby vision)$/i.test(m[1]) ? 'DV' : m[1].toUpperCase().replace('PLUS', '+')),
            pick(/\b(x264|x265|H 264|H 265|H264|H265|HEVC|AVC|AV1|XviD)\b/i, m => m[1].replace(' ', '.').replace(/^h/, 'H').replace(/^X/, 'x')),
            (() => {
                const m = s.match(/\b(TrueHD|DTS HD MA|DTS HD|DTS X|DTS|DDP|DD\+|EAC3|E AC3|AC3|DD|FLAC|AAC|Opus|MP3)(?: ?(\d) ?(\d))?(?![A-Za-z])/i);
                const atmos = /\bAtmos\b/i.test(s);
                if (!m) return atmos ? 'Atmos' : null;
                const codec = m[1].toUpperCase() === 'E AC3' ? 'EAC3' : m[1].replace(/ /g, '-');
                return [codec, m[2] ? `${m[2]}.${m[3] || 0}` : '', atmos ? 'Atmos' : ''].filter(Boolean).join(' ');
            })(),
            (n.match(/-([A-Za-z0-9]+)$/) || [])[1] || null,
        ];
        return tags.filter(Boolean);
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
