// Popup types
export const POPUP_TYPES = {
    SUCCESS: 'success',
    ERROR: 'error',
    INFO: 'info',
    WARNING: 'warning',
    CONFIRM: 'confirm',
    PROMPT: 'prompt'
};

// Show a universal popup
export function showPopup(options) {
    const {
        type = POPUP_TYPES.INFO,
        message,
        title,
        confirmText = 'Confirm',
        cancelText = 'Cancel',
        extraButtonText,  // Optional 3rd action button, shown between Confirm and Cancel
        onExtra,
        suppressEscapeAction = false,  // If true, Escape just closes the popup instead of triggering Cancel
        inputPlaceholder,
        dropdownOptions,
        formHtml,  // New option for custom form HTML
        onConfirm,
        onCancel,
        autoClose = 5000,
        hiddenCloseButton = false,  // New option for hidden close button
        onHiddenClose = null  // Callback for hidden close button
    } = options;

    // Remove existing non-interactive popups (success/error/info/warning) before showing a new one
    if (type !== POPUP_TYPES.CONFIRM && type !== POPUP_TYPES.PROMPT) {
        const existingPopups = document.querySelectorAll('.universal-popup');
        existingPopups.forEach(existing => {
            const popupContent = existing.querySelector('.popup-content');
            // Only remove if it's a non-interactive popup (no buttons except close)
            if (popupContent && !popupContent.classList.contains('confirm') && !popupContent.classList.contains('prompt')) {
                existing.remove();
            }
        });
    }

    const popup = document.createElement('div');
    popup.className = 'universal-popup';
    
    let content = `
        <div class="popup-content ${type}">
            <h3>${title || capitalizeFirstLetter(type)}</h3>
            <p>${message}</p>
    `;

    if (type === POPUP_TYPES.PROMPT) {
        if (formHtml) {
            content += formHtml;  // Use custom form HTML if provided
        } else if (dropdownOptions) {
            content += `
                <select id="popupDropdown">
                    ${dropdownOptions.map(option => `<option value="${option.value}">${option.text}</option>`).join('')}
                </select>`;
        } else {
            content += `<input type="text" id="popupInput" placeholder="${inputPlaceholder || ''}">`;
        }
    }

    if (type === POPUP_TYPES.CONFIRM || type === POPUP_TYPES.PROMPT) {
        content += `
            <div class="popup-buttons">
                <button id="popupConfirm">${confirmText}</button>
                ${extraButtonText ? `<button id="popupExtra">${extraButtonText}</button>` : ''}
                <button id="popupCancel">${cancelText}</button>
            </div>
        `;
    } else {
        content += `<div class="popup-buttons"><button id="popupClose">Close</button></div>`;
    }
    
    // Add hidden close button if requested
    if (hiddenCloseButton) {
        content += `<span id="hiddenCloseButton" style="position: absolute; top: 10px; right: 15px; cursor: pointer; color: #aaa; font-size: 24px; font-weight: bold; line-height: 1; opacity: 0;">&times;</span>`;
    }

    content += `</div>`;
    popup.innerHTML = content;
    document.body.appendChild(popup);

    // Add styles
    const style = document.createElement('style');
    style.textContent = `
        .universal-popup {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0, 0, 0, 0.7);
            display: flex;
            justify-content: center;
            align-items: center;
            z-index: 1000;
        }
        .universal-popup .popup-content {
            background-color: #2a2a2a;
            padding: 35px;
            border-radius: 8px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
            max-width: 80%;
            max-height: 80%;
            overflow-y: auto;
            color: #f4f4f4;
        }
        .universal-popup .popup-content h3 {
            margin: 0 0 15px;
            font-size: 1.2em;
            text-align: center;
        }
        .universal-popup .popup-content p {
            margin-bottom: 15px;
        }
        .universal-popup .popup-buttons {
            display: flex;
            justify-content: center;
            gap: 10px;
            margin-top: 20px;
        }
        .universal-popup button {
            padding: 8px 15px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
            transition: background-color 0.3s;
        }
        .universal-popup button:hover {
            opacity: 0.9;
        }
        .universal-popup #popupConfirm {
            background-color: #4CAF50;
            color: white;
        }
        .universal-popup #popupExtra {
            background-color: #2196F3;
            color: white;
        }
        .universal-popup #popupCancel, .universal-popup #popupClose {
            background-color: #f44336;
            color: white;
        }
        .universal-popup #popupInput {
            width: 100%;
            padding: 8px;
            margin-bottom: 15px;
            background-color: #333;
            border: 1px solid #555;
            color: #f4f4f4;
            border-radius: 4px;
        }
        .universal-popup .popup-content.error { border-left-color: #f44336; }
        .universal-popup .popup-content.success { border-left-color: #4CAF50; }
        .universal-popup .popup-content.info { border-left-color: #2196F3; }
        .universal-popup .popup-content.warning { border-left-color: #FF9800; }
        .universal-popup .popup-content.error h3 { color: #f44336; }
        .universal-popup .popup-content.success h3 { color: #4CAF50; }
        .universal-popup .popup-content.info h3 { color: #2196F3; }
        .universal-popup .popup-content.warning h3 { color: #FF9800; }
        .universal-popup #popupDropdown {
            width: 100%;
            padding: 8px;
            margin-bottom: 15px;
            background-color: #333;
            border: 1px solid #555;
            color: #f4f4f4;
            border-radius: 4px;
        }
        .universal-popup form {
            width: 100%;
        }
        .universal-popup form input[type="text"],
        .universal-popup form input[type="email"],
        .universal-popup form input[type="number"],
        .universal-popup form input[type="password"],
        .universal-popup form textarea,
        .universal-popup form select {
            width: 100%;
            padding: 8px;
            margin-bottom: 15px;
            background-color: #333;
            border: 1px solid #555;
            color: #f4f4f4;
            border-radius: 4px;
            font-size: 14px;
        }
        .universal-popup form label {
            display: block;
            margin-bottom: 5px;
            color: #f4f4f4;
            font-size: 14px;
        }
        .universal-popup form input[type="checkbox"],
        .universal-popup form input[type="radio"] {
            margin-right: 5px;
        }
        .universal-popup form fieldset {
            border: 1px solid #555;
            padding: 10px;
            margin-bottom: 15px;
            border-radius: 4px;
        }
        .universal-popup form legend {
            padding: 0 5px;
            color: #f4f4f4;
        }
    `;
    document.head.appendChild(style);

    function closePopup() {
        popup.remove();
        style.remove();
        document.removeEventListener('keydown', handleKeyPress);
    }

    function handleKeyPress(event) {
        if (event.key === 'Enter') {
            event.preventDefault(); // Prevent default form submission
            const confirmButton = popup.querySelector('#popupConfirm');
            if (confirmButton) {
                confirmButton.click();
            } else {
                const closeButton = popup.querySelector('#popupClose');
                if (closeButton) {
                    closeButton.click();
                }
            }
        } else if (event.key === 'Escape') {
            if (suppressEscapeAction) {
                closePopup();
                return;
            }
            const cancelButton = popup.querySelector('#popupCancel');
            if (cancelButton) {
                cancelButton.click();
            } else {
                const closeButton = popup.querySelector('#popupClose');
                if (closeButton) {
                    closeButton.click();
                }
            }
        }
    }

    popup.addEventListener('submit', (e) => e.preventDefault());

    document.addEventListener('keydown', handleKeyPress);

    if (type === POPUP_TYPES.CONFIRM || type === POPUP_TYPES.PROMPT) {
        const confirmButton = popup.querySelector('#popupConfirm');
        const cancelButton = popup.querySelector('#popupCancel');

        if (confirmButton) {
            confirmButton.addEventListener('click', () => {
                let inputValue;
                if (type === POPUP_TYPES.PROMPT) {
                    if (formHtml) {
                        // If custom form, collect all input values
                        const form = popup.querySelector('form');
                        inputValue = Object.fromEntries(new FormData(form));
                    } else if (dropdownOptions) {
                        inputValue = popup.querySelector('#popupDropdown').value;
                    } else {
                        inputValue = popup.querySelector('#popupInput').value;
                    }
                }
                if (onConfirm) onConfirm(inputValue);
                closePopup();
            });
        }

        if (cancelButton) {
            cancelButton.addEventListener('click', () => {
                if (onCancel) onCancel();
                closePopup();
            });
        }

        const extraButton = popup.querySelector('#popupExtra');
        if (extraButton) {
            extraButton.addEventListener('click', () => {
                if (onExtra) onExtra();
                closePopup();
            });
        }
    } else {
        const closeButton = popup.querySelector('#popupClose');
        if (closeButton) {
            closeButton.addEventListener('click', closePopup);
        }
        if (autoClose) {
            setTimeout(closePopup, autoClose);
        }
    }
    
    // Add event listener for hidden close button if it exists
    const hiddenCloseButtonElement = popup.querySelector('#hiddenCloseButton');
    if (hiddenCloseButtonElement) {
        hiddenCloseButtonElement.addEventListener('click', () => {
            if (onHiddenClose) {
                onHiddenClose();
            }
            closePopup();
        });
    }
}

// Helper function to capitalize the first letter of a string
function capitalizeFirstLetter(string) {
    return string.charAt(0).toUpperCase() + string.slice(1);
}

// Loading spinner popup
let loadingPopup = null;

export function showLoading(message = 'Loading...') {
    // Remove any existing loading popup
    if (loadingPopup) {
        loadingPopup.remove();
    }

    loadingPopup = document.createElement('div');
    loadingPopup.className = 'universal-popup loading-popup';

    loadingPopup.innerHTML = `
        <div class="popup-content loading">
            <div class="loading-spinner"></div>
            <p>${message}</p>
        </div>
    `;

    document.body.appendChild(loadingPopup);

    // Add loading spinner styles
    const style = document.createElement('style');
    style.id = 'loading-popup-style';
    style.textContent = `
        .loading-popup {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0, 0, 0, 0.7);
            display: flex;
            justify-content: center;
            align-items: center;
            z-index: 1001;
        }
        .loading-popup .popup-content {
            background-color: #2a2a2a;
            padding: 35px;
            border-radius: 8px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
            max-width: 400px;
            color: #f4f4f4;
            text-align: center;
        }
        .loading-spinner {
            width: 50px;
            height: 50px;
            margin: 0 auto 20px;
            border: 4px solid rgba(255, 255, 255, 0.3);
            border-top: 4px solid #ff6b35;
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        .loading-popup .popup-content p {
            margin: 0;
            font-size: 1.1em;
            color: #f4f4f4;
        }

        /* Tangerine theme overrides */
        body[data-theme="tangerine"] .loading-popup {
            background-color: rgba(0, 0, 0, 0.85) !important;
            backdrop-filter: blur(8px);
        }
        body[data-theme="tangerine"] .loading-popup .popup-content {
            background: linear-gradient(145deg, rgba(30, 30, 30, 0.98), rgba(20, 20, 20, 0.98)) !important;
            border: 1px solid rgba(255, 107, 53, 0.3) !important;
            border-radius: 16px !important;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6), 0 0 0 1px rgba(255, 107, 53, 0.1) !important;
            padding: 2.5rem !important;
        }
        body[data-theme="tangerine"] .loading-spinner {
            border: 4px solid rgba(255, 107, 53, 0.2) !important;
            border-top: 4px solid #ff6b35 !important;
            width: 60px;
            height: 60px;
        }
        body[data-theme="tangerine"] .loading-popup .popup-content p {
            color: rgba(255, 255, 255, 0.9) !important;
            font-size: 1.1rem !important;
            font-weight: 500 !important;
        }
    `;
    document.head.appendChild(style);

    return loadingPopup;
}

export function hideLoading() {
    if (loadingPopup) {
        loadingPopup.remove();
        loadingPopup = null;
    }
    const style = document.getElementById('loading-popup-style');
    if (style) {
        style.remove();
    }
}

// Deletion-specific loading box with progress tracking
let deletionLoadingPopup = null;

// Store cleanup callback for continue button
let deletionLoadingCleanup = null;

export function showDeletionLoading(title = 'Deleting...', onContinueBackground = null) {
    // Remove any existing deletion loading popup
    if (deletionLoadingPopup) {
        deletionLoadingPopup.remove();
    }

    // Store cleanup callback
    deletionLoadingCleanup = onContinueBackground;

    deletionLoadingPopup = document.createElement('div');
    deletionLoadingPopup.className = 'universal-popup deletion-loading-popup';

    deletionLoadingPopup.innerHTML = `
        <div class="popup-content deletion-loading">
            <h3>${title}</h3>
            <div class="spinner"></div>
            <p id="deletionCurrentStep">Preparing deletion...</p>
            <div id="deletionProgress" style="margin-top: 15px; font-size: 0.9em; color: #aaa;">
                <span id="deletionProgressText"></span>
            </div>
            <pre id="deletionDetails" style="text-align: left; margin-top: 15px; white-space: pre-wrap; color: #ccc; font-size: 0.85em; display: none;"></pre>
            <button id="deletionContinueBackground" class="close-loading" style="margin-top: 20px; display: none; padding: 10px 20px; background: #ff6b35; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 0.95em;">Continue in background</button>
        </div>
    `;

    document.body.appendChild(deletionLoadingPopup);

    // Add deletion loading styles
    const style = document.createElement('style');
    style.id = 'deletion-loading-popup-style';
    style.textContent = `
        .deletion-loading-popup {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0, 0, 0, 0.7);
            display: flex;
            justify-content: center;
            align-items: center;
            z-index: 1001;
        }
        .deletion-loading-popup .popup-content {
            background-color: #2a2a2a;
            padding: 35px;
            border-radius: 8px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
            min-width: 400px;
            max-width: 500px;
            color: #f4f4f4;
            text-align: center;
        }
        .deletion-loading-popup .popup-content h3 {
            margin: 0 0 20px 0;
            font-size: 1.3em;
            color: #ff6b35;
        }
        .deletion-loading-popup #deletionCurrentStep {
            margin: 20px 0 0 0;
            font-size: 1.05em;
            color: #f4f4f4;
            min-height: 24px;
        }
        .deletion-loading-popup #deletionProgressText {
            color: #aaa;
            font-style: italic;
        }
        .deletion-loading-popup .spinner {
            width: 40px;
            height: 40px;
            margin: 0 auto 15px;
            border: 4px solid #f3f3f3;
            border-top: 4px solid #ff6b35;
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }

        /* Tangerine theme overrides */
        body[data-theme="tangerine"] .deletion-loading-popup {
            background-color: rgba(0, 0, 0, 0.85) !important;
            backdrop-filter: blur(8px);
        }
        body[data-theme="tangerine"] .deletion-loading-popup .popup-content {
            background: linear-gradient(145deg, rgba(30, 30, 30, 0.98), rgba(20, 20, 20, 0.98)) !important;
            border: 1px solid rgba(255, 107, 53, 0.3) !important;
            border-radius: 16px !important;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6), 0 0 0 1px rgba(255, 107, 53, 0.1) !important;
            padding: 2.5rem !important;
        }
    `;
    document.head.appendChild(style);

    // Add click handler for continue in background button
    const continueButton = deletionLoadingPopup.querySelector('#deletionContinueBackground');
    if (continueButton) {
        continueButton.addEventListener('click', () => {
            // Call cleanup callback if provided (clears intervals)
            if (deletionLoadingCleanup) {
                deletionLoadingCleanup();
                deletionLoadingCleanup = null;
            }
            hideDeletionLoading();
        });
    }

    return deletionLoadingPopup;
}

export function updateDeletionLoading(step, progress = null, details = null, showContinueButton = false) {
    if (!deletionLoadingPopup) return;

    const stepElement = deletionLoadingPopup.querySelector('#deletionCurrentStep');
    if (stepElement) {
        stepElement.textContent = step;
    }

    if (progress) {
        const progressElement = deletionLoadingPopup.querySelector('#deletionProgressText');
        if (progressElement) {
            progressElement.textContent = progress;
        }
    }

    // Update details text
    const detailsElement = deletionLoadingPopup.querySelector('#deletionDetails');
    if (detailsElement) {
        if (details) {
            detailsElement.textContent = details;
            detailsElement.style.display = 'block';
        } else {
            detailsElement.textContent = '';
            detailsElement.style.display = 'none';
        }
    }

    // Show/hide continue button
    const continueButton = deletionLoadingPopup.querySelector('#deletionContinueBackground');
    if (continueButton) {
        continueButton.style.display = showContinueButton ? 'inline-block' : 'none';
    }
}

export function hideDeletionLoading() {
    if (deletionLoadingPopup) {
        deletionLoadingPopup.remove();
        deletionLoadingPopup = null;
    }
    const style = document.getElementById('deletion-loading-popup-style');
    if (style) {
        style.remove();
    }
}

// Make functions available globally
window.showLoading = showLoading;
window.hideLoading = hideLoading;
window.showDeletionLoading = showDeletionLoading;
window.updateDeletionLoading = updateDeletionLoading;
window.hideDeletionLoading = hideDeletionLoading;
window.showPopup = showPopup;
window.POPUP_TYPES = POPUP_TYPES;
