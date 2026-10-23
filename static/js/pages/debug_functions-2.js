/* Moved from templates/debug_functions.html. */
function switchDebugTab(tabName) {
    // Hide all tab contents
    const contents = document.querySelectorAll('.debug-tab-content');
    contents.forEach(content => content.classList.remove('active'));

    // Remove active class from all tabs
    const tabs = document.querySelectorAll('.debug-tab');
    tabs.forEach(tab => tab.classList.remove('active'));

    // Show selected tab content
    document.getElementById(tabName + '-tab').classList.add('active');

    // Add active class to clicked tab
    event.target.classList.add('active');

    // Save the active tab to localStorage
    localStorage.setItem('activeDebugTab', tabName);
}

// Function to restore the active tab on page load
function restoreActiveTab() {
    const savedTab = localStorage.getItem('activeDebugTab');
    if (savedTab) {
        // Hide all tab contents
        const contents = document.querySelectorAll('.debug-tab-content');
        contents.forEach(content => content.classList.remove('active'));

        // Remove active class from all tabs
        const tabs = document.querySelectorAll('.debug-tab');
        tabs.forEach(tab => tab.classList.remove('active'));

        // Show saved tab content
        const tabContent = document.getElementById(savedTab + '-tab');
        if (tabContent) {
            tabContent.classList.add('active');

            // Find and activate the corresponding tab button
            const tabButtons = document.querySelectorAll('.debug-tab');
            tabButtons.forEach(button => {
                if (button.textContent && button.textContent.toLowerCase() === savedTab) {
                    button.classList.add('active');
                }
            });
        }
    }
}

// Restore tab on page load
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', restoreActiveTab);
} else {
    restoreActiveTab();
}
