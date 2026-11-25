// Settings → Export / Import (templates/settings_tabs/page_settings_transfer.html)
(function () {
    const API = '/settings/api/settings_transfer';
    let groups = [];
    let fileData = null;
    let previewTimer = null;

    const $ = (id) => document.getElementById(id);
    const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const show = (v) => {
        if (v === null || v === undefined || v === '') return '<i>empty</i>';
        const s = typeof v === 'string' ? v : JSON.stringify(v);
        return esc(s.length > 160 ? s.slice(0, 157) + '…' : s);
    };

    function groupBox(g, opts) {
        const label = document.createElement('label');
        label.className = 'st-group' + (opts.unavailable ? ' st-unavailable' : '');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = g.id;
        cb.checked = !opts.unavailable;
        cb.disabled = !!opts.unavailable;
        const text = document.createElement('div');
        text.innerHTML = `<b>${esc(g.label)} <span class="st-count" data-count="${esc(g.id)}"></span></b><span>${esc(opts.unavailable ? 'Not in this file.' : g.description)}</span>`;
        label.append(cb, text);
        return label;
    }

    const checked = (box) => [...box.querySelectorAll('input:checked')].map((i) => i.value);

    async function loadGroups() {
        const r = await fetch(`${API}/groups`);
        groups = (await r.json()).groups || [];
        const box = $('stExportGroups');
        box.innerHTML = '';
        groups.forEach((g) => box.appendChild(groupBox(g, {})));
    }

    function exportFile() {
        const sel = checked($('stExportGroups'));
        if (!sel.length) {
            window.showPopup && window.showPopup({ type: 'error', message: 'Pick at least one section to export.' });
            return;
        }
        window.location.href = `${API}/export?groups=${encodeURIComponent(sel.join(','))}`;
    }

    function setError(msg) {
        const el = $('stImportError');
        el.textContent = msg || '';
        el.hidden = !msg;
    }

    function resetImport() {
        fileData = null;
        $('stImportFile').value = '';
        $('stFileName').textContent = '';
        $('stImportStep').hidden = true;
        $('stPreview').innerHTML = '';
        setError('');
    }

    async function post(path, body) {
        const r = await fetch(`${API}/${path}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        let data = {};
        try { data = await r.json(); } catch (e) { /* not JSON */ }
        if (!r.ok || data.success === false) throw new Error(data.error || `Request failed (${r.status})`);
        return data;
    }

    function onFile(ev) {
        const file = ev.target.files && ev.target.files[0];
        setError('');
        if (!file) return;
        $('stFileName').textContent = file.name;
        const reader = new FileReader();
        reader.onload = async () => {
            try {
                fileData = JSON.parse(reader.result);
            } catch (e) {
                resetImport();
                setError('That file is not valid JSON.');
                return;
            }
            const inFile = new Set(fileData.groups || groups.map((g) => g.id));
            const box = $('stImportGroups');
            box.innerHTML = '';
            groups.forEach((g) => box.appendChild(groupBox(g, { unavailable: !inFile.has(g.id) })));
            const meta = [];
            if (fileData.app_version) meta.push(`Made with Clyde ${fileData.app_version}`);
            if (fileData.exported_at) meta.push(`on ${String(fileData.exported_at).replace('T', ' ')}`);
            $('stFileMeta').textContent = meta.join(' ');
            $('stImportStep').hidden = false;
            await refreshPreview();
        };
        reader.readAsText(file);
    }

    async function refreshPreview() {
        if (!fileData) return;
        const sel = checked($('stImportGroups'));
        const out = $('stPreview');
        $('stApplyBtn').disabled = true;
        document.querySelectorAll('#stImportGroups [data-count]').forEach((el) => { el.textContent = ''; });
        if (!sel.length) {
            out.innerHTML = '<div class="st-empty">Pick at least one section.</div>';
            return;
        }
        out.innerHTML = '<div class="st-empty">Checking…</div>';
        let data;
        try {
            data = await post('preview', { file: fileData, groups: sel });
        } catch (e) {
            out.innerHTML = '';
            setError(e.message);
            return;
        }
        setError('');
        const counts = {};
        data.changes.forEach((c) => { counts[c.group] = (counts[c.group] || 0) + 1; });
        Object.entries(counts).forEach(([g, n]) => {
            const el = document.querySelector(`#stImportGroups [data-count="${g}"]`);
            if (el) el.textContent = `· ${n} change${n === 1 ? '' : 's'}`;
        });

        let html = '';
        if (data.notes && data.notes.length) {
            html += `<div class="st-notes"><ul>${data.notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul></div>`;
        }
        if (!data.changes.length) {
            html += '<div class="st-empty">Nothing to change: these settings already match.</div>';
        } else {
            groups.forEach((g) => {
                const rows = data.changes.filter((c) => c.group === g.id);
                if (!rows.length) return;
                html += `<h5>${esc(g.label)} (${rows.length})</h5>`;
                html += rows.map((c) => `<div class="st-row"><span class="st-path">${esc(c.path)}</span>`
                    + `<span class="st-old">${c.added ? '<i>new</i>' : show(c.old)}</span>`
                    + `<span class="st-new">${show(c.new)}</span></div>`).join('');
            });
        }
        out.innerHTML = html;
        $('stApplyBtn').disabled = !data.changes.length;
        $('stApplyBtn').textContent = data.changes.length ? `Import ${data.changes.length} change${data.changes.length === 1 ? '' : 's'}` : 'Import';
    }

    async function applyImport() {
        const sel = checked($('stImportGroups'));
        const btn = $('stApplyBtn');
        btn.disabled = true;
        btn.textContent = 'Importing…';
        try {
            const res = await post('apply', { file: fileData, groups: sel });
            window.showPopup && window.showPopup({
                type: 'success',
                message: `Imported ${res.changed} change${res.changed === 1 ? '' : 's'}.` + (res.backup ? ` Backup: ${res.backup}` : ''),
            });
            // The settings form still holds the old values; reload so Save can't write them back.
            setTimeout(() => { window.location.hash = 'settings-transfer'; window.location.reload(); }, 1200);
        } catch (e) {
            setError(e.message);
            btn.disabled = false;
            btn.textContent = 'Import';
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        if (!$('settingsTransferPage')) return;
        loadGroups().catch(() => setError('Could not load the section list.'));
        $('stExportBtn').addEventListener('click', exportFile);
        $('stImportFile').addEventListener('change', onFile);
        $('stCancelBtn').addEventListener('click', resetImport);
        $('stApplyBtn').addEventListener('click', applyImport);
        $('stImportGroups').addEventListener('change', () => {
            clearTimeout(previewTimer);
            previewTimer = setTimeout(refreshPreview, 200);
        });
    });
})();
