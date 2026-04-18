// Loading overlay styles are now defined in theme-specific CSS files:
// - Classic theme: static/css/settings.css
// - Tangerine theme: static/css/tangerine/tangerine_settings.css
// This ensures the loading overlay matches the selected theme

// Global loading object
const Loading = {
    element: null,
    messageElement: null,
    onCloseCallback: null,
    progressTimer: null,
    startTime: null,
    lastUpdateTime: null,
    progressElement: null,
    progressBarElement: null,
    currentProgress: 0,
    progressCallback: null,
    showTime: null,  // Track when loading was shown
    minDisplayTime: 200,  // Minimum time to show loading (ms)

    init: function() {
        // Create loading element if it doesn't exist
        if (!this.element) {
            this.element = document.createElement('div');
            this.element.id = 'loading';
            this.element.className = 'loading';
            this.element.innerHTML = `
                <div class="loading-content">
                    <div class="loading-progress-bar-container">
                        <div class="loading-progress-bar"></div>
                    </div>
                    <div class="loading-inner">
                        <div class="loading-title-row">
                            <i class="fas fa-circle-notch fa-spin loading-spin-icon"></i>
                            <div class="loading-message">Processing command in background...</div>
                        </div>
                        <div class="loading-details"></div>
                        <div class="loading-progress" style="display:none;">0%</div>
                        <div class="loading-button-row">
                            <button class="close-loading">Continue in background</button>
                        </div>
                    </div>
                </div>
            `;
            document.body.appendChild(this.element);

            // Store reference to message elements
            this.messageElement = this.element.querySelector('.loading-message');
            this.detailsElement = this.element.querySelector('.loading-details');
            this.progressElement = this.element.querySelector('.loading-progress');
            this.progressBarElement = this.element.querySelector('.loading-progress-bar');

            // Add click handler for close button
            this.element.querySelector('.close-loading').addEventListener('click', () => {
                if (this.onCloseCallback) {
                    this.onCloseCallback();
                } else {
                    this.hide();
                }
            });
        }
    },

    setOnClose: function(callback) {
        this.onCloseCallback = callback;
    },

    startProgressTimer: function() {
        console.log('[Loading] startProgressTimer called - using requestAnimationFrame');

        // Clear any existing timer
        if (this.progressTimer) {
            console.log('[Loading] Clearing existing timer');
            cancelAnimationFrame(this.progressTimer);
        }

        // Reset progress
        this.currentProgress = 0;
        this.startTime = Date.now();
        this.lastUpdateTime = this.startTime;

        // Use requestAnimationFrame instead of setInterval for more reliable execution
        const updateProgress = () => {
            const now = Date.now();
            const elapsed = (now - this.startTime) / 1000;
            const timeSinceLastUpdate = now - this.lastUpdateTime;

            // Only update every ~100ms to avoid excessive updates
            if (timeSinceLastUpdate >= 100) {
                this.lastUpdateTime = now;

                if (this.progressElement) {
                    // If a progress callback is provided, use it
                    if (this.progressCallback && typeof this.progressCallback === 'function') {
                        const progress = this.progressCallback();
                        if (progress !== null && progress !== undefined) {
                            this.currentProgress = Math.min(100, Math.max(0, progress));
                        }
                    } else {
                        // Faster progress curve optimized for quick operations (600-1000ms)
                        // Use a logarithmic curve that progresses faster initially
                        if (elapsed < 0.1) {
                            this.currentProgress = 20; // Start at 20%
                        } else if (elapsed < 0.5) {
                            // 20% -> 60% in first 500ms
                            this.currentProgress = 20 + (elapsed - 0.1) * 100;
                        } else if (elapsed < 1.5) {
                            // 60% -> 85% in next second
                            this.currentProgress = 60 + (elapsed - 0.5) * 25;
                        } else if (elapsed < 3) {
                            // 85% -> 92% slowly for longer operations
                            this.currentProgress = 85 + (elapsed - 1.5) * 4.7;
                        } else {
                            // Asymptotically approach 95% for very long operations
                            this.currentProgress = 95 - (95 - 92) * Math.exp(-(elapsed - 3) / 5);
                        }
                        this.currentProgress = Math.min(95, this.currentProgress);
                    }

                    // Update display
                    this.progressElement.textContent = `${Math.round(this.currentProgress)}%`;

                    // Progress bar uses CSS pulse animation — no manual width update needed

                    // Color coding based on progress
                    if (this.currentProgress < 50) {
                        this.progressElement.style.color = '#888'; // Gray
                    } else if (this.currentProgress < 75) {
                        this.progressElement.style.color = '#4caf50'; // Green
                    } else if (this.currentProgress < 90) {
                        this.progressElement.style.color = '#ff9800'; // Orange
                    } else {
                        this.progressElement.style.color = '#f44336'; // Red (taking long)
                    }
                }
            }

            // Continue the animation loop
            this.progressTimer = requestAnimationFrame(updateProgress);
        };

        // Start the animation loop
        this.progressTimer = requestAnimationFrame(updateProgress);
        console.log('[Loading] requestAnimationFrame started');
    },

    stopProgressTimer: function() {
        console.log('[Loading] stopProgressTimer called');
        if (this.progressTimer) {
            console.log('[Loading] Clearing animation frame');
            cancelAnimationFrame(this.progressTimer);
            this.progressTimer = null;
        }
        this.startTime = null;
        this.lastUpdateTime = null;
        this.currentProgress = 0;
        this.progressCallback = null;
    },

    setProgress: function(progress) {
        this.currentProgress = Math.min(100, Math.max(0, progress));
        if (this.progressElement) {
            this.progressElement.textContent = `${Math.round(this.currentProgress)}%`;
        }
        if (this.progressBarElement) {
            this.progressBarElement.style.width = `${this.currentProgress}%`;
        }
    },

    setProgressCallback: function(callback) {
        this.progressCallback = callback;
    },

    show: function(message, details, hideCloseButton, hideProgress) {
        this.init();
        if (this.messageElement) {
            this.messageElement.textContent = message || 'Processing command in background...';
        }
        if (details && this.detailsElement) {
            this.detailsElement.textContent = details;
        } else if (this.detailsElement) {
            this.detailsElement.textContent = '';
        }

        // Reset progress display
        if (this.progressElement) {
            this.progressElement.textContent = '0%';
            this.progressElement.style.color = '#888';
            // Always hide percentage counter
            this.progressElement.style.display = 'none';
        }
        if (this.progressBarElement) {
            this.progressBarElement.style.width = '0%';
        }
        this.currentProgress = 0;

        // Show/hide close button based on parameter
        const closeButton = this.element.querySelector('.close-loading');
        if (closeButton) {
            closeButton.style.display = hideCloseButton ? 'none' : 'inline-block';
        }

        this.element.style.display = 'flex';

        // Track when loading was shown
        this.showTime = Date.now();
        console.log('[Loading] Shown at:', this.showTime);

        // Only start progress timer if we're showing progress
        if (!hideProgress) {
            this.startProgressTimer();
        }
    },

    updateMessage: function(message, details) {
        if (this.messageElement && message) {
            this.messageElement.textContent = message;
        }
        if (this.detailsElement) {
            if (details) {
                this.detailsElement.textContent = details;
            } else {
                this.detailsElement.textContent = '';
            }
        }
    },

    hide: function() {
        console.log('[Loading] hide() called');

        // Set progress to 100% before hiding
        if (this.progressElement) {
            this.currentProgress = 100;
            this.progressElement.textContent = '100%';
            this.progressElement.style.color = '#4caf50'; // Green for complete
        }
        if (this.progressBarElement) {
            this.progressBarElement.style.width = '100%';
        }

        // Calculate how long loading has been shown
        const now = Date.now();
        const elapsed = this.showTime ? (now - this.showTime) : 0;
        const remaining = Math.max(0, this.minDisplayTime - elapsed);

        console.log('[Loading] Shown for:', elapsed, 'ms, minimum:', this.minDisplayTime, 'ms, delay:', remaining, 'ms');

        // If we haven't shown it for the minimum time, delay hiding
        if (remaining > 0) {
            console.log('[Loading] Delaying hide by', remaining, 'ms to ensure minimum display time');
            setTimeout(() => {
                this._actuallyHide();
            }, remaining);
        } else {
            this._actuallyHide();
        }
    },

    _actuallyHide: function() {
        console.log('[Loading] Actually hiding now');

        // Stop progress timer
        this.stopProgressTimer();

        if (this.element) {
            this.element.style.display = 'none';
            if (this.detailsElement) {
                this.detailsElement.textContent = '';
            }
            if (this.progressElement) {
                this.progressElement.textContent = '0%';
                this.progressElement.style.color = '#888';
            }
            if (this.progressBarElement) {
                this.progressBarElement.style.width = '0%';
            }
        }

        // Reset show time
        this.showTime = null;
    }
};

window.Loading = Loading; // Make it globally accessible
