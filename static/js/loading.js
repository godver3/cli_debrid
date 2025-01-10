// Add CSS styles for loading
const loadingStyles = document.createElement('style');
loadingStyles.textContent = `
    .loading {
        display: none;
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        background-color: rgba(0, 0, 0, 0.5);
        z-index: 9999;
        justify-content: center;
        align-items: center;
    }

    .loading-content {
        background-color: #333;
        padding: 20px;
        border-radius: 5px;
        text-align: center;
    }

    .loading-content p {
        color: #f4f4f4;
        margin-bottom: 15px;
    }

    .spinner {
        border: 4px solid #f3f3f3;
        border-top: 4px solid #3498db;
        border-radius: 50%;
        width: 40px;
        height: 40px;
        animation: spin 1s linear infinite;
        margin: 0 auto 10px;
    }

    @keyframes spin {
        0% { transform: rotate(0deg); }
        100% { transform: rotate(360deg); }
    }

    .close-loading {
        background-color: #007bff;
        color: white;
        border: none;
        padding: 12px 20px;
        text-align: center;
        text-decoration: none;
        display: inline-block;
        font-size: 16px;
        margin: 4px 2px;
        cursor: pointer;
        border-radius: 4px;
        transition: background-color 0.3s;
    }

    .close-loading:hover {
        background-color: #0056b3;
    }
`;
document.head.appendChild(loadingStyles);

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
                    <p>Processing command in background...</p>
                    <button class="close-loading">Continue in background</button>
                </div>
            `;
            document.body.appendChild(this.element);
            
            // Add click handler for close button
            this.element.querySelector('.close-loading').addEventListener('click', () => {
                this.hide();
            });
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