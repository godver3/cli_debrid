// Theme Switcher Module
// Handles theme switching between Classic and custom themes
// When a theme other than Classic is selected, all classic CSS files in /static/css are disabled
// and the theme-specific CSS files from /static/css/themename are loaded instead

(function() {
    'use strict';

    const THEMES = {
        CLASSIC: 'classic',
        TANGERINE: 'tangerine'
    };

    const STORAGE_KEY = 'selectedTheme';

    // Initialize theme on page load
    function initializeTheme() {
        const savedTheme = getSavedTheme();
        // Ensure cookie is set on page load
        saveTheme(savedTheme);
        applyTheme(savedTheme);

        // Set up the theme selector if it exists
        const themeSelector = document.getElementById('theme-selector');
        if (themeSelector) {
            themeSelector.value = savedTheme;
            themeSelector.addEventListener('change', handleThemeChange);
        }
    }

    // Get the saved theme from localStorage, default to classic
    function getSavedTheme() {
        const savedTheme = localStorage.getItem(STORAGE_KEY);
        return savedTheme || THEMES.CLASSIC;
    }

    // Save theme preference to localStorage and cookie
    function saveTheme(theme) {
        localStorage.setItem(STORAGE_KEY, theme);
        // Also save as cookie so backend can access it
        document.cookie = `selectedTheme=${theme}; path=/; max-age=31536000`; // 1 year
    }

    // Apply the selected theme
    function applyTheme(theme) {
        // Set data attribute on body for CSS targeting
        document.body.setAttribute('data-theme', theme);

        // Add/remove tangerine-theme class for mobile nav styling
        if (theme === THEMES.TANGERINE) {
            document.body.classList.add('tangerine-theme');
        } else {
            document.body.classList.remove('tangerine-theme');
        }

        if (theme === THEMES.CLASSIC) {
            // Classic theme: Enable all classic CSS files, remove theme CSS files
            enableClassicStylesheets();
            removeThemeStylesheets();
        } else {
            // Custom theme: Disable all classic CSS files, load theme CSS files
            disableClassicStylesheets();
            loadThemeStylesheets(theme);
        }

        // Dispatch custom event for other scripts that might need to know about theme changes
        const themeChangeEvent = new CustomEvent('themeChanged', {
            detail: { theme: theme }
        });
        document.dispatchEvent(themeChangeEvent);
    }

    // Disable all classic CSS files in /static/css
    function disableClassicStylesheets() {
        const allStylesheets = document.querySelectorAll('link[rel="stylesheet"][href*="/static/css/"]');
        allStylesheets.forEach(stylesheet => {
            const href = stylesheet.getAttribute('href');
            // Only disable classic CSS files (not theme-specific ones)
            if (href && !href.includes('/tangerine/') && !href.match(/\/static\/css\/[^\/]+\//) && href.includes('/static/css/')) {
                stylesheet.setAttribute('data-classic-css', 'true');
                stylesheet.disabled = true;
            }
        });
    }

    // Enable all classic CSS files
    function enableClassicStylesheets() {
        const classicStylesheets = document.querySelectorAll('link[data-classic-css="true"]');
        classicStylesheets.forEach(stylesheet => {
            stylesheet.disabled = false;
            stylesheet.removeAttribute('data-classic-css');
        });
    }

    // Load theme-specific CSS files from /static/css/themename/
    function loadThemeStylesheets(themeName) {
        // Get all currently loaded classic stylesheets to determine which theme files to load
        const classicStylesheets = document.querySelectorAll('link[rel="stylesheet"][href*="/static/css/"][data-classic-css="true"]');

        classicStylesheets.forEach(classicLink => {
            const href = classicLink.getAttribute('href');

            // Extract the CSS filename from the classic path
            // e.g., /static/css/base.css -> base.css
            const match = href.match(/\/static\/css\/([^\/]+\.css)$/);
            if (match) {
                const filename = match[1];

                // Build the theme CSS path
                // e.g., /static/css/tangerine/tangerine_base.css
                const themeFilename = `${themeName}_${filename}`;
                const themePath = `/static/css/${themeName}/${themeFilename}`;

                // Create a unique ID for this theme stylesheet
                const themeId = `theme-${themeName}-${filename.replace('.css', '')}`;

                // Check if this theme stylesheet is already loaded
                let existingThemeLink = document.getElementById(themeId);
                if (!existingThemeLink) {
                    const themeLink = document.createElement('link');
                    themeLink.id = themeId;
                    themeLink.rel = 'stylesheet';
                    themeLink.href = themePath;
                    themeLink.setAttribute('data-theme-css', themeName);

                    // Insert the theme stylesheet after the classic one
                    if (classicLink.nextSibling) {
                        classicLink.parentNode.insertBefore(themeLink, classicLink.nextSibling);
                    } else {
                        classicLink.parentNode.appendChild(themeLink);
                    }
                }
            }
        });

        // Load mobile nav styles for Tangerine theme
        if (themeName === THEMES.TANGERINE) {
            const mobileNavId = 'theme-tangerine-mobile-nav';
            if (!document.getElementById(mobileNavId)) {
                const mobileNavLink = document.createElement('link');
                mobileNavLink.id = mobileNavId;
                mobileNavLink.rel = 'stylesheet';
                mobileNavLink.href = '/static/css/tangerine/tangerine_mobile_nav.css';
                mobileNavLink.setAttribute('data-theme-css', themeName);
                document.head.appendChild(mobileNavLink);
            }
        }
    }

    // Remove all theme-specific CSS files
    function removeThemeStylesheets() {
        const themeStylesheets = document.querySelectorAll('link[data-theme-css]');
        themeStylesheets.forEach(stylesheet => {
            stylesheet.remove();
        });
    }

    // Handle theme selection change
    function handleThemeChange(event) {
        const selectedTheme = event.target.value;

        // Save the theme preference
        saveTheme(selectedTheme);

        // Apply the theme (which disables/enables stylesheets)
        applyTheme(selectedTheme);
    }

    // Make functions available globally if needed
    window.themeSwitcher = {
        getTheme: getSavedTheme,
        setTheme: function(theme) {
            if (Object.values(THEMES).includes(theme)) {
                saveTheme(theme);
                applyTheme(theme);
            }
        },
        THEMES: THEMES
    };

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initializeTheme);
    } else {
        initializeTheme();
    }
})();
