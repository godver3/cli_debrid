/**
 * ai_butler.js — CLI Debrid AI Butler chat widget
 * Phase 2: Assistant with setting-change suggestions + Apply buttons
 */

(function () {
    'use strict';

    const STORAGE_KEY = 'ai_butler_messages';
    const SESSION_KEY = 'ai_butler_session_id';
    const PANEL_OPEN_KEY = 'ai_butler_panel_open';
    const MAX_HISTORY = 20; // max messages to keep in localStorage

    let messages = []; // [{role, content}] sent to API
    let isStreaming = false;
    let statusChecked = false;
    let sessionId = localStorage.getItem(SESSION_KEY) || _newSessionId();
    let _availableVersions = null; // cached from /api/ai/versions

    function _fetchVersions() {
        if (_availableVersions !== null) return Promise.resolve(_availableVersions);
        return fetch('/content/versions').then(r => r.json()).then(data => {
            _availableVersions = data.versions || ['1080p'];
            return _availableVersions;
        }).catch(() => {
            _availableVersions = ['1080p'];
            return _availableVersions;
        });
    }

    function _newSessionId() {
        return String(Date.now());
    }

    // DOM refs (populated on init)
    let toggle, panel, messagesEl, input, sendBtn, clearBtn, newSessionBtn, closeBtn, statusDot, titleEl, helpBtn, settingsBtn, expandBtn;
    let isExpanded = false;

    function init() {
        toggle = document.getElementById('ai-butler-toggle');
        if (!toggle) return;

        panel = document.getElementById('ai-butler-panel');
        messagesEl = document.getElementById('ai-butler-messages');
        input = document.getElementById('ai-butler-input');
        sendBtn = document.getElementById('ai-butler-send');
        clearBtn = document.getElementById('ai-butler-clear');
        newSessionBtn = document.getElementById('ai-butler-new-session');
        closeBtn = document.getElementById('ai-butler-close');
        statusDot = document.getElementById('ai-butler-status-dot');
        titleEl = document.getElementById('ai-butler-title');
        helpBtn = document.getElementById('ai-butler-help');
        settingsBtn = document.getElementById('ai-butler-settings');
        expandBtn = document.getElementById('ai-butler-expand');

        // Show toggle button
        toggle.style.display = '';

        // Pre-fetch available versions for library card checkboxes, then restore history
        // so that library cards are rendered with version checkboxes already available
        _fetchVersions().then(function() {
            restoreHistory();
        });

        // Restore panel open state
        if (localStorage.getItem(PANEL_OPEN_KEY) === '1') {
            openPanel();
        }

        // Event listeners
        toggle.addEventListener('click', togglePanel);
        closeBtn.addEventListener('click', closePanel);
        clearBtn.addEventListener('click', clearConversation);
        newSessionBtn.addEventListener('click', startNewSession);
        sendBtn.addEventListener('click', sendMessage);
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });
        input.addEventListener('input', autoResizeInput);

        if (helpBtn) helpBtn.addEventListener('click', openHelp);
        if (settingsBtn) settingsBtn.addEventListener('click', openSettingsModal);
        if (expandBtn) expandBtn.addEventListener('click', toggleExpand);

        // Wire up settings modal
        const modalClose = document.getElementById('ai-butler-modal-close');
        const modalSave = document.getElementById('ai-butler-modal-save');
        const modalOverlay = document.getElementById('ai-butler-settings-modal');
        if (modalClose) modalClose.addEventListener('click', closeSettingsModal);
        if (modalSave) modalSave.addEventListener('click', saveSettings);
        if (modalOverlay) modalOverlay.addEventListener('click', function(e) {
            if (e.target === modalOverlay) closeSettingsModal();
        });

        // Toggle plex labels fixed field visibility
        const plexLabelsCb = document.getElementById('aim-plex-labels-enabled');
        if (plexLabelsCb) {
            plexLabelsCb.addEventListener('change', function() {
                const fixedField = document.getElementById('aim-plex-labels-fixed-field');
                if (fixedField) fixedField.style.display = this.checked ? '' : 'none';
            });
        }

        // Check status in background
        checkStatus();
    }

    function openHelp() {
        // Use the global help modal system from base.js
        const overlay = document.getElementById('help-overlay');
        const modalBox = document.getElementById('help-modal-box');
        const modalBody = document.getElementById('help-modal-body');
        if (!overlay || !modalBox || !modalBody) return;

        document.body.classList.add('modal-open');
        overlay.style.display = 'block';
        modalBox.style.display = 'flex';
        overlay.classList.add('visible');
        modalBox.classList.add('visible');

        modalBody.innerHTML = '<p>Loading help...</p>';
        fetch('/base/api/help-content?page_path=' + encodeURIComponent('/ai_butler'))
            .then(r => r.json())
            .then(data => {
                if (data.success && data.html) {
                    modalBody.innerHTML = data.html;
                } else {
                    modalBody.innerHTML = '<p>Could not load help content.</p>';
                }
            })
            .catch(() => { modalBody.innerHTML = '<p>Could not load help content.</p>'; });
    }

    function toggleExpand() {
        isExpanded = !isExpanded;
        if (isExpanded) {
            panel.classList.add('ai-butler-panel--expanded');
            expandBtn.title = 'Restore';
            expandBtn.querySelector('i').className = 'fas fa-compress-alt';
        } else {
            panel.classList.remove('ai-butler-panel--expanded');
            expandBtn.title = 'Expand';
            expandBtn.querySelector('i').className = 'fas fa-expand-alt';
        }
        scrollToBottom();
    }

    function togglePanel() {
        const isOpen = panel.style.display !== 'none';
        if (isOpen) {
            closePanel();
        } else {
            openPanel();
        }
    }

    function openPanel() {
        panel.style.display = 'flex';
        toggle.classList.add('active');
        localStorage.setItem(PANEL_OPEN_KEY, '1');
        scrollToBottom();
        input.focus();
        if (!statusChecked) checkStatus();
    }

    function closePanel() {
        panel.style.display = 'none';
        toggle.classList.remove('active');
        localStorage.removeItem(PANEL_OPEN_KEY);
    }

    function clearConversation() {
        messages = [];
        localStorage.removeItem(STORAGE_KEY);
        messagesEl.innerHTML = '';
        addAssistantBubble('Conversation cleared. How can I help you?');
    }

    function startNewSession() {
        // Generate a new session ID — OpenClaw scopes memory per user field,
        // so a new ID = fresh server-side memory with no prior conversation context.
        sessionId = _newSessionId();
        localStorage.setItem(SESSION_KEY, sessionId);
        // Also clear local history
        messages = [];
        localStorage.removeItem(STORAGE_KEY);
        messagesEl.innerHTML = '';
        addAssistantBubble('New session started. I have no memory of previous conversations. How can I help?');
    }

    function autoResizeInput() {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    }

    function scrollToBottom() {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function setStreaming(active) {
        isStreaming = active;
        sendBtn.disabled = active;
        input.disabled = active;
        sendBtn.classList.toggle('loading', active);
    }

    // --- Status check ---

    function checkStatus() {
        fetch('/api/ai/status')
            .then(r => r.json())
            .then(data => {
                statusChecked = true;
                if (data.agent_name && titleEl) {
                    titleEl.textContent = data.agent_name;
                    toggle.title = data.agent_name;
                    toggle.setAttribute('aria-label', 'Open ' + data.agent_name);
                }
                if (data.enabled && data.reachable) {
                    statusDot.className = 'ai-butler-status online';
                    statusDot.title = 'Connected: ' + data.message;
                } else if (data.enabled) {
                    statusDot.className = 'ai-butler-status offline';
                    statusDot.title = 'OpenClaw unreachable: ' + data.message;
                } else {
                    statusDot.className = 'ai-butler-status disabled';
                    statusDot.title = data.message;
                }
            })
            .catch(() => {
                statusDot.className = 'ai-butler-status offline';
                statusDot.title = 'Status check failed';
            });
    }

    // --- Settings modal ---

    function openSettingsModal() {
        const modal = document.getElementById('ai-butler-settings-modal');
        if (!modal) return;
        fetch('/api/ai/settings')
            .then(r => r.json())
            .then(data => {
                _setModalField('aim-enabled', data.enabled, 'checkbox');
                _setModalField('aim-openclaw-url', data.openclaw_url, 'text');
                _setModalField('aim-openclaw-token', data.openclaw_token, 'text');
                _setModalField('aim-agent-id', data.agent_id, 'text');
                _setModalField('aim-display-name', data.display_name, 'text');
                _setModalField('aim-settings-assistant', data.enable_settings_assistant, 'checkbox');
                _setModalField('aim-proactive', data.enable_proactive_notifications, 'checkbox');
                _setModalField('aim-recommendations', data.enable_recommendations, 'checkbox');
                _setModalField('aim-habits', data.enable_habit_tracking, 'checkbox');
                _setModalField('aim-share-config', data.share_full_config, 'checkbox');
                _setModalField('aim-health-notifications', data.health_notifications, 'checkbox');
                _setModalField('aim-health-interval', data.health_check_interval, 'text');
                const plexLabels = data.plex_labels || {};
                _setModalField('aim-plex-labels-enabled', plexLabels.enabled, 'checkbox');
                _setModalField('aim-plex-labels-fixed-label', plexLabels.fixed_label !== undefined ? plexLabels.fixed_label : 'AI Butler', 'text');
                const fixedField = document.getElementById('aim-plex-labels-fixed-field');
                if (fixedField) fixedField.style.display = plexLabels.enabled ? '' : 'none';
                const statusEl = document.getElementById('ai-butler-modal-status');
                if (statusEl) statusEl.textContent = '';
            })
            .catch(() => {});
        modal.style.display = 'flex';
    }

    function closeSettingsModal() {
        const modal = document.getElementById('ai-butler-settings-modal');
        if (modal) modal.style.display = 'none';
    }

    function _setModalField(id, value, type) {
        const el = document.getElementById(id);
        if (!el) return;
        if (type === 'checkbox') {
            el.checked = !!value;
        } else {
            el.value = value !== null && value !== undefined ? value : '';
        }
    }

    function saveSettings() {
        const body = {
            enabled: document.getElementById('aim-enabled').checked,
            openclaw_url: document.getElementById('aim-openclaw-url').value.trim(),
            openclaw_token: document.getElementById('aim-openclaw-token').value,
            agent_id: document.getElementById('aim-agent-id').value.trim() || 'main',
            display_name: document.getElementById('aim-display-name').value.trim(),
            enable_settings_assistant: document.getElementById('aim-settings-assistant').checked,
            enable_proactive_notifications: document.getElementById('aim-proactive').checked,
            enable_recommendations: document.getElementById('aim-recommendations').checked,
            enable_habit_tracking: document.getElementById('aim-habits').checked,
            share_full_config: document.getElementById('aim-share-config').checked,
            health_notifications: document.getElementById('aim-health-notifications').checked,
            health_check_interval: parseInt(document.getElementById('aim-health-interval').value) || 900,
            plex_labels: {
                enabled: document.getElementById('aim-plex-labels-enabled').checked,
                label_mode: 'fixed',
                fixed_label: document.getElementById('aim-plex-labels-fixed-label').value.trim() || 'AI Butler',
            },
        };
        const statusEl = document.getElementById('ai-butler-modal-status');
        const saveBtn = document.getElementById('ai-butler-modal-save');
        if (saveBtn) saveBtn.disabled = true;
        fetch('/api/ai/settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        })
        .then(r => r.json())
        .then(data => {
            if (saveBtn) saveBtn.disabled = false;
            if (data.ok) {
                if (statusEl) { statusEl.textContent = 'Saved.'; statusEl.className = 'ai-butler-modal-status ok'; }
                setTimeout(closeSettingsModal, 800);
                // Re-check status to pick up any URL/token changes
                statusChecked = false;
                checkStatus();
            } else {
                if (statusEl) { statusEl.textContent = data.error || 'Failed to save.'; statusEl.className = 'ai-butler-modal-status error'; }
            }
        })
        .catch(() => {
            if (saveBtn) saveBtn.disabled = false;
            if (statusEl) { statusEl.textContent = 'Network error.'; statusEl.className = 'ai-butler-modal-status error'; }
        });
    }

    // --- Message rendering ---

    function addUserBubble(text) {
        const div = document.createElement('div');
        div.className = 'ai-butler-message ai-butler-message--user';
        div.innerHTML = '<div class="ai-butler-message-content">' + escapeHtml(text) + '</div>';
        messagesEl.appendChild(div);
        scrollToBottom();
        return div;
    }

    function addAssistantBubble(text) {
        const div = document.createElement('div');
        div.className = 'ai-butler-message ai-butler-message--assistant';
        const content = document.createElement('div');
        content.className = 'ai-butler-message-content';
        content.innerHTML = renderMarkdownWithApply(text);
        attachApplyHandlers(content);
        div.appendChild(content);
        messagesEl.appendChild(div);
        scrollToBottom();
        return div;
    }

    function addStreamingBubble() {
        const div = document.createElement('div');
        div.className = 'ai-butler-message ai-butler-message--assistant ai-butler-message--streaming';
        div.innerHTML = '<div class="ai-butler-message-content"><span class="ai-butler-cursor"></span></div>';
        messagesEl.appendChild(div);
        scrollToBottom();
        return div;
    }

    function updateStreamingBubble(div, text) {
        const content = div.querySelector('.ai-butler-message-content');
        content.innerHTML = renderMarkdown(text) + '<span class="ai-butler-cursor"></span>';
        scrollToBottom();
    }

    function finalizeStreamingBubble(div, text) {
        div.classList.remove('ai-butler-message--streaming');
        const content = div.querySelector('.ai-butler-message-content');
        content.innerHTML = renderMarkdownWithApply(text);
        attachApplyHandlers(content);
        scrollToBottom();
    }

    function addErrorBubble(text) {
        const div = document.createElement('div');
        div.className = 'ai-butler-message ai-butler-message--error';
        div.innerHTML = '<div class="ai-butler-message-content"><i class="fas fa-exclamation-triangle"></i> ' + escapeHtml(text) + '</div>';
        messagesEl.appendChild(div);
        scrollToBottom();
    }

    // --- Send ---

    function sendMessage() {
        if (isStreaming) return;
        const text = input.value.trim();
        if (!text) return;

        input.value = '';
        input.style.height = 'auto';

        // Add to display and history
        addUserBubble(text);
        messages.push({ role: 'user', content: text });
        saveHistory();

        setStreaming(true);

        const streamDiv = addStreamingBubble();
        let accumulated = '';

        // Page context
        const page = window.location.pathname;
        const page_data = {};

        fetch('/api/ai/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                messages: messages.slice(-MAX_HISTORY),
                page: page,
                page_data: page_data,
                session_id: sessionId
            })
        })
        .then(resp => {
            if (!resp.ok) {
                return resp.json().then(d => { throw new Error(d.error || 'Request failed'); });
            }
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            function pump() {
                return reader.read().then(({ done, value }) => {
                    if (done) {
                        finalize();
                        return;
                    }
                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop(); // keep incomplete line

                    for (const line of lines) {
                        if (!line.startsWith('data:')) continue;
                        const data = line.slice(5).trim();
                        if (data === '[DONE]') { finalize(); return; }
                        try {
                            const parsed = JSON.parse(data);
                            // Error from our backend
                            if (parsed.error) {
                                streamDiv.remove();
                                addErrorBubble(parsed.error);
                                setStreaming(false);
                                return;
                            }
                            // OpenAI-compatible chunk
                            const delta = parsed.choices?.[0]?.delta?.content;
                            if (delta) {
                                accumulated += delta;
                                updateStreamingBubble(streamDiv, accumulated);
                            }
                        } catch (e) {
                            // non-JSON line, ignore
                        }
                    }
                    return pump();
                });
            }

            function finalize() {
                if (!accumulated) {
                    streamDiv.remove();
                    setStreaming(false);
                    return;
                }

                // Check for SEARCH_LIBRARY blocks — intercept, execute, re-ask with results
                const searchBlocks = parseSearchBlocks(accumulated);
                if (searchBlocks.length > 0) {
                    // Show the AI's intermediate response (with search blocks stripped)
                    const displayText = accumulated.replace(/^SEARCH_LIBRARY:\s*\{[^\n]+\}\s*$/gm, '').trim();
                    if (displayText) {
                        updateStreamingBubble(streamDiv, displayText + '\n\n_Looking up library..._');
                    } else {
                        updateStreamingBubble(streamDiv, '_Looking up library..._');
                    }

                    executeSearchBlocks(searchBlocks).then(function (results) {
                        // Inject search results as a tool-result message
                        const toolResultContent = results.join('\n\n');
                        const toolMsg = { role: 'user', content: '[LIBRARY_LOOKUP_RESULTS]\n' + toolResultContent + '\n\nPlease continue your response using these ground-truth results.' };

                        // Remove the streaming bubble — we'll add a fresh one for the follow-up
                        streamDiv.remove();

                        // Add the intermediate AI response to history (without search blocks)
                        messages.push({ role: 'assistant', content: accumulated });
                        messages.push(toolMsg);

                        // Show the tool result as a system note
                        const noteDiv = document.createElement('div');
                        noteDiv.className = 'ai-butler-message ai-butler-message--system';
                        noteDiv.innerHTML = '<div class="ai-butler-message-content"><em>Library lookup complete. Asking AI to continue...</em></div>';
                        messagesEl.appendChild(noteDiv);
                        scrollToBottom();

                        // Fire off a follow-up request
                        const streamDiv2 = addStreamingBubble();
                        let accumulated2 = '';
                        fetch('/api/ai/chat', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ messages: messages.slice(-MAX_HISTORY), page: page, page_data: page_data, session_id: sessionId })
                        })
                        .then(resp2 => {
                            if (!resp2.ok) return resp2.json().then(d => { throw new Error(d.error || 'Request failed'); });
                            const reader2 = resp2.body.getReader();
                            const decoder2 = new TextDecoder();
                            let buffer2 = '';

                            function pump2() {
                                return reader2.read().then(({ done, value }) => {
                                    if (done) { finalize2(); return; }
                                    buffer2 += decoder2.decode(value, { stream: true });
                                    const lines2 = buffer2.split('\n');
                                    buffer2 = lines2.pop();
                                    for (const line of lines2) {
                                        if (!line.startsWith('data:')) continue;
                                        const data2 = line.slice(5).trim();
                                        if (data2 === '[DONE]') { finalize2(); return; }
                                        try {
                                            const parsed2 = JSON.parse(data2);
                                            if (parsed2.error) { streamDiv2.remove(); addErrorBubble(parsed2.error); setStreaming(false); return; }
                                            const delta2 = parsed2.choices?.[0]?.delta?.content;
                                            if (delta2) { accumulated2 += delta2; updateStreamingBubble(streamDiv2, accumulated2); }
                                        } catch (e) {}
                                    }
                                    return pump2();
                                });
                            }

                            function finalize2() {
                                if (accumulated2) {
                                    finalizeStreamingBubble(streamDiv2, accumulated2);
                                    messages.push({ role: 'assistant', content: accumulated2 });
                                    saveHistory();
                                } else {
                                    streamDiv2.remove();
                                }
                                setStreaming(false);
                            }
                            return pump2();
                        })
                        .catch(err => {
                            streamDiv2.remove();
                            addErrorBubble(err.message || 'Follow-up request failed.');
                            setStreaming(false);
                        });
                    });
                    return; // setStreaming will be called in the follow-up flow
                }

                finalizeStreamingBubble(streamDiv, accumulated);
                messages.push({ role: 'assistant', content: accumulated });
                saveHistory();
                setStreaming(false);
            }

            return pump();
        })
        .catch(err => {
            streamDiv.remove();
            addErrorBubble(err.message || 'Failed to connect to AI Butler.');
            setStreaming(false);
        });
    }

    // --- History persistence ---

    function saveHistory() {
        try {
            // Only save last MAX_HISTORY messages
            const toSave = messages.slice(-MAX_HISTORY);
            localStorage.setItem(STORAGE_KEY, JSON.stringify(toSave));
        } catch (e) {
            // localStorage full or unavailable
        }
    }

    function restoreHistory() {
        try {
            const saved = localStorage.getItem(STORAGE_KEY);
            if (!saved) return;
            const parsed = JSON.parse(saved);
            if (!Array.isArray(parsed)) return;
            messages = parsed;
            // Render saved messages (skip the initial greeting bubble by clearing first)
            if (messages.length > 0) {
                messagesEl.innerHTML = '';
                for (const msg of messages) {
                    if (msg.role === 'user') {
                        addUserBubble(msg.content);
                    } else if (msg.role === 'assistant') {
                        addAssistantBubble(msg.content);
                    }
                }
            }
        } catch (e) {
            messages = [];
        }
    }

    // --- Utilities ---

    function escapeHtml(text) {
        return text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    /**
     * Minimal Markdown renderer: bold, italic, inline code, code blocks, bullet lists, line breaks.
     * Not a full parser — covers the most common AI output patterns.
     */
    function renderMarkdown(text) {
        // Code blocks (``` ... ```)
        text = text.replace(/```[\w]*\n?([\s\S]*?)```/g, function (_, code) {
            return '<pre class="ai-butler-code"><code>' + escapeHtml(code.trim()) + '</code></pre>';
        });

        // Escape HTML in remaining text (outside code blocks)
        // We already escaped code blocks above; escape the rest
        // Split by pre tags to avoid double-escaping
        const parts = text.split(/(<pre[\s\S]*?<\/pre>)/);
        text = parts.map((p, i) => {
            if (i % 2 === 1) return p; // already a <pre> block
            return p
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;');
        }).join('');

        // Inline code `...`
        text = text.replace(/`([^`]+)`/g, '<code class="ai-butler-inline-code">$1</code>');

        // Markdown links [text](url) — allow any https:// URL or relative /path
        text = text.replace(/\[([^\]]+)\]\(((?:https?:\/\/[^)]+|\/[^)]+))\)/g, function (_, label, href) {
            const isExternal = href.startsWith('http');
            return '<a href="' + href + '" class="ai-butler-link"'
                + (isExternal ? ' target="_blank" rel="noopener"' : '')
                + '>' + label + '</a>';
        });

        // Bold **text**
        text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

        // Italic *text* (not inside **)
        text = text.replace(/\*([^*]+)\*/g, '<em>$1</em>');

        // Bullet lists (lines starting with - or *)
        text = text.replace(/^[ \t]*[-*] (.+)$/gm, '<li>$1</li>');
        text = text.replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>');
        // Collapse nested <ul> wrapping
        text = text.replace(/<\/ul>\s*<ul>/g, '');

        // Headings ### ## #
        text = text.replace(/^### (.+)$/gm, '<h4>$1</h4>');
        text = text.replace(/^## (.+)$/gm, '<h3>$1</h3>');
        text = text.replace(/^# (.+)$/gm, '<h2>$1</h2>');

        // Line breaks
        text = text.replace(/\n/g, '<br>');

        // Clean up <br> inside block elements
        text = text.replace(/<br>\s*(<\/?(?:ul|li|pre|h[2-4]))/g, '$1');
        text = text.replace(/(<\/?(?:ul|li|pre|h[2-4])[^>]*>)\s*<br>/g, '$1');

        return text;
    }

    // --- Phase 2, 4 & 6: APPLY_SETTING, ADD_TO_LIBRARY, SEARCH_LIBRARY parsing ---

    /**
     * Parse SEARCH_LIBRARY: {...} lines out of AI response text.
     * Returns array of {raw: string, json: object, type: 'search'}.
     * The AI uses these to request a ground-truth DB lookup rather than self-verifying.
     */
    function parseSearchBlocks(text) {
        const blocks = [];
        const re = /^SEARCH_LIBRARY:\s*(\{[^\n]+\})\s*$/gm;
        let m;
        while ((m = re.exec(text)) !== null) {
            try {
                const obj = JSON.parse(m[1]);
                if (obj.title || obj.imdb_id || obj.q) {
                    blocks.push({ raw: m[0], json: obj, type: 'search' });
                }
            } catch (e) {
                // malformed JSON — skip
            }
        }
        return blocks;
    }

    /**
     * Perform all SEARCH_LIBRARY lookups found in the AI's response text.
     * Returns a promise that resolves to an array of result strings to inject back.
     */
    function executeSearchBlocks(searchBlocks) {
        const promises = searchBlocks.map(function (block) {
            const obj = block.json;
            const q = obj.q || obj.title || '';
            const typeParam = obj.type ? '&type=' + encodeURIComponent(obj.type) : '';
            const url = '/api/ai/tools/search_library?q=' + encodeURIComponent(q) + typeParam;
            return fetch(url)
                .then(r => r.json())
                .then(data => {
                    if (data.error) {
                        return '[SEARCH_RESULT] query="' + q + '" error: ' + data.error;
                    }
                    if (!data.items || data.items.length === 0) {
                        return '[SEARCH_RESULT] query="' + q + '" -> NOT FOUND in library (count=0). Safe to recommend.';
                    }
                    const lines = data.items.map(function (item) {
                        // Determine link URL based on collection state and media type
                        let linkUrl = null;
                        const isCollected = item.state === 'Collected' || item.state === 'Upgrading';
                        const tmdb = item.tmdb_id;
                        const isTv = item.type === 'episode';
                        if (isCollected && tmdb) {
                            linkUrl = isTv ? '/library/show/' + tmdb : '/library/movie/' + tmdb;
                        } else if (!isCollected && tmdb) {
                            linkUrl = '/discover/details/' + tmdb + (isTv ? '/tv' : '/movie');
                        }
                        const linkHint = linkUrl ? ' link=' + linkUrl : '';  // for AI context only
                        return item.title + ' (' + (item.year || '?') + ') [' + item.type + '] state=' + item.state + (item.imdb_id ? ' imdb=' + item.imdb_id : '') + (tmdb ? ' tmdb=' + tmdb : '') + linkHint;
                    });
                    return '[SEARCH_RESULT] query="' + q + '" -> FOUND ' + data.count + ' match(es):\n' + lines.join('\n');
                })
                .catch(function (err) {
                    return '[SEARCH_RESULT] query="' + q + '" error: ' + err.message;
                });
        });
        return Promise.all(promises);
    }

    /**
     * Parse APPLY_SETTING: {...} lines out of AI response text.
     * Returns array of {raw: string, json: object} for each valid block found.
     */
    function parseApplyBlocks(text) {
        const blocks = [];
        const re = /^APPLY_SETTING:\s*(\{[^\n]+\})\s*$/gm;
        let m;
        while ((m = re.exec(text)) !== null) {
            try {
                const obj = JSON.parse(m[1]);
                if (obj.section && obj.key && obj.value !== undefined) {
                    blocks.push({ raw: m[0], json: obj, type: 'apply' });
                }
            } catch (e) {
                // malformed JSON — skip
            }
        }
        return blocks;
    }

    /**
     * Parse ADD_TO_LIBRARY: {...} lines out of AI response text.
     * Returns array of {raw: string, json: object, type: 'library'} for each valid block.
     */
    function parseLibraryBlocks(text) {
        const blocks = [];
        const re = /^ADD_TO_LIBRARY:\s*(\{[^\n]+\})\s*$/gm;
        let m;
        while ((m = re.exec(text)) !== null) {
            try {
                const obj = JSON.parse(m[1]);
                if (obj.title) {
                    blocks.push({ raw: m[0], json: obj, type: 'library' });
                }
            } catch (e) {
                // malformed JSON — skip
            }
        }
        return blocks;
    }

    /**
     * Parse SEND_NOTIFICATION: {...} lines out of AI response text.
     */
    function parseNotificationBlocks(text) {
        const blocks = [];
        const re = /^SEND_NOTIFICATION:\s*(\{[^\n]+\})\s*$/gm;
        let m;
        while ((m = re.exec(text)) !== null) {
            try {
                const obj = JSON.parse(m[1]);
                if (obj.message) {
                    blocks.push({ raw: m[0], json: obj, type: 'notification' });
                }
            } catch (e) {
                // malformed JSON — skip
            }
        }
        return blocks;
    }

    /**
     * Render markdown, replacing APPLY_SETTING:, ADD_TO_LIBRARY:, and SEND_NOTIFICATION: lines with inline card HTML.
     */
    function renderMarkdownWithApply(text) {
        // Strip any SEARCH_LIBRARY blocks from final display — they're intercepted before finalize
        text = text.replace(/^SEARCH_LIBRARY:\s*\{[^\n]+\}\s*$/gm, '').trim();

        const applyBlocks = parseApplyBlocks(text);
        const libraryBlocks = parseLibraryBlocks(text);
        const notifBlocks = parseNotificationBlocks(text);
        const allBlocks = [...applyBlocks, ...libraryBlocks, ...notifBlocks];

        const placeholders = {};
        allBlocks.forEach((block, i) => {
            const key = `\x00BLOCK_${i}\x00`;
            if (block.type === 'library') {
                placeholders[key] = buildLibraryCardHtml(block.json, i);
            } else if (block.type === 'notification') {
                placeholders[key] = buildNotificationCardHtml(block.json, i);
            } else {
                placeholders[key] = buildApplyCardHtml(block.json, i);
            }
            text = text.replace(block.raw, key);
        });

        // Run normal markdown render
        let html = renderMarkdown(text);

        // Restore cards
        for (const [ph, card] of Object.entries(placeholders)) {
            html = html.split(ph).join(card);
        }
        return html;
    }

    function buildApplyCardHtml(obj, idx) {
        const section = escapeHtml(obj.section || '');
        const key = escapeHtml(obj.key || '');
        const value = escapeHtml(String(obj.value));
        const reason = escapeHtml(obj.reason || '');
        const dataB64 = btoa(unescape(encodeURIComponent(JSON.stringify(obj))));
        return `<div class="ai-apply-card" data-apply-idx="${idx}">` +
            `<div class="ai-apply-card-setting"><span class="ai-apply-section">${section}</span>` +
            ` → <span class="ai-apply-key">${key}</span>` +
            ` = <span class="ai-apply-value">${value}</span></div>` +
            (reason ? `<div class="ai-apply-reason">${reason}</div>` : '') +
            `<div class="ai-apply-actions">` +
            `<button class="ai-apply-btn" data-apply-b64="${dataB64}">Apply</button>` +
            `<span class="ai-apply-status"></span>` +
            `</div></div>`;
    }

    function buildLibraryCardHtml(obj, idx) {
        const title = escapeHtml(obj.title || '');
        const year = obj.year ? escapeHtml(String(obj.year)) : '';
        const mediaType = (obj.media_type || 'movie');
        const reason = escapeHtml(obj.reason || '');
        const label = mediaType === 'tv' ? 'TV Show' : 'Movie';
        const yearStr = year ? ` (${year})` : '';
        const dataB64 = btoa(unescape(encodeURIComponent(JSON.stringify(obj))));

        // Build discover link
        let discoverLink = '';
        if (obj.tmdb_id) {
            const discoverType = mediaType === 'tv' ? 'tv' : 'movie';
            const tmdbInt = parseInt(obj.tmdb_id, 10);
            if (tmdbInt) {
                const discoverUrl = '/discover/details/' + tmdbInt + '/' + discoverType;
                discoverLink = ` <a href="${discoverUrl}" class="ai-library-discover-link" title="View on Discover">&#x1F517;</a>`;
            }
        }

        // Build version checkboxes from cached versions (populated async before render)
        let versionsHtml = '';
        if (_availableVersions && _availableVersions.length > 0) {
            const checks = _availableVersions.map(v =>
                `<label class="ai-library-version-label">` +
                `<input type="checkbox" class="ai-library-version-cb" value="${escapeHtml(v)}" checked> ${escapeHtml(v)}` +
                `</label>`
            ).join('');
            versionsHtml = `<div class="ai-library-versions">${checks}</div>`;
        }

        return `<div class="ai-library-card" data-library-idx="${idx}">` +
            `<div class="ai-library-card-title">` +
            `<span class="ai-library-type-badge">${escapeHtml(label)}</span> ` +
            `<span class="ai-library-title">${title}${yearStr}</span>${discoverLink}` +
            `</div>` +
            (reason ? `<div class="ai-library-reason">${reason}</div>` : '') +
            versionsHtml +
            `<div class="ai-library-actions">` +
            `<button class="ai-library-btn" data-library-b64="${dataB64}">Add to Library</button>` +
            `<span class="ai-library-status"></span>` +
            `</div></div>`;
    }

    function buildNotificationCardHtml(obj, idx) {
        const title = escapeHtml(obj.title || 'AI Butler');
        const message = escapeHtml(obj.message || '');
        const dataB64 = btoa(unescape(encodeURIComponent(JSON.stringify(obj))));
        return `<div class="ai-notification-card" data-notification-idx="${idx}">` +
            `<div class="ai-notification-card-title">&#x1F514; Send notification: <span class="ai-notification-title">${title}</span></div>` +
            `<div class="ai-notification-message">${message}</div>` +
            `<div class="ai-notification-actions">` +
            `<button class="ai-notification-btn" data-notification-b64="${dataB64}">Send</button>` +
            `<span class="ai-notification-status"></span>` +
            `</div></div>`;
    }

    /**
     * Wire up click handlers on .ai-apply-btn, .ai-library-btn, and .ai-notification-btn elements inside a container.
     * Called after rendering any bubble that may contain action cards.
     */
    function attachApplyHandlers(container) {
        container.querySelectorAll('.ai-apply-btn').forEach(btn => {
            btn.addEventListener('click', function () {
                if (btn.disabled) return;
                let obj;
                try { obj = JSON.parse(decodeURIComponent(escape(atob(btn.dataset.applyB64)))); } catch (e) { return; }
                applySettingFromCard(btn, obj);
            });
        });
        container.querySelectorAll('.ai-library-btn').forEach(btn => {
            btn.addEventListener('click', function () {
                if (btn.disabled) return;
                let obj;
                try { obj = JSON.parse(decodeURIComponent(escape(atob(btn.dataset.libraryB64)))); } catch (e) { return; }
                addToLibraryFromCard(btn, obj);
            });
        });
        container.querySelectorAll('.ai-notification-btn').forEach(btn => {
            btn.addEventListener('click', function () {
                if (btn.disabled) return;
                let obj;
                try { obj = JSON.parse(decodeURIComponent(escape(atob(btn.dataset.notificationB64)))); } catch (e) { return; }
                sendNotificationFromCard(btn, obj);
            });
        });
    }

    function applySettingFromCard(btn, obj) {
        const card = btn.closest('.ai-apply-card');
        const statusEl = card ? card.querySelector('.ai-apply-status') : null;

        btn.disabled = true;
        btn.textContent = 'Applying…';
        if (statusEl) statusEl.textContent = '';

        fetch('/api/ai/apply_setting', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ section: obj.section, key: obj.key, value: obj.value })
        })
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                btn.textContent = 'Applied ✓';
                btn.classList.add('applied');
                if (statusEl) statusEl.textContent = '';

                if (data.requires_restart) {
                    const restartNote = document.createElement('div');
                    restartNote.className = 'ai-apply-restart-note';
                    restartNote.innerHTML =
                        'Setting saved. A restart is required for changes to take effect. ' +
                        '<button class="ai-apply-restart-btn">Restart now</button>';
                    restartNote.querySelector('.ai-apply-restart-btn').addEventListener('click', function () {
                        triggerRestart(restartNote);
                    });
                    card.appendChild(restartNote);
                }
            } else {
                btn.disabled = false;
                btn.textContent = 'Apply';
                if (statusEl) {
                    statusEl.textContent = data.error || 'Failed';
                    statusEl.classList.add('error');
                }
            }
        })
        .catch(() => {
            btn.disabled = false;
            btn.textContent = 'Apply';
            if (statusEl) {
                statusEl.textContent = 'Request failed';
                statusEl.classList.add('error');
            }
        });
    }

    function addToLibraryFromCard(btn, obj) {
        const card = btn.closest('.ai-library-card');
        const statusEl = card ? card.querySelector('.ai-library-status') : null;

        btn.disabled = true;
        btn.textContent = 'Adding…';
        if (statusEl) statusEl.textContent = '';

        // Collect selected versions from checkboxes in this card
        const selectedVersions = [];
        if (card) {
            card.querySelectorAll('.ai-library-version-cb:checked').forEach(cb => {
                selectedVersions.push(cb.value);
            });
        }

        fetch('/api/ai/add_to_library', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                imdb_id: obj.imdb_id || '',
                title: obj.title || '',
                year: obj.year || null,
                media_type: obj.media_type || 'movie',
                selected_versions: selectedVersions.length > 0 ? selectedVersions : null
            })
        })
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                btn.textContent = 'Added ✓';
                btn.classList.add('applied');
                if (statusEl) {
                    statusEl.textContent = 'Queued for download';
                    statusEl.classList.remove('error');
                }
            } else {
                btn.disabled = false;
                btn.textContent = 'Add to Library';
                if (statusEl) {
                    statusEl.textContent = data.error || 'Failed';
                    statusEl.classList.add('error');
                }
            }
        })
        .catch(() => {
            btn.disabled = false;
            btn.textContent = 'Add to Library';
            if (statusEl) {
                statusEl.textContent = 'Request failed';
                statusEl.classList.add('error');
            }
        });
    }

    function sendNotificationFromCard(btn, obj) {
        const card = btn.closest('.ai-notification-card');
        const statusEl = card ? card.querySelector('.ai-notification-status') : null;

        btn.disabled = true;
        btn.textContent = 'Sending…';
        if (statusEl) statusEl.textContent = '';

        fetch('/api/ai/tools/send_notification', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: obj.title || 'AI Butler', message: obj.message || '' })
        })
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                btn.textContent = 'Sent ✓';
                btn.classList.add('applied');
                if (statusEl) { statusEl.textContent = 'Notification sent'; statusEl.classList.remove('error'); }
            } else {
                btn.disabled = false;
                btn.textContent = 'Send';
                if (statusEl) {
                    statusEl.textContent = data.message || data.error || 'No channels configured';
                    statusEl.classList.add('error');
                }
            }
        })
        .catch(() => {
            btn.disabled = false;
            btn.textContent = 'Send';
            if (statusEl) { statusEl.textContent = 'Request failed'; statusEl.classList.add('error'); }
        });
    }

    function triggerRestart(noteEl) {
        const btn = noteEl.querySelector('.ai-apply-restart-btn');
        if (btn) { btn.disabled = true; btn.textContent = 'Restarting…'; }

        fetch('/api/ai/restart', { method: 'POST' })
        .then(r => r.json())
        .then(() => {
            if (noteEl) noteEl.innerHTML = '<em>Restart initiated. The page will become available again shortly.</em>';
        })
        .catch(() => {
            if (btn) { btn.disabled = false; btn.textContent = 'Restart now'; }
        });
    }

    // Init on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
