/* Moved from templates/user_token_collection.html. */
import { showPopup, POPUP_TYPES } from '/static/js/notifications.js'; // Assuming you use this

const generateBtn = document.getElementById('generate-btn');
const authDetailsDiv = document.getElementById('auth-details');
const plexCodeEl = document.getElementById('plex-code');
const plexLinkEl = document.getElementById('plex-link');
const pollingStatusEl = document.getElementById('polling-status');
const authErrorEl = document.getElementById('auth-error');
const authSuccessEl = document.getElementById('auth-success');
const storedTokensList = document.getElementById('stored-tokens-list');
const noTokensMessage = document.getElementById('no-tokens-message');

let pollIntervalId = null;
let currentPinId = null;
let currentClientId = null;

function stopPolling() {
    if (pollIntervalId) {
        clearInterval(pollIntervalId);
        pollIntervalId = null;
    }
    generateBtn.disabled = false; // Re-enable generate button
    pollingStatusEl.textContent = ''; // Clear status text
    // Keep auth details visible for reference unless explicitly hidden
}

function updateStoredTokensList(username) {
    if (noTokensMessage) {
        noTokensMessage.style.display = 'none';
    }
    if (!storedTokensList) { // If the list didn't exist initially
         const listContainer = document.querySelector('.stored-tokens-section');
         const newList = document.createElement('ul');
         newList.id = 'stored-tokens-list';
         newList.className = 'stored-tokens-list';
         listContainer.appendChild(newList);
         // Re-assign the global variable
         window.storedTokensList = newList; // Use window scope if needed, or pass list around
    }

    // Check if user already exists
    if (Array.from(document.querySelectorAll('#stored-tokens-list li')).some(li => li.dataset.username === username)) {
         console.log(`Token for ${username} updated.`);
         return; // Already in list, just updated silently
    }

    // Built with textContent: a profile title is free text and must not be parsed as HTML.
    const listItem = document.createElement('li');
    listItem.setAttribute('data-username', username);
    const nameSpan = document.createElement('span');
    nameSpan.textContent = username;
    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'btn-delete';
    deleteBtn.dataset.username = username;
    deleteBtn.textContent = 'Delete';
    listItem.appendChild(nameSpan);
    listItem.appendChild(deleteBtn);
    storedTokensList.appendChild(listItem);

    // Add event listener to the new delete button
    addDeleteEventListener(deleteBtn);
}

async function checkPinStatus(pinId, clientId) {
    console.log(`Checking status for PIN: ${pinId}, Client: ${clientId}`);
    try {
        const response = await fetch('/user_token/collect_tokens/check_pin', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pin_id: pinId, client_id: clientId })
        });

        if (!response.ok && response.status !== 404) { // Allow 404 (pin expired/invalid) to be handled as an error below
             throw new Error(`HTTP error ${response.status}: ${response.statusText}`);
        }

        const data = await response.json();
        console.log("Pin check response:", data);

        if (data.success && data.status === 'authorized') {
            pollingStatusEl.textContent = `User ${data.username} successfully authorized! Token stored.`;
            pollingStatusEl.style.color = 'var(--success-color)';
            authSuccessEl.textContent = `Authorization complete for ${data.username}.`;
            authSuccessEl.style.display = 'block';
            authErrorEl.style.display = 'none';
            updateStoredTokensList(data.username);
            stopPolling();
        } else if (data.success && data.status === 'waiting') {
            pollingStatusEl.textContent = 'Waiting for user to authorize... (Checking again soon)';
            pollingStatusEl.style.color = 'inherit'; // Reset color
        } else {
            // Handle errors like expired pin or other failures
            pollingStatusEl.textContent = `Authorization failed or expired: ${data.error || 'Unknown reason'}`;
            pollingStatusEl.style.color = 'var(--danger-color)';
            authErrorEl.textContent = `Error: ${data.error || 'Failed to complete authorization.'}`;
            authErrorEl.style.display = 'block';
            authSuccessEl.style.display = 'none';
            stopPolling();
        }
    } catch (error) {
        console.error('Error checking pin status:', error);
        pollingStatusEl.textContent = `Error checking status: ${error.message}`;
        pollingStatusEl.style.color = 'var(--danger-color)';
        authErrorEl.textContent = `Error: ${error.message}`;
        authErrorEl.style.display = 'block';
        authSuccessEl.style.display = 'none';
        stopPolling();
    }
}

generateBtn.addEventListener('click', async () => {
    generateBtn.disabled = true;
    authDetailsDiv.style.display = 'none';
    pollingStatusEl.textContent = 'Generating link...';
    authErrorEl.style.display = 'none'; // Clear previous errors
    authSuccessEl.style.display = 'none'; // Clear previous success
    stopPolling(); // Clear any previous polling

    try {
        const response = await fetch('/user_token/collect_tokens/initiate', { method: 'POST' });
        const data = await response.json();

        if (data.success) {
            currentPinId = data.pin_id;
            currentClientId = data.client_id; // Store client ID for checking
            plexCodeEl.textContent = data.code;
            plexLinkEl.href = data.auth_url;
            plexLinkEl.textContent = data.auth_url;
            authDetailsDiv.style.display = 'block';
            pollingStatusEl.textContent = 'Waiting for user to authorize...';
            pollingStatusEl.style.color = 'inherit';

            // Start polling
            pollIntervalId = setInterval(() => {
                checkPinStatus(currentPinId, currentClientId);
            }, 5000); // Poll every 5 seconds

             // Optional: Auto-stop polling after a timeout (e.g., 5 minutes)
             setTimeout(() => {
                 if (pollIntervalId) { // Check if still polling
                    console.log("Stopping polling due to timeout.");
                    pollingStatusEl.textContent = 'Authorization timed out. Please generate a new link.';
                    pollingStatusEl.style.color = 'var(--warning-color)';
                    stopPolling();
                 }
             }, 300000); // 300000 ms = 5 minutes

        } else {
            throw new Error(data.error || 'Failed to generate link');
        }
    } catch (error) {
        console.error('Error generating link:', error);
        authErrorEl.textContent = `Error: ${error.message}`;
        authErrorEl.style.display = 'block';
        generateBtn.disabled = false;
        pollingStatusEl.textContent = '';
    }
    // Keep generate button disabled while polling is active
});

 // --- Delete Functionality ---
async function deleteToken(username) {
    if (!confirm(`Are you sure you want to delete the stored token for user "${username}"?`)) {
        return;
    }
    try {
        const response = await fetch('/user_token/collect_tokens/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: username })
        });
        const data = await response.json();

        if (data.success) {
            // Remove the list item from the page
            const listItem = Array.from(document.querySelectorAll('#stored-tokens-list li')).find(li => li.dataset.username === username);
            if (listItem) {
                listItem.remove();
            }
             // Show message if list becomes empty
             if (storedTokensList && storedTokensList.children.length === 0 && noTokensMessage) {
                 noTokensMessage.style.display = 'block';
             }
            showPopup({ type: POPUP_TYPES.SUCCESS, title: 'Token Deleted', message: `Token for ${username} removed.` });
        } else {
            throw new Error(data.error || 'Failed to delete token');
        }
    } catch (error) {
        console.error('Error deleting token:', error);
        showPopup({ type: POPUP_TYPES.ERROR, title: 'Deletion Failed', message: error.message });
    }
}

// --- Plex Home (managed) users ---
const loadHomeBtn = document.getElementById('load-home-users-btn');
const homeList = document.getElementById('home-users-list');
const homeError = document.getElementById('home-users-error');
const homeSuccess = document.getElementById('home-users-success');

function showHome(el, text) {
    homeError.style.display = 'none';
    homeSuccess.style.display = 'none';
    if (text) { el.textContent = text; el.style.display = 'block'; }
}

function renderHomeUsers(users) {
    homeList.innerHTML = '';
    if (!users.length) {
        showHome(homeError, 'No Plex Home users were found on this account.');
        return;
    }
    users.forEach(user => {
        const li = document.createElement('li');
        const name = document.createElement('span');
        name.className = 'utc-home-name';
        name.textContent = user.name;
        [user.managed ? 'managed' : 'home', user.protected ? 'PIN' : null, user.stored ? 'token stored' : null]
            .filter(Boolean).forEach(label => {
                const tag = document.createElement('span');
                tag.className = 'utc-home-tag';
                tag.textContent = label;
                name.appendChild(tag);
            });
        const actions = document.createElement('div');
        actions.className = 'utc-home-actions';
        let pinInput = null;
        if (user.protected) {
            pinInput = document.createElement('input');
            pinInput.type = 'password';
            pinInput.placeholder = 'PIN';
            pinInput.className = 'utc-pin';
            pinInput.autocomplete = 'off';
            actions.appendChild(pinInput);
        }
        const btn = document.createElement('button');
        btn.className = 'utc-btn utc-btn-secondary';
        btn.textContent = user.stored ? 'Refresh token' : 'Get token';
        btn.addEventListener('click', () => addHomeUserToken(user, btn, pinInput));
        actions.appendChild(btn);
        li.appendChild(name);
        li.appendChild(actions);
        homeList.appendChild(li);
    });
}

async function loadHomeUsers() {
    loadHomeBtn.disabled = true;
    showHome(null, '');
    try {
        const response = await fetch('/user_token/collect_tokens/home_users');
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Failed to load Plex Home users');
        renderHomeUsers(data.users);
    } catch (error) {
        homeList.innerHTML = '';
        showHome(homeError, `Error: ${error.message}`);
    } finally {
        loadHomeBtn.disabled = false;
    }
}

async function addHomeUserToken(user, btn, pinInput) {
    btn.disabled = true;
    showHome(null, '');
    try {
        const response = await fetch('/user_token/collect_tokens/home_users/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: user.id, pin: pinInput ? pinInput.value : '' })
        });
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Failed to fetch the token');
        showHome(homeSuccess, `Token stored for ${data.username}.`);
        updateStoredTokensList(data.username);
        user.stored = true;
        btn.textContent = 'Refresh token';
        if (pinInput) pinInput.value = '';
    } catch (error) {
        showHome(homeError, `Error: ${error.message}`);
    } finally {
        btn.disabled = false;
    }
}

loadHomeBtn.addEventListener('click', loadHomeUsers);


// Add event listeners to existing delete buttons
function addDeleteEventListener(button) {
     button.addEventListener('click', (event) => {
         const username = event.target.dataset.username;
         deleteToken(username);
     });
}

document.querySelectorAll('.btn-delete').forEach(button => {
    addDeleteEventListener(button);
});
