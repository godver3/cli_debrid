/* Moved from templates/_provider_transfer.html. */
(function () {
  var root = document.getElementById('provider-transfer');
  if (!root || root.dataset.ptInit) return;
  root.dataset.ptInit = '1';
  var BASE = '/program_operation/api/provider_transfer';
  function $(id){ return document.getElementById(id); }
  function val(id){ var e=$(id); return e ? e.value.trim() : ''; }
  function esc(s){ return String(s).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];}); }

  var CFG = {};
  fetch(BASE + '/config').then(function(r){return r.json();}).then(function(d){
    CFG = d || {};
    if (CFG.provider === 'nzbdav') { if(!val('pt-nzbdav-url')) $('pt-nzbdav-url').value = CFG.url||''; if(!val('pt-nzbdav-token')) $('pt-nzbdav-token').value = CFG.token||''; }
    if (CFG.provider === 'climount') { if(!val('pt-decy-url')) $('pt-decy-url').value = CFG.url||''; if(!val('pt-decy-token')) $('pt-decy-token').value = CFG.token||''; }
    if (!val('pt-decy-nzbpath')) $('pt-decy-nzbpath').value = CFG.nzb_path_guess || '/cli_mount_data/usenet/nzbs';
    if (!val('pt-nzbdav-cat')) $('pt-nzbdav-cat').value = '__unplayable__';
  }).catch(function(){});

  function applyDirection(){
    var d = $('pt-direction').value;
    var show = (d === 'climount_to_nzbdav')
      ? {'decy-nzbpath':1,'nzbdav-conn':1,'nzbdav-cat':1,'decy-conn':0}
      : {'nzbdav-conn':1,'decy-conn':1,'decy-nzbpath':0,'nzbdav-cat':0};
    root.querySelectorAll('.pt-field').forEach(function(el){
      el.style.display = show[el.dataset.for] ? '' : 'none';
    });
    $('pt-nzbdav-role').textContent = (d === 'climount_to_nzbdav') ? '(target)' : '(source)';
  }
  $('pt-direction').addEventListener('change', applyDirection);
  applyDirection();

  function sourcePayload(){
    var d = $('pt-direction').value;
    if (d === 'climount_to_nzbdav') return {direction:d, source_nzb_path: val('pt-decy-nzbpath')};
    return {direction:d, source_url: val('pt-nzbdav-url'), source_token: val('pt-nzbdav-token')};
  }
  function startPayload(){
    var d = $('pt-direction').value;
    var base = {direction:d, skip_existing:$('pt-skip').checked,
                max_queue: parseInt(val('pt-maxqueue')||'3',10), limit: parseInt(val('pt-limit')||'0',10)};
    if (d === 'climount_to_nzbdav') {
      base.source_nzb_path = val('pt-decy-nzbpath');
      base.target_url = val('pt-nzbdav-url'); base.target_token = val('pt-nzbdav-token');
      base.target_category = val('pt-nzbdav-cat');
    } else {
      base.source_url = val('pt-nzbdav-url'); base.source_token = val('pt-nzbdav-token');
      base.target_url = val('pt-decy-url'); base.target_token = val('pt-decy-token');
    }
    return base;
  }

  $('pt-preview').addEventListener('click', function(){
    var box = $('pt-preview-result'); box.innerHTML = 'Previewing…';
    fetch(BASE+'/list', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(sourcePayload())})
      .then(function(r){return r.json();}).then(function(d){
        if (!d.success) { box.innerHTML = '<span style="color:#e57373;">✗ '+esc(d.error||'failed')+'</span>'; return; }
        var sample = (d.sample||[]).slice(0,10).map(esc).join('<br>');
        box.innerHTML = '<strong>'+d.count+'</strong> transferable item(s) at source.'
          + (sample ? '<details style="margin-top:4px;"><summary style="cursor:pointer;">sample</summary><div style="font-size:0.85em;color:#aaa;margin-top:4px;">'+sample+(d.count>10?'<br>…':'')+'</div></details>' : '');
      }).catch(function(e){ box.innerHTML = '<span style="color:#e57373;">✗ '+esc(e)+'</span>'; });
  });

  var poll = null;
  function stopPoll(){ if(poll){clearInterval(poll);poll=null;} $('pt-cancel').style.display='none'; }
  var curJob = null;

  $('pt-start').addEventListener('click', function(){
    var pg = $('pt-progress');
    if (!startPayload().target_url) { pg.innerHTML='<span style="color:#e57373;">Enter the target URL first.</span>'; return; }
    if (!confirm('Start transferring? The target will re-download these from usenet.')) return;
    pg.innerHTML = 'Starting…';
    fetch(BASE+'/start', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(startPayload())})
      .then(function(r){return r.json();}).then(function(d){
        if (!d.success){ pg.innerHTML='<span style="color:#e57373;">✗ '+esc(d.error||'failed')+'</span>'; return; }
        curJob = d.job_id; $('pt-cancel').style.display='';
        poll = setInterval(refresh, 2000); refresh();
      }).catch(function(e){ pg.innerHTML='<span style="color:#e57373;">✗ '+esc(e)+'</span>'; });
  });

  $('pt-cancel').addEventListener('click', function(){
    if (curJob) fetch(BASE+'/cancel/'+curJob, {method:'POST'});
  });

  function refresh(){
    if (!curJob) return;
    fetch(BASE+'/status/'+curJob).then(function(r){return r.json();}).then(function(d){
      if (!d.success){ return; }
      var s = d.status||{};
      var bar = s.total ? Math.round(100*(s.done||0)/s.total) : 0;
      var color = s.status==='error' ? '#e57373' : (s.complete ? '#4caf50' : '#3a6ea5');
      var html = '<div style="font-weight:600;color:'+color+';">'+esc(s.status||'')+(s.total?(' — '+(s.done||0)+'/'+s.total):'')+'</div>'
        + '<div style="background:#333;border-radius:4px;height:8px;margin:4px 0;overflow:hidden;"><div style="background:'+color+';height:8px;width:'+bar+'%;"></div></div>'
        + '<div style="font-size:0.85em;">submitted: '+(s.submitted||0)+' · skipped: '+(s.skipped||0)+' · failed: '+(s.failed||0)+'</div>';
      if (s.error) html += '<div style="color:#e57373;">'+esc(s.error)+'</div>';
      var log = (s.log||[]).slice(-6).map(esc).join('<br>');
      if (log) html += '<details style="margin-top:4px;"><summary style="cursor:pointer;">log</summary><div style="font-size:0.8em;color:#aaa;margin-top:4px;">'+log+'</div></details>';
      $('pt-progress').innerHTML = html;
      if (s.complete) { stopPoll(); }
    }).catch(function(){});
  }
})();
