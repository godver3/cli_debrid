// Task Help System
// Provides contextual help for task manager tiles

class TaskHelpSystem {
    constructor() {
        this.helpData = {};
        this.init();
    }
    
    async loadHelpData() {
        try {
            const response = await fetch('/static/js/task_help_data.json');
            if (!response.ok) {
                throw new Error(`Failed to load help data: ${response.status}`);
            }
            const data = await response.json();
            this.helpData = data.helpData || {};
        } catch (error) {
            console.error('Error loading task help data:', error);
            // Fallback to default help text
            this.helpData = {
                'default': 'This task performs automated operations as part of the content management system. Check the documentation for specific details about this task.'
            };
        }
    }
    
    // Helper method to find help text case-insensitively
    findHelpText(taskName) {
        if (!taskName) return this.helpData['default'];
        
        // First try exact match
        if (this.helpData[taskName]) {
            return this.helpData[taskName];
        }
        
        // Then try case-insensitive match
        const taskNameLower = taskName.toLowerCase();
        for (const [key, value] of Object.entries(this.helpData)) {
            if (key.toLowerCase() === taskNameLower) {
                return value;
            }
        }
        
        // Fallback to default
        return this.helpData['default'];
    }
    
    async init() {
        // Load help data first
        await this.loadHelpData();
        
        // Wait for DOM to be ready
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => this.setupHelpIcons());
        } else {
            this.setupHelpIcons();
        }
        
        // Set up observer for dynamically added content
        this.setupObserver();
    }
    
    setupObserver() {
        // Create a mutation observer to watch for new task tiles
        const observer = new MutationObserver((mutations) => {
            mutations.forEach((mutation) => {
                if (mutation.type === 'childList') {
                    mutation.addedNodes.forEach((node) => {
                        if (node.nodeType === Node.ELEMENT_NODE) {
                            if (node.classList && node.classList.contains('task-tile')) {
                                this.addHelpIcon(node);
                            } else if (node.querySelectorAll) {
                                const newTiles = node.querySelectorAll('.task-tile');
                                newTiles.forEach(tile => this.addHelpIcon(tile));
                            }
                        }
                    });
                }
            });
        });
        
        // Start observing
        observer.observe(document.body, {
            childList: true,
            subtree: true
        });
    }
    
    setupHelpIcons() {
        const taskTiles = document.querySelectorAll('.task-tile');
        taskTiles.forEach(tile => this.addHelpIcon(tile));
    }
    
    addHelpIcon(taskTile) {
        // Check if help icon already exists
        if (taskTile.querySelector('.task-help-icon')) {
            return;
        }
        
        const taskName = taskTile.dataset.taskName;
        if (!taskName) return;
        
        // Create help icon
        const helpIcon = document.createElement('div');
        helpIcon.className = 'task-help-icon';
        helpIcon.innerHTML = '<i class="fas fa-question-circle"></i>';
        helpIcon.title = 'Click for help';
        
        // Add click event
        helpIcon.addEventListener('click', (e) => {
            e.stopPropagation(); // Prevent tile click
            this.showHelp(taskName, taskTile);
        });
        
        // Add to tile
        taskTile.appendChild(helpIcon);
    }
    
    showHelp(taskName, taskTile) {
        const displayName = taskTile.querySelector('.task-name')?.textContent?.trim() || taskName;
        const helpText = this.findHelpText(displayName);
        
        // Create help popup
        const popup = document.createElement('div');
        popup.className = 'task-help-overlay';
        popup.innerHTML = `
            <div class="task-help-modal">
                <div class="task-help-modal-header">
                    <h3>${displayName}</h3>
                    <button class="task-help-modal-close">&times;</button>
                </div>
                <div class="task-help-modal-body">
                    <p>${helpText}</p>
                </div>
            </div>
        `;
        
        // Add close functionality
        const closeBtn = popup.querySelector('.task-help-modal-close');
        closeBtn.addEventListener('click', () => {
            document.body.removeChild(popup);
        });
        
        // Close on outside click
        popup.addEventListener('click', (e) => {
            if (e.target === popup) {
                document.body.removeChild(popup);
            }
        });
        
        // Close on escape key
        const handleEscape = (e) => {
            if (e.key === 'Escape') {
                document.body.removeChild(popup);
                document.removeEventListener('keydown', handleEscape);
            }
        };
        document.addEventListener('keydown', handleEscape);
        
        // Position popup near the task tile
        document.body.appendChild(popup);
        this.positionPopup(popup, taskTile);
        
        // Focus for accessibility
        closeBtn.focus();
    }
    
    positionPopup(popup, taskTile) {
        // For full-screen overlay, we don't need to calculate position
        // The CSS flexbox centering will handle it automatically
        // This method is kept for potential future use but doesn't set positioning
    }
    
    // Method to add custom help text
    addHelpText(taskName, helpText) {
        this.helpData[taskName] = helpText;
    }
    
    // Method to update help text for existing tasks
    updateHelpText(taskName, helpText) {
        this.helpData[taskName] = helpText;
    }
}

// Initialize the help system when the script loads
let taskHelpSystem;

// Initialize asynchronously
(async () => {
    taskHelpSystem = new TaskHelpSystem();
    // Make it available globally for potential customization
    window.taskHelpSystem = taskHelpSystem;
})(); 