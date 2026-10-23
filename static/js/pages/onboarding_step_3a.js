/* Moved from templates/onboarding_step_3a.html. */
import { showPopup, POPUP_TYPES } from '/static/js/notifications.js';

function updateCanProceed() {
    fetch('/onboarding/update_can_proceed', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ 
            step: '3a', 
            can_proceed: true 
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            const nextStepBtn = document.querySelector('.next-step-btn');
            if (nextStepBtn) {
                nextStepBtn.disabled = false;
            }
        }
    })
    .catch(error => console.error('Error updating can_proceed status:', error));
}

function addDefaultVersion() {
    // First clear all versions
    fetch('/settings/versions/clear_all', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Then add the default version
            return fetch('/settings/versions/add_default', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                }
            });
        } else {
            throw new Error(data.error || 'Failed to clear versions');
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            updateCanProceed();
            location.reload();
        } else {
            throw new Error(data.error || 'Failed to add default version');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: 'Error: ' + error.message
        });
    });
}

function addSeparateVersions() {
    // First clear all versions
    fetch('/settings/versions/clear_all', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Then add the separate versions
            return fetch('/settings/versions/add_separate_versions', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                }
            });
        } else {
            throw new Error(data.error || 'Failed to clear versions');
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            updateCanProceed();
            location.reload();
        } else {
            throw new Error(data.error || 'Failed to add separate versions');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: 'Error: ' + error.message
        });
    });
}

function importDefaultVersions() {
    // First clear all versions
    fetch('/settings/versions/clear_all', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Then import the default versions
            return fetch('/settings/versions/import_defaults', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                }
            });
        } else {
            throw new Error(data.error || 'Failed to clear versions');
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            updateCanProceed();
            location.reload();
        } else {
            throw new Error(data.error || 'Failed to import versions');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: 'Error: ' + error.message
        });
    });
}

document.addEventListener('DOMContentLoaded', function() {
    // Initialize advice section toggle
    const adviceHeader = document.querySelector('.advice-header');
    const adviceContent = document.querySelector('.advice-content');
    const toggleIcon = document.querySelector('.toggle-icon');

    if (adviceHeader && adviceContent && toggleIcon) {
        adviceHeader.addEventListener('click', function() {
            adviceContent.style.display = adviceContent.style.display === 'none' ? 'block' : 'none';
            toggleIcon.style.transform = adviceContent.style.display === 'none' ? 'rotate(0deg)' : 'rotate(180deg)';
        });
    }

    // Check if we already have versions and enable the next button if we do
    const existingVersions = document.querySelector('#existing-versions');
    if (existingVersions && existingVersions.querySelector('.version-item')) {
        updateCanProceed();
    }

    // Initialize version buttons
    const defaultVersionBtn = document.getElementById('add-default-version-btn');
    const separateVersionsBtn = document.getElementById('add-separate-versions-btn');
    const importVersionsBtn = document.getElementById('import-default-versions-btn');

    if (defaultVersionBtn) {
        defaultVersionBtn.addEventListener('click', function() {
            showPopup({
                type: POPUP_TYPES.CONFIRM,
                title: 'Use Default Version',
                message: 'This will apply a single default version with recommended settings.',
                confirmText: 'Replace',
                cancelText: 'Cancel',
                onConfirm: () => {
                    addDefaultVersion();
                }
            });
        });
    }

    if (separateVersionsBtn) {
        separateVersionsBtn.addEventListener('click', function() {
            showPopup({
                type: POPUP_TYPES.CONFIRM,
                title: 'Use Separate Versions',
                message: 'This will apply two separate versions: one for 1080p content and another for 4K HDR content.',
                confirmText: 'Replace',
                cancelText: 'Cancel',
                onConfirm: () => {
                    addSeparateVersions();
                }
            });
        });
    }

    if (importVersionsBtn) {
        importVersionsBtn.addEventListener('click', function() {
            showPopup({
                type: POPUP_TYPES.CONFIRM,
                title: 'Use Alternate Default Versions',
                message: 'This apply alternate versions that include comprehensive 2160p, REMUX and WEB versions for the connoisseur.',
                confirmText: 'Replace',
                cancelText: 'Cancel',
                onConfirm: () => {
                    importDefaultVersions();
                }
            });
        });
    }
});
