import { initializeTooltips, setUpdatingContent } from './tooltips.js';
import { initializeProgramControls } from './program_controls.js';
import { initializeTaskMonitor } from './task_monitor.js';
import { showPopup, POPUP_TYPES } from './notifications.js';

// Make popup functionality available globally
window.showPopup = showPopup;
window.POPUP_TYPES = POPUP_TYPES;

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
                if (window.innerWidth <= 776) {
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
            if (window.innerWidth <= 776) {
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
function initializeRateLimits() {
    const rateLimitsSection = document.querySelector('.rate-limits-section');
    const container = document.getElementById('rate-limits-container');
    let isDragging = false;
    let startX, startY;
    let lastX, lastY;

    // Load saved position from localStorage
    const savedPosition = localStorage.getItem('rateLimitsPosition');
    if (savedPosition) {
        try {
            const { x, y } = JSON.parse(savedPosition);
            rateLimitsSection.style.left = `${x}px`;
            rateLimitsSection.style.top = `${y}px`;
        } catch (e) {
            console.error('Error loading saved position:', e);
        }
    }

    function getPosition(element) {
        const style = window.getComputedStyle(element);
        return {
            left: parseInt(style.left),
            top: parseInt(style.top)
        };
    }

    function handleDragStart(e) {
        // Only start dragging from the toggle area
        if (!e.target.closest('.rate-limits-toggle')) return;

        isDragging = true;
        const pos = getPosition(rateLimitsSection);

        if (e.type === 'touchstart') {
            startX = e.touches[0].clientX - pos.left;
            startY = e.touches[0].clientY - pos.top;
        } else {
            startX = e.clientX - pos.left;
            startY = e.clientY - pos.top;
        }

        // Add dragging class for visual feedback
        rateLimitsSection.classList.add('dragging');
    }

    function handleDragMove(e) {
        if (!isDragging) return;

        e.preventDefault();

        let clientX, clientY;
        if (e.type === 'touchmove') {
            clientX = e.touches[0].clientX;
            clientY = e.touches[0].clientY;
        } else {
            clientX = e.clientX;
            clientY = e.clientY;
        }

        // Calculate new position
        let newX = clientX - startX;
        let newY = clientY - startY;

        // Get viewport and element dimensions
        const viewportWidth = window.innerWidth;
        const viewportHeight = window.innerHeight;
        const rect = rateLimitsSection.getBoundingClientRect();

        // Constrain to viewport bounds with padding
        const padding = 10;
        newX = Math.max(padding, Math.min(newX, viewportWidth - rect.width - padding));
        newY = Math.max(padding, Math.min(newY, viewportHeight - rect.height - padding));

        // Update position
        rateLimitsSection.style.left = `${newX}px`;
        rateLimitsSection.style.top = `${newY}px`;

        lastX = newX;
        lastY = newY;
    }

    function handleDragEnd() {
        if (!isDragging) return;
        
        isDragging = false;
        rateLimitsSection.classList.remove('dragging');

        // Save final position
        if (lastX !== undefined && lastY !== undefined) {
            localStorage.setItem('rateLimitsPosition', JSON.stringify({
                x: lastX,
                y: lastY
            }));
        }
    }

    // Mouse events
    rateLimitsSection.addEventListener('mousedown', handleDragStart);
    document.addEventListener('mousemove', handleDragMove);
    document.addEventListener('mouseup', handleDragEnd);

    // Touch events
    rateLimitsSection.addEventListener('touchstart', handleDragStart, { passive: false });
    document.addEventListener('touchmove', handleDragMove, { passive: false });
    document.addEventListener('touchend', handleDragEnd);

    // Handle edge cases
    window.addEventListener('resize', () => {
        const pos = getPosition(rateLimitsSection);
        const rect = rateLimitsSection.getBoundingClientRect();
        const viewportWidth = window.innerWidth;
        const viewportHeight = window.innerHeight;
        const padding = 10;

        let newX = Math.max(padding, Math.min(pos.left, viewportWidth - rect.width - padding));
        let newY = Math.max(padding, Math.min(pos.top, viewportHeight - rect.height - padding));

        rateLimitsSection.style.left = `${newX}px`;
        rateLimitsSection.style.top = `${newY}px`;

        localStorage.setItem('rateLimitsPosition', JSON.stringify({
            x: newX,
            y: newY
        }));
    });
}

function toggleRateLimits() {
    const container = document.getElementById('rate-limits-container');
    container.classList.toggle('show');
    
    // If showing the container, fetch the latest info
    if (container.classList.contains('show')) {
        fetchRateLimitInfo();
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

function initializeTorrentActivity() {
    const container = document.getElementById('torrentActivityContainer');
    const toggle = document.getElementById('torrentActivityToggle');
    const toggleIcon = toggle?.querySelector('i');
    const list = document.getElementById('torrentActivityList');
    const clearButton = document.getElementById('torrentActivityClear');

    if (!container || !toggle || !list) return;

    // Clear button functionality
    if (clearButton) {
        clearButton.addEventListener('click', async () => {
            if (confirm('Are you sure you want to clear the recent torrent history?')) {
                try {
                    const response = await fetch('/base/api/clear-torrent-activity', {
                        method: 'POST'
                    });
                    const data = await response.json();
                    if (data.success) {
                        list.innerHTML = '<div class="no-activity">No recent activity</div>';
                    }
                } catch (error) {
                    console.error('Error clearing torrent activity:', error);
                }
            }
        });
    }

    // Load initial state from localStorage
    const isHidden = localStorage.getItem('torrentActivityHidden') === 'true';
    if (isHidden) {
        container.classList.add('hidden');
        toggleIcon?.classList.remove('fa-chevron-right');
        toggleIcon?.classList.add('fa-chevron-left');
    }

    // Toggle visibility
    toggle.addEventListener('click', () => {
        container.classList.toggle('hidden');
        const isNowHidden = container.classList.contains('hidden');
        localStorage.setItem('torrentActivityHidden', isNowHidden);
        
        // Toggle icon direction
        if (toggleIcon) {
            if (isNowHidden) {
                toggleIcon.classList.remove('fa-chevron-right');
                toggleIcon.classList.add('fa-chevron-left');
            } else {
                toggleIcon.classList.remove('fa-chevron-left');
                toggleIcon.classList.add('fa-chevron-right');
            }
        }
    });

    function formatTimeAgo(dateString) {
        // Parse the ISO date string as UTC
        const date = new Date(dateString + 'Z');  // Append Z to treat as UTC
        const now = new Date();
        const diffMs = now - date;
        const seconds = Math.floor(diffMs / 1000);

        // Return appropriate time string based on elapsed time
        if (seconds < 30) return 'just now';
        if (seconds < 60) return `${seconds}s ago`;
        
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) return `${minutes}m ago`;
        
        const hours = Math.floor(minutes / 60);
        if (hours < 24) return `${hours}h ago`;
        
        const days = Math.floor(hours / 24);
        if (days < 7) return `${days}d ago`;
        
        // For older dates, return the formatted date
        return date.toLocaleDateString(undefined, {
            year: 'numeric',
            month: 'short',
            day: 'numeric'
        });
    }

    function updateActivity() {
        fetch('/base/api/torrent-activity')
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    if (!data.activities || data.activities.length === 0) {
                        list.innerHTML = '<div class="no-activity">No recent activity</div>';
                    } else {
                        list.innerHTML = data.activities.map(activity => `
                            <div class="torrent-activity-item">
                                <div class="torrent-name">${activity.torrent_name}</div>
                                <div class="torrent-details">
                                    ${activity.media_title} (${activity.media_year})
                                    ${activity.season ? ` S${String(activity.season).padStart(2, '0')}` : ''}
                                    ${activity.episode ? `E${String(activity.episode).padStart(2, '0')}` : ''}
                                </div>
                                <div class="torrent-user">Added by ${activity.username}</div>
                                <div class="torrent-time">${formatTimeAgo(activity.added_at)}</div>
                            </div>
                        `).join('');
                    }
                } else {
                    list.innerHTML = '<div class="error">Failed to load activity</div>';
                }
            })
            .catch(error => {
                console.error('Error fetching torrent activity:', error);
                list.innerHTML = '<div class="error">Error loading activity</div>';
            });
    }

    // Update activity every 30 seconds
    updateActivity();
    setInterval(updateActivity, 30000);
}

// Initialize everything when DOM is loaded
document.addEventListener('DOMContentLoaded', function() {
    // Set updating content state to false before initializing tooltips
    setUpdatingContent(false);
    
    // Initialize all components
    initializeNavigation();
    initializeReleaseNotes();
    initializeTaskMonitor();
    initializeTooltips();
    
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

    initializeTorrentActivity();
    initializeRateLimits();
}); 