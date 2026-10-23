/* Moved from templates/manage_users.html. */
import { showPopup, POPUP_TYPES } from '/static/js/notifications.js';

document.addEventListener('DOMContentLoaded', function() {
    const addUserForm = document.getElementById('addUserForm');
    if (addUserForm) {
        addUserForm.addEventListener('submit', function(e) {
            e.preventDefault();
            const formData = new FormData(this);
            const username = formData.get('username');
            const role = formData.get('role');
            showPopup({
                type: POPUP_TYPES.CONFIRM,
                title: "Add User",
                message: `Are you sure you want to add user "${username}" with role "${role}"?`,
                onConfirm: function() {
                    fetch(window.pageData.manage_users.user_management_add_user_url, { method: 'POST', body: formData })
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            showPopup({ type: POPUP_TYPES.SUCCESS, title: 'Success', message: 'User added successfully. Page will reload.', onConfirm: () => window.location.reload() });
                        } else {
                            showPopup({ type: POPUP_TYPES.ERROR, title: 'Error', message: data.error || 'An error occurred while adding the user.' });
                        }
                    })
                    .catch(() => showPopup({ type: POPUP_TYPES.ERROR, title: 'Error', message: 'An unexpected error occurred.' }));
                }
            });
        });
    }

    document.querySelectorAll('.delete-button').forEach(button => {
        button.addEventListener('click', function() {
            const userId = this.dataset.userId;
            const username = this.closest('tr').querySelector('td:first-child').textContent.trim();
            showPopup({
                type: POPUP_TYPES.CONFIRM,
                title: "Delete User",
                message: `Are you sure you want to delete user "${username}"?`,
                onConfirm: function() {
                    fetch(`/user_management/delete_user/${userId}`, { method: 'POST' })
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            showPopup({ type: POPUP_TYPES.SUCCESS, title: 'Success', message: 'User deleted successfully. Page will reload.', onConfirm: () => window.location.reload() });
                        } else {
                            showPopup({ type: POPUP_TYPES.ERROR, title: 'Error', message: 'Error deleting user: ' + (data.error || 'Unknown error') });
                        }
                    })
                    .catch(() => showPopup({ type: POPUP_TYPES.ERROR, title: 'Error', message: 'Error deleting user' }));
                }
            });
        });
    });

    const changeOwnPasswordForm = document.getElementById('changeOwnPasswordForm');
    if (changeOwnPasswordForm) {
        changeOwnPasswordForm.addEventListener('submit', function(e) {
            e.preventDefault();
            const newPassword = this.elements.new_password.value;
            const confirmPassword = this.elements.confirm_password.value;
            if (newPassword !== confirmPassword) { showPopup({ type: POPUP_TYPES.ERROR, title: 'Error', message: 'Passwords do not match.' }); return; }
            if (!newPassword) { showPopup({ type: POPUP_TYPES.ERROR, title: 'Error', message: 'Password cannot be empty.' }); return; }
            showPopup({
                type: POPUP_TYPES.CONFIRM,
                title: "Change Password",
                message: "Are you sure you want to change your password?",
                onConfirm: function() {
                    fetch(window.pageData.manage_users.user_management_change_own_password_url, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
                        body: JSON.stringify({ new_password: newPassword, confirm_password: confirmPassword })
                    })
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            showPopup({ type: POPUP_TYPES.SUCCESS, title: 'Success', message: data.message || 'Password changed successfully!', onConfirm: () => changeOwnPasswordForm.reset() });
                        } else {
                            showPopup({ type: POPUP_TYPES.ERROR, title: 'Error', message: data.error || 'An error occurred.' });
                        }
                    })
                    .catch(() => showPopup({ type: POPUP_TYPES.ERROR, title: 'Error', message: 'An unexpected error occurred.' }));
                }
            });
        });
    }
});

// API Token
(function() {
    const copyBtn = document.getElementById('copy-api-token-btn');
    const regenBtn = document.getElementById('regen-api-token-btn');
    const tokenDisplay = document.getElementById('api-token-display');
    if (!copyBtn || !regenBtn || !tokenDisplay) return;

    copyBtn.addEventListener('click', function() {
        navigator.clipboard.writeText(tokenDisplay.value).then(() => {
            const orig = copyBtn.textContent;
            copyBtn.textContent = 'Copied!';
            setTimeout(() => copyBtn.textContent = orig, 1500);
        });
    });

    regenBtn.addEventListener('click', function() {
        showPopup({
            type: POPUP_TYPES.CONFIRM,
            title: 'Regenerate API Token',
            message: 'This will invalidate your current token immediately. Any services using it will need to be updated. Continue?',
            onConfirm: function() {
                fetch('/user_management/regenerate_api_token', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        tokenDisplay.value = data.api_token;
                        showPopup({ type: POPUP_TYPES.SUCCESS, title: 'Token Regenerated', message: 'Your new API token is ready. Update any external services.' });
                    } else {
                        showPopup({ type: POPUP_TYPES.ERROR, title: 'Error', message: 'Failed to regenerate token.' });
                    }
                });
            }
        });
    });
})();

// SSO Settings
(async function() {
    const enabledCb = document.getElementById('sso-enabled');
    const fieldsDiv = document.getElementById('sso-fields');
    const saveBtn   = document.getElementById('sso-save-btn');
    if (!enabledCb) return;

    function toggleFields() { fieldsDiv.style.display = enabledCb.checked ? '' : 'none'; }
    enabledCb.addEventListener('change', toggleFields);

    const redirectBaseInput  = document.getElementById('sso-redirect-uri-base');
    const callbackDisplay    = document.getElementById('sso-callback-uri-display');

    function updateCallbackDisplay() {
        const base = redirectBaseInput.value.trim().replace(/\/$/, '');
        callbackDisplay.value = (base || window.location.origin) + '/auth/oidc/callback';
    }
    redirectBaseInput.addEventListener('input', updateCallbackDisplay);

    try {
        const resp = await fetch('/auth/oidc/settings');
        if (resp.ok) {
            const s = await resp.json();
            enabledCb.checked = !!s.enabled;
            if (s.provider)       document.getElementById('sso-provider').value = s.provider;
            if (s.discovery_url)  document.getElementById('sso-discovery-url').value = s.discovery_url;
            if (s.client_id)      document.getElementById('sso-client-id').value = s.client_id;
            if (s.client_secret)  document.getElementById('sso-client-secret').value = s.client_secret;
            if (s.default_role)   document.getElementById('sso-default-role').value = s.default_role;
            document.getElementById('sso-auto-provision').checked   = s.auto_provision !== false;
            document.getElementById('sso-disable-local-auth').checked = !!s.disable_local_auth;
            if (s.redirect_uri_base) redirectBaseInput.value = s.redirect_uri_base;
        }
    } catch(e) { console.error('Failed to load SSO settings', e); }
    updateCallbackDisplay();
    toggleFields();

    saveBtn.addEventListener('click', async function() {
        const payload = {
            enabled:           enabledCb.checked,
            provider:          document.getElementById('sso-provider').value,
            discovery_url:     document.getElementById('sso-discovery-url').value.trim(),
            client_id:         document.getElementById('sso-client-id').value.trim(),
            client_secret:     document.getElementById('sso-client-secret').value,
            default_role:      document.getElementById('sso-default-role').value,
            auto_provision:    document.getElementById('sso-auto-provision').checked,
            disable_local_auth:document.getElementById('sso-disable-local-auth').checked,
            redirect_uri_base: redirectBaseInput.value.trim().replace(/\/$/, ''),
        };
        try {
            const resp = await fetch('/auth/oidc/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await resp.json();
            if (data.ok) {
                showPopup({ type: POPUP_TYPES.SUCCESS, title: 'Saved', message: 'SSO settings saved.' });
            } else {
                showPopup({ type: POPUP_TYPES.ERROR, title: 'Error', message: data.error || 'Failed to save.' });
            }
        } catch(e) {
            showPopup({ type: POPUP_TYPES.ERROR, title: 'Error', message: 'An unexpected error occurred.' });
        }
    });
})();
