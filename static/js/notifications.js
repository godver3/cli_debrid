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
        inputPlaceholder,
        onConfirm,
        onCancel,
        autoClose = 5000 // Auto close after 5 seconds for non-interactive popups
    } = options;

    const popup = document.createElement('div');
    popup.className = 'universal-popup';
    
    let content = `
        <div class="popup-content ${type}">
            <h3>${title || capitalizeFirstLetter(type)}</h3>
            <p>${message}</p>
    `;

    if (type === POPUP_TYPES.PROMPT) {
        content += `<input type="text" id="popupInput" placeholder="${inputPlaceholder || ''}">`;
    }

    if (type === POPUP_TYPES.CONFIRM || type === POPUP_TYPES.PROMPT) {
        content += `
            <div class="popup-buttons">
                <button id="popupConfirm">${confirmText}</button>
                <button id="popupCancel">${cancelText}</button>
            </div>
        `;
    } else {
        content += `<button id="popupClose">Close</button>`;
    }

    content += `</div>`;
    popup.innerHTML = content;
    document.body.appendChild(popup);

    function closePopup() {
        popup.remove();
    }

    if (type === POPUP_TYPES.CONFIRM || type === POPUP_TYPES.PROMPT) {
        document.getElementById('popupConfirm').addEventListener('click', () => {
            const inputValue = type === POPUP_TYPES.PROMPT ? document.getElementById('popupInput').value : null;
            if (onConfirm) onConfirm(inputValue);
            closePopup();
        });
        document.getElementById('popupCancel').addEventListener('click', () => {
            if (onCancel) onCancel();
            closePopup();
        });
    } else {
        document.getElementById('popupClose').addEventListener('click', closePopup);
        if (autoClose) {
            setTimeout(closePopup, autoClose);
        }
    }
}

// Helper function to capitalize the first letter of a string
function capitalizeFirstLetter(string) {
    return string.charAt(0).toUpperCase() + string.slice(1);
}

// No need to export showPopup and POPUP_TYPES again, they're already exported above