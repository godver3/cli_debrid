/* Moved from templates/badges.html. */
// ── State ────────────────────────────────────────────────────────────────────
let allBadgeTypes = [];
let currentCategory = 'all';

// ── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    loadStats();
    loadBadgeTypes();

    document.querySelectorAll('.category-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.category-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            currentCategory = tab.dataset.category;
            renderBadgeTypes();
        });
    });
});

// ── Stats ─────────────────────────────────────────────────────────────────────
async function loadStats() {
    try {
        const r = await fetch('/api/overlays/badges/stats');
        const d = await r.json();
        if (d.success) {
            document.getElementById('stat-types').textContent      = d.stats.total_types;
            document.getElementById('stat-variations').textContent = d.stats.total_variations;
            document.getElementById('stat-filled').textContent     = d.stats.filled_variations;
            document.getElementById('stat-empty').textContent      = d.stats.empty_variations;
        }
    } catch(e) { console.error('Stats failed:', e); }
}

// ── Load badge types ──────────────────────────────────────────────────────────
async function loadBadgeTypes() {
    const container = document.getElementById('badge-types-container');
    try {
        const r = await fetch('/api/overlays/badges/types');
        const d = await r.json();
        if (!d.success) throw new Error(d.error);
        allBadgeTypes = d.badge_types;
        renderBadgeTypes();
    } catch(e) {
        container.innerHTML = `<div class="loading-msg" style="color:#dc3545;">Failed to load badge types: ${e.message}</div>`;
    }
}

// ── Render badge type cards ───────────────────────────────────────────────────
function renderBadgeTypes() {
    const container = document.getElementById('badge-types-container');
    const filtered = currentCategory === 'all'
        ? allBadgeTypes
        : allBadgeTypes.filter(t => t.category === currentCategory);

    if (filtered.length === 0) {
        container.innerHTML = '<div class="loading-msg">No badge types found for this category.</div>';
        return;
    }

    container.innerHTML = filtered.map(t => renderBadgeTypeCard(t)).join('');

    // Attach file-upload handlers
    container.querySelectorAll('.variation-slot').forEach(slot => {
        // Clicking the slot (or its preview/placeholder) triggers file upload
        slot.addEventListener('click', (e) => {
            if (e.target.closest('.variation-actions')) return; // handled by buttons
            const vid = slot.dataset.variationId;
            if (vid) triggerFileUpload(vid, slot.dataset.slug);
        });
    });
}

function renderBadgeTypeCard(t) {
    const compositeClass = t.is_composite ? 'composite' : '';
    const compositeBadge = t.is_composite ? '<span class="composite-badge">Composite</span>' : '';
    const filled = t.filled_variations || 0;
    const total  = t.total_variations  || 0;
    return `
    <div class="badge-type-card" id="type-card-${t.slug}">
        <div class="badge-type-header">
            <div>
                <div class="badge-type-title">${t.display_name}${compositeBadge}</div>
                <div class="badge-type-meta">Category: ${t.category} &nbsp;|&nbsp; Fields: ${(t.metadata_fields || []).join(', ')}</div>
            </div>
            <div class="badge-type-actions">
                <span class="fill-indicator">${filled}/${total} assets uploaded</span>
            </div>
        </div>
        <div class="variations-grid ${compositeClass}" id="grid-${t.slug}">
            <div class="loading-msg" style="padding:20px; grid-column:1/-1;">Loading variations...</div>
        </div>
        <div>
            <button class="btn btn-secondary btn-add-variation" onclick="openAddVariationModal('${t.slug}')">+ Add Custom Variation</button>
        </div>
    </div>`;
}

// ── Load variations for a type (lazy, triggered after render) ─────────────────
async function loadVariations(slug) {
    const grid = document.getElementById(`grid-${slug}`);
    if (!grid) return;
    try {
        const r = await fetch(`/api/overlays/badges/types/${slug}/variations`);
        const d = await r.json();
        if (!d.success) throw new Error(d.error);
        renderVariationSlots(grid, slug, d.variations, d.badge_type?.is_composite);
    } catch(e) {
        if (grid) grid.innerHTML = `<div style="padding:16px;color:#dc3545;">Failed: ${e.message}</div>`;
    }
}

function renderVariationSlots(grid, slug, variations, isComposite) {
    if (variations.length === 0) {
        grid.innerHTML = '<div style="padding:16px;color:var(--text-secondary)">No variations defined.</div>';
        return;
    }
    grid.innerHTML = variations.map(v => renderSlot(slug, v)).join('');
}

function renderSlot(slug, v) {
    const hasUserAsset   = v.has_asset;
    const hasSystemAsset = v.has_system_asset;
    const hasAnyAsset    = hasUserAsset || hasSystemAsset;

    // Border: green = user upload, blue = system default only, dashed = nothing
    let slotClass = 'variation-slot';
    if (hasUserAsset)        slotClass += ' has-asset';           // green border
    else if (hasSystemAsset) slotClass += ' has-system-asset';    // blue border
    if (!v.is_default)       slotClass += ' custom-slot';

    const preview = hasAnyAsset
        ? `<img class="variation-badge-preview" src="/api/overlays/badges/variations/${v.id}/asset?t=${Date.now()}" alt="${v.display_name}" loading="lazy">`
        : `<div class="variation-placeholder">+</div>`;

    const sourceTag = hasUserAsset
        ? `<span class="asset-source-tag user-tag">Custom</span>`
        : hasSystemAsset
            ? `<span class="asset-source-tag system-tag">Default</span>`
            : '';

    const deleteBtn = !v.is_default
        ? `<button class="btn btn-danger btn-sm" onclick="deleteVariation(${v.id},'${slug}',event)">Del</button>`
        : '';
    const clearBtn = hasUserAsset
        ? `<button class="btn btn-secondary btn-sm" onclick="clearAsset(${v.id},'${slug}',event)">Clear</button>`
        : '';

    return `
    <div class="${slotClass}" data-variation-id="${v.id}" data-slug="${slug}" title="${hasSystemAsset && !hasUserAsset ? 'System default — upload to override' : 'Click to upload badge PNG'}">
        ${preview}
        ${sourceTag}
        <div class="variation-name">${v.display_name}</div>
        <div class="variation-key">${v.variation_key}</div>
        <div class="variation-actions">
            <button class="btn btn-primary btn-sm" onclick="triggerFileUpload(${v.id},'${slug}',event)">${hasUserAsset ? 'Replace' : 'Upload'}</button>
            ${clearBtn}
            ${deleteBtn}
        </div>
    </div>`;
}

// ── Observe when grids become visible and lazy-load their variations ───────────
const observedSlugs = new Set();
function observeGrids() {
    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                const slug = entry.target.id.replace('grid-', '');
                if (!observedSlugs.has(slug)) {
                    observedSlugs.add(slug);
                    loadVariations(slug);
                }
            }
        });
    }, { threshold: 0.1 });

    document.querySelectorAll('[id^="grid-"]').forEach(g => observer.observe(g));
}

// Override renderBadgeTypes to also set up observers
const _origRender = renderBadgeTypes;
window.renderBadgeTypes = function() {
    observedSlugs.clear();
    _origRender();
    setTimeout(observeGrids, 50);
};

// ── Toast notification ────────────────────────────────────────────────────────
function showToast(msg, duration = 4000) {
    let t = document.getElementById('badge-toast');
    if (!t) {
        t = document.createElement('div');
        t.id = 'badge-toast';
        t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#28a745;color:#fff;padding:10px 20px;border-radius:6px;font-size:14px;z-index:9999;box-shadow:0 2px 8px rgba(0,0,0,.3);transition:opacity .3s;';
        document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.opacity = '1';
    clearTimeout(t._hideTimer);
    t._hideTimer = setTimeout(() => { t.style.opacity = '0'; }, duration);
}

// ── File upload ───────────────────────────────────────────────────────────────
function triggerFileUpload(variationId, slug, event) {
    if (event) event.stopPropagation();
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/png,image/jpeg,image/webp';
    input.onchange = async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        const fd = new FormData();
        fd.append('file', file);
        try {
            const r = await fetch(`/api/overlays/badges/variations/${variationId}/asset`, {
                method: 'POST', body: fd
            });
            const d = await r.json();
            if (d.success) {
                loadStats();
                observedSlugs.delete(slug);
                loadVariations(slug);
                refreshTypeCard(slug);
                const msg = d.reset_overlays > 0
                    ? `Badge saved. ${d.reset_overlays} applied overlay(s) queued for regeneration.`
                    : 'Badge saved.';
                showToast(msg);
            } else {
                showPopup({ type: 'error', title: 'Error', message: 'Upload failed: ' + (d.error || 'Unknown error'), autoClose: 5000 });
            }
        } catch(e) {
            showPopup({ type: 'error', title: 'Error', message: 'Upload failed: ' + e.message, autoClose: 5000 });
        }
    };
    input.click();
}

async function clearAsset(variationId, slug, event) {
    if (event) event.stopPropagation();
    showPopup({
        type: 'confirm',
        title: 'Remove Asset',
        message: 'Remove this badge asset? The slot will remain but the PNG will be deleted.',
        confirmText: 'Remove',
        cancelText: 'Cancel',
        onConfirm: async function() {
            try {
                const r = await fetch(`/api/overlays/badges/variations/${variationId}/asset`, { method: 'DELETE' });
                const d = await r.json();
                if (d.success) {
                    loadStats();
                    observedSlugs.delete(slug);
                    loadVariations(slug);
                    refreshTypeCard(slug);
                } else {
                    showPopup({ type: 'error', title: 'Error', message: 'Clear failed: ' + (d.error || 'Unknown error'), autoClose: 5000 });
                }
            } catch(e) {
                showPopup({ type: 'error', title: 'Error', message: 'Clear failed: ' + e.message, autoClose: 5000 });
            }
        }
    });
}

async function deleteVariation(variationId, slug, event) {
    if (event) event.stopPropagation();
    showPopup({
        type: 'confirm',
        title: 'Delete Variation',
        message: 'Delete this custom variation slot? This cannot be undone.',
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
            try {
                const r = await fetch(`/api/overlays/badges/variations/${variationId}`, { method: 'DELETE' });
                const d = await r.json();
                if (d.success) {
                    loadStats();
                    observedSlugs.delete(slug);
                    loadVariations(slug);
                    refreshTypeCard(slug);
                } else {
                    showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + (d.error || 'Unknown error'), autoClose: 5000 });
                }
            } catch(e) {
                showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + e.message, autoClose: 5000 });
            }
        }
    });
}

// Refresh just the fill indicator for a card header
async function refreshTypeCard(slug) {
    try {
        const r = await fetch(`/api/overlays/badges/types/${slug}/variations`);
        const d = await r.json();
        if (!d.success) return;
        const filled = d.variations.filter(v => v.has_asset).length;
        const total  = d.variations.length;
        const card = document.getElementById(`type-card-${slug}`);
        if (card) {
            const ind = card.querySelector('.fill-indicator');
            if (ind) ind.textContent = `${filled}/${total} assets uploaded`;
        }
        // Update allBadgeTypes cache
        const idx = allBadgeTypes.findIndex(t => t.slug === slug);
        if (idx >= 0) {
            allBadgeTypes[idx].filled_variations = filled;
            allBadgeTypes[idx].total_variations  = total;
        }
    } catch(e) { /* ignore */ }
}

// ── Add variation modal ───────────────────────────────────────────────────────
function openAddVariationModal(slug) {
    document.getElementById('add-variation-slug').value = slug;
    document.getElementById('new-variation-key').value  = '';
    document.getElementById('new-variation-name').value = '';
    document.getElementById('add-variation-modal').classList.add('active');
    document.getElementById('new-variation-key').focus();
}

function closeAddVariationModal() {
    document.getElementById('add-variation-modal').classList.remove('active');
}

async function submitAddVariation() {
    const slug    = document.getElementById('add-variation-slug').value;
    const key     = document.getElementById('new-variation-key').value.trim();
    const name    = document.getElementById('new-variation-name').value.trim();
    if (!key || !name) { showPopup({ type: 'warning', title: 'Required', message: 'Please fill in both fields.', autoClose: 4000 }); return; }

    try {
        const r = await fetch(`/api/overlays/badges/types/${slug}/variations`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({variation_key: key, display_name: name})
        });
        const d = await r.json();
        if (d.success) {
            closeAddVariationModal();
            observedSlugs.delete(slug);
            loadVariations(slug);
            refreshTypeCard(slug);
            loadStats();
        } else {
            showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + (d.error || 'Unknown error'), autoClose: 5000 });
        }
    } catch(e) {
        showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + e.message, autoClose: 5000 });
    }
}

// Close modal on overlay click
document.getElementById('add-variation-modal').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeAddVariationModal();
});
