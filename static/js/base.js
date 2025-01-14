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
}); 