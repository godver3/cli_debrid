// Tags pill input for Content Sources (Plex mode)
// Manages global tag list stored in config Tags.tags_list

let _csTags = [];

async function csTagsLoad() {
    try {
        const r = await fetch('/settings/api/config');
        const cfg = await r.json();
        _csTags = (cfg['Tags'] || {})['tags_list'] || [];
    } catch(e) { _csTags = []; }
}

function csTagsGet(sourceId) {
    const h = document.getElementById(`tags-hidden-${sourceId}`);
    if (!h || !h.value.trim()) return [];
    return h.value.split(',').map(t => t.trim()).filter(Boolean);
}

function csTagsSet(sourceId, tags) {
    const h = document.getElementById(`tags-hidden-${sourceId}`);
    if (h) {
        h.value = tags.join(',');
        h.dispatchEvent(new Event('change', { bubbles: true }));
    }
}

function csTagsRender(sourceId) {
    const container = document.getElementById(`tags-pill-input-${sourceId}`);
    if (!container) return;
    container.querySelectorAll('.tag-pill').forEach(p => p.remove());
    const tags = csTagsGet(sourceId);
    const wrapper = container.querySelector('.tags-input-wrapper');
    tags.forEach(tag => {
        const pill = document.createElement('span');
        pill.className = 'tag-pill'; pill.dataset.tag = tag;
        pill.innerHTML = `${tag}<button type="button" class="tag-pill-remove" onclick="csTagsRemove('${sourceId}','${tag}')">&times;</button>`;
        container.insertBefore(pill, wrapper);
    });
}

function csTagsAdd(sourceId, tag) {
    tag = tag.trim();
    if (!tag) return;
    const tags = csTagsGet(sourceId);
    if (tags.includes(tag)) return;
    tags.push(tag);
    if (!_csTags.includes(tag)) { _csTags.push(tag); csTagsSave(); }
    csTagsSet(sourceId, tags);
    csTagsRender(sourceId);
    const inp = document.getElementById(`tags-search-${sourceId}`);
    if (inp) { inp.value = ''; csTagsFilter(sourceId); }
}

function csTagsRemove(sourceId, tag) {
    csTagsSet(sourceId, csTagsGet(sourceId).filter(t => t !== tag));
    csTagsRender(sourceId);
}

function csTagsFilter(sourceId) {
    const inp = document.getElementById(`tags-search-${sourceId}`);
    const dd = document.getElementById(`tags-dropdown-${sourceId}`);
    if (!inp || !dd) return;
    const q = inp.value.trim().toLowerCase();
    const current = csTagsGet(sourceId);
    const matches = _csTags.filter(t => t.toLowerCase().includes(q) && !current.includes(t));
    if (!q && !matches.length) { dd.style.display = 'none'; return; }
    dd.innerHTML = '';
    if (q && !_csTags.map(t => t.toLowerCase()).includes(q) && !current.includes(inp.value.trim())) {
        const c = document.createElement('div');
        c.className = 'tags-dropdown-item tags-dropdown-create';
        c.textContent = `+ Create "${inp.value.trim()}"`;
        c.onclick = () => { csTagsAdd(sourceId, inp.value.trim()); dd.style.display = 'none'; };
        dd.appendChild(c);
    }
    matches.forEach(tag => {
        const item = document.createElement('div');
        item.className = 'tags-dropdown-item'; item.textContent = tag;
        item.onclick = () => { csTagsAdd(sourceId, tag); dd.style.display = 'none'; };
        dd.appendChild(item);
    });
    dd.style.display = dd.children.length ? 'block' : 'none';
}

function csTagsKeydown(event, sourceId) {
    if (event.key === 'Enter') {
        event.preventDefault();
        const inp = document.getElementById(`tags-search-${sourceId}`);
        if (inp && inp.value.trim()) csTagsAdd(sourceId, inp.value.trim());
    } else if (event.key === 'Escape') {
        const dd = document.getElementById(`tags-dropdown-${sourceId}`);
        if (dd) dd.style.display = 'none';
    }
}

async function csTagsSave() {
    try {
        await fetch('/settings/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ Tags: { tags_list: _csTags } })
        });
    } catch(e) {}
}

function csTagsUpdateVisibility(sourceId, fileManagementValue) {
    const containers = document.querySelectorAll(`.tags-container[data-source-id="${sourceId}"]`);
    containers.forEach(c => {
        c.style.display = (fileManagementValue === 'Plex') ? 'block' : 'none';
    });
}

// Close dropdowns on outside click
document.addEventListener('click', function(e) {
    if (!e.target.closest('.tags-pill-input')) {
        document.querySelectorAll('.tags-dropdown').forEach(d => d.style.display = 'none');
    }
});

// Initialize on load
csTagsLoad();

// Expose functions globally
window.csTagsAdd = csTagsAdd;
window.csTagsRemove = csTagsRemove;
window.csTagsFilter = csTagsFilter;
window.csTagsKeydown = csTagsKeydown;
window.csTagsUpdateVisibility = csTagsUpdateVisibility;
