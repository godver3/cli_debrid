/* Moved from templates/performance/dashboard.html. */
(function() {
'use strict';

// ── Chart state ─────────────────────────────────────────────────────
let _memEntries = [];
let _cpuEntries = [];
let _currentData = null;
let _lastSnapshotData = null;

// ── Helpers ──────────────────────────────────────────────────────────
function formatBytes(bytes, decimals) {
  decimals = decimals === undefined ? 2 : decimals;
  if (!bytes || bytes === 0) return '0 B';
  var k = 1024;
  var sizes = ['B', 'KB', 'MB', 'GB'];
  var i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(decimals)) + ' ' + sizes[i];
}

function downloadJson(data, filename) {
  var blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  var url = window.URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  window.URL.revokeObjectURL(url);
  document.body.removeChild(a);
}

// ── Toast ──────────────────────────────────────────────────────────
var _toastTimer;
var tColors = { green: '#22c55e', red: '#ef4444', blue: '#3b82f6', orange: '#f97316' };
function toast(msg, color) {
  color = color || 'green';
  var t = document.getElementById('pd-toast');
  document.getElementById('pd-toast-dot').style.background = tColors[color] || tColors.green;
  document.getElementById('pd-toast-msg').textContent = msg;
  t.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(function() { t.classList.remove('show'); }, 2500);
}

// ── Canvas charts ────────────────────────────────────────────────────
function drawMemChart() {
  var canvas = document.getElementById('memChart');
  if (!canvas) return;
  var dpr = window.devicePixelRatio || 1;
  var rect = canvas.parentElement.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  var ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  var W = rect.width, H = rect.height;
  var pad = { top: 12, right: 20, bottom: 30, left: 56 };

  var entries = _memEntries;
  if (!entries || !entries.length) {
    ctx.fillStyle = '#1a1a1a';
    ctx.beginPath(); ctx.roundRect(0, 0, W, H, 6); ctx.fill();
    ctx.fillStyle = '#555'; ctx.font = '12px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('No data yet', W / 2, H / 2);
    return;
  }

  var reduced = entries.filter(function(_, i) { return i % Math.max(1, Math.floor(entries.length / 40)) === 0; });
  var rss = reduced.map(function(e) { return e.metrics.memory_rss || 0; });
  var vms = reduced.map(function(e) { return e.metrics.memory_vms || 0; });
  var times = reduced.map(function(e) {
    var d = new Date(e.timestamp + 'Z');
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
  });

  var maxY = Math.ceil(Math.max(Math.max.apply(null, vms), Math.max.apply(null, rss)) * 1.15 / 100) * 100 || 600;
  var cW = W - pad.left - pad.right;
  var cH = H - pad.top - pad.bottom;
  var n = reduced.length;

  function xOf(i) { return pad.left + (n > 1 ? i / (n - 1) : 0.5) * cW; }
  function yOf(v) { return pad.top + cH - (v / maxY) * cH; }

  ctx.fillStyle = '#1a1a1a';
  ctx.beginPath(); ctx.roundRect(0, 0, W, H, 6); ctx.fill();

  // Grid
  ctx.strokeStyle = '#2d2d2d'; ctx.lineWidth = 1;
  var step = maxY / 4;
  for (var g = 0; g <= 4; g++) {
    var gv = g * step;
    var gy = yOf(gv);
    ctx.beginPath(); ctx.moveTo(pad.left, gy); ctx.lineTo(W - pad.right, gy); ctx.stroke();
    ctx.fillStyle = '#555'; ctx.font = '10px JetBrains Mono, monospace'; ctx.textAlign = 'right';
    ctx.fillText(Math.round(gv) + ' MB', pad.left - 6, gy + 4);
  }

  // X labels
  ctx.fillStyle = '#555'; ctx.textAlign = 'center'; ctx.font = '10px JetBrains Mono, monospace';
  var step2 = Math.max(1, Math.floor(n / 8));
  times.forEach(function(t, i) { if (i % step2 === 0) ctx.fillText(t, xOf(i), H - 6); });

  // VMS area
  ctx.beginPath();
  vms.forEach(function(v, i) { i === 0 ? ctx.moveTo(xOf(i), yOf(v)) : ctx.lineTo(xOf(i), yOf(v)); });
  ctx.lineTo(xOf(n - 1), yOf(0)); ctx.lineTo(xOf(0), yOf(0)); ctx.closePath();
  var vg = ctx.createLinearGradient(0, pad.top, 0, H - pad.bottom);
  vg.addColorStop(0, '#a855f744'); vg.addColorStop(1, '#a855f711');
  ctx.fillStyle = vg; ctx.fill();

  // RSS area
  ctx.beginPath();
  rss.forEach(function(v, i) { i === 0 ? ctx.moveTo(xOf(i), yOf(v)) : ctx.lineTo(xOf(i), yOf(v)); });
  ctx.lineTo(xOf(n - 1), yOf(0)); ctx.lineTo(xOf(0), yOf(0)); ctx.closePath();
  var rg = ctx.createLinearGradient(0, pad.top, 0, H - pad.bottom);
  rg.addColorStop(0, '#06b6d444'); rg.addColorStop(1, '#06b6d411');
  ctx.fillStyle = rg; ctx.fill();

  // VMS line + dots
  ctx.beginPath(); ctx.strokeStyle = '#a855f7'; ctx.lineWidth = 2;
  vms.forEach(function(v, i) { i === 0 ? ctx.moveTo(xOf(i), yOf(v)) : ctx.lineTo(xOf(i), yOf(v)); });
  ctx.stroke();
  vms.forEach(function(v, i) {
    ctx.beginPath(); ctx.arc(xOf(i), yOf(v), 3, 0, Math.PI * 2);
    ctx.fillStyle = '#a855f7'; ctx.fill();
  });

  // RSS line + dots
  ctx.beginPath(); ctx.strokeStyle = '#06b6d4'; ctx.lineWidth = 2;
  rss.forEach(function(v, i) { i === 0 ? ctx.moveTo(xOf(i), yOf(v)) : ctx.lineTo(xOf(i), yOf(v)); });
  ctx.stroke();
  rss.forEach(function(v, i) {
    ctx.beginPath(); ctx.arc(xOf(i), yOf(v), 3, 0, Math.PI * 2);
    ctx.fillStyle = '#06b6d4'; ctx.fill();
  });
}

function drawCpuChart() {
  var canvas = document.getElementById('cpuChart');
  if (!canvas) return;
  var dpr = window.devicePixelRatio || 1;
  var rect = canvas.parentElement.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  var ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  var W = rect.width, H = rect.height;
  var pad = { top: 12, right: 20, bottom: 30, left: 46 };

  var entries = _cpuEntries;
  if (!entries || !entries.length) {
    ctx.fillStyle = '#1a1a1a';
    ctx.beginPath(); ctx.roundRect(0, 0, W, H, 6); ctx.fill();
    ctx.fillStyle = '#555'; ctx.font = '12px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('No data yet', W / 2, H / 2);
    return;
  }

  var cpu = entries.map(function(e) { return e.metrics.process_cpu_percent || 0; });
  var labels = entries.map(function(e) {
    var d = new Date(e.timestamp);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
  });

  var maxY = Math.ceil(Math.max.apply(null, cpu) * 1.2 / 50) * 50 || 100;
  var cW = W - pad.left - pad.right;
  var cH = H - pad.top - pad.bottom;
  var n = entries.length;

  function xOf(i) { return pad.left + (n > 1 ? i / (n - 1) : 0.5) * cW; }
  function yOf(v) { return pad.top + cH - (v / maxY) * cH; }

  ctx.fillStyle = '#1a1a1a';
  ctx.beginPath(); ctx.roundRect(0, 0, W, H, 6); ctx.fill();

  ctx.strokeStyle = '#2d2d2d'; ctx.lineWidth = 1;
  var ySteps = [0, 0.25, 0.5, 0.75, 1.0];
  ySteps.forEach(function(s) {
    var v = s * maxY;
    var y = yOf(v);
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(W - pad.right, y); ctx.stroke();
    ctx.fillStyle = '#555'; ctx.font = '10px JetBrains Mono, monospace'; ctx.textAlign = 'right';
    ctx.fillText(Math.round(v) + '%', pad.left - 5, y + 4);
  });

  ctx.fillStyle = '#555'; ctx.textAlign = 'center'; ctx.font = '9px JetBrains Mono, monospace';
  var step2 = Math.max(1, Math.floor(n / 8));
  labels.forEach(function(l, i) { if (i % step2 === 0) ctx.fillText(l, xOf(i), H - 6); });

  ctx.beginPath();
  cpu.forEach(function(v, i) { i === 0 ? ctx.moveTo(xOf(i), yOf(v)) : ctx.lineTo(xOf(i), yOf(v)); });
  ctx.lineTo(xOf(n - 1), yOf(0)); ctx.lineTo(xOf(0), yOf(0)); ctx.closePath();
  var grad = ctx.createLinearGradient(0, pad.top, 0, H - pad.bottom);
  grad.addColorStop(0, '#ef444455'); grad.addColorStop(1, '#ef444411');
  ctx.fillStyle = grad; ctx.fill();

  ctx.beginPath(); ctx.strokeStyle = '#ef4444'; ctx.lineWidth = 2;
  cpu.forEach(function(v, i) { i === 0 ? ctx.moveTo(xOf(i), yOf(v)) : ctx.lineTo(xOf(i), yOf(v)); });
  ctx.stroke();

  cpu.forEach(function(v, i) {
    ctx.beginPath(); ctx.arc(xOf(i), yOf(v), 2.5, 0, Math.PI * 2);
    ctx.fillStyle = '#ef4444'; ctx.fill();
  });
}

// ── Data fetch + render ───────────────────────────────────────────────
function updateMemoryGrowthTable(entries) {
  var last = entries[entries.length - 1];
  if (!last) return;
  var lastTime = new Date(last.timestamp + 'Z');
  var snapshots = [];
  for (var i = 0; i < 5; i++) {
    var target = new Date(lastTime.getTime() - i * 10 * 60 * 1000);
    var closest = entries.reduce(function(prev, curr) {
      return Math.abs(new Date(curr.timestamp + 'Z') - target) < Math.abs(new Date(prev.timestamp + 'Z') - target) ? curr : prev;
    });
    snapshots.push(closest);
  }
  var html = snapshots.map(function(e) {
    var m = e.metrics;
    var ts = new Date(e.timestamp + 'Z').toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true });
    return '<div class="pd-growth-row">' +
      '<div class="pd-growth-time">' + ts + '</div>' +
      '<div class="pd-growth-badges">' +
        '<span class="pd-gbadge rss">RSS: ' + m.memory_rss.toFixed(2) + ' MB</span>' +
        '<span class="pd-gbadge vms">VMS: ' + m.memory_vms.toFixed(2) + ' MB</span>' +
        '<span class="pd-gbadge swap">Swap: ' + m.swap_used.toFixed(2) + ' MB</span>' +
      '</div>' +
    '</div>';
  }).join('');
  document.getElementById('memory-growth-text').innerHTML = html;
}

function updateDashboard() {
  fetch('/performance/api/performance/log?hours=1')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      _currentData = data;
      if (!data.entries || !data.entries.length) return;

      var entries = data.entries;
      var basicMetrics = null;
      for (var i = entries.length - 1; i >= 0; i--) {
        if (entries[i].type === 'basic_metrics') { basicMetrics = entries[i]; break; }
      }

      if (basicMetrics) {
        var m = basicMetrics.metrics;
        var cpuPct = m.cpu_percent || 0;
        var cpuBar = document.getElementById('cpu-progress');
        var cpuText = document.getElementById('cpu-text');
        cpuBar.style.width = Math.min(cpuPct, 100) + '%';
        cpuBar.className = 'pd-prog-bar-fill cpu' + (cpuPct >= 80 ? ' danger' : cpuPct >= 60 ? ' warn' : '');
        cpuText.textContent = cpuPct.toFixed(1) + '%';
        document.getElementById('cpu-details').innerHTML =
          '<div class="pd-prog-meta-item">System Time: <b>' + m.cpu_system_time.toFixed(2) + 's</b></div>' +
          '<div class="pd-prog-meta-item">User Time: <b>' + m.cpu_user_time.toFixed(2) + 's</b></div>';

        var memPct = m.system_memory_used || 0;
        var memBar = document.getElementById('memory-progress');
        var memText = document.getElementById('memory-text');
        memBar.style.width = Math.min(memPct, 100) + '%';
        memBar.className = 'pd-prog-bar-fill mem' + (memPct >= 85 ? ' danger' : memPct >= 70 ? ' warn' : '');
        memText.textContent = memPct.toFixed(1) + '%';
        document.getElementById('memory-details').innerHTML =
          '<div class="pd-prog-meta-item">RSS Memory: <b>' + m.memory_rss.toFixed(2) + ' MB</b></div>' +
          '<div class="pd-prog-meta-item">Virtual Memory: <b>' + m.memory_vms.toFixed(2) + ' MB</b></div>' +
          '<div class="pd-prog-meta-item">Swap Used: <b>' + m.swap_used.toFixed(2) + ' MB</b></div>';
      }

      // Memory chart + growth table
      var basicEntries = entries.filter(function(e) { return e.type === 'basic_metrics'; });
      _memEntries = basicEntries;
      drawMemChart();
      if (basicEntries.length) updateMemoryGrowthTable(basicEntries);

      // Memory analysis stats
      if (basicEntries.length) {
        var rssVals = basicEntries.map(function(e) { return e.metrics.memory_rss; });
        var vmsVals = basicEntries.map(function(e) { return e.metrics.memory_vms; });
        var avgRss = rssVals.reduce(function(a, b) { return a + b; }, 0) / rssVals.length;
        var maxRss = Math.max.apply(null, rssVals);
        var avgVms = vmsVals.reduce(function(a, b) { return a + b; }, 0) / vmsVals.length;
        var maxVms = Math.max.apply(null, vmsVals);
        document.getElementById('avg-rss-memory').textContent = avgRss.toFixed(1) + ' MB';
        document.getElementById('max-rss-memory').textContent = maxRss.toFixed(1) + ' MB';
        document.getElementById('avg-vms-memory').textContent = avgVms.toFixed(1) + ' MB';
        document.getElementById('max-vms-memory').textContent = maxVms.toFixed(1) + ' MB';
      }

      // Detailed memory (separate full-range fetch)
      fetch('/performance/api/performance/log')
        .then(function(r) { return r.json(); })
        .then(function(fullData) {
          var detail = null;
          for (var j = fullData.entries.length - 1; j >= 0; j--) {
            if (fullData.entries[j].memory) { detail = fullData.entries[j]; break; }
          }
          if (!detail) return;
          var mem = detail.memory;

          document.getElementById('anon-memory-size').textContent = 'Size: ' + (mem.anonymous && mem.anonymous.formatted_size ? mem.anonymous.formatted_size : '0 B');
          document.getElementById('anon-memory-count').textContent = 'Mappings: ' + (mem.anonymous ? mem.anonymous.count || 0 : 0);
          document.getElementById('file-memory-size').textContent = 'Size: ' + (mem.file_backed && mem.file_backed.formatted_size ? mem.file_backed.formatted_size : '0 B');
          document.getElementById('file-memory-count').textContent = 'Mappings: ' + (mem.file_backed ? mem.file_backed.count || 0 : 0);

          var openFiles = (mem.open_files && mem.open_files.files) ? mem.open_files.files : [];
          var fileListEl = document.getElementById('open-files-list');
          if (openFiles.length) {
            fileListEl.innerHTML = openFiles.map(function(f) {
              var name = f.path.split('/').pop();
              return '<div class="pd-file-row">' +
                '<div class="pd-file-info">' +
                  '<div class="pd-file-name">' + name + '</div>' +
                  '<div class="pd-file-path">' + f.path + '</div>' +
                '</div>' +
                '<div class="pd-file-size">' + formatBytes(f.size) + '</div>' +
              '</div>';
            }).join('');
          } else {
            fileListEl.innerHTML = '<div style="color:var(--pd-text3);font-size:12px;padding:8px 0;">No open files data</div>';
          }

          var count = mem.open_files ? mem.open_files.count || 0 : 0;
          if (mem.open_files && mem.open_files.open_handles !== undefined) {
            // Windows: open files aren't collected (too slow); show the handle count instead.
            count = mem.open_files.open_handles + ' handles';
          }
          document.getElementById('open-files-count').textContent = count;

          var types = openFiles.map(function(f) { return f.path.split('.').pop(); }).filter(function(v, i, a) { return a.indexOf(v) === i; });
          var tagsEl = document.getElementById('file-types-tags');
          if (types.length) {
            tagsEl.innerHTML = types.map(function(t) { return '<span class="pd-rh-tag">' + t + '</span>'; }).join('');
          } else {
            tagsEl.innerHTML = '<span style="color:var(--pd-text3);font-size:12px;">None</span>';
          }
        })
        .catch(function(e) { console.error('detailed memory error', e); });
    })
    .catch(function(e) { console.error('performance log error', e); });

  // CPU metrics
  fetch('/performance/api/performance/cpu?hours=1&threads=true')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (!data.summary) return;
      document.getElementById('avg-cpu-percent').textContent = data.summary.avg_cpu_percent.toFixed(1) + '%';
      document.getElementById('max-cpu-percent').textContent = data.summary.max_cpu_percent.toFixed(1) + '%';

      var latest = data.entries[data.entries.length - 1];
      if (latest && latest.metrics && latest.metrics.cpu_times) {
        var ct = latest.metrics.cpu_times;
        document.getElementById('cpu-time').innerHTML =
          'User: ' + ct.user.toFixed(1) + 's<br><span style="color:var(--pd-text3)">System: ' + ct.system.toFixed(1) + 's</span>';
      }

      _cpuEntries = data.entries;
      drawCpuChart();
    })
    .catch(function(e) { console.error('cpu error', e); });
}

// ── Memory Snapshot ───────────────────────────────────────────────────
document.getElementById('memory-snapshot-btn').addEventListener('click', function() {
  var btn = document.getElementById('memory-snapshot-btn');
  var panel = document.getElementById('memory-snapshot-panel');
  var content = document.getElementById('memory-snapshot-content');
  var ts = document.getElementById('memory-snapshot-ts');

  btn.disabled = true;
  btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Capturing…';
  content.innerHTML = '<p style="opacity:0.6;font-size:12px;">Fetching snapshot…</p>';
  panel.style.display = 'block';

  fetch('/debug/api/memory_snapshot')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      _lastSnapshotData = data;
      btn.disabled = false;
      btn.innerHTML = '<i class="fas fa-microscope"></i> Memory Snapshot';
      ts.textContent = 'Captured at ' + new Date().toLocaleTimeString();
      if (!data.success) {
        content.innerHTML = '<p style="color:#ef4444;">Error: ' + (data.error || 'Unknown error') + '</p>';
        return;
      }
      var s = data.snapshot;
      var mb = function(v) { return v != null ? v + ' MB' : '—'; };
      var ks = s.known_structures || {};
      var html = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;margin-bottom:16px;">';
      html += '<div class="pd-metric-item"><h6>RSS Memory</h6><span>' + mb(s.vmrss_mb || s.process_rss_mb) + '</span></div>';
      html += '<div class="pd-metric-item"><h6>Virtual Memory</h6><span>' + mb(s.vmsize_mb) + '</span></div>';
      html += '<div class="pd-metric-item"><h6>Active Threads</h6><span>' + (s.thread_count != null ? s.thread_count : '—') + '</span></div>';
      html += '<div class="pd-metric-item"><h6>Session Store</h6><span>' + (ks.session_store_count != null ? ks.session_store_count + ' sessions' : '—') + '</span></div>';
      html += '<div class="pd-metric-item"><h6>Task Queue</h6><span>' + (ks.task_queue_count != null ? ks.task_queue_count + ' tasks (' + ks.task_queue_completed + ' done)' : '—') + '</span></div>';
      html += '<div class="pd-metric-item"><h6>Function Cache</h6><span>' + (ks.function_cache_total_entries != null ? ks.function_cache_total_entries + ' entries' : '—') + '</span></div>';
      html += '<div class="pd-metric-item"><h6>Scan Progress Keys</h6><span>' + (ks.scan_progress_keys != null ? ks.scan_progress_keys : '—') + '</span></div>';
      html += '<div class="pd-metric-item"><h6>Analysis Progress Keys</h6><span>' + (ks.analysis_progress_keys != null ? ks.analysis_progress_keys : '—') + '</span></div>';
      html += '<div class="pd-metric-item"><h6>Notification Queues</h6><span>' + (ks.notification_queues_count != null ? ks.notification_queues_count : '—') + '</span></div>';
      html += '</div>';
      if (s.top_object_types && s.top_object_types.length) {
        html += '<h6 style="margin-top:8px;margin-bottom:6px;font-size:12px;color:var(--pd-text2)"><i class="fas fa-list"></i> Top Python Object Types (by count)</h6>';
        html += '<table style="width:100%;border-collapse:collapse;font-size:0.85em;">';
        html += '<thead><tr><th style="text-align:left;padding:4px 8px;border-bottom:1px solid rgba(128,128,128,0.3);color:var(--pd-text3)">Type</th><th style="text-align:right;padding:4px 8px;border-bottom:1px solid rgba(128,128,128,0.3);color:var(--pd-text3)">Count</th></tr></thead><tbody>';
        s.top_object_types.forEach(function(pair) {
          html += '<tr><td style="padding:3px 8px;">' + pair[0] + '</td><td style="text-align:right;padding:3px 8px;">' + pair[1].toLocaleString() + '</td></tr>';
        });
        html += '</tbody></table>';
      }
      if (s.thread_names && s.thread_names.length) {
        html += '<details style="margin-top:12px;"><summary style="cursor:pointer;opacity:0.7;font-size:12px;">Thread names (' + s.thread_names.length + ')</summary>';
        html += '<div style="font-size:0.8em;margin-top:6px;column-count:2;">' + s.thread_names.map(function(n) { return '<div>' + n + '</div>'; }).join('') + '</div></details>';
      }
      content.innerHTML = html;
    })
    .catch(function(err) {
      btn.disabled = false;
      btn.innerHTML = '<i class="fas fa-microscope"></i> Memory Snapshot';
      content.innerHTML = '<p style="color:#ef4444;">Request failed: ' + err + '</p>';
    });
});

document.getElementById('memory-snapshot-close-btn').addEventListener('click', function() {
  document.getElementById('memory-snapshot-panel').style.display = 'none';
});

document.getElementById('memory-snapshot-download-btn').addEventListener('click', function() {
  if (_lastSnapshotData) {
    downloadJson(_lastSnapshotData, 'memory-snapshot-' + new Date().toISOString().replace(/[:.]/g, '-') + '.json');
  }
});

// ── Trim Memory ────────────────────────────────────────────────────────
document.getElementById('trim-memory-btn').addEventListener('click', function() {
  var btn = document.getElementById('trim-memory-btn');
  btn.disabled = true;
  btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Trimming…';
  fetch('/debug/api/trim_memory', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.success) {
        var freed = data.freed_mb !== null ? data.freed_mb : '?';
        var before = data.before_mb !== null ? data.before_mb : '?';
        var after = data.after_mb !== null ? data.after_mb : '?';
        btn.innerHTML = '<i class="fas fa-check"></i> ' + before + '→' + after + ' MB (freed ' + freed + ' MB)';
        toast('Memory trimmed: ' + before + '→' + after + ' MB', 'green');
        setTimeout(function() { btn.disabled = false; btn.innerHTML = '<i class="fas fa-broom"></i> Trim Memory'; }, 5000);
      } else {
        btn.innerHTML = '<i class="fas fa-times"></i> Failed';
        toast('Trim failed', 'red');
        setTimeout(function() { btn.disabled = false; btn.innerHTML = '<i class="fas fa-broom"></i> Trim Memory'; }, 3000);
      }
    })
    .catch(function(e) {
      btn.innerHTML = '<i class="fas fa-times"></i> Error';
      toast('Error trimming memory', 'red');
      setTimeout(function() { btn.disabled = false; btn.innerHTML = '<i class="fas fa-broom"></i> Trim Memory'; }, 3000);
    });
});

// ── Export ──────────────────────────────────────────────────────────────
document.getElementById('export-btn').addEventListener('click', function() {
  if (_currentData) {
    downloadJson(_currentData, 'performance-data-' + new Date().toISOString().replace(/[:.]/g, '-') + '.json');
    toast('Exporting data…', 'orange');
  }
});

// ── Resize ─────────────────────────────────────────────────────────────
window.addEventListener('resize', function() {
  drawMemChart();
  drawCpuChart();
});

// ── Init ───────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
  drawMemChart();
  drawCpuChart();
  updateDashboard();
  setTimeout(updateDashboard, 1000);
  setInterval(updateDashboard, 60000);
});

})();
