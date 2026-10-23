/* Moved from templates/onboarding_step_6.html. */
import { showPopup, POPUP_TYPES } from '/static/js/notifications.js';

const healthList = document.getElementById('health-list');
const rerunBtn = document.getElementById('rerun-checks');
const startBtn = document.getElementById('start-program-btn');
let lastHealth = null;

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
}

async function runChecks() {
    rerunBtn.disabled = true;
    healthList.innerHTML = '<li><span class="ob-msg">Running checks…</span></li>';
    try {
        const response = await fetch(window.pageData.onboarding_step_6.onboarding_onboarding_health_url);
        lastHealth = await response.json();
        healthList.innerHTML = lastHealth.checks.map(check => {
            const state = !check.valid && !check.warning ? 'err' : (check.warning ? 'warn' : 'ok');
            return `<li><span class="ob-dot ${state}"></span>
                <span><b>${escapeHtml(check.name)}</b><br><span class="ob-msg">${escapeHtml(check.message)}</span></span></li>`;
        }).join('');
    } catch (error) {
        lastHealth = null;
        healthList.innerHTML = `<li><span class="ob-dot err"></span><span>Could not run checks: ${escapeHtml(error.message)}</span></li>`;
    } finally {
        rerunBtn.disabled = false;
    }
}

async function completeAndStart() {
    startBtn.disabled = true;
    startBtn.textContent = 'Starting…';
    try {
        const completed = await (await fetch(window.pageData.onboarding_step_6.onboarding_complete_onboarding_url, { method: 'POST' })).json();
        if (!completed.success) throw new Error(completed.error || 'Could not finish setup');
        const started = await (await fetch('/program_operation/api/start_program', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}',
        })).json();
        if (started.status !== 'success') {
            // Setup is finished either way; show why it didn't start, then go to the dashboard.
            showPopup({
                type: POPUP_TYPES.CONFIRM,
                title: 'Setup finished, but the program did not start',
                message: escapeHtml(started.message || 'Unknown error') + '<br><br>Fix it in Settings, then press Start in the top right.',
                confirmText: 'Go to Dashboard',
                cancelText: 'Stay here',
                onConfirm: () => { window.location.href = window.pageData.onboarding_step_6.root_root_url; },
            });
            return;
        }
        window.location.href = window.pageData.onboarding_step_6.root_root_url;
    } catch (error) {
        showPopup({ type: POPUP_TYPES.ERROR, title: 'Error', message: escapeHtml(error.message) });
    } finally {
        startBtn.disabled = false;
        startBtn.textContent = 'Start Program & Go to Dashboard';
    }
}

rerunBtn.addEventListener('click', runChecks);
startBtn.addEventListener('click', () => {
    const failing = lastHealth ? lastHealth.checks.filter(c => !c.valid && !c.warning) : [];
    if (failing.length) {
        showPopup({
            type: POPUP_TYPES.CONFIRM,
            title: 'Some checks failed',
            message: 'These checks failed:<ul>' + failing.map(c => `<li>${escapeHtml(c.name)}</li>`).join('') +
                '</ul>cli_debrid may not work until they are fixed. Start anyway?',
            confirmText: 'Start anyway',
            cancelText: 'Cancel',
            onConfirm: completeAndStart,
        });
    } else {
        completeAndStart();
    }
});

runChecks();
