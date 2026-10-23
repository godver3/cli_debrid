/* Moved from templates/metadata_debug.html. */
import { showPopup, POPUP_TYPES } from '/static/js/notifications.js';
document.addEventListener('DOMContentLoaded', function() {
    const imdbInput = document.getElementById('imdbIdInput');
    const fetchBtn = document.getElementById('fetchBtn');
    const deleteBtn = document.getElementById('deleteBtn');
    const deleteOnlyBtn = document.getElementById('deleteOnlyBtn');
    const status = document.getElementById('status');
    const result = document.getElementById('result');
    const itemBlock = document.getElementById('itemBlock');
    const metadataBlock = document.getElementById('metadataBlock');
    const seasonsBlock = document.getElementById('seasonsBlock');

    function setStatus(text, isError=false) {
        status.textContent = text || '';
        status.style.color = isError ? '#ff6b6b' : '#888';
    }

    function renderJSON(element, data) {
        try {
            // Deep-parse any string values that are themselves JSON
            function deepParse(obj) {
                if (typeof obj === 'string') {
                    const t = obj.trim();
                    if ((t.startsWith('{') && t.endsWith('}')) || (t.startsWith('[') && t.endsWith(']'))) {
                        try { return deepParse(JSON.parse(t)); } catch {}
                    }
                    return obj;
                }
                if (Array.isArray(obj)) return obj.map(deepParse);
                if (obj && typeof obj === 'object') {
                    const out = {};
                    for (const [k, v] of Object.entries(obj)) out[k] = deepParse(v);
                    return out;
                }
                return obj;
            }
            element.textContent = JSON.stringify(deepParse(data), null, 2);
        }
        catch { element.textContent = 'Unable to render JSON'; }
    }

    function fetchMetadata() {
        const imdbId = (imdbInput.value || '').trim();
        if (!imdbId) { setStatus('Please enter an IMDB ID.', true); return; }
        setStatus('Fetching...');
        result.style.display = 'none';
        fetch(`/metadata/api/item/${encodeURIComponent(imdbId)}`)
            .then(r => r.json())
            .then(data => {
                if (data && !data.error) {
                    renderJSON(itemBlock, data.item);
                    renderJSON(metadataBlock, data.metadata);
                    renderJSON(seasonsBlock, data.seasons);
                    result.style.display = 'block';
                    setStatus('Fetched.');
                } else {
                    setStatus(data && data.error ? data.error : 'Not found.', true);
                }
            })
            .catch(() => setStatus('Error fetching metadata.', true));
    }

    function deleteByImdb() {
        const imdbId = (imdbInput.value || '').trim();
        if (!imdbId) { setStatus('Please enter an IMDB ID.', true); return; }
        showPopup({
            type: POPUP_TYPES.CONFIRM,
            title: 'Confirm',
            message: `Recreate item ${imdbId}?`,
            confirmText: 'Yes',
            cancelText: 'No',
            onConfirm: () => {
                setStatus('Deleting...');
                fetch(`/metadata/api/delete/${encodeURIComponent(imdbId)}`, { method: 'POST' })
                    .then(r => r.json())
                    .then(data => {
                        if (data && data.success) {
                            setStatus('Deleted.');
                            result.style.display = 'none';
                            itemBlock.textContent = '';
                            metadataBlock.textContent = '';
                            seasonsBlock.textContent = '';
                        } else {
                            setStatus('Delete failed.' + (data.error ? ` ${data.error}` : ''), true);
                        }
                    })
                    .catch(() => setStatus('Error deleting item.', true))
                    .finally(() => {
                        fetchMetadata();
                    });
            }
        });
    }

    function deleteOnlyByImdb() {
        const imdbId = (imdbInput.value || '').trim();
        if (!imdbId) { setStatus('Please enter an IMDB ID.', true); return; }
        showPopup({
            type: POPUP_TYPES.CONFIRM,
            title: 'Confirm',
            message: `Delete item ${imdbId}? This will not re-fetch.`,
            confirmText: 'Yes',
            cancelText: 'No',
            onConfirm: () => {
                setStatus('Deleting...');
                fetch(`/metadata/api/delete/${encodeURIComponent(imdbId)}`, { method: 'POST' })
                    .then(r => r.json())
                    .then(data => {
                        if (data && data.success) {
                            setStatus('Deleted.');
                            result.style.display = 'none';
                            itemBlock.textContent = '';
                            metadataBlock.textContent = '';
                            seasonsBlock.textContent = '';
                        } else {
                            setStatus('Delete failed.' + (data.error ? ` ${data.error}` : ''), true);
                        }
                    })
                    .catch(() => setStatus('Error deleting item.', true));
            }
        });
    }

    fetchBtn.addEventListener('click', fetchMetadata);
    deleteBtn.addEventListener('click', deleteByImdb);
    deleteOnlyBtn.addEventListener('click', deleteOnlyByImdb);
    imdbInput.addEventListener('keydown', function(e) { if (e.key === 'Enter') { e.preventDefault(); fetchMetadata(); }});
});
