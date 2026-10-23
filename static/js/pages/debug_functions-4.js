/* Moved from templates/debug_functions.html. */
(function() {
  /* ── Shared helpers ── */
  function mkTable(headers, rows, colStyles) {
    const thStyle = 'padding:7px 10px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#aaa;font-weight:600;border-bottom:1px solid #333;white-space:nowrap';
    const tdStyle = 'padding:7px 10px;border-bottom:1px solid #2a2a2a;color:#e0e0e0;font-size:12px';
    let h = headers.map((h,i) => `<th style="${thStyle}${colStyles&&colStyles[i]?';'+colStyles[i]:''}">${h}</th>`).join('');
    let r = rows.map(cells =>
      '<tr>' + cells.map((c,i) => `<td style="${tdStyle}${colStyles&&colStyles[i]?';'+colStyles[i]:''}">${c}</td>`).join('') + '</tr>'
    ).join('');
    return `<table style="width:100%;border-collapse:collapse">${h?'<thead><tr>'+h+'</tr></thead>':''}${r?'<tbody>'+r+'</tbody>':''}</table>`;
  }

  function loading(el) { el.innerHTML = '<div style="color:#666;font-size:12px;padding:8px 0">Loading…</div>'; }
  function errMsg(el, e) { el.innerHTML = `<div style="color:#ef4444;font-size:12px;padding:8px 0">Error: ${e}</div>`; }

  /* ── API Summary ── */
  function loadApiSummary(timeframe, targetEl) {
    loading(targetEl);
    fetch(`/api_call_summary/api/summary?time_frame=${timeframe}`)
      .then(r => r.json())
      .then(data => {
        const periods = Object.entries(data.periods || {}).sort((a,b) => b[0].localeCompare(a[0]));
        if (!periods.length) { targetEl.innerHTML = '<div style="color:#666;font-size:12px">No data available.</div>'; return; }
        const domains = data.domains || [];
        const rows = periods.map(([period, info]) => {
          const byDomain = info.by_domain || {};
          const top = domains
            .map(d => [d, byDomain[d] || 0])
            .sort((a,b) => b[1]-a[1])
            .slice(0, 3)
            .filter(([,v]) => v > 0)
            .map(([d,v]) => `<span style="color:#aaa">${d}</span> <span style="color:#e8601c;font-weight:600">${v}</span>`)
            .join(' &nbsp;·&nbsp; ');
          return [period, `<span style="color:#e8601c;font-weight:700">${info.total}</span>`, top || '<span style="color:#555">—</span>'];
        });
        targetEl.innerHTML = mkTable(['Period','Total','Top Domains'], rows);
      })
      .catch(e => errMsg(targetEl, e.message));
  }

  function loadApiSummaryPreview() {
    fetch('/api_call_summary/api/summary?time_frame=hour')
      .then(r => r.json())
      .then(data => {
        const el = document.getElementById('api-summary-preview');
        if (!el) return;
        const periods = Object.entries(data.periods || {}).sort((a,b) => b[0].localeCompare(a[0])).slice(0, 3);
        if (!periods.length) { el.innerHTML = '<div style="color:#666;font-size:12px;margin-bottom:8px">No data.</div>'; return; }
        const rows = periods.map(([period, info]) => {
          const topDomains = Object.entries(info.by_domain || {})
            .sort((a,b) => b[1]-a[1]).slice(0, 3)
            .map(([d,c]) => `<span style="color:#e8601c">${d}</span> <span style="color:#e0e0e0;font-weight:700">${c}</span>`)
            .join(' <span style="color:#444">·</span> ');
          return [
            `<span style="color:#aaa;font-family:monospace;font-size:11px">${period}</span>`,
            `<span style="color:#e8601c;font-weight:700">${info.total}</span>`,
            `<span style="font-size:11px">${topDomains}</span>`
          ];
        });
        el.innerHTML = mkTable(['Period','Total','Top Domains'], rows);
      })
      .catch(e => {
        const el = document.getElementById('api-summary-preview');
        if (el) el.innerHTML = '<div style="color:#666;font-size:12px;margin-bottom:8px">Could not load preview.</div>';
      });
  }

  /* ── Real Time API ── */
  function renderRealtimeCalls(calls, targetEl, filterDomain) {
    const filtered = filterDomain ? calls.filter(c => c.domain === filterDomain) : calls;
    if (!filtered.length) { targetEl.innerHTML = '<div style="color:#666;font-size:12px">No calls found.</div>'; return; }
    const rows = filtered.map(c => [
      `<span style="color:#aaa;font-family:monospace;font-size:11px">${c.timestamp}</span>`,
      `<span style="color:#3b82f6;font-weight:600">${c.method}</span>`,
      `<span style="color:#e8601c">${c.domain}</span>`,
      `<span style="color:#e0e0e0;font-family:monospace;font-size:11px;word-break:break-all">${c.endpoint}</span>`
    ]);
    targetEl.innerHTML = mkTable(['Time','Method','Domain','Endpoint'], rows);
  }

  function loadRealtimePreview() {
    fetch('/api_call_summary/api/latest_calls')
      .then(r => r.json())
      .then(calls => {
        const el = document.getElementById('realtime-api-preview');
        if (!el) return;
        if (!calls.length) { el.innerHTML = '<div style="color:#666;font-size:12px;margin-bottom:8px">No calls logged.</div>'; return; }
        const rows = calls.slice(0, 5).map(c => [
          `<span style="color:#aaa;font-family:monospace;font-size:11px">${c.timestamp}</span>`,
          `<span style="color:#3b82f6;font-weight:600">${c.method}</span>`,
          `<span style="color:#e8601c">${c.domain}</span>`
        ]);
        el.innerHTML = mkTable(['Time','Method','Domain'], rows) + '<div style="color:#666;font-size:11px;margin-top:4px">Last 5 calls shown</div>';
      })
      .catch(e => {
        const el = document.getElementById('realtime-api-preview');
        if (el) el.innerHTML = '<div style="color:#666;font-size:12px;margin-bottom:8px">Could not load preview.</div>';
      });
  }

  /* ── Modal wiring ── */
  let _realtimeCalls = [];

  function openApiSummary() {
    const modal = document.getElementById('api-summary-modal');
    const body = document.getElementById('api-summary-modal-body');
    const tf = document.getElementById('api-summary-timeframe');
    modal.style.display = 'flex';
    loadApiSummary(tf.value, body);
    tf.onchange = () => loadApiSummary(tf.value, body);
  }

  function openRealtimeApi() {
    const modal = document.getElementById('realtime-api-modal');
    const body = document.getElementById('realtime-api-modal-body');
    const filterSel = document.getElementById('realtime-api-filter');
    modal.style.display = 'flex';
    loading(body);
    fetch('/api_call_summary/api/latest_calls')
      .then(r => r.json())
      .then(calls => {
        _realtimeCalls = calls;
        // Populate domain filter
        const domains = [...new Set(calls.map(c => c.domain))].sort();
        filterSel.innerHTML = '<option value="">All Domains</option>' +
          domains.map(d => `<option value="${d}">${d}</option>`).join('');
        renderRealtimeCalls(calls, body, '');
        filterSel.onchange = () => renderRealtimeCalls(_realtimeCalls, body, filterSel.value);
      })
      .catch(e => errMsg(body, e.message));
  }

  document.addEventListener('DOMContentLoaded', function() {
    // Delay previews until after buildCards() has moved elements into the tangerine grid
    setTimeout(function() {
      loadApiSummaryPreview();
      loadRealtimePreview();
    }, 200);

    // Open buttons
    const apiSumBtn = document.getElementById('api-summary-open-btn');
    const rtBtn = document.getElementById('realtime-api-open-btn');
    if (apiSumBtn) apiSumBtn.addEventListener('click', openApiSummary);
    if (rtBtn) rtBtn.addEventListener('click', openRealtimeApi);

    // Close buttons
    document.getElementById('api-summary-close').addEventListener('click', () => {
      document.getElementById('api-summary-modal').style.display = 'none';
    });
    document.getElementById('realtime-api-close').addEventListener('click', () => {
      document.getElementById('realtime-api-modal').style.display = 'none';
    });
    document.getElementById('realtime-api-refresh').addEventListener('click', () => {
      const body = document.getElementById('realtime-api-modal-body');
      const filterSel = document.getElementById('realtime-api-filter');
      loading(body);
      fetch('/api_call_summary/api/latest_calls')
        .then(r => r.json())
        .then(calls => { _realtimeCalls = calls; renderRealtimeCalls(calls, body, filterSel.value); })
        .catch(e => errMsg(body, e.message));
    });

    // Close on backdrop click
    document.getElementById('api-summary-modal').addEventListener('click', function(e) {
      if (e.target === this) this.style.display = 'none';
    });
    document.getElementById('realtime-api-modal').addEventListener('click', function(e) {
      if (e.target === this) this.style.display = 'none';
    });
  });
})();
