// Theme bootstrap.
//
// Tangerine is the only theme. Its stylesheets are linked directly by the
// templates, so nothing is swapped at runtime any more. This script only keeps
// the theme markers that other scripts and per-page CSS loaders still read
// (body data-theme / .tangerine-theme, localStorage + cookie "selectedTheme"),
// and the window.themeSwitcher API.

(function() {
    'use strict';

    const THEME = 'tangerine';
    const STORAGE_KEY = 'selectedTheme';

    function persistTheme() {
        try {
            localStorage.setItem(STORAGE_KEY, THEME);
        } catch (e) {
            // Storage can be unavailable (private mode, blocked site data).
        }
        document.cookie = `${STORAGE_KEY}=${THEME}; path=/; max-age=31536000`; // 1 year
    }

    function applyTheme() {
        document.body.setAttribute('data-theme', THEME);
        document.body.classList.add('tangerine-theme');
        document.dispatchEvent(new CustomEvent('themeChanged', { detail: { theme: THEME } }));
    }

    persistTheme();

    window.themeSwitcher = {
        getTheme: function() { return THEME; },
        setTheme: function() {},
        THEMES: { TANGERINE: THEME }
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', applyTheme);
    } else {
        applyTheme();
    }
})();
