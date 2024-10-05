// Global loading object
const Loading = {
    element: null,
    
    init: function() {
        // Create loading element if it doesn't exist
        if (!this.element) {
            this.element = document.createElement('div');
            this.element.id = 'loading';
            this.element.className = 'loading';
            this.element.innerHTML = `
                <div class="loading-content">
                    <div class="spinner"></div>
                    <p>Processing command, please wait...</p>
                </div>
            `;
            document.body.appendChild(this.element);
        }
    },

    show: function() {
        this.init();
        this.element.style.display = 'flex';
    },

    hide: function() {
        if (this.element) {
            this.element.style.display = 'none';
        }
    }
};