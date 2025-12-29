/**
 * Tangerine Theme Filename Formatter
 * Transforms filenames into a more readable pipe-separated format
 * Examples:
 *   The.Copenhagen.Test.S01E02.2160p.WEB.h265-ETHEL.mkv -> 2160p|WEB|h265|ETHEL|MKV
 *   A.Madea.Christmas.2013.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-NOGROUP.mkv -> 1080p|BLURAY|REMUX|AVC|DTS-HD|MA.5.1|NOGROUP|MKV
 *   goodbye.june.2025.hdr.2160p.web.h265-poke.mkv -> 2160p|WEB|HDR|h265|POKE|MKV
 */

(function() {
    'use strict';

    /**
     * Format a filename into pipe-separated components
     * @param {string} filename - The original filename
     * @returns {string} - Formatted filename with pipes
     */
    function formatFilename(filename) {
        if (!filename) return '';

        const components = [];
        let remaining = filename;

        // Step 1: Extract resolution (2160p, 1080p, etc.) - keep 'p' lowercase
        const resolutionMatch = remaining.match(/\b(2160p|1080p|720p|480p|4k|8k)\b/i);
        if (resolutionMatch) {
            const res = resolutionMatch[1].toLowerCase();
            components.push(res.replace(/(\d+)(p)/, '$1p'));
            remaining = remaining.replace(resolutionMatch[0], ' ');
        }

        // Step 2: Extract source type
        if (/\bbluray\b/i.test(remaining)) {
            components.push('BLURAY');
            remaining = remaining.replace(/\bbluray\b/i, ' ');
        } else if (/\bweb-?dl\b/i.test(remaining)) {
            components.push('WEB-DL');
            remaining = remaining.replace(/\bweb-?dl\b/i, ' ');
        } else if (/\bwebrip\b/i.test(remaining)) {
            components.push('WEBRIP');
            remaining = remaining.replace(/\bwebrip\b/i, ' ');
        } else if (/\bweb\b/i.test(remaining)) {
            components.push('WEB');
            remaining = remaining.replace(/\bweb\b/i, ' ');
        } else if (/\bhdtv\b/i.test(remaining)) {
            components.push('HDTV');
            remaining = remaining.replace(/\bhdtv\b/i, ' ');
        } else if (/\bamzn\b/i.test(remaining)) {
            components.push('AMZN');
            remaining = remaining.replace(/\bamzn\b/i, ' ');
        } else if (/\btheater\b/i.test(remaining)) {
            components.push('THEATER');
            remaining = remaining.replace(/\btheater\b/i, ' ');
        }

        // Step 3: Check for REMUX
        if (/\bremux\b/i.test(remaining)) {
            components.push('REMUX');
            remaining = remaining.replace(/\bremux\b/i, ' ');
        }

        // Step 4: Extract video codec - keep 'h' lowercase in h264/h265
        if (/\bavc\b/i.test(remaining)) {
            components.push('AVC');
            remaining = remaining.replace(/\bavc\b/i, ' ');
        } else if (/\bhevc\b/i.test(remaining)) {
            components.push('HEVC');
            remaining = remaining.replace(/\bhevc\b/i, ' ');
        } else if (/\bh\.?265\b/i.test(remaining)) {
            components.push('h265');
            remaining = remaining.replace(/\bh\.?265\b/i, ' ');
        } else if (/\bh\.?264\b/i.test(remaining)) {
            components.push('h264');
            remaining = remaining.replace(/\bh\.?264\b/i, ' ');
        } else if (/\bx\.?265\b/i.test(remaining)) {
            components.push('x265');
            remaining = remaining.replace(/\bx\.?265\b/i, ' ');
        } else if (/\bx\.?264\b/i.test(remaining)) {
            components.push('x264');
            remaining = remaining.replace(/\bx\.?264\b/i, ' ');
        }

        // Step 5: Extract HDR info
        if (/\bhdr10\+?\b/i.test(remaining)) {
            components.push('HDR10');
            remaining = remaining.replace(/\bhdr10\+?\b/i, ' ');
        } else if (/\bhdr\b/i.test(remaining)) {
            components.push('HDR');
            remaining = remaining.replace(/\bhdr\b/i, ' ');
        }
        if (/\b(dv|dolby\.?vision)\b/i.test(remaining)) {
            components.push('DV');
            remaining = remaining.replace(/\b(dv|dolby\.?vision)\b/i, ' ');
        }

        // Step 6: Extract audio codec and format
        // DTS-HD with optional MA and channel config
        const dtsHdMatch = remaining.match(/\bDTS-HD(?:\s*\.?\s*MA)?(?:\s*\.?\s*(\d\.?\d?))?/i);
        if (dtsHdMatch) {
            components.push('DTS-HD');
            remaining = remaining.replace(dtsHdMatch[0], ' ');

            // Check for separate MA indicator
            const maMatch = remaining.match(/\bMA\s*\.?\s*(\d\.?\d?)/i);
            if (maMatch) {
                components.push('MA.' + maMatch[1]);
                remaining = remaining.replace(maMatch[0], ' ');
            }
        }
        // DDP (Dolby Digital Plus)
        else if (/\bDDP(\d\.?\d?)?/i.test(remaining)) {
            const ddpMatch = remaining.match(/\bDDP(\d\.?\d?)?/i);
            let audio = 'DDP';
            if (ddpMatch[1]) audio += ddpMatch[1];
            components.push(audio);
            remaining = remaining.replace(ddpMatch[0], ' ');
        }
        // AAC
        else if (/\bAAC(\d\.?\d?)?/i.test(remaining)) {
            const aacMatch = remaining.match(/\bAAC(\d\.?\d?)?/i);
            let audio = 'AAC';
            if (aacMatch[1]) audio += aacMatch[1];
            components.push(audio);
            remaining = remaining.replace(aacMatch[0], ' ');
        }
        // PCM
        else if (/\bPCM(?:\s*\.?\s*(\d\.?\d?))?/i.test(remaining)) {
            const pcmMatch = remaining.match(/\bPCM(?:\s*\.?\s*(\d\.?\d?))?/i);
            let audio = 'PCM';
            if (pcmMatch[1]) audio += '.' + pcmMatch[1];
            components.push(audio);
            remaining = remaining.replace(pcmMatch[0], ' ');
        }

        // Check for Atmos
        if (/\bAtmos\b/i.test(remaining)) {
            components.push('ATMOS');
            remaining = remaining.replace(/\bAtmos\b/i, ' ');
        }

        // Step 7: Extract release group (after last dash before extension)
        const releaseGroupMatch = filename.match(/-([A-Za-z0-9]+)\.(mkv|mp4|avi|m4v|ts|m2ts)$/i);
        if (releaseGroupMatch) {
            components.push(releaseGroupMatch[1].toUpperCase());
        }

        // Step 8: Extract file extension
        const extensionMatch = filename.match(/\.(mkv|mp4|avi|m4v|ts|m2ts)$/i);
        if (extensionMatch) {
            components.push(extensionMatch[1].toUpperCase());
        }

        // If we didn't extract any meaningful components, return original
        if (components.length === 0) {
            return filename;
        }

        return components.join('|');
    }

    /**
     * Combine From/To filenames in Recently Upgraded section
     */
    function combineRecentlyUpgradedFilenames() {
        // Check if we're using Tangerine theme
        const currentTheme = document.body.getAttribute('data-theme');
        console.log('Current theme:', currentTheme);
        if (currentTheme !== 'tangerine') {
            return;
        }

        // Find all Recently Upgraded sections
        const upgradedSections = document.querySelectorAll('.recently-upgraded-section .file-details, .recently-upgraded-section .poster-hover');
        console.log('Found upgraded sections:', upgradedSections.length);

        upgradedSections.forEach(container => {
            const fromElement = container.querySelector('.filename[data-label="From:"]');
            const toElement = container.querySelector('.filename[data-label="To:"]');

            console.log('From element:', fromElement);
            console.log('To element:', toElement);

            // Only combine if both elements exist and haven't been combined yet
            if (fromElement && toElement && !fromElement.hasAttribute('data-combined')) {
                const fromText = fromElement.textContent.trim();
                const toText = toElement.textContent.trim();

                console.log('Combining:', fromText, '>>', toText);

                // Clear the From element and rebuild with styled arrow
                fromElement.textContent = '';

                // Add the from text
                const fromTextNode = document.createTextNode(fromText);
                fromElement.appendChild(fromTextNode);

                // Add styled arrow
                const arrow = document.createElement('span');
                arrow.className = 'upgrade-arrow';
                arrow.textContent = ' >> ';
                fromElement.appendChild(arrow);

                // Add the to text
                const toTextNode = document.createTextNode(toText);
                fromElement.appendChild(toTextNode);

                fromElement.setAttribute('data-combined', 'true');

                // Hide the To element
                toElement.style.display = 'none';

                console.log('Combined successfully');
            }
        });
    }

    /**
     * Apply filename formatting to all filename elements on the page
     * Only applies to Tangerine theme
     */
    function applyFilenameFormatting() {
        // Check if we're using Tangerine theme
        const currentTheme = document.body.getAttribute('data-theme');
        if (currentTheme !== 'tangerine') {
            return;
        }

        // Find all filename elements
        const filenameElements = document.querySelectorAll('.file-details .filename, .poster-hover .filename');

        filenameElements.forEach(element => {
            const originalFilename = element.textContent.trim();

            // Skip if already formatted (contains pipes)
            if (originalFilename.includes('|')) {
                return;
            }

            // Format the filename
            const formattedFilename = formatFilename(originalFilename);

            // Store original in data attribute for reference
            if (!element.hasAttribute('data-original-filename')) {
                element.setAttribute('data-original-filename', originalFilename);
            }

            // Update the display
            element.textContent = formattedFilename;
        });

        // After formatting individual filenames, combine Recently Upgraded ones
        combineRecentlyUpgradedFilenames();
    }

    /**
     * Restore original filenames (used when switching back to classic theme)
     */
    function restoreOriginalFilenames() {
        const filenameElements = document.querySelectorAll('.file-details .filename[data-original-filename], .poster-hover .filename[data-original-filename]');

        filenameElements.forEach(element => {
            const originalFilename = element.getAttribute('data-original-filename');
            if (originalFilename) {
                element.textContent = originalFilename;
            }

            // Remove combined attribute and restore hidden To elements
            if (element.hasAttribute('data-combined')) {
                element.removeAttribute('data-combined');
            }
        });

        // Restore hidden "To" elements
        const hiddenToElements = document.querySelectorAll('.filename[data-label="To:"][style*="display: none"]');
        hiddenToElements.forEach(element => {
            element.style.display = '';
        });
    }

    /**
     * Initialize the formatter
     */
    function initializeFormatter() {
        // Apply formatting on initial load
        applyFilenameFormatting();

        // Listen for theme changes
        document.addEventListener('themeChanged', function(event) {
            if (event.detail.theme === 'tangerine') {
                applyFilenameFormatting();
            } else {
                restoreOriginalFilenames();
            }
        });

        // Create a MutationObserver to watch for dynamically added content
        const observer = new MutationObserver(function(mutations) {
            const currentTheme = document.body.getAttribute('data-theme');
            if (currentTheme === 'tangerine') {
                mutations.forEach(function(mutation) {
                    if (mutation.type === 'childList' && mutation.addedNodes.length > 0) {
                        // Check if any added nodes contain filename elements
                        mutation.addedNodes.forEach(function(node) {
                            if (node.nodeType === 1) { // Element node
                                // Check if the node itself or its children contain filename elements
                                if (node.matches && (node.matches('.file-details .filename') || node.matches('.poster-hover .filename'))) {
                                    applyFilenameFormatting();
                                } else if (node.querySelectorAll) {
                                    const filenameElements = node.querySelectorAll('.file-details .filename, .poster-hover .filename');
                                    if (filenameElements.length > 0) {
                                        applyFilenameFormatting();
                                    }
                                }
                            }
                        });
                    }
                });
            }
        });

        // Observe the statistics wrapper for changes
        const statsWrapper = document.querySelector('.statistics-wrapper');
        if (statsWrapper) {
            observer.observe(statsWrapper, {
                childList: true,
                subtree: true
            });
        }
    }

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initializeFormatter);
    } else {
        initializeFormatter();
    }

    // Export functions to global scope for debugging if needed
    window.tangerineFilenameFormatter = {
        format: formatFilename,
        apply: applyFilenameFormatting,
        restore: restoreOriginalFilenames
    };
})();
