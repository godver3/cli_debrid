/* Moved from templates/settings_tabs/required.html. */
(function() {
    const PROVIDER_CHOICES = ['RealDebrid','AllDebrid','Premiumize','Torbox','DebridLink'];

    function buildRow(name, key) {
        const row = document.createElement('div');
        row.className = 'fallback-provider-row settings-form-group';
        row.style.cssText = 'display:flex;flex-direction:column;gap:8px;align-items:baseline;margin-bottom:8px';
        const sel = document.createElement('select');
        sel.className = 'settings-input fallback-provider-name';
        sel.style.cssText = 'flex:0 0 150px';
        PROVIDER_CHOICES.forEach(c => {
            const o = document.createElement('option');
            o.value = c; o.textContent = c;
            if (c === name) o.selected = true;
            sel.appendChild(o);
        });
        const inp = document.createElement('input');
        inp.type = 'password'; inp.className = 'settings-input fallback-provider-key';
        inp.placeholder = 'API key'; inp.value = key || '';
        inp.style.cssText = 'flex:1;min-width:0';
        const btn = document.createElement('button');
        btn.type = 'button'; btn.className = 'delete-version-btn btn-danger fallback-provider-remove';
        btn.innerHTML = '<i class="fa-solid fa-trash"></i> <span class="btn-text">Remove</span>';
        btn.addEventListener('click', () => { row.remove(); saveFallbacks(); });
        sel.addEventListener('change', saveFallbacks);
        inp.addEventListener('change', saveFallbacks);
        row.appendChild(sel); row.appendChild(inp); row.appendChild(btn);
        return row;
    }

    function saveFallbacks() {
        const rows = document.querySelectorAll('#fallback-providers-list .fallback-provider-row');
        const fallbacks = [];
        rows.forEach(row => {
            const name = row.querySelector('.fallback-provider-name')?.value;
            const key  = row.querySelector('.fallback-provider-key')?.value;
            if (name && key) fallbacks.push({provider: name, api_key: key});
        });
        // Store as JSON in a hidden input that the settings save picks up
        let hidden = document.getElementById('fallback-providers-hidden');
        if (!hidden) {
            hidden = document.createElement('input');
            hidden.type = 'hidden'; hidden.id = 'fallback-providers-hidden';
            hidden.name = 'Debrid Provider.fallback_providers';
            hidden.dataset.section = 'Debrid Provider';
            hidden.dataset.key = 'fallback_providers';
            hidden.dataset.jsonList = 'true';
            document.getElementById('fallback-providers-list').after(hidden);
        }
        hidden.value = JSON.stringify(fallbacks);
    }

    document.querySelectorAll('#fallback-providers-list .fallback-provider-remove').forEach(btn => {
        btn.addEventListener('click', () => { btn.closest('.fallback-provider-row').remove(); saveFallbacks(); });
    });
    document.querySelectorAll('#fallback-providers-list .fallback-provider-name, #fallback-providers-list .fallback-provider-key').forEach(el => {
        el.addEventListener('change', saveFallbacks);
    });
    document.getElementById('add-fallback-provider-btn')?.addEventListener('click', () => {
        const row = buildRow('Torbox', '');
        document.getElementById('fallback-providers-list').appendChild(row);
        saveFallbacks();
    });
    // Initialize hidden input on load
    saveFallbacks();
})();
