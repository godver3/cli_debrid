window.flash = function(message, type = 'info') {
    console.log('Flash function called with message:', message, 'and type:', type);
    const flashContainer = document.getElementById('flash-messages');
    if (!flashContainer) {
        console.error('Flash messages container not found');
        return;
    }
    console.log('Flash container found');

    const popup = document.createElement('div');
    popup.className = 'popup';
    popup.textContent = message;
    popup.style.backgroundColor = type === 'error' ? '#f44336' : '#4CAF50';
    popup.style.color = 'white';
    popup.style.padding = '15px 20px';
    popup.style.borderRadius = '5px';
    popup.style.marginBottom = '10px';
    popup.style.fontSize = '16px';
    popup.style.textAlign = 'center';
    popup.style.boxShadow = '0 4px 6px rgba(0, 0, 0, 0.1)';
    popup.style.wordWrap = 'break-word';
    popup.style.maxWidth = '100%';
    popup.style.boxSizing = 'border-box';
    popup.style.opacity = '0';
    popup.style.transition = 'opacity 0.3s ease-in-out';

    flashContainer.appendChild(popup);
    console.log('Popup added to flash container');

    // Force a reflow to ensure the transition works
    popup.offsetHeight;

    // Fade in
    popup.style.opacity = '1';
    console.log('Popup opacity set to 1');

    // Remove the popup after 3 seconds
    setTimeout(() => {
        popup.style.opacity = '0';
        setTimeout(() => {
            popup.remove();
            console.log('Popup removed');
        }, 300);
    }, 3000);
};