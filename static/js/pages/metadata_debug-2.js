/* Moved from templates/metadata_debug.html. */
(function() {
// ── JSON tree renderer (same logic as design file) ────────
let _tmdUid = 0;
const _tmdData = { item: null, meta: null, seasons: null };

function tmdEsc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function tmdValClass(v) {
  if (v === null) return 'j2-null';
  if (typeof v === 'boolean') return 'j2-bool';
  if (typeof v === 'number') return 'j2-num';
  return 'j2-str';
}
function tmdValText(v) {
  if (v === null) return 'null';
  if (typeof v === 'string') return '"' + tmdEsc(v) + '"';
  return tmdEsc(String(v));
}
function tmdDeepParse(obj) {
  if (typeof obj === 'string') {
    const t = obj.trim();
    if ((t.startsWith('{') && t.endsWith('}')) || (t.startsWith('[') && t.endsWith(']'))) {
      try { return tmdDeepParse(JSON.parse(t)); } catch {}
    }
    return obj;
  }
  if (Array.isArray(obj)) return obj.map(tmdDeepParse);
  if (obj && typeof obj === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(obj)) out[k] = tmdDeepParse(v);
    return out;
  }
  return obj;
}
function tmdIsComplex(v) {
  return (Array.isArray(v) && v.length > 0) || (v !== null && typeof v === 'object' && !Array.isArray(v) && Object.keys(v||{}).length > 0);
}
function tmdSortKeys(obj) {
  // Scalars first, then complex — so "season", "title" etc appear before "episodes"
  const keys = Object.keys(obj);
  return keys.sort((a,b) => {
    const ac = tmdIsComplex(obj[a]), bc = tmdIsComplex(obj[b]);
    if (ac === bc) return 0;
    return ac ? 1 : -1;
  });
}
// Special-key badge renderer for ID fields
const _tmdBadgeCls = { plex_guid:'j2-badge-plex', imdb_id:'j2-badge-imdb', tmdb_id:'j2-badge-tmdb', tvdb_id:'j2-badge-tvdb' };
function tmdSpecialVal(key, val) {
  if (!_tmdBadgeCls[key]) return null;
  if (val === null || val === undefined || val === '') {
    return '<span class="j2-badge j2-badge-null">null</span>';
  }
  return '<span class="j2-badge '+_tmdBadgeCls[key]+'">' + tmdEsc(String(val)) + '</span>';
}

// Unified recursive builder — rootArr=true means skip toggle on direct array items (top-level seasons)
function tmdBuildNode(val, rootArr) {
  if (Array.isArray(val)) {
    if (!val.length) return '<span class="j2-bracket">[ ]</span>';
    const rows = val.map((item, i) => {
      const comma = i < val.length-1 ? '<span class="j2-bracket">,</span>' : '';
      if (tmdIsComplex(item) && !rootArr) {
        const cid = 'u' + (++_tmdUid);
        return '<div class="j2-row">'
          + '<span class="j2-toggle" id="t'+cid+'" onclick="tmdTog(\''+cid+'\')">&#x2212;</span>'
          + '<div class="j2-body" id="b'+cid+'">' + tmdBuildNode(item) + '</div>' + comma + '</div>';
      }
      return '<div class="j2-row">' + tmdBuildNode(item) + comma + '</div>';
    }).join('');
    return '<span class="j2-bracket">[</span>'
      + '<div class="j2-nested">' + rows + '</div>'
      + '<span class="j2-bracket">]</span>';
  }
  if (val !== null && typeof val === 'object') {
    const keys = tmdSortKeys(val);
    if (!keys.length) return '<span class="j2-bracket">{ }</span>';
    const rows = keys.map((k, i) => {
      const comma = i < keys.length-1 ? '<span class="j2-bracket">,</span>' : '';
      const v = val[k];
      const special = tmdSpecialVal(k, v);
      if (special) {
        return '<div class="j2-row"><span class="j2-key">"'+tmdEsc(k)+'"</span><span class="j2-colon">:</span>' + special + comma + '</div>';
      }
      if (tmdIsComplex(v)) {
        const cid = 'u' + (++_tmdUid);
        return '<div class="j2-row">'
          + '<span class="j2-toggle" id="t'+cid+'" onclick="tmdTog(\''+cid+'\')">&#x2212;</span>'
          + '<span class="j2-key">"'+tmdEsc(k)+'"</span><span class="j2-colon">:</span>'
          + '<div class="j2-body" id="b'+cid+'">' + tmdBuildNode(v) + '</div>' + comma + '</div>';
      }
      return '<div class="j2-row"><span class="j2-key">"'+tmdEsc(k)+'"</span><span class="j2-colon">:</span>' + tmdBuildNode(v) + comma + '</div>';
    }).join('');
    return '<span class="j2-bracket">{</span><div class="j2-nested">' + rows + '</div><span class="j2-bracket">}</span>';
  }
  return '<span class="' + tmdValClass(val) + '">' + tmdValText(val) + '</span>';
}
function tmdRenderRoot(data, elId, labelId) {
  const el = document.getElementById(elId);
  if (!el) return;
  const parsed = tmdDeepParse(data);
  const isArr = Array.isArray(parsed);
  const label = document.getElementById(labelId);
  if (label) label.textContent = isArr ? 'Array · ' + parsed.length + ' item' + (parsed.length!==1?'s':'') : 'Object';
  el.innerHTML = tmdBuildNode(parsed, Array.isArray(parsed));
}

window.tmdTog = function(id) {
  const t = document.getElementById('t'+id), b = document.getElementById('b'+id);
  if (!t||!b) return;
  const collapsed = b.style.display === 'none';
  b.style.display = collapsed ? '' : 'none';
  t.innerHTML = collapsed ? '&#x2212;' : '+';
  const hid = 'h'+id;
  if (!collapsed) {
    if (!document.getElementById(hid)) { const h=document.createElement('span'); h.className='j2-hint'; h.id=hid; h.textContent=' … '; t.after(h); }
  } else { const h=document.getElementById(hid); if(h) h.remove(); }
};

window.tmdToggleAll = function(treeId, mode) {
  document.querySelectorAll('#'+treeId+' .j2-toggle').forEach(el => {
    const id = el.id.slice(1);
    const b=document.getElementById('b'+id), h=document.getElementById('h'+id);
    if (!b) return;
    if (mode==='collapse' && b.style.display !== 'none') {
      b.style.display='none'; el.innerHTML='+';
      if(!h){const hn=document.createElement('span');hn.className='j2-hint';hn.id='h'+id;hn.textContent=' … ';el.after(hn);}
    } else if (mode==='expand' && b.style.display === 'none') {
      b.style.display=''; el.innerHTML='&#x2212;';
      if(h) h.remove();
    }
  });
};

function tmdSetStatus(msg, color) {
  const line=document.getElementById('tmdStatus'), dot=document.getElementById('tmdStatusDot'), txt=document.getElementById('tmdStatusText');
  if(!line) return;
  const colors={green:'#22c55e',red:'#ef4444',yellow:'#f59e0b',blue:'#3b82f6'};
  line.style.color = colors[color]||colors.green;
  dot.style.background = colors[color]||colors.green;
  txt.textContent = msg;
  line.style.display = msg ? 'flex' : 'none';
}

function tmdCopy(data) {
  navigator.clipboard.writeText(JSON.stringify(tmdDeepParse(data), null, 2)).then(() => {
    const t=document.getElementById('tmdToast'); t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),2000);
  });
}

document.addEventListener('DOMContentLoaded', function() {
  const input = document.getElementById('tmdInput');
  if (!input) return;

  document.getElementById('tmdFetchBtn').addEventListener('click', tmdFetch);
  document.getElementById('tmdRecreateBtn').addEventListener('click', tmdRecreate);
  document.getElementById('tmdDeleteBtn').addEventListener('click', tmdDelete);
  document.getElementById('tmdCopyItem').addEventListener('click', ()=>{ if(_tmdData.item) tmdCopy(_tmdData.item); });
  document.getElementById('tmdCopyMeta').addEventListener('click', ()=>{ if(_tmdData.meta) tmdCopy(_tmdData.meta); });
  document.getElementById('tmdCopySeasons').addEventListener('click', ()=>{ if(_tmdData.seasons) tmdCopy(_tmdData.seasons); });
  input.addEventListener('keydown', e=>{ if(e.key==='Enter'){e.preventDefault();tmdFetch();} });

  // Mirror classic input ↔ tangerine input
  const classicInput = document.getElementById('imdbIdInput');
  if (classicInput) {
    input.addEventListener('input', ()=>{ classicInput.value = input.value; });
    classicInput.addEventListener('input', ()=>{ input.value = classicInput.value; });
  }

  // Auto-fetch if imdb_id passed as URL param
  const urlImdbId = new URLSearchParams(window.location.search).get('imdb_id');
  if (urlImdbId) {
    input.value = urlImdbId;
    if (classicInput) classicInput.value = urlImdbId;
    tmdFetch();
  }
});

function tmdFetch() {
  const id = (document.getElementById('tmdInput').value||'').trim();
  if (!id) { tmdSetStatus('Please enter an IMDB ID.', 'red'); return; }
  tmdSetStatus('Fetching…', 'blue');
  document.getElementById('tmdResults').style.display = 'none';
  fetch('/metadata/api/item/'+encodeURIComponent(id))
    .then(r=>r.json())
    .then(data=>{
      if (data && !data.error) {
        _tmdData.item=data.item; _tmdData.meta=data.metadata; _tmdData.seasons=data.seasons;
        _tmdUid = 0;
        tmdRenderRoot(data.item,     'tmdItemTree',    'tmdItemLabel');
        tmdRenderRoot(data.metadata, 'tmdMetaTree',    'tmdMetaLabel');
        tmdRenderRoot(data.seasons,  'tmdSeasonsTree', 'tmdSeasonsLabel');
        document.getElementById('tmdResults').style.display = 'block';
        tmdSetStatus('Fetched.', 'green');
      } else {
        tmdSetStatus(data&&data.error ? data.error : 'Not found.', 'red');
      }
    })
    .catch(()=>tmdSetStatus('Error fetching metadata.','red'));
}

function tmdRecreate() {
  // Delegate to classic button which has the showPopup confirm
  const btn = document.getElementById('deleteBtn');
  if (btn) { document.getElementById('imdbIdInput').value = document.getElementById('tmdInput').value; btn.click(); }
}

function tmdDelete() {
  const btn = document.getElementById('deleteOnlyBtn');
  if (btn) { document.getElementById('imdbIdInput').value = document.getElementById('tmdInput').value; btn.click(); }
}

})();
