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
        content += `<div class="popup-buttons"><button id="popupClose">Close</button></div>`;
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
    `;
    document.head.appendChild(style);

    function closePopup() {
        popup.remove();
        style.remove();
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