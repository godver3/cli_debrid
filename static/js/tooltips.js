let tooltips = {};
let tooltipElement = null;
let tooltipTimeout = null;
let hideTooltipTimeout = null;
let lastMousePosition = { x: 0, y: 0 };
const TOOLTIP_DELAY = 500; // Delay in milliseconds (0.5 seconds)
const TOOLTIP_FADE_IN = 300; // Fade in duration in milliseconds
const TOOLTIP_FADE_OUT = 150; // Fade out duration in milliseconds
const TOOLTIP_PADDING = 10; // Padding from screen edges

let activeTooltipElement = null;

let mobileTooltipContent = null;

let scrollTimeout = null;
const SCROLL_HIDE_DELAY = 100; // ms to wait after scrolling stops before hiding tooltip

function isMobileDevice() {
    return (typeof window.orientation !== "undefined") || 
           (navigator.userAgent.indexOf('IEMobile') !== -1) ||
           ('ontouchstart' in window) ||
           (navigator.maxTouchPoints > 0) ||
           (navigator.msMaxTouchPoints > 0);
}

async function fetchTooltips() {
    try {
        const response = await fetch('/tooltip/tooltips');
        tooltips = await response.json();
        console.log('Fetched tooltips:', tooltips);
    } catch (error) {
        console.error('Failed to fetch tooltips:', error);
    }
}

function showTooltip(event) {
    if (isMobileDevice()) return;

    const element = event.currentTarget;
    const tooltipKey = element.dataset.tooltip;
    console.log('Preparing to show tooltip for:', tooltipKey);
    
    // Clear any existing timeouts
    if (tooltipTimeout) {
        clearTimeout(tooltipTimeout);
    }
    if (hideTooltipTimeout) {
        clearTimeout(hideTooltipTimeout);
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
    updateTooltipPosition(event);

    // Start updating position immediately
    const updatePosition = (e) => updateTooltipPosition(e);
    document.addEventListener('mousemove', updatePosition);

    activeTooltipElement = element;

    tooltipTimeout = setTimeout(() => {
        if (activeTooltipElement !== element) return; // Don't show if we've moved to another element

        const [page, key] = tooltipKey.split('.');
        
        if (tooltips[page] && tooltips[page][key]) {
            const tooltipText = tooltips[page][key];
            
            tooltipElement.textContent = tooltipText;
            tooltipElement.style.transition = `opacity ${TOOLTIP_FADE_IN}ms ease-in`;
            
            // Force a reflow before changing the opacity
            tooltipElement.offsetHeight;
            tooltipElement.style.opacity = '1';
            
            console.log('Tooltip displayed:', tooltipText);
        } else {
            console.log('Tooltip not found for:', tooltipKey);
            tooltipElement.style.display = 'none';
            document.removeEventListener('mousemove', updatePosition);
        }
    }, TOOLTIP_DELAY);

    // Update hideTooltip to remove the mousemove listener
    element.hideTooltip = function() {
        document.removeEventListener('mousemove', updatePosition);
        hideTooltip();
    };
    element.addEventListener('mouseleave', element.hideTooltip);
}

function updateTooltipPosition(event) {
    if (!tooltipElement) return;

    const offset = 10; // Distance from the cursor
    let x = event.pageX + offset;
    let y = event.pageY + offset;

    // Ensure the tooltip doesn't go off-screen
    const tooltipRect = tooltipElement.getBoundingClientRect();
    const maxX = window.innerWidth + window.pageXOffset - tooltipRect.width - TOOLTIP_PADDING;
    const maxY = window.innerHeight + window.pageYOffset - tooltipRect.height - TOOLTIP_PADDING;

    // If tooltip would go off the right side, position it to the left of the cursor
    if (x > maxX) {
        x = event.pageX - tooltipRect.width - offset;
    }

    x = Math.max(window.pageXOffset + TOOLTIP_PADDING, Math.min(x, maxX));
    y = Math.max(window.pageYOffset + TOOLTIP_PADDING, Math.min(y, maxY));

    // Position relative to the document
    tooltipElement.style.position = 'absolute';
    tooltipElement.style.left = `${x}px`;
    tooltipElement.style.top = `${y}px`;
}

function hideTooltip() {
    if (tooltipTimeout) {
        clearTimeout(tooltipTimeout);
        tooltipTimeout = null;
    }
    if (hideTooltipTimeout) {
        clearTimeout(hideTooltipTimeout);
    }
    hideTooltipTimeout = setTimeout(() => {
        if (tooltipElement) {
            tooltipElement.style.transition = `opacity ${TOOLTIP_FADE_OUT}ms ease-out`;
            tooltipElement.style.opacity = '0';
            setTimeout(() => {
                tooltipElement.style.display = 'none';
                activeTooltipElement = null;
            }, TOOLTIP_FADE_OUT);
            console.log('Tooltip hidden');
        }
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

    const [page, key] = tooltipKey.split('.');
    if (tooltips[page] && tooltips[page][key]) {
        mobileTooltipContent.innerHTML = `<p>${tooltips[page][key]}</p>`;
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

function initializeTooltips() {
    console.log('Initializing tooltips');
    console.log('Is mobile device?', isMobileDevice());
    if (isMobileDevice()) {
        console.log('Mobile device detected. Setting up mobile tooltips.');
        fetchTooltips().then(() => {
            createMobileTooltipButtons();
            console.log('Mobile tooltip buttons created');
        });

        // Add event listeners for scroll, touch, and click
        window.addEventListener('scroll', handleInteraction);
        document.addEventListener('touchstart', handleInteraction);
        document.addEventListener('click', handleInteraction);
    } else {
        console.log('Desktop device detected. Setting up desktop tooltips.');
        fetchTooltips();
        
        const tooltipElements = document.querySelectorAll('[data-tooltip]');
        
        tooltipElements.forEach(element => {
            element.addEventListener('mouseenter', showTooltip);
        });
    }

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
    `;
    document.head.appendChild(style);
}

// Call this function when the script loads
addMobileTooltipStyles();

export { initializeTooltips };