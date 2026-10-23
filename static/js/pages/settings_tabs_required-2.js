/* Moved from templates/settings_tabs/required.html. */
// Provider-aware field visibility (climount vs nzbdav).
// Watches the Provider <select> and hides fields tagged data-provider-only=*.
(function() {
    const sel = document.getElementById('usenet_provider-provider');
    if (!sel) return;
    const apply = () => {
        document.querySelectorAll('[data-provider-only]').forEach(el => {
            const only = el.dataset.providerOnly.split(',').map(s => s.trim());
            el.style.display = only.includes(sel.value) ? '' : 'none';
        });
    };
    sel.addEventListener('change', apply);
    apply();
})();
// NZB naming sub-options show/hide
(function() {
    const cb = document.getElementById('usenet_provider-enable_nzb_naming');
    const sub = document.getElementById('nzb-naming-suboptions');
    if (cb && sub) {
        cb.addEventListener('change', function() { sub.style.display = this.checked ? '' : 'none'; });
    }
})();
