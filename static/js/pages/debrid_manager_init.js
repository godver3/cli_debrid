/* Moved from templates/debrid_manager.html. */
// ── INIT ───────────────────────────────────────────────────────────────────
// Loaded after every Debrid Manager section, because the initial-tab loads
// call functions from the later ones (battery audit, symlink audit).
if (_dmActiveTab === 'reconcile') { loadReconcile(false); loadBatteryAudit(false); if (typeof _slInitAudit === 'function') _slInitAudit(); }

// Only start active polling if active tab is shown on load
if (_dmActiveTab === 'active') fetchActive();
