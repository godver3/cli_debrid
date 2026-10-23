/* Moved from templates/settings_tabs/notifications.html. */
function initializeNotificationsFunctionality() {
    console.log('Initializing Notifications Functionality');

    const addNotificationBtn = document.getElementById('add-notification-btn');

    if (addNotificationBtn) {
        // Remove existing listener before adding a new one
        addNotificationBtn.removeEventListener('click', addNotificationHandler);
        addNotificationBtn.addEventListener('click', addNotificationHandler);
    }

    document.querySelectorAll('.delete-notification-btn').forEach(button => {
        // Remove existing listeners before adding new ones
        button.removeEventListener('click', deleteNotificationHandler);
        button.addEventListener('click', deleteNotificationHandler);
    });

    document.querySelectorAll('.test-notification-btn').forEach(button => {
        // Remove existing listeners before adding new ones
        button.removeEventListener('click', testNotificationHandler);
        button.addEventListener('click', testNotificationHandler);
    });
}

// Separate handler function for add button
function addNotificationHandler(e) {
    e.preventDefault();
    showAddNotificationPopup();
}

// Separate handler function for delete button
function deleteNotificationHandler(e) {
    e.stopPropagation();
    const notificationId = this.getAttribute('data-notification-id');
    deleteNotification(notificationId);
}

// Handler function for test notification button
function testNotificationHandler(e) {
    e.stopPropagation();
    const notificationId = this.getAttribute('data-notification-id');
    testNotification(notificationId);
}

function testNotification(notificationId) {
    // Disable the button and show loading state
    const button = document.querySelector(`.test-notification-btn[data-notification-id="${notificationId}"]`);
    const btnText = button.querySelector('.btn-text');
    const originalText = btnText.textContent;
    btnText.textContent = 'Sending...';
    button.disabled = true;

    fetch('/settings/notifications/test', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ notification_id: notificationId })
    })
    .then(response => response.json())
    .then(data => {
        // Reset button state
        btnText.textContent = originalText;
        button.disabled = false;

        if (data.success) {
            import('/static/js/notifications.js').then(module => {
                const { showPopup, POPUP_TYPES } = module;
                showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    title: 'Success',
                    message: 'Test notification sent successfully',
                    confirmText: 'OK'
                });
            });
        } else {
            throw new Error(data.error || 'Unknown error');
        }
    })
    .catch(error => {
        // Reset button state
        button.textContent = originalText;
        button.disabled = false;

        console.error('Error:', error);
        import('/static/js/notifications.js').then(module => {
            const { showPopup, POPUP_TYPES } = module;
            showPopup({
                type: POPUP_TYPES.ERROR,
                title: 'Error',
                message: 'Error sending test notification: ' + error.message,
                confirmText: 'OK'
            });
        });
    });
}

function showAddNotificationPopup() {
    import('/static/js/notifications.js').then(module => {
        const { showPopup, POPUP_TYPES } = module;
        showPopup({
            type: POPUP_TYPES.PROMPT,
            title: 'Add New Notification',
            message: 'Select the notification type:',
            dropdownOptions: [
                { value: 'Telegram', text: 'Telegram' },
                { value: 'Discord', text: 'Discord' },
                { value: 'NTFY', text: 'NTFY' },
                { value: 'Email', text: 'Email' }
            ],
            confirmText: 'Add',
            cancelText: 'Cancel',
            onConfirm: (selectedType) => {
                if (selectedType) {
                    addNotification(selectedType);
                }
            }
        });
    });
}

function addNotification(notificationType) {
    fetch('/settings/notifications/add', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ type: notificationType })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            return updateNotificationsTab();
        } else {
            throw new Error(data.error || 'Unknown error');
        }
    })
    .then(() => {
        import('/static/js/notifications.js').then(module => {
            const { showPopup, POPUP_TYPES } = module;
            showPopup({
                type: POPUP_TYPES.SUCCESS,
                title: 'Success',
                message: 'Notification added successfully',
                confirmText: 'OK'
            });
        });
        initializeNotificationsFunctionality();
    })
    .catch(error => {
        console.error('Error:', error);
        import('/static/js/notifications.js').then(module => {
            const { showPopup, POPUP_TYPES } = module;
            showPopup({
                type: POPUP_TYPES.ERROR,
                title: 'Error',
                message: 'Error adding notification: ' + error.message,
                confirmText: 'OK'
            });
        });
    });
}

function deleteNotification(notificationId) {
    import('/static/js/notifications.js').then(module => {
        const { showPopup, POPUP_TYPES } = module;
        showPopup({
            type: POPUP_TYPES.CONFIRM,
            title: 'Delete Notification',
            message: 'Are you sure you want to delete this notification?',
            confirmText: 'Delete',
            cancelText: 'Cancel',
            onConfirm: () => {
                performDeleteNotification(notificationId);
            }
        });
    });
}

function performDeleteNotification(notificationId) {
    fetch('/settings/notifications/delete', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ notification_id: notificationId })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            return updateNotificationsTab();
        } else {
            throw new Error(data.error || 'Unknown error');
        }
    })
    .then(() => {
        import('/static/js/notifications.js').then(module => {
            const { showPopup, POPUP_TYPES } = module;
            showPopup({
                type: POPUP_TYPES.SUCCESS,
                title: 'Success',
                message: 'Notification deleted successfully',
                confirmText: 'OK'
            });
        });
        initializeNotificationsFunctionality();
    })
    .catch(error => {
        console.error('Error:', error);
        import('/static/js/notifications.js').then(module => {
            const { showPopup, POPUP_TYPES } = module;
            showPopup({
                type: POPUP_TYPES.ERROR,
                title: 'Error',
                message: 'Error deleting notification: ' + error.message,
                confirmText: 'OK'
            });
        });
    });
}

function updateNotificationsTab() {
    return fetch('/settings')
        .then(response => response.text())
        .then(html => {
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, 'text/html');
            const newNotificationsTab = doc.getElementById('notifications');

            if (newNotificationsTab) {
                const currentNotificationsTab = document.getElementById('notifications');
                if (currentNotificationsTab) {
                    currentNotificationsTab.innerHTML = newNotificationsTab.innerHTML;
                    initializeNotificationsFunctionality();
                    initializeExpandCollapse();
                }
            } else {
                throw new Error('Notifications tab not found in the response');
            }
        })
        .catch(error => {
            console.error('Error updating Notifications tab:', error);
            showPopup({
                type: POPUP_TYPES.ERROR,
                title: 'Error',
                message: 'Error updating Notifications tab: ' + error.message,
                confirmText: 'OK'
            });
            initializeNotificationsFunctionality();
        });
}

document.addEventListener('DOMContentLoaded', function() {
    initializeNotificationsFunctionality();
    initializeContentSourceFilter();
});

function initializeContentSourceFilter() {
    const selectElement = document.getElementById('notifications-general-enabled-content-sources');
    if (!selectElement) return;

    // Get current selected values
    const currentValue = selectElement.getAttribute('data-current-value') || '';
    const selectedSources = currentValue ? currentValue.split(',').map(s => s.trim()).filter(s => s) : [];

    // Create hidden input for form submission
    let hiddenInput = document.getElementById('notifications-general-enabled-content-sources-hidden');
    if (!hiddenInput) {
        hiddenInput = document.createElement('input');
        hiddenInput.type = 'hidden';
        hiddenInput.id = 'notifications-general-enabled-content-sources-hidden';
        hiddenInput.name = 'Notifications.General.enabled_content_sources';
        hiddenInput.setAttribute('data-section', 'Notifications');
        hiddenInput.setAttribute('data-key', 'General.enabled_content_sources');
        hiddenInput.value = currentValue;
        selectElement.parentNode.appendChild(hiddenInput);
    }

    // Fetch content sources from the API
    fetch('/settings/api/enabled_content_sources')
        .then(response => response.json())
        .then(data => {
            if (data.success && data.sources) {
                // Clear existing options
                selectElement.innerHTML = '';

                // Add options for each content source
                data.sources.forEach(source => {
                    const option = document.createElement('option');
                    option.value = source.id;

                    // Show display name with backend ID in parentheses for clarity
                    const displayText = source.display_name || source.id;
                    option.textContent = source.display_name && source.display_name !== source.id 
                        ? `${displayText} (${source.id})`
                        : displayText;

                    // Mark as selected if it was previously selected
                    if (selectedSources.includes(source.id)) {
                        option.selected = true;
                    }

                    selectElement.appendChild(option);
                });
            }
        })
        .catch(error => {
            console.error('Error loading content sources:', error);
        });

    // Handle change events to update the hidden input for form submission
    selectElement.addEventListener('change', function() {
        const selectedOptions = Array.from(this.selectedOptions).map(option => option.value);
        const selectedValue = selectedOptions.join(', ');

        // Update the data attribute to preserve the value
        this.setAttribute('data-current-value', selectedValue);

        // Create or update a hidden input for form submission
        let hiddenInput = document.getElementById('notifications-general-enabled-content-sources-hidden');
        if (!hiddenInput) {
            hiddenInput = document.createElement('input');
            hiddenInput.type = 'hidden';
            hiddenInput.id = 'notifications-general-enabled-content-sources-hidden';
            hiddenInput.name = 'Notifications.General.enabled_content_sources';
            hiddenInput.setAttribute('data-section', 'Notifications');
            hiddenInput.setAttribute('data-key', 'General.enabled_content_sources');
            selectElement.parentNode.appendChild(hiddenInput);
        }
        hiddenInput.value = selectedValue;
    });
}
