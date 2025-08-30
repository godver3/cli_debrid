import { initializeTooltips, setUpdatingContent } from './tooltips.js';
import { initializeProgramControls } from './program_controls.js';
import { initializeTaskMonitor } from './task_monitor.js';
import { showPopup, POPUP_TYPES } from './notifications.js';

// Make popup functionality available globally
window.showPopup = showPopup;
window.POPUP_TYPES = POPUP_TYPES;

// Set initial notification disabled state from localStorage
try {
    const notificationsDisabled = localStorage.getItem('notificationsDisabled') === 'true';
    document.body.setAttribute('data-notifications-disabled', notificationsDisabled);
} catch (e) {
    console.error('Error setting initial notification state:', e);
}

// Rate limiting check
document.addEventListener('DOMContentLoaded', function() {
    if (window.isRateLimited && window.location.pathname !== '/over_usage/') {
        window.location.href = '/over_usage/';
    }
});

// Initialize navigation and UI components
function initializeNavigation() {
    const hamburger = document.querySelector('.hamburger-menu');
    const navMenu = document.querySelector('#navMenu');

    if (hamburger && navMenu) {
        // Hamburger menu toggle
        hamburger.addEventListener('click', function() {
            hamburger.classList.toggle('active');
            navMenu.classList.toggle('show');
            
            // Close all dropdowns when closing the menu
            if (!navMenu.classList.contains('show')) {
                const allDropdowns = navMenu.querySelectorAll('.dropdown');
                const allGroupTitles = navMenu.querySelectorAll('.group-title');
                allDropdowns.forEach(dropdown => dropdown.classList.remove('show'));
                allGroupTitles.forEach(title => title.classList.remove('active'));
            }
        });

        // Group title clicks
        const groupTitles = navMenu.querySelectorAll('.group-title');
        groupTitles.forEach(title => {
            const handleClick = function(e) {
                if (window.innerWidth <= 1045) {
                    e.preventDefault();
                    e.stopPropagation();
                    
                    const dropdown = this.nextElementSibling;
                    const wasActive = this.classList.contains('active');
                    
                    // Close all dropdowns and remove active states
                    const allDropdowns = navMenu.querySelectorAll('.dropdown');
                    const allGroupTitles = navMenu.querySelectorAll('.group-title');
                    allDropdowns.forEach(d => d.classList.remove('show'));
                    allGroupTitles.forEach(t => t.classList.remove('active'));
                    
                    // Toggle current if it wasn't active
                    if (!wasActive) {
                        this.classList.add('active');
                        dropdown.classList.add('show');
                    }
                }
            };

            // Handle both click and touch events
            title.addEventListener('click', handleClick);
            title.addEventListener('touchstart', function(e) {
                e.preventDefault();
                handleClick.call(this, e);
            }, { passive: false });
        });

        // Close menu when clicking outside
        document.addEventListener('click', function(e) {
            if (window.innerWidth <= 1045) {
                if (!navMenu.contains(e.target) && !hamburger.contains(e.target)) {
                    navMenu.classList.remove('show');
                    hamburger.classList.remove('active');
                    const allDropdowns = navMenu.querySelectorAll('.dropdown');
                    const allGroupTitles = navMenu.querySelectorAll('.group-title');
                    allDropdowns.forEach(dropdown => dropdown.classList.remove('show'));
                    allGroupTitles.forEach(title => title.classList.remove('active'));
                }
            }
        });
    }
}

// Release notes functionality
function initializeReleaseNotes() {
    const releaseNotesButton = document.getElementById('releaseNotesButton');
    const releaseNotesPopup = document.getElementById('releaseNotesPopup');
    const releaseNotesOverlay = document.getElementById('releaseNotesOverlay');
    const releaseNotesClose = document.getElementById('releaseNotesClose');
    const releaseNotesContent = document.getElementById('releaseNotesContent');

    function showReleaseNotes() {
        releaseNotesPopup.style.display = 'block';
        releaseNotesOverlay.style.display = 'block';
        
        fetch('/base/api/release-notes')
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    releaseNotesContent.innerHTML = marked.parse(data.body);
                } else {
                    releaseNotesContent.innerHTML = '<div class="error">Failed to load release notes</div>';
                }
            })
            .catch(error => {
                console.error('Error fetching release notes:', error);
                releaseNotesContent.innerHTML = '<div class="error">Error loading release notes</div>';
            });
    }

    function hideReleaseNotes() {
        releaseNotesPopup.style.display = 'none';
        releaseNotesOverlay.style.display = 'none';
    }

    if (releaseNotesButton) {
        releaseNotesButton.addEventListener('click', showReleaseNotes);
    }

    if (releaseNotesClose) {
        releaseNotesClose.addEventListener('click', hideReleaseNotes);
    }

    if (releaseNotesOverlay) {
        releaseNotesOverlay.addEventListener('click', hideReleaseNotes);
    }

    // Close on escape key
    document.addEventListener('keydown', function(event) {
        if (event.key === 'Escape' && releaseNotesPopup.style.display === 'block') {
            hideReleaseNotes();
        }
    });
}

// Rate Limits Functions
function toggleRateLimits() {
    const container = document.getElementById('rate-limits-container');
    container.classList.toggle('show');
    
    // If showing the container, fetch the latest info
    if (container.classList.contains('show')) {
        fetchRateLimitInfo();
    }
}

function updateBodyPadding() {
    // Skip on mobile
    if (window.innerWidth <= 776) {
        return;
    }

    const taskMonitorContainer = document.querySelector('.task-monitor-container');
    const rateLimitsSection = document.querySelector('.rate-limits-section');
    
    const taskMonitorVisible = taskMonitorContainer && taskMonitorContainer.classList.contains('visible');
    const rateLimitsVisible = rateLimitsSection && rateLimitsSection.classList.contains('show');
    
    if (taskMonitorVisible || rateLimitsVisible) {
        document.body.classList.add('has-visible-section');
    } else {
        document.body.classList.remove('has-visible-section');
    }
}

function toggleRateLimitsSection() {
    const section = document.querySelector('.rate-limits-section');
    const button = document.getElementById('rateLimitsSectionToggle');
    const container = document.getElementById('rate-limits-container');
    
    if (section && button) {
        const isHidden = !section.classList.contains('show');
        
        if (isHidden) {
            section.classList.add('show');
            button.classList.add('active');
        } else {
            section.classList.remove('show');
            container.classList.remove('show'); // Also hide container when hiding section
            button.classList.remove('active');
        }
        
        // Store the state in localStorage
        localStorage.setItem('rateLimitsSectionVisible', isHidden ? 'true' : 'false');
        
        // Update body padding
        updateBodyPadding();
    }
}

function initializeRateLimitsSection() {
    const button = document.getElementById('rateLimitsSectionToggle');
    if (button) {
        // Set initial state based on localStorage or default to visible
        const shouldBeVisible = localStorage.getItem('rateLimitsSectionVisible') !== 'false';
        const section = document.querySelector('.rate-limits-section');
        const container = document.getElementById('rate-limits-container');
        
        if (section && container) {
            if (shouldBeVisible) {
                section.classList.add('show');
                button.classList.add('active');
                // Don't show container by default, only section
            } else {
                section.classList.remove('show');
                container.classList.remove('show');
                button.classList.remove('active');
            }
        }
        
        button.addEventListener('click', toggleRateLimitsSection);
        
        // Initial padding update
        updateBodyPadding();
    }
}

function fetchRateLimitInfo() {
    fetch('/debug/api/rate_limit_info')
        .then(response => response.json())
        .then(data => {
            const rateLimitInfo = document.getElementById('rate-limit-info');
            let html = '';
            for (const [domain, limits] of Object.entries(data)) {
                const fiveMinClass = limits.five_minute.count > limits.five_minute.limit ? 'rate-limit-warning' : 'rate-limit-normal';
                const hourlyClass = limits.hourly.count > limits.hourly.limit ? 'rate-limit-warning' : 'rate-limit-normal';
                
                html += `
                    <div class="domain-rate-limit">
                        <h5>${domain}</h5>
                        <p class="${fiveMinClass}">5-minute: ${limits.five_minute.count} / ${limits.five_minute.limit} requests</p>
                        <p class="${hourlyClass}">Hourly: ${limits.hourly.count} / ${limits.hourly.limit} requests</p>
                    </div>
                `;
            }
            rateLimitInfo.innerHTML = html;
        })
        .catch(error => {
            console.error('Error fetching rate limit info:', error);
            document.getElementById('rate-limit-info').innerHTML = '<p class="error">Error loading rate limit information. Please try again.</p>';
        });
}

// Make functions globally available
window.toggleRateLimits = toggleRateLimits;
window.fetchRateLimitInfo = fetchRateLimitInfo;
window.updateBodyPadding = updateBodyPadding;

// Add before the DOMContentLoaded event listener
async function checkAndShowPhalanxDisclaimer() {
    try {
        const response = await fetch('/settings/api/phalanx-disclaimer-status');
        const data = await response.json();
        
        if (!data.hasSeenDisclaimer) {
            showPopup({
                type: POPUP_TYPES.CONFIRM,
                title: 'Welcome to Phalanx DB',
                message: `I am excited to introduce Phalanx_DB as the newest feature within cli_debrid. This disseminated database allows all users to keep track of cache status for items instead of needing to check cache statuses independently. This is a peer to peer database that is not stored in any one location, and instead propagates across users. Would you like to enable Phalanx_DB (setting applies on restart)?<br><br><i>Note that data shared within phalanx_db is anonymous.</i>`,
                confirmText: 'Yes, Enable',
                cancelText: 'No, Disable',
                onConfirm: async () => {
                    await fetch('/settings/api/phalanx-disclaimer-accept', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({ accepted: true })
                    });
                },
                onCancel: async () => {
                    await fetch('/settings/api/phalanx-disclaimer-accept', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({ accepted: false })
                    });
                    
                    showPopup({
                        type: POPUP_TYPES.INFO,
                        title: 'Phalanx DB Disabled',
                        message: 'Phalanx_DB has been disabled. You can re-enable it from the Settings Menu/Additional Settings/UI Settings',
                        autoClose: 5000
                    });
                }
            });
        }
    } catch (error) {
        console.error('Error checking Phalanx disclaimer status:', error);
    }
}

// Add this function
function initializeHelpModal() {
    const helpButton = document.getElementById('helpButton');
    const helpOverlay = document.getElementById('help-overlay');
    const helpModalBox = document.getElementById('help-modal-box');
    const helpModalClose = document.getElementById('help-modal-close');
    const helpModalBody = document.getElementById('help-modal-body');

    if (!helpButton || !helpOverlay || !helpModalBox || !helpModalClose || !helpModalBody) {
        // If helpButton is not here, no need to proceed with its specific logic
        if (!helpButton && helpOverlay && helpModalBox && helpModalClose) {
             // Still set up close listeners for overlay and ESC if modal box parts exist
            helpModalClose.addEventListener('click', hideHelpModal); // Assuming hideHelpModal is defined
            helpOverlay.addEventListener('click', function(event) {
                if (event.target === helpOverlay) {
                    hideHelpModal(); // Assuming hideHelpModal is defined
                }
            });
            document.addEventListener('keydown', function(event) {
                if (event.key === 'Escape' && helpModalBox.classList.contains('visible')) {
                    hideHelpModal(); // Assuming hideHelpModal is defined
                }
            });
        }
        return;
    }

    // Animation logic for the help button
    if (!localStorage.getItem('helpButtonInteracted')) {
        // 1 in 20 chance to animate
        if (Math.random() < 1) {
            helpButton.classList.add('help-button-animate-appear');
        }
    }

    function showHelpModal() {
        // Add class to body to prevent scrolling
        document.body.classList.add('modal-open');

        // Add the 'visible' class to show modal elements
        helpOverlay.classList.add('visible');
        helpModalBox.classList.add('visible');
        fetchHelpContent(window.location.pathname);
    }

    function hideHelpModal() {
        // Remove class from body to restore scrolling
        document.body.classList.remove('modal-open');

        // Remove the 'visible' class to hide modal elements
        helpOverlay.classList.remove('visible');
        helpModalBox.classList.remove('visible');
    }

    // Fetch content function remains the same
    async function fetchHelpContent(pagePath) {
        helpModalBody.innerHTML = `<p>Loading help...</p>`;
        try {
            const response = await fetch(`/base/api/help-content?page_path=${encodeURIComponent(pagePath)}`);
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const data = await response.json();

            if (data.success && data.html) {
                helpModalBody.innerHTML = data.html;
            } else {
                throw new Error(data.error || 'Failed to load help content from server.');
            }

        } catch (error) {
            console.error("Error fetching help content:", error);
            helpModalBody.innerHTML = '<p class="error">Could not load help content. Please try again later.</p>';
        }
    }

    helpButton.addEventListener('click', function() {
        // User has interacted, store this information
        localStorage.setItem('helpButtonInteracted', 'true');
        // Remove animation class if it was applied
        helpButton.classList.remove('help-button-animate-appear');
        showHelpModal();
    });
    helpModalClose.addEventListener('click', hideHelpModal);

    helpOverlay.addEventListener('click', function(event) {
        if (event.target === helpOverlay) {
            hideHelpModal();
        }
    });

    document.addEventListener('keydown', function(event) {
        if (event.key === 'Escape' && helpModalBox.classList.contains('visible')) {
            hideHelpModal();
        }
    });
}

// --- START: Top Overlay Body Padding Logic ---
function updateBodyPaddingForTopOverlays() {
    const taskMonitor = document.getElementById('taskMonitorContainer');
    const rateLimits = document.querySelector('.rate-limits-section'); // Use querySelector for class
    
    // Check if either element exists and is visible (has the 'visible' class)
    const isTaskMonitorVisible = taskMonitor && taskMonitor.classList.contains('visible');
    const isRateLimitsVisible = rateLimits && rateLimits.classList.contains('show');

    if (isTaskMonitorVisible || isRateLimitsVisible) {
        document.body.classList.add('has-top-overlay');
    } else {
        document.body.classList.remove('has-top-overlay');
    }
    // Add a small delay for smoother transition start if needed, but CSS transition should handle it
    // setTimeout(() => { /* Add/remove class */ }, 10); 
}

// Expose the function globally if task_monitor.js is not a module importing this
window.updateBodyPaddingForTopOverlays = updateBodyPaddingForTopOverlays; 
// --- END: Top Overlay Body Padding Logic ---

// Initialize everything when DOM is loaded
document.addEventListener('DOMContentLoaded', function() {
    // Add this line near the beginning of the DOMContentLoaded handler
    // checkAndShowPhalanxDisclaimer(); // Disabled per user request
    
    // Auto-mark notifications as read if they're disabled
    if (localStorage.getItem('notificationsDisabled') === 'true') {
        const markAllNotificationsAsReadSilently = async () => {
            try {
                await fetch('/base/api/notifications/mark-all-read', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    }
                });
                console.log('Auto-marked all notifications as read (notifications disabled)');
            } catch (error) {
                console.error('Error auto-marking notifications as read:', error);
            }
        };
        markAllNotificationsAsReadSilently();
    }
    
    // Check initial visibility state before any other initialization
    const taskMonitorVisible = localStorage.getItem('taskMonitorVisible') === 'true';
    const rateLimitsVisible = localStorage.getItem('rateLimitsSectionVisible') !== 'false';
    
    // Set initial padding and visibility state
    if (window.innerWidth > 776 && (taskMonitorVisible || rateLimitsVisible)) {
        document.body.classList.add('has-visible-section');
    }

    // Show the body after initial state is set
    requestAnimationFrame(() => {
        document.body.classList.add('initialized');
        // Enable transitions after a small delay to ensure initial state is rendered
        setTimeout(() => {
            document.body.classList.add('transitions-enabled');
        }, 100);
    });

    // Set updating content state to false before initializing tooltips
    setUpdatingContent(false);
    
    // Initialize all components
    initializeNavigation();
    initializeReleaseNotes();
    initializeTaskMonitor();
    // Only initialize tooltips on non-mobile screens
    if (window.innerWidth > 1045) {
        initializeTooltips();
    }
    initializeRateLimitsSection();
    
    // Initialize program controls if the button exists
    if (document.getElementById('programControlButton')) {
        initializeProgramControls();
    }

    // Initialize logout button handler
    const logoutButton = document.getElementById('logout_button');
    if (logoutButton) {
        logoutButton.addEventListener('click', function(e) {
            e.preventDefault();
            // Create a form to submit POST request
            const form = document.createElement('form');
            form.method = 'POST';
            form.action = '/auth/logout';
            document.body.appendChild(form);
            form.submit();
        });
    }

    // Add click outside handler for rate limits
    document.addEventListener('click', function(e) {
        const container = document.getElementById('rate-limits-container');
        const toggle = document.querySelector('.rate-limits-toggle');
        
        if (container && container.classList.contains('show')) {
            if (!toggle.contains(e.target) && !container.contains(e.target)) {
                container.classList.remove('show');
            }
        }
    });

    // Update checker
    async function checkForUpdates() {
        try {
            const response = await fetch('/base/api/check-update', {
                cache: 'no-store',  // Force bypass browser cache
                headers: {
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache'
                }
            });
            const data = await response.json();
            console.log('Update check response:', data);
            
            const updateButton = document.getElementById('updateAvailableButton');
            if (!updateButton) {
                console.log('Update button not found');
                return;
            }
            
            // Force hide by default
            updateButton.style.display = 'none';
            updateButton.classList.add('hidden');
            
            if (data.success && data.update_available === true) {
                console.log('Update is available, showing button');
                updateButton.style.display = '';
                updateButton.classList.remove('hidden');
                updateButton.setAttribute('data-tooltip', `New version available: ${data.latest_version} (${data.branch} branch)`);
            } else {
                console.log('No update available or check failed, keeping button hidden');
            }
        } catch (error) {
            console.error('Error checking for updates:', error);
        }
    }

    // Check for updates periodically
    setInterval(checkForUpdates, 5 * 60 * 1000); // Check every 5 minutes
    checkForUpdates(); // Initial check

    // Add click handler for update button
    document.getElementById('updateAvailableButton')?.addEventListener('click', () => {
        // Open GitHub repository in new tab
        window.open('https://github.com/godver3/cli_debrid', '_blank');
    });

    // Add observer for task monitor visibility changes
    const taskMonitorContainer = document.querySelector('.task-monitor-container');
    if (taskMonitorContainer) {
        const observer = new MutationObserver((mutations) => {
            mutations.forEach((mutation) => {
                if (mutation.type === 'attributes' && mutation.attributeName === 'class') {
                    updateBodyPadding();
                }
            });
        });
        
        observer.observe(taskMonitorContainer, {
            attributes: true
        });
    }
    
    // Initial padding update
    updateBodyPadding();

    // Notifications Modal Logic
    const notificationsModal = document.getElementById('notificationsModal');
    const notificationsBtn = document.getElementById('notifications_button');
    const notificationsCloseBtn = notificationsModal.querySelector('.close');
    const notificationsContainer = document.getElementById('notifications-container');
    const markAllAsReadBtn = document.getElementById('markAllAsReadBtn');
    const disableNotificationsToggle = document.getElementById('disableNotificationsToggle');
    let notifications = [];
    
    // Check if notifications are disabled in localStorage and update UI accordingly
    function initializeNotificationPreferences() {
        const notificationsDisabled = localStorage.getItem('notificationsDisabled') === 'true';
        
        // Update the toggle state
        if (disableNotificationsToggle) {
            disableNotificationsToggle.checked = notificationsDisabled;
        }
        
        // Update the body attribute to control CSS
        document.body.setAttribute('data-notifications-disabled', notificationsDisabled);
        
        // If notifications are disabled, automatically mark all as read
        if (notificationsDisabled) {
            markAllNotificationsAsRead(false); // Don't show popup when auto-marking
        }
    }

    // Fetch notifications
    async function fetchNotifications() {
        try {
            const response = await fetch('/base/api/notifications');
            const data = await response.json();
            notifications = data.notifications || [];
            updateNotificationDisplay();
            
            // Only update indicator if notifications are not disabled
            if (localStorage.getItem('notificationsDisabled') !== 'true') {
                updateNotificationIndicator();
            }
        } catch (error) {
            console.error('Error fetching notifications:', error);
        }
    }

    // Update notification display in modal
    function updateNotificationDisplay() {
        if (notifications.length === 0) {
            notificationsContainer.innerHTML = '<div class="no-notifications">No notifications</div>';
            return;
        }

        notificationsContainer.innerHTML = notifications
            .sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp))
            .map(notification => `
                <div class="notification ${notification.read ? 'read' : 'unread'}" data-id="${notification.id}">
                    <div class="notification-header">
                        <span class="notification-type ${notification.type || 'info'}">${notification.type || 'info'}</span>
                        <span class="notification-time">${formatTimestamp(notification.timestamp)}</span>
                    </div>
                    <div class="notification-title">${notification.title}</div>
                    <div class="notification-message">${notification.message}</div>
                    ${notification.link ? `<a href="${notification.link}" class="notification-link">View Details</a>` : ''}
                </div>
            `).join('');

        // Add click handlers for marking as read
        document.querySelectorAll('.notification.unread').forEach(notif => {
            notif.addEventListener('click', async () => {
                const id = notif.dataset.id;
                
                // Immediately update UI for better user experience
                notif.classList.remove('unread');
                notif.classList.add('read');
                
                // Update local notification state
                const notification = notifications.find(n => n.id === id);
                if (notification) {
                    notification.read = true;
                }
                
                // Update indicator immediately
                updateNotificationIndicator();
                
                // Then send request to server
                await markNotificationRead(id);
            });
        });
    }

    // Update the notification indicator (red dot)
    function updateNotificationIndicator() {
        // Don't show indicator if notifications are disabled
        if (localStorage.getItem('notificationsDisabled') === 'true') {
            notificationsBtn.classList.remove('has-notifications');
            return;
        }
        
        const hasUnread = notifications.some(n => !n.read);
        notificationsBtn.classList.toggle('has-notifications', hasUnread);
    }

    // Mark notification as read
    async function markNotificationRead(id) {
        try {
            await fetch('/base/api/notifications/mark-read', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ id }),
            });
            // Note: UI is already updated immediately, so no need to refresh here
            // The periodic refresh will ensure consistency with server state
        } catch (error) {
            console.error('Error marking notification as read:', error);
            // If the request fails, we could revert the UI changes here
            // For now, we'll let the periodic refresh handle any inconsistencies
        }
    }

    // Mark all notifications as read
    async function markAllNotificationsAsRead(showFeedback = true) {
        try {
            const response = await fetch('/base/api/notifications/mark-all-read', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                }
            });
            
            if (response.ok) {
                // Immediately refresh notifications to get updated read status
                await fetchNotifications();
                
                // Show feedback to the user if requested
                if (showFeedback) {
                    showPopup({
                        type: POPUP_TYPES.SUCCESS,
                        title: 'Success',
                        message: 'All notifications marked as read',
                        autoClose: 2000
                    });
                }
            } else {
                throw new Error('Failed to mark all notifications as read');
            }
        } catch (error) {
            console.error('Error marking all notifications as read:', error);
            if (showFeedback) {
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    title: 'Error',
                    message: 'Failed to mark all notifications as read',
                    autoClose: 3000
                });
            }
        }
    }

    // Toggle notifications enabled/disabled
    function toggleNotificationsEnabled() {
        const isDisabled = disableNotificationsToggle.checked;
        
        // Save preference to localStorage
        localStorage.setItem('notificationsDisabled', isDisabled);
        
        // Update body attribute for CSS
        document.body.setAttribute('data-notifications-disabled', isDisabled);
        
        // If toggling to disabled, mark all as read
        if (isDisabled) {
            markAllNotificationsAsRead(false); // Don't show popup when auto-marking
        }
        
        // Update notification indicator
        updateNotificationIndicator();
        
        // Show feedback
        showPopup({
            type: POPUP_TYPES.INFO,
            title: isDisabled ? 'Notifications Disabled' : 'Notifications Enabled',
            message: isDisabled ? 'Notifications will be automatically marked as read.' : 'You will now receive notifications.',
            autoClose: 2000
        });
    }

    // Format timestamp
    function formatTimestamp(timestamp) {
        const date = new Date(timestamp);
        const now = new Date();
        const diffHours = Math.abs(now - date) / 36e5;

        if (diffHours < 24) {
            return date.toLocaleTimeString();
        } else if (diffHours < 48) {
            return 'Yesterday';
        } else {
            return date.toLocaleDateString();
        }
    }

    // Modal controls
    notificationsBtn.addEventListener('click', function() {
        notificationsModal.style.display = 'block';
        fetchNotifications(); // Refresh notifications when opening modal
    });

    notificationsCloseBtn.addEventListener('click', function() {
        notificationsModal.style.display = 'none';
    });

    // Mark all as read button handler
    if (markAllAsReadBtn) {
        markAllAsReadBtn.addEventListener('click', () => markAllNotificationsAsRead(true));
    }
    
    // Disable notifications toggle handler
    if (disableNotificationsToggle) {
        disableNotificationsToggle.addEventListener('change', toggleNotificationsEnabled);
    }

    window.addEventListener('click', function(event) {
        if (event.target == notificationsModal) {
            notificationsModal.style.display = 'none';
        }
    });

    // Initial setup
    initializeNotificationPreferences();
    
    // Initial fetch
    fetchNotifications();

    initializeHelpModal();

    // --- START: Rate Limits Toggle Logic ---
    const rateLimitsToggle = document.getElementById('rateLimitsSectionToggle');
    const rateLimitsSection = document.querySelector('.rate-limits-section');

    if (rateLimitsToggle && rateLimitsSection) {
        rateLimitsToggle.addEventListener('click', () => {
            const isVisible = rateLimitsSection.classList.toggle('visible');
            updateBodyPaddingForTopOverlays(); // Update body padding
            // If rate limits info needs fetching on toggle:
            // if (isVisible && typeof fetchRateLimitInfo === 'function') {
            //     fetchRateLimitInfo();
            // }
        });
        
        // Ensure initial state is correct if visibility is persisted somehow (e.g., localStorage)
        // Example: if (localStorage.getItem('rateLimitsVisible') === 'true') { ... }
        // For now, assume it starts hidden.
        updateBodyPaddingForTopOverlays(); // Initial check

    } else {
        console.warn('Rate limits toggle button or section not found.');
    }
    // --- END: Rate Limits Toggle Logic ---
});

// Add resize listener to handle screen size changes
window.addEventListener('resize', () => {
    updateBodyPadding();
}); 