/* Moved from templates/settings.html. */
import { updateSettings } from '/static/js/settings.js?v=1778267907';
import { showPopup, POPUP_TYPES } from '/static/js/notifications.js';

// Make POPUP_TYPES available globally for other scripts
window.POPUP_TYPES = POPUP_TYPES;

// Define reloadTabContent in the global scope
window.reloadTabContent = function(tabName, callback) {
    return fetch(`/settings/${tabName}/content`)
        .then(response => {
            return response.text();
        })
        .then(html => {
            const tabContent = document.getElementById(tabName);
            if (tabContent) {
                console.log('Updating HTML for tab:', tabName);
                tabContent.innerHTML = html;
                console.log('HTML updated, tab content length:', tabContent.innerHTML.length);
                if (tabName === 'scrapers') {
                    console.log('Dispatching scrapersContentLoaded event');
                    const event = new CustomEvent('scrapersContentLoaded');
                    document.dispatchEvent(event);
                } else if (tabName === 'scraping') {
                    console.log('Dispatching scrapingContentLoaded event');
                    const event = new CustomEvent('scrapingContentLoaded');
                    document.dispatchEvent(event);
                } else if (tabName === 'true-debug') {
                    console.log('Dispatching trueDebugContentLoaded event');
                    const event = new CustomEvent('trueDebugContentLoaded');
                    document.dispatchEvent(event);
                }
                initializeExpandCollapse();
                if (callback && typeof callback === 'function') {
                    callback();
                }
            } else {
                console.error('Tab content element not found:', tabName);
            }
        })
        .catch(error => {
            console.error(`Error reloading ${tabName} tab:`, error);
            showPopup({
                type: POPUP_TYPES.ERROR,
                message: `Error reloading ${tabName} tab: ${error.message}`
            });
        });
};

window.initializeExpandCollapse = function() {
    const allTabContents = document.querySelectorAll('.settings-tab-content');

    allTabContents.forEach(tabContent => {
        const expandAllButton = tabContent.querySelector('.settings-expand-all');
        const collapseAllButton = tabContent.querySelector('.settings-collapse-all');
        const settingsSections = tabContent.querySelectorAll('.settings-section');

        if (expandAllButton) {
            expandAllButton.addEventListener('click', () => expandAll(tabContent));
        }

        if (collapseAllButton) {
            collapseAllButton.addEventListener('click', () => collapseAll(tabContent));
        }

        settingsSections.forEach(section => {
            const header = section.querySelector('.settings-section-header');
            const content = section.querySelector('.settings-section-content');
            const toggleIcon = header.querySelector('.settings-toggle-icon');

            if (header && content && toggleIcon) {
                header.addEventListener('click', toggleSectionHandler);
            }
        });
    });
};

function expandAll(tabContent) {
    const sections = tabContent.querySelectorAll('.settings-section');
    sections.forEach(section => {
        const content = section.querySelector('.settings-section-content');
        if (content) {
            content.style.display = 'block';
            section.classList.add('expanded');
        }
    });
}

function collapseAll(tabContent) {
    const sections = tabContent.querySelectorAll('.settings-section');
    sections.forEach(section => {
        const content = section.querySelector('.settings-section-content');
        if (content) {
            content.style.display = 'none';
            section.classList.remove('expanded');
        }
    });
}

function toggleSectionHandler(e) {
    if (!e.target.closest('button')) {
        const header = e.currentTarget;
        const section = header.closest('.settings-section');
        const content = header.nextElementSibling;

        if (content.style.display === 'none' || content.style.display === '') {
            content.style.display = 'block';
            section.classList.add('expanded');
        } else {
            content.style.display = 'none';
            section.classList.remove('expanded');
        }
    }
}

document.addEventListener('DOMContentLoaded', function() {
    const navItems = document.querySelectorAll('.settings-nav-item');
    const tabContents = document.querySelectorAll('.settings-tab-content');
    const mobileMenuToggle = document.getElementById('settingsMobileMenuToggle');
    const mobileOverlay = document.getElementById('settingsMobileOverlay');
    const sidebar = document.getElementById('settingsSidebar');

    // Initialize format modal handlers
    initializeFormatModal();

    // Mobile menu functionality
    function toggleMobileMenu() {
        sidebar.classList.toggle('mobile-open');
        mobileOverlay.classList.toggle('active');
        document.body.style.overflow = sidebar.classList.contains('mobile-open') ? 'hidden' : '';

        // Close nav menu when settings sidebar is opened
        if (sidebar.classList.contains('mobile-open')) {
            const navMenu = document.getElementById('navMenu');
            const hamburger = document.querySelector('.hamburger-menu');
            const navOverlay = document.getElementById('navOverlay');

            if (navMenu && navMenu.classList.contains('show')) {
                navMenu.classList.remove('show');
                if (hamburger) {
                    hamburger.classList.remove('active');
                }
                if (navOverlay) {
                    navOverlay.classList.remove('active');
                }
            }
        }
    }

    mobileMenuToggle.addEventListener('click', toggleMobileMenu);
    mobileOverlay.addEventListener('click', toggleMobileMenu);

    // Navigation item click handler
    navItems.forEach(item => {
        item.addEventListener('click', function() {
            const tabName = this.getAttribute('data-tab');

            // Update active states
            navItems.forEach(i => i.classList.remove('active'));
            this.classList.add('active');

            // Switch tabs
            openTab(tabName);

            // Close mobile menu on mobile
            if (window.innerWidth <= 768) {
                toggleMobileMenu();
            }
        });
    });

    // Handle window resize
    window.addEventListener('resize', function() {
        if (window.innerWidth > 768) {
            sidebar.classList.remove('mobile-open');
            mobileOverlay.classList.remove('active');
            document.body.style.overflow = '';
        }
    });

    function openTab(tabName) {
        tabContents.forEach(content => content.style.display = 'none');

        const activeTab = document.getElementById(tabName);
        if (activeTab) {
            activeTab.style.display = 'block';
            localStorage.setItem('currentTab', tabName);
        }
    }

    initializeExpandCollapse();

    // Open the last active tab or default to 'required'
    const lastActiveTab = localStorage.getItem('currentTab') || 'required';
    const defaultNavItem = document.querySelector(`.settings-nav-item[data-tab="${lastActiveTab}"]`);
    if (defaultNavItem) {
        navItems.forEach(i => i.classList.remove('active'));
        defaultNavItem.classList.add('active');
    }
    openTab(lastActiveTab);

    // Dispatch an event to signal that dashboard initialization is complete
    document.dispatchEvent(new Event('dashboardInitialized'));

    // Add event listener for the Save Settings button
    const saveSettingsButton = document.getElementById('saveSettingsButton');
    saveSettingsButton.addEventListener('click', function() {
        updateSettings();
    });
});

function initializeFormatModal() {
    // Handle clicks on format help links
    document.addEventListener('click', function(e) {
        if (e.target.classList.contains('format-help-link')) {
            e.preventDefault();
            const modalId = e.target.getAttribute('data-modal');
            const modal = document.getElementById(modalId);
            if (modal) {
                modal.style.display = 'block';
            }
        }
    });

    // Handle close button clicks
    document.querySelectorAll('.format-modal-close').forEach(closeBtn => {
        closeBtn.addEventListener('click', function() {
            this.closest('.format-modal').style.display = 'none';
        });
    });

    // Handle overlay clicks to close modal
    document.querySelectorAll('.format-modal-overlay').forEach(overlay => {
        overlay.addEventListener('click', function() {
            this.closest('.format-modal').style.display = 'none';
        });
    });

    // Handle ESC key to close modal
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            document.querySelectorAll('.format-modal').forEach(modal => {
                modal.style.display = 'none';
            });
        }
    });
}

// Settings Search Functionality
function initializeSettingsSearch() {
    const searchInput = document.getElementById('settingsSearch');
    const clearButton = document.getElementById('clearSearch');
    let searchTimeout;

    if (!searchInput) return;

    // Handle input changes
    searchInput.addEventListener('input', function() {
        const query = this.value.trim();

        // Show/hide clear button
        clearButton.style.display = query ? 'flex' : 'none';

        // Debounce search
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(() => {
            performSearch(query);
        }, 300);
    });

    // Handle clear button click
    clearButton.addEventListener('click', function() {
        searchInput.value = '';
        clearButton.style.display = 'none';
        performSearch('');
        searchInput.focus();
    });

    // Handle ESC key to clear search
    searchInput.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            searchInput.value = '';
            clearButton.style.display = 'none';
            performSearch('');
        }
    });
}

function performSearch(query) {
    const lowerQuery = query.toLowerCase();
    const tabContents = document.querySelectorAll('.settings-tab-content');

    // Always reset first to clear any previous search state
    removeSearchHighlights();

    if (!query) {
        return;
    }

    let totalMatches = 0;
    const matchesByTab = {};

    // Search through all tabs (skip true-debug — it's merged into Advanced Settings
    // and inaccessible via the nav, so its matches would duplicate the debug tab count)
    tabContents.forEach(tabContent => {
        const tabId = tabContent.id;
        if (tabId === 'true-debug') return;
        const sections = tabContent.querySelectorAll('.settings-section');
        let tabMatches = 0;

        sections.forEach(section => {
            const sectionHeader = section.querySelector('.settings-section-header h4');
            const formGroups = section.querySelectorAll('.settings-form-group');
            let sectionHasMatch = false;
            let matchedGroups = [];

            // Check section header
            const sectionTitle = sectionHeader ? sectionHeader.textContent.toLowerCase() : '';
            if (sectionTitle.includes(lowerQuery)) {
                sectionHasMatch = true;
            }

            // Check each form group
            formGroups.forEach(group => {
                const label = group.querySelector('.settings-title, label');
                const description = group.querySelector('.settings-description');
                const input = group.querySelector('input, select, textarea');

                const labelText = label ? label.textContent.toLowerCase() : '';
                const descText = description ? description.textContent.toLowerCase() : '';
                const inputValue = input ? (input.value || '').toLowerCase() : '';
                const inputId = input ? (input.id || '').toLowerCase() : '';

                if (labelText.includes(lowerQuery) ||
                    descText.includes(lowerQuery) ||
                    inputValue.includes(lowerQuery) ||
                    inputId.includes(lowerQuery)) {
                    sectionHasMatch = true;
                    matchedGroups.push(group);
                }
            });

            // Mark section visibility
            if (sectionHasMatch) {
                section.classList.add('search-match');
                section.classList.remove('search-hidden');

                // Expand section to show matches
                const content = section.querySelector('.settings-section-content');
                if (content) {
                    content.style.display = 'block';
                    section.classList.add('expanded');
                }

                // Highlight matched form groups
                matchedGroups.forEach(group => {
                    group.classList.add('search-highlight');
                });

                tabMatches++;
            } else {
                section.classList.remove('search-match');
                section.classList.add('search-hidden');
            }
        });

        if (tabMatches > 0) {
            matchesByTab[tabId] = tabMatches;
            totalMatches += tabMatches;
        }
    });

    // Update search results indicator
    displaySearchResults(matchesByTab, totalMatches, query);
}

function removeSearchHighlights() {
    // Remove all search-related classes
    document.querySelectorAll('.search-match, .search-hidden, .search-highlight').forEach(el => {
        el.classList.remove('search-match', 'search-hidden', 'search-highlight');
    });

    // Remove search results indicator
    const resultsIndicator = document.querySelector('.search-results-indicator');
    if (resultsIndicator) {
        resultsIndicator.remove();
    }
}

function displaySearchResults(matchesByTab, totalMatches, query) {
    // Remove existing indicator
    const existingIndicator = document.querySelector('.search-results-indicator');
    if (existingIndicator) {
        existingIndicator.remove();
    }

    if (totalMatches === 0) {
        // Show "no results" message
        const nav = document.querySelector('.settings-sidebar nav');
        const indicator = document.createElement('div');
        indicator.className = 'search-results-indicator no-results';
        indicator.innerHTML = `
            <svg fill="none" stroke="currentColor" viewBox="0 0 24 24" style="width: 20px; height: 20px; margin-right: 8px;">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.172 16.172a4 4 0 015.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
            </svg>
            No results found for "${query}"
        `;
        nav.insertBefore(indicator, nav.firstChild);
    } else {
        // Show results count and matched tabs
        const nav = document.querySelector('.settings-sidebar nav');
        const indicator = document.createElement('div');
        indicator.className = 'search-results-indicator';

        // Map tab IDs to their proper display names
        const tabDisplayNames = {
            'required': 'Required Settings',
            'additional': 'Additional Settings',
            'scrapers': 'Scrapers',
            'scraping': 'Version Settings',
            'content-sources': 'Content Sources',
            'debug': 'Advanced Settings',
            'notifications': 'Notifications',
            'reverse-parser': 'Reverse Parser',
            'true-debug': 'Advanced Settings'
        };

        // Merge counts for tabs that share the same display name (debug + true-debug → Advanced Settings)
        const mergedMatches = {};
        const mergedTabId = {};  // display name → first tab id (for navigation)
        Object.entries(matchesByTab).forEach(([tabId, count]) => {
            const name = tabDisplayNames[tabId] || tabId.charAt(0).toUpperCase() + tabId.slice(1).replace(/-/g, ' ');
            if (mergedMatches[name] === undefined) {
                mergedMatches[name] = 0;
                mergedTabId[name] = tabId;
            }
            mergedMatches[name] += count;
        });

        let tabsList = Object.entries(mergedMatches)
            .map(([name, count]) => {
                const tabId = mergedTabId[name];
                return `<span class="search-tab-match" data-tab="${tabId}">${name} (${count})</span>`;
            })
            .join('');

        indicator.innerHTML = `
            <div class="search-results-header">
                <svg fill="none" stroke="currentColor" viewBox="0 0 24 24" style="width: 18px; height: 18px; margin-right: 6px;">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>
                </svg>
                Found ${totalMatches} match${totalMatches !== 1 ? 'es' : ''}
            </div>
            <div class="search-tabs-list">${tabsList}</div>
        `;
        nav.insertBefore(indicator, nav.firstChild);

        // Add click handlers to tab matches
        indicator.querySelectorAll('.search-tab-match').forEach(match => {
            match.addEventListener('click', function() {
                const tabId = this.getAttribute('data-tab');
                const navItem = document.querySelector(`.settings-nav-item[data-tab="${tabId}"]`);
                if (navItem) {
                    navItem.click();
                }
            });
        });
    }
}

// Initialize search on page load
document.addEventListener('DOMContentLoaded', function() {
    initializeSettingsSearch();
});
