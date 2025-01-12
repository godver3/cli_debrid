let tooltips = {};
let tooltipElement = null;
let tooltipTimeout = null;
let hideTooltipTimeout = null;
let lastMousePosition = { x: 0, y: 0 };
const TOOLTIP_DELAY = 500; // Delay in milliseconds (0.5 seconds)
const TOOLTIP_FADE_IN = 250; // Fade in duration in milliseconds
const TOOLTIP_FADE_OUT = 600; // Fade out duration in milliseconds
const TOOLTIP_PADDING = 10; // Padding from screen edges

let activeTooltipElement = null;

let mobileTooltipContent = null;

let scrollTimeout = null;
const SCROLL_HIDE_DELAY = 100; // ms to wait after scrolling stops before hiding tooltip

let isUpdatingContent = false;

function isMobileDevice() {
    // Check if the device has a touch screen AND a small viewport
    // This is more accurate than just checking for touch capability
    const hasTouchScreen = (
        'ontouchstart' in window ||
        navigator.maxTouchPoints > 0 ||
        navigator.msMaxTouchPoints > 0
    );
    
    // Check viewport width - typical breakpoint for mobile devices
    const isMobileViewport = window.innerWidth <= 768;
    
    return hasTouchScreen && isMobileViewport;
}

async function fetchTooltips() {
    if (window.isRateLimited) {
        console.log("Rate limit exceeded. Skipping tooltips fetch.");
        return {};
    }

    try {
        const response = await fetch('/tooltip/tooltips');
        tooltips = await response.json();
        console.log('Fetched tooltips:', tooltips);
    } catch (error) {
        console.error('Failed to fetch tooltips:', error);
    }
}

function showTooltip(event) {
    if (isMobileDevice() || isUpdatingContent) return;

    const element = event.currentTarget;
    const tooltipKey = element.dataset.tooltip;
    
    console.log('Preparing to show tooltip for:', tooltipKey);
    
    // Clear any existing timeouts
    if (tooltipTimeout) {
        clearTimeout(tooltipTimeout);
    }
    if (hideTooltipTimeout) {
        clearTimeout(hideTooltipTimeout);
        hideTooltipTimeout = null;
    }

    // Create tooltip element immediately if it doesn't exist
    if (!tooltipElement) {
        tooltipElement = document.createElement('div');
        tooltipElement.className = 'tooltip';
        document.body.appendChild(tooltipElement);
    }

    // Position the tooltip immediately, but keep it invisible
    tooltipElement.style.opacity = '0';
    tooltipElement.style.display = 'block';
    tooltipElement.style.visibility = 'visible';

    activeTooltipElement = element;

    let finalPosition = { x: event.pageX, y: event.pageY };
    let isMoving = false;
    let moveTimer = null;

    const showTooltipContent = () => {
        if (!activeTooltipElement || activeTooltipElement !== element) return;

        let tooltipText;
        if (tooltipKey) {
            if (tooltipKey.startsWith('database|||')) {
                // For database tooltips, everything after the delimiter is the content
                tooltipText = tooltipKey.split('|||')[1];
                tooltipElement.className = 'tooltip database-tooltip';
            } else {
                // For regular tooltips, split on period
                const [section, key] = tooltipKey.split('.');
                tooltipText = tooltips[section][key];
                tooltipElement.className = 'tooltip';
            }
            console.log(`Showing tooltip for ${tooltipKey}: "${tooltipText}"`);
        } else {
            console.log('No tooltip content found');
            tooltipElement.style.display = 'none';
            return;
        }
        
        tooltipElement.textContent = tooltipText;
        
        // Force layout recalculation to get accurate dimensions
        tooltipElement.style.visibility = 'hidden';
        tooltipElement.style.display = 'block';
        
        // Position tooltip at the final mouse position
        positionTooltipAtPoint(finalPosition.x, finalPosition.y);
        
        // Make visible with transition
        tooltipElement.style.visibility = 'visible';
        tooltipElement.style.transition = `opacity ${TOOLTIP_FADE_IN}ms ease-in`;
        tooltipElement.style.opacity = '1';
        
        console.log('Tooltip displayed:', tooltipText);
    };

    // Track mouse movement
    const handleMouseMove = (e) => {
        finalPosition = { x: e.pageX, y: e.pageY };
        
        if (!isMoving) {
            isMoving = true;
            if (tooltipTimeout) {
                clearTimeout(tooltipTimeout);
            }
        }

        // Clear existing move timer
        if (moveTimer) {
            clearTimeout(moveTimer);
        }

        // Set new move timer
        moveTimer = setTimeout(() => {
            isMoving = false;
            tooltipTimeout = setTimeout(showTooltipContent, TOOLTIP_DELAY);
        }, 100); // Wait for 100ms of no movement before starting tooltip timer
    };

    document.addEventListener('mousemove', handleMouseMove);

    // Set up cleanup on mouse leave
    element.hideTooltip = () => {
        if (moveTimer) {
            clearTimeout(moveTimer);
        }
        if (tooltipTimeout) {
            clearTimeout(tooltipTimeout);
        }
        document.removeEventListener('mousemove', handleMouseMove);
        hideTooltip();
    };
    element.addEventListener('mouseleave', element.hideTooltip);

    // Start initial timer
    tooltipTimeout = setTimeout(showTooltipContent, TOOLTIP_DELAY);
}

function positionTooltipAtPoint(x, y) {
    if (!tooltipElement) return;

    const offset = 10; // Distance from the cursor
    let tooltipX = x + offset;
    let tooltipY = y + offset;

    // Ensure the tooltip doesn't go off-screen
    const tooltipRect = tooltipElement.getBoundingClientRect();
    const maxX = window.innerWidth + window.pageXOffset - tooltipRect.width - TOOLTIP_PADDING;
    const maxY = window.innerHeight + window.pageYOffset - tooltipRect.height - TOOLTIP_PADDING;
    const minY = window.pageYOffset + TOOLTIP_PADDING;

    // If tooltip would go off the right side, position it to the left of the cursor
    if (tooltipX > maxX) {
        tooltipX = x - tooltipRect.width - offset;
    }

    // If tooltip would go off the top, position it below the cursor
    if (tooltipY < minY) {
        tooltipY = y + tooltipRect.height + offset;
    }

    tooltipX = Math.max(window.pageXOffset + TOOLTIP_PADDING, Math.min(tooltipX, maxX));
    tooltipY = Math.max(window.pageYOffset + TOOLTIP_PADDING, Math.min(tooltipY, maxY));

    // Position relative to the document
    tooltipElement.style.position = 'absolute';
    tooltipElement.style.left = `${tooltipX}px`;
    tooltipElement.style.top = `${tooltipY}px`;
}

function hideTooltip() {
    if (isUpdatingContent) return;

    if (tooltipTimeout) {
        clearTimeout(tooltipTimeout);
        tooltipTimeout = null;
    }
    
    // If there's already a hide operation in progress, don't queue another one
    if (hideTooltipTimeout) {
        return;
    }
    
    hideTooltipTimeout = setTimeout(() => {
        if (tooltipElement) {
            tooltipElement.style.transition = `opacity ${TOOLTIP_FADE_OUT}ms ease-out`;
            tooltipElement.style.opacity = '0';
            setTimeout(() => {
                if (hideTooltipTimeout) {  // Only hide if we haven't started showing again
                    tooltipElement.style.display = 'none';
                    activeTooltipElement = null;
                }
            }, TOOLTIP_FADE_OUT);
            console.log('Tooltip hidden');
        }
        hideTooltipTimeout = null;  // Clear the timeout reference
    }, 50); // Reduced delay before hiding
}

function createMobileTooltipButtons() {
    const tooltipElements = document.querySelectorAll('[data-tooltip]');
    tooltipElements.forEach(element => {
        const button = document.createElement('i');
        button.className = 'fas fa-info-circle mobile-tooltip-button';
        button.setAttribute('aria-label', 'More information');
        element.appendChild(button);

        button.addEventListener('click', (event) => {
            event.stopPropagation();
            showMobileTooltip(element.getAttribute('data-tooltip'), button);
        });
    });
}

function showMobileTooltip(tooltipKey, button) {
    console.log('showMobileTooltip called with key:', tooltipKey);
    
    // Hide any existing tooltip without animation
    if (mobileTooltipContent) {
        hideMobileTooltip(false);
    }

    mobileTooltipContent = document.createElement('div');
    mobileTooltipContent.className = 'mobile-tooltip-content';

    const [section, key] = tooltipKey.split('.');
    if (tooltips[section] && tooltips[section][key]) {
        mobileTooltipContent.innerHTML = `<p>${tooltips[section][key]}</p>`;
    } else {
        mobileTooltipContent.innerHTML = '<p>Tooltip content not found.</p>';
    }

    // Insert the tooltip right after the button
    button.parentNode.insertBefore(mobileTooltipContent, button.nextSibling);
    
    // Set initial styles
    mobileTooltipContent.style.opacity = '0';
    mobileTooltipContent.style.display = 'block';

    // Position the tooltip relative to the button
    positionTooltip(button, mobileTooltipContent);

    // Fade in after positioning
    requestAnimationFrame(() => {
        mobileTooltipContent.style.transition = `opacity ${TOOLTIP_FADE_IN}ms ease-in`;
        mobileTooltipContent.style.opacity = '0.75';
    });

    // Clear any existing scroll timeout
    if (scrollTimeout) {
        clearTimeout(scrollTimeout);
        scrollTimeout = null;
    }
}

function positionTooltip(button, tooltip) {
    const buttonRect = button.getBoundingClientRect();
    const parentRect = button.offsetParent.getBoundingClientRect();

    // Set initial position and styles
    tooltip.style.position = 'absolute';
    tooltip.style.maxWidth = '250px';
    tooltip.style.width = 'auto';

    // Calculate position relative to the button's parent
    const top = buttonRect.bottom - parentRect.top;
    const left = buttonRect.left - parentRect.left;

    // Position the tooltip
    tooltip.style.top = `${top}px`;
    tooltip.style.left = `${left}px`;

    // Adjust position if tooltip goes off-screen
    setTimeout(() => {
        const tooltipRect = tooltip.getBoundingClientRect();
        
        if (tooltipRect.right > window.innerWidth - TOOLTIP_PADDING) {
            tooltip.style.left = `${window.innerWidth - tooltipRect.width - TOOLTIP_PADDING - parentRect.left}px`;
        }
        
        if (tooltipRect.bottom > window.innerHeight - TOOLTIP_PADDING) {
            tooltip.style.top = `${buttonRect.top - parentRect.top - tooltipRect.height}px`;
        }
    }, 0);
}

function hideMobileTooltip(animate = true) {
    if (mobileTooltipContent) {
        if (animate) {
            mobileTooltipContent.style.transition = `opacity ${TOOLTIP_FADE_OUT}ms ease-out`;
            mobileTooltipContent.style.opacity = '0';
            setTimeout(() => {
                removeMobileTooltip();
            }, TOOLTIP_FADE_OUT);
        } else {
            removeMobileTooltip();
        }
    }
    // Clear any existing scroll timeout
    if (scrollTimeout) {
        clearTimeout(scrollTimeout);
        scrollTimeout = null;
    }
}

function removeMobileTooltip() {
    if (mobileTooltipContent && mobileTooltipContent.parentNode) {
        mobileTooltipContent.parentNode.removeChild(mobileTooltipContent);
    }
    mobileTooltipContent = null;
}

function initializeDatabaseTooltips() {
    console.log('Initializing database tooltips');
    const cells = document.querySelectorAll('.truncate');
    console.log(`Found ${cells.length} database cells`);
    cells.forEach(cell => {
        if (isMobileDevice()) {
            addMobileTooltipButton(cell);
        } else {
            // Add desktop event listeners - use the same showTooltip function
            cell.addEventListener('mouseenter', (event) => {
                // Create a temporary tooltip element if it doesn't exist
                if (!tooltipElement) {
                    tooltipElement = document.createElement('div');
                    tooltipElement.className = 'tooltip database-tooltip';
                    document.body.appendChild(tooltipElement);
                }

                // Use the cell's data-full-content attribute directly with a different delimiter
                cell.dataset.tooltip = `database|||${cell.getAttribute('data-full-content')}`;
                
                // Use the main tooltip show function
                showTooltip(event);
                
                // Clean up the temporary dataset entry
                delete cell.dataset.tooltip;
            });
            cell.addEventListener('mouseleave', hideTooltip);
        }
    });
    console.log('Database tooltips initialized');
}

function addMobileTooltipButton(cell) {
    const button = document.createElement('i');
    button.className = 'fas fa-ellipsis-h mobile-tooltip-button';
    button.setAttribute('aria-label', 'Show full content');
    cell.appendChild(button);

    button.addEventListener('click', (event) => {
        event.stopPropagation();
        showMobileDatabaseTooltip(cell, button);
    });
}

function showMobileDatabaseTooltip(cell, button) {
    const fullContent = cell.getAttribute('data-full-content');
    showMobileTooltip(`database|||${fullContent}`, button);
}

function initializeTooltips() {
    if (window.isRateLimited) {
        console.log("Rate limit exceeded. Skipping tooltips initialization.");
        return;
    }

    console.log('Initializing tooltips');
    console.log('Is mobile device?', isMobileDevice());

    fetchTooltips().then(() => {
        if (isMobileDevice()) {
            console.log('Mobile device detected. Setting up mobile tooltips.');
            createMobileTooltipButtons();
            console.log('Mobile tooltip buttons created');
        } else {
            console.log('Desktop device detected. Setting up desktop tooltips.');
            const pageName = getPageName();
            applyTooltipsToPage(pageName);
            console.log('Desktop tooltips initialized');
        }

        // Always initialize database tooltips
        initializeDatabaseTooltips();
    });

    // Close mobile tooltip when tapping outside
    document.addEventListener('click', (event) => {
        if (mobileTooltipContent && !event.target.classList.contains('mobile-tooltip-button')) {
            hideMobileTooltip();
        }
    });

    // Reposition tooltip on resize
    window.addEventListener('resize', () => {
        if (mobileTooltipContent) {
            const button = document.querySelector('.mobile-tooltip-button:hover');
            if (button) {
                positionTooltip(button, mobileTooltipContent);
            }
        }
    });
}

function applyTooltipsToPage(pageName) {
    console.log('Applying tooltips for page:', pageName);

    // Apply global tooltips
    if (tooltips.global) {
        applyTooltipsForSection('global', tooltips.global);
    }

    // Apply page-specific tooltips
    if (tooltips[pageName]) {
        applyTooltipsForSection(pageName, tooltips[pageName]);
    } else {
        console.log('No specific tooltips found for page:', pageName);
    }

    // Special case for database page
    if (pageName === 'database') {
        console.log('Initializing database tooltips');
        initializeDatabaseTooltips();
    }
}

function applyTooltipsForSection(sectionName, sectionTooltips) {
    Object.entries(sectionTooltips).forEach(([elementId, tooltipText]) => {
        const element = document.getElementById(elementId);
        if (element) {
            console.log(`Adding tooltip to element with ID: ${elementId}`);
            if (isMobileDevice()) {
                addMobileTooltipButton(element, tooltipText);
            } else {
                element.setAttribute('data-tooltip', `${sectionName}.${elementId}`);
                element.addEventListener('mouseenter', showTooltip);
                element.addEventListener('mouseleave', hideTooltip);
            }
        } else {
            console.log(`Element with ID ${elementId} not found for section ${sectionName}`);
        }
    });
}

function getPageName() {
    const path = window.location.pathname.replace(/\/$/, '');  // Remove trailing slash if present
    const pageName = path.split('/').pop() || 'home';
    console.log('Detected page name:', pageName);
    return pageName;
}

// Add this new function to handle user interactions
function handleInteraction(event) {
    if (event.type === 'scroll') {
        // For scroll events, wait a short delay before hiding
        if (scrollTimeout) {
            clearTimeout(scrollTimeout);
        }
        scrollTimeout = setTimeout(() => {
            hideMobileTooltip();
        }, SCROLL_HIDE_DELAY);
    } else {
        // For touch and click events, hide immediately
        // but only if the click is not on the tooltip or the info button
        if (!event.target.closest('.mobile-tooltip-content') && 
            !event.target.classList.contains('mobile-tooltip-button')) {
            hideMobileTooltip();
        }
    }
}

// Add styles for mobile tooltips
function addMobileTooltipStyles() {
    const style = document.createElement('style');
    style.textContent = `
        .mobile-tooltip-button {
            display: inline-block; /* Change this from 'none' to 'inline-block' */
            color: #FFA500;
            cursor: pointer;
            font-size: 1.2em;
            margin-left: 5px;
            vertical-align: middle;
        }

        .mobile-tooltip-content {
            position: absolute;
            background-color: rgba(0, 0, 0, 0.9);
            color: white;
            padding: 15px;
            border-radius: 5px;
            z-index: 1000;
            max-width: 250px;
            width: auto;
            max-height: 80vh;
            overflow-y: auto;
            box-shadow: 0 2px 10px rgba(0,0,0,0.2);
            font-size: 14px;
            line-height: 1.4;
            word-wrap: break-word;
            opacity: 0;
            transition: opacity ${TOOLTIP_FADE_IN}ms ease-in;
        }

        @media (max-width: 768px) {
            .mobile-tooltip-button {
                display: inline-block;
            }
            .mobile-tooltip-content {
                max-width: calc(100vw - 30px);
            }
        }

        .database-tooltip {
            white-space: normal;
            word-break: break-word;
        }

        .mobile-tooltip-button.fa-ellipsis-h {
            color: #007bff;
            font-size: 1em;
            margin-left: 5px;
        }
    `;
    document.head.appendChild(style);
}

// Call this function when the script loads
addMobileTooltipStyles();

function cleanupExistingTooltips() {
    // Remove existing tooltip elements
    const existingTooltips = document.querySelectorAll('.tooltip, .mobile-tooltip-content');
    existingTooltips.forEach(tooltip => tooltip.remove());

    // Remove existing event listeners
    const tooltipElements = document.querySelectorAll('[data-tooltip], .truncate');
    tooltipElements.forEach(element => {
        element.removeEventListener('mouseenter', showTooltip);
        element.removeEventListener('mouseleave', hideTooltip);
        element.removeEventListener('mouseenter', showDatabaseTooltip);
    });

    // Remove mobile tooltip buttons
    const mobileTooltipButtons = document.querySelectorAll('.mobile-tooltip-button');
    mobileTooltipButtons.forEach(button => button.remove());

    // Reset global variables
    tooltipElement = null;
    mobileTooltipContent = null;
    activeTooltipElement = null;
}

// Add these functions to control content updating
function setUpdatingContent(value) {
    isUpdatingContent = value;
}

export { initializeTooltips, setUpdatingContent, initializeDatabaseTooltips };
