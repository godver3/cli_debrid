/* Moved from templates/phalanxdb_status.html. */
(function() {
  async function submitHash(inputId, btnId, resultId, statusId, tsRowId, tsId, exRowId, exId) {
    var input  = document.getElementById(inputId);
    var btn    = document.getElementById(btnId);
    var result = document.getElementById(resultId);
    var statusEl = document.getElementById(statusId);
    var tsRow  = document.getElementById(tsRowId);
    var tsEl   = document.getElementById(tsId);
    var exRow  = document.getElementById(exRowId);
    var exEl   = document.getElementById(exId);

    if (!input || !btn) return;

    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Testing…';
    if (result) result.style.display = 'none';
    if (tsRow)  tsRow.style.display  = 'none';
    if (exRow)  exRow.style.display  = 'none';

    try {
      var res  = await fetch('/database/phalanxdb/test_hash', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ hash: input.value })
      });
      var data = await res.json();
      if (data.error) throw new Error(data.error);

      if (result) result.style.display = 'block';

      if (data.status === 'not_found') {
        if (statusEl) statusEl.innerHTML = '<span class="px-result-badge not-found"><i class="fas fa-circle-question"></i> Not Found in Database</span>';
      } else {
        var r = data.data;
        if (statusEl) statusEl.innerHTML = r.is_cached
          ? '<span class="px-result-badge cached"><i class="fas fa-circle-check"></i> Cached</span>'
          : '<span class="px-result-badge not-cached"><i class="fas fa-circle-xmark"></i> Not Cached</span>';
        if (r.timestamp && tsEl) { tsEl.textContent = r.timestamp; if (tsRow) tsRow.style.display = 'flex'; }
        if (r.expiry    && exEl) { exEl.textContent = r.expiry;    if (exRow) exRow.style.display = 'flex'; }
      }
    } catch(err) {
      if (result)   result.style.display = 'block';
      if (statusEl) statusEl.innerHTML = '<span class="px-result-badge error"><i class="fas fa-triangle-exclamation"></i> Error: ' + err.message + '</span>';
    } finally {
      btn.disabled = false;
      btn.innerHTML = inputId.startsWith('tng') ? '<i class="fas fa-search"></i> Test Hash' : 'Test Hash';
    }
  }

  // Classic form
  var classicForm = document.getElementById('classicHashForm');
  if (classicForm) classicForm.addEventListener('submit', function(e) {
    e.preventDefault();
    submitHash('classicHashInput','classicHashForm','classicHashResult','classicHashStatus','classicHashTimestamp','classicHashTimestamp','classicHashExpiry','classicHashExpiry');
  });

  // Tangerine form
  var tngForm = document.getElementById('tngHashForm');
  if (tngForm) tngForm.addEventListener('submit', function(e) {
    e.preventDefault();
    submitHash('tngHashInput','tngHashBtn','tngHashResult','tngHashStatus','tngHashTsRow','tngHashTs','tngHashExRow','tngHashEx');
  });
})();
