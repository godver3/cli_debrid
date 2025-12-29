// Loading overlay styles are now defined in theme-specific CSS files:
// - Classic theme: static/css/settings.css
// - Tangerine theme: static/css/tangerine/tangerine_settings.css
// This ensures the loading overlay matches the selected theme

// Global loading object
const Loading = {
    element: null,
    messageElement: null,
    onCloseCallback: null,
    
    init: function() {
        // Create loading element if it doesn't exist
        if (!this.element) {
            this.element = document.createElement('div');
            this.element.id = 'loading';
            this.element.className = 'loading';
            this.element.innerHTML = `
                <div class="loading-content">
                    <div class="spinner"></div>
                    <div class="loading-message">
                        <p>Processing command in background...</p>
                        <pre class="loading-details" style="text-align: left; margin-top: 10px; white-space: pre-wrap; color: #ccc;"></pre>
                    </div>
                    <button class="close-loading">Continue in background</button>
                </div>
            `;
            document.body.appendChild(this.element);
            
            // Store reference to message elements
            this.messageElement = this.element.querySelector('.loading-message p');
            this.detailsElement = this.element.querySelector('.loading-details');
            
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

    show: function(message, details, hideCloseButton) {
        this.init();
        if (message && this.messageElement) {
            this.messageElement.textContent = message;
        }
        if (details && this.detailsElement) {
            this.detailsElement.textContent = details;
        } else if (this.detailsElement) {
            this.detailsElement.textContent = '';
        }

        // Show/hide close button based on parameter
        const closeButton = this.element.querySelector('.close-loading');
        if (closeButton) {
            closeButton.style.display = hideCloseButton ? 'none' : 'inline-block';
        }

        this.element.style.display = 'flex';
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
        if (this.element) {
            this.element.style.display = 'none';
            if (this.detailsElement) {
                this.detailsElement.textContent = '';
            }
        }
    }
};

window.Loading = Loading; // Make it globally accessible
