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
        dropdownOptions,
        formHtml,  // New option for custom form HTML
        onConfirm,
        onCancel,
        autoClose = 5000
    } = options;

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
    }

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
    } else {
        const closeButton = popup.querySelector('#popupClose');
        if (closeButton) {
            closeButton.addEventListener('click', closePopup);
        }
        if (autoClose) {
            setTimeout(closePopup, autoClose);
        }
    }
}

// Helper function to capitalize the first letter of a string
function capitalizeFirstLetter(string) {
    return string.charAt(0).toUpperCase() + string.slice(1);
}