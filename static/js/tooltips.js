let tooltips = {};
let tooltipElement = null;
let tooltipTimeout = null;
let hideTooltipTimeout = null;
let actualHideProcessTimeout = null;
let lastMousePosition = { x: 0, y: 0 };
const TOOLTIP_DELAY = 500; // Delay in milliseconds (0.5 seconds)
const TOOLTIP_FADE_IN = 250; // Fade in duration in milliseconds
const TOOLTIP_FADE_OUT = 600; // Fade out duration in milliseconds
const TOOLTIP_PADDING = 10; // Padding from screen edges
const HIDE_GRACE_PERIOD = 75; // ms, short delay before hide starts

let activeTooltipElement = null;
let currentTooltipKeyForHide = null; // Store the key that a hide was initiated for

let mobileTooltipContent = null;

let scrollTimeout = null;
const SCROLL_HIDE_DELAY = 100; // ms to wait after scrolling stops before hiding tooltip

let isUpdatingContent = false;

// Declare finalPosition at a higher scope
let finalPosition = { x: 0, y: 0 };
let isMoving = false;
let moveTimer = null;

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

function handleGlobalMouseMove(e) {
    finalPosition = { x: e.pageX, y: e.pageY };
    if (!isMoving) {
        isMoving = true;
        if (tooltipTimeout && activeTooltipElement) { 
            clearTimeout(tooltipTimeout);
            tooltipTimeout = null;
        }
    }
    if (moveTimer) clearTimeout(moveTimer);
    moveTimer = setTimeout(() => {
        isMoving = false;
        if (activeTooltipElement && 
            tooltipElement && 
            tooltipElement.dataset.currentKey &&
            tooltipElement.dataset.isHiding !== 'true' && 
            !actualHideProcessTimeout) { 

            if (tooltipTimeout) clearTimeout(tooltipTimeout);
            console.log("Mouse stopped, scheduling show for:", tooltipElement.dataset.currentKey);
            tooltipTimeout = setTimeout(() => showTooltipContentInternal(activeTooltipElement, tooltipElement.dataset.currentKey, true), TOOLTIP_DELAY);
        } else if (tooltipElement && tooltipElement.dataset.isHiding === 'true') {
            console.log("Mouse stopped, but tooltip is flagged as 'isHiding'. Not scheduling show.");
        } else if (actualHideProcessTimeout) {
            console.log("Mouse stopped, but tooltip is in actualHideProcess. Not scheduling show.");
        }
    }, 100);
}

function showTooltipContentInternal(elementForTooltip, keyForTooltip, animate) {
    if (!tooltipElement || activeTooltipElement !== elementForTooltip || tooltipElement.dataset.currentKey !== keyForTooltip) {
        if (activeTooltipElement === null && tooltipElement && tooltipElement.dataset.currentKey === keyForTooltip && tooltipElement.dataset.isHiding === 'true') {
            console.log('showTooltipContentInternal: Aborted because tooltip is marked as hiding, even if keys match.');
            return;
        }
        console.log('showTooltipContentInternal: Aborted due to mismatched active element/key.');
        return;
    }
    
    tooltipElement.removeAttribute('data-is-hiding'); 
    
    let tooltipText;
    if (keyForTooltip) {
        if (keyForTooltip.startsWith('database|||')) {
            tooltipText = keyForTooltip.split('|||')[1];
            tooltipElement.className = 'tooltip database-tooltip';
        } else {
            const [section, key] = keyForTooltip.split('.');
            if (tooltips && tooltips[section] && tooltips[section][key]) {
                tooltipText = tooltips[section][key];
            } else {
                console.warn(`Tooltip content not found for ${keyForTooltip}. Section: '${section}', Key: '${key}'. Tooltips loaded:`, Object.keys(tooltips || {}).length > 0);
                tooltipText = 'Info not available'; 
            }
            tooltipElement.className = 'tooltip';
        }
    } else {
        console.log('No tooltip content key provided to showTooltipContentInternal.');
        if (tooltipElement) tooltipElement.style.display = 'none';
        return;
    }
    
    tooltipElement.textContent = tooltipText;

    tooltipElement.style.visibility = 'hidden'; 
    tooltipElement.style.display = 'block';    
    positionTooltipAtPoint(finalPosition.x, finalPosition.y); 
    tooltipElement.style.visibility = 'visible'; 

    if (animate) {
        console.log(`Tooltip displayed (animate: true) for ${keyForTooltip}`);
        tooltipElement.style.transition = `opacity ${TOOLTIP_FADE_IN}ms ease-in`;
        tooltipElement.style.opacity = '1';
    } else {
        console.log(`Tooltip displayed (animate: false - snap) for ${keyForTooltip}`);
        tooltipElement.style.transition = 'none'; 
        tooltipElement.style.opacity = '1';
        requestAnimationFrame(() => { 
            if (tooltipElement) {
                tooltipElement.style.transition = `opacity ${TOOLTIP_FADE_OUT}ms ease-out`;
            }
        });
    }
}

function showTooltip(event) {
    if (isMobileDevice() || isUpdatingContent) return;

    const element = event.currentTarget;
    const tooltipKey = element.dataset.tooltip;
    
    console.log(`Preparing to show tooltip for: ${tooltipKey}`);

    if (tooltipElement) {
        tooltipElement.removeAttribute('data-is-hiding'); 
    }

    // Scenario 1: Re-affirming an already visible tooltip for the SAME key
    if (tooltipElement && 
        tooltipElement.dataset.currentKey === tooltipKey && 
        tooltipElement.style.display !== 'none' && 
        parseFloat(tooltipElement.style.opacity) > 0.1) {

        console.log(`ShowTooltip: Re-affirming (Scenario 1) for key "${tooltipKey}".`);
        clearTimeout(hideTooltipTimeout); 
        clearTimeout(actualHideProcessTimeout); 
        hideTooltipTimeout = null;
        actualHideProcessTimeout = null;
        currentTooltipKeyForHide = null;

        if (tooltipTimeout) clearTimeout(tooltipTimeout);
        tooltipTimeout = null;

        activeTooltipElement = element; 
        if (element.hideTooltipHandler) element.removeEventListener('mouseleave', element.hideTooltipHandler);
        document.removeEventListener('mousemove', handleGlobalMouseMove);
        document.addEventListener('mousemove', handleGlobalMouseMove);
        element.hideTooltipHandler = () => {
            document.removeEventListener('mousemove', handleGlobalMouseMove);
            hideTooltip(element, tooltipKey);
        };
        element.addEventListener('mouseleave', element.hideTooltipHandler);
        
        showTooltipContentInternal(element, tooltipKey, false);
        return;
    }

    // Scenario 2: Intercept a PENDING HIDE (grace period) for this exact key
    if (hideTooltipTimeout && currentTooltipKeyForHide === tooltipKey) {
        console.log(`ShowTooltip: Intercepting PENDING hide (Scenario 2) for key "${tooltipKey}".`);
        clearTimeout(hideTooltipTimeout);
        hideTooltipTimeout = null;
        
        if (actualHideProcessTimeout) { 
            clearTimeout(actualHideProcessTimeout);
            actualHideProcessTimeout = null;
        }
        
        if (tooltipTimeout) clearTimeout(tooltipTimeout); 
        tooltipTimeout = null;

        activeTooltipElement = element;
        if (!tooltipElement) { 
            tooltipElement = document.createElement('div');
            document.body.appendChild(tooltipElement);
            tooltipElement.style.opacity = '0';
        }
        tooltipElement.style.display = 'block';
        tooltipElement.style.visibility = 'visible';
        tooltipElement.dataset.currentKey = tooltipKey;

        if (element.hideTooltipHandler) element.removeEventListener('mouseleave', element.hideTooltipHandler);
        document.removeEventListener('mousemove', handleGlobalMouseMove);
        document.addEventListener('mousemove', handleGlobalMouseMove);
        element.hideTooltipHandler = () => {
            document.removeEventListener('mousemove', handleGlobalMouseMove);
            hideTooltip(element, tooltipKey);
        };
        element.addEventListener('mouseleave', element.hideTooltipHandler);

        const wasMostlyVisible = parseFloat(tooltipElement.style.opacity) > 0.1;
        showTooltipContentInternal(element, tooltipKey, !wasMostlyVisible); 
        return; 
    }

    // Normal new tooltip show path
    console.log(`ShowTooltip: Normal path for key "${tooltipKey}".`);
    if (tooltipTimeout) clearTimeout(tooltipTimeout);
    if (hideTooltipTimeout) clearTimeout(hideTooltipTimeout); 
    if (actualHideProcessTimeout) clearTimeout(actualHideProcessTimeout);

    currentTooltipKeyForHide = null; 

    if (!tooltipElement) {
        tooltipElement = document.createElement('div');
        document.body.appendChild(tooltipElement);
    }

    tooltipElement.style.opacity = '0'; 
    tooltipElement.style.display = 'block';
    tooltipElement.style.visibility = 'visible';
    tooltipElement.dataset.currentKey = tooltipKey;
    tooltipElement.removeAttribute('data-is-hiding');


    activeTooltipElement = element;
    finalPosition = { x: event.pageX, y: event.pageY }; 

    document.removeEventListener('mousemove', handleGlobalMouseMove);
    document.addEventListener('mousemove', handleGlobalMouseMove);
    
    if (element.hideTooltipHandler) element.removeEventListener('mouseleave', element.hideTooltipHandler);
    element.hideTooltipHandler = () => {
        document.removeEventListener('mousemove', handleGlobalMouseMove);
        hideTooltip(element, tooltipKey);
    };
    element.addEventListener('mouseleave', element.hideTooltipHandler);

    tooltipTimeout = setTimeout(() => showTooltipContentInternal(element, tooltipKey, true), TOOLTIP_DELAY);
}

function hideTooltip(originatingElement = null, originatingKey = null) {
    if (isUpdatingContent) return;

    if (tooltipTimeout && activeTooltipElement === originatingElement && tooltipElement && tooltipElement.dataset.currentKey === originatingKey) {
        clearTimeout(tooltipTimeout);
        tooltipTimeout = null;
    }
    
    if ((hideTooltipTimeout && currentTooltipKeyForHide === originatingKey) || actualHideProcessTimeout) {
         // If an actual fade out is in progress FOR ANY KEY, or grace period for THIS key, let it be.
        if (actualHideProcessTimeout) {
             console.log("hideTooltip: Aborted, an actualHideProcessTimeout is already active.");
             return;
        }
        if (hideTooltipTimeout && currentTooltipKeyForHide === originatingKey) {
            console.log("hideTooltip: Aborted, grace period already active for this key.");
            return;
        }
    }

    if (!tooltipElement || tooltipElement.style.display === 'none') return;
    if (originatingKey && tooltipElement.dataset.currentKey !== originatingKey && parseFloat(tooltipElement.style.opacity) > 0.1) {
        console.log(`hideTooltip: Aborted. Originating key ${originatingKey} != current key ${tooltipElement.dataset.currentKey}`);
        return;
    }

    const keyToHide = originatingKey || (tooltipElement ? tooltipElement.dataset.currentKey : null);
    if (!keyToHide) {
        console.log("hideTooltip: No key to hide. Aborting.");
        return;
    }
    console.log(`hideTooltip: Starting grace period for: ${keyToHide}`);
    currentTooltipKeyForHide = keyToHide;

    if (hideTooltipTimeout) clearTimeout(hideTooltipTimeout); // Clear any old grace period

    hideTooltipTimeout = setTimeout(() => {
        hideTooltipTimeout = null; 
        
        if (!tooltipElement || tooltipElement.dataset.currentKey !== keyToHide) {
             console.log(`hideTooltip (grace expired): Aborted. Key changed from ${keyToHide} to ${tooltipElement.dataset.currentKey}, or tooltip gone.`);
             currentTooltipKeyForHide = null;
             return;
        }
        
        console.log(`hideTooltip (grace expired): Starting actual fade out for ${keyToHide}.`);
        
        tooltipElement.dataset.isHiding = 'true'; // Mark as actively hiding
        
        // Important: If this hide is for the currently active element, nullify activeTooltipElement
        // This prevents handleGlobalMouseMove from re-showing it.
        if (activeTooltipElement === originatingElement) {
            activeTooltipElement = null; 
            console.log("hideTooltip: activeTooltipElement nulled for", originatingKey);
        }
        
        tooltipElement.style.transition = `opacity ${TOOLTIP_FADE_OUT}ms ease-out`;
        tooltipElement.style.opacity = '0';
        
        currentTooltipKeyForHide = null; 

        if (actualHideProcessTimeout) clearTimeout(actualHideProcessTimeout); // Clear any previous lingering one
        actualHideProcessTimeout = setTimeout(() => {
            const currentActualHideId = actualHideProcessTimeout;
            
            // Check if it's still marked as hiding THIS key and opacity is low
            if (tooltipElement && tooltipElement.dataset.isHiding === 'true' && tooltipElement.dataset.currentKey === keyToHide && parseFloat(tooltipElement.style.opacity) < 0.1) {
                tooltipElement.style.display = 'none';
                tooltipElement.removeAttribute('data-current-key'); 
                tooltipElement.removeAttribute('data-is-hiding');   
                console.log(`hideTooltip (fade complete): Display none for key ${keyToHide}`);
            } else if (tooltipElement) {
                console.log(`hideTooltip (fade complete): Display:none aborted for ${keyToHide}. Opacity: ${tooltipElement.style.opacity}, IsHiding: ${tooltipElement.dataset.isHiding}, CurrentKey: ${tooltipElement.dataset.currentKey}`);
                if (parseFloat(tooltipElement.style.opacity) < 0.1 && tooltipElement.dataset.isHiding === 'true') { // Still hide if low opacity and marked
                    tooltipElement.style.display = 'none';
                    tooltipElement.removeAttribute('data-is-hiding');
                    console.log("hideTooltip (fade complete): Forced display:none for low opacity ghost marked as hiding.");
                } else if (tooltipElement.dataset.isHiding !== 'true' && parseFloat(tooltipElement.style.opacity) > 0.9){
                    console.log("hideTooltip (fade complete): A new tooltip likely took over. Not setting display:none for old key.");
                }

            }
            if (actualHideProcessTimeout === currentActualHideId) { // Only clear if it's this instance
                actualHideProcessTimeout = null;
            }
        }, TOOLTIP_FADE_OUT);

    }, HIDE_GRACE_PERIOD);
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

    // Add scroll event listener for both mobile and desktop tooltips
    window.addEventListener('scroll', () => {
        if (isMobileDevice()) {
            hideMobileTooltip();
        } else {
            hideTooltip();
        }
    }, { passive: true });

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
        // Hide immediately on scroll
        hideMobileTooltip();
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

function addTooltipEventListeners(element) {
    if (element && element.dataset && element.dataset.tooltip) {
        if (!isMobileDevice()) {
            // Ensure we don't add duplicate listeners if called multiple times on the same element
            element.removeEventListener('mouseenter', showTooltip); 
            element.addEventListener('mouseenter', showTooltip);

            // The hideTooltip function is designed to be called with the element and its key.
            // We create a specific handler for mouseleave.
            const specificMouseLeaveHandler = (event) => {
                // When mouse leaves, call hideTooltip with the element and its tooltip key
                hideTooltip(event.currentTarget, event.currentTarget.dataset.tooltip);
            };
            
            // To prevent multiple listeners if this function is called again for the same element,
            // we store a reference to the handler and remove it first.
            if (element.specificMouseLeaveHandlerRef) {
                 element.removeEventListener('mouseleave', element.specificMouseLeaveHandlerRef);
            }
            element.specificMouseLeaveHandlerRef = specificMouseLeaveHandler;
            element.addEventListener('mouseleave', specificMouseLeaveHandler);

            console.log(`Tooltip listeners dynamically added for: ${element.dataset.tooltip}`);
        }
    } else {
        // This console log can help debug if elements without tooltips are being processed
        // console.warn("addTooltipEventListeners called on invalid element or element without data-tooltip", element);
    }
}

export { initializeTooltips, setUpdatingContent, initializeDatabaseTooltips, addTooltipEventListeners };
