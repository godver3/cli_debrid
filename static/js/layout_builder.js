/**
 * Layout Builder — Badge-Centric Model
 *
 * Each overlay is a list of "badges" — composite elements:
 *   background  (rounded rect with opacity)
 *   icon        (image/SVG logo)
 *   text        (value from DB variable, with align)
 *
 * Conditions are NOT user-editable here — they come from presets and are
 * evaluated by the backend renderer.
 */

// ═══════════════════════════════════════════════════════════
//  BADGE PRESETS  (condition stored but not shown in UI)
// ═══════════════════════════════════════════════════════════

// Badge preset defaults are sized at 600px canvas width.
// The renderer scales everything by poster_width/600, so 60px font → ~100px on a 1000px poster.
// All dimensions scaled ~2.3× from original so proportions stay consistent at 60px font.
const BADGE_PRESETS = {
    imdb_rating: {
        label: 'IMDb Rating',
        condition: 'imdbRating IS NOT NULL',
        background: { enabled: true, color: '#1A1A1ACC', width: 320, height: 130, borderRadius: 18 },
        icon: { enabled: true, type: 'image', path: '/logos/rating/IMDb.png', width: 115, height: 60, side: 'left' },
        text: { enabled: true, value: '{{imdbRating}}', font: 'DejaVuSans-Bold', size: 60, color: '#F5C518', align: 'left', fallback: 'N/A' }
    },
    tmdb_rating: {
        label: 'TMDb Rating',
        condition: 'tmdbRating IS NOT NULL',
        background: { enabled: true, color: '#1A1A1ACC', width: 320, height: 130, borderRadius: 18 },
        icon: { enabled: true, type: 'image', path: '/logos/rating/TMDb.png', width: 100, height: 65, side: 'left' },
        text: { enabled: true, value: '{{tmdbRating}}', font: 'DejaVuSans-Bold', size: 60, color: '#FFFFFF', align: 'left', fallback: 'N/A' }
    },
    rt_critics: {
        label: 'RT Critics',
        condition: 'rtCriticsScore IS NOT NULL',
        background: { enabled: true, color: '#1A1A1ACC', width: 320, height: 130, borderRadius: 18 },
        icon: { enabled: true, type: 'image', path: '/logos/rating/RT-Crit-Fresh.png', width: 74, height: 74, side: 'left' },
        text: { enabled: true, value: '{{rtCriticsScore}}%', font: 'DejaVuSans-Bold', size: 60, color: '#FA320A', align: 'left', fallback: 'N/A' }
    },
    rt_audience: {
        label: 'RT Audience',
        condition: 'rtUserScore IS NOT NULL',
        background: { enabled: true, color: '#1A1A1ACC', width: 320, height: 130, borderRadius: 18 },
        icon: { enabled: true, type: 'image', path: '/logos/rating/RT-Aud-Fresh.png', width: 74, height: 74, side: 'left' },
        text: { enabled: true, value: '{{rtUserScore}}%', font: 'DejaVuSans-Bold', size: 60, color: '#FFFFFF', align: 'left', fallback: 'N/A' }
    },
    trakt_rating: {
        label: 'Trakt Rating',
        condition: 'traktRating IS NOT NULL',
        background: { enabled: true, color: '#1A1A1ACC', width: 320, height: 130, borderRadius: 18 },
        icon: { enabled: true, type: 'image', path: '/logos/rating/Trakt.png', width: 70, height: 70, side: 'left' },
        text: { enabled: true, value: '{{traktRating}}', font: 'DejaVuSans-Bold', size: 60, color: '#ED1C24', align: 'left', fallback: 'N/A' }
    },
    resolution: {
        label: 'Resolution',
        condition: 'resolution IS NOT NULL',
        background: { enabled: true, color: '#000000CC', width: 275, height: 110, borderRadius: 14 },
        icon: { enabled: false, type: 'image', path: '', width: 70, height: 70, side: 'left' },
        text: { enabled: true, value: '{{resolution}}', font: 'DejaVuSans-Bold', size: 60, color: '#FFFFFF', align: 'center', fallback: '' }
    },
    hdr: {
        label: 'HDR Format',
        condition: 'hdr == true',
        background: { enabled: true, color: '#000000CC', width: 275, height: 110, borderRadius: 14 },
        icon: { enabled: false, type: 'image', path: '', width: 70, height: 70, side: 'left' },
        text: { enabled: true, value: '{{hdr}}', font: 'DejaVuSans-Bold', size: 60, color: '#FFD700', align: 'center', fallback: 'HDR' }
    },
    audio: {
        label: 'Audio Codec',
        condition: 'audioCodec IS NOT NULL',
        background: { enabled: true, color: '#000000CC', width: 345, height: 110, borderRadius: 14 },
        icon: { enabled: false, type: 'image', path: '', width: 70, height: 70, side: 'left' },
        text: { enabled: true, value: '{{audioCodec}}', font: 'DejaVuSans-Bold', size: 60, color: '#FFFFFF', align: 'center', fallback: '' }
    },
    audio_channels: {
        label: 'Audio Channels',
        condition: 'audioChannels IS NOT NULL',
        background: { enabled: true, color: '#000000CC', width: 230, height: 110, borderRadius: 14 },
        icon: { enabled: false, type: 'image', path: '', width: 70, height: 70, side: 'left' },
        text: { enabled: true, value: '{{audioChannels}}', font: 'DejaVuSans-Bold', size: 60, color: '#FFFFFF', align: 'center', fallback: '' }
    },
    video_codec: {
        label: 'Video Codec',
        condition: 'videoCodec IS NOT NULL',
        background: { enabled: true, color: '#000000CC', width: 275, height: 110, borderRadius: 14 },
        icon: { enabled: false, type: 'image', path: '', width: 70, height: 70, side: 'left' },
        text: { enabled: true, value: '{{videoCodec}}', font: 'DejaVuSans-Bold', size: 60, color: '#FFFFFF', align: 'center', fallback: '' }
    },
    format: {
        label: 'Format / Source',
        condition: 'format IS NOT NULL',
        background: { enabled: true, color: '#000000CC', width: 300, height: 110, borderRadius: 14 },
        icon: { enabled: false, type: 'image', path: '', width: 70, height: 70, side: 'left' },
        text: { enabled: true, value: '{{format}}', font: 'DejaVuSans-Bold', size: 60, color: '#FFFFFF', align: 'center', fallback: '' }
    },
    network: {
        label: 'Network',
        condition: 'network IS NOT NULL',
        background: { enabled: true, color: '#000000CC', width: 320, height: 110, borderRadius: 14 },
        icon: { enabled: false, type: 'image', path: '', width: 70, height: 70, side: 'left' },
        text: { enabled: true, value: '{{network}}', font: 'DejaVuSans-Bold', size: 60, color: '#FFFFFF', align: 'center', fallback: '' }
    },
    studio: {
        label: 'Studio',
        condition: 'studio IS NOT NULL',
        background: { enabled: true, color: '#000000CC', width: 320, height: 110, borderRadius: 14 },
        icon: { enabled: false, type: 'image', path: '', width: 70, height: 70, side: 'left' },
        text: { enabled: true, value: '{{studio}}', font: 'DejaVuSans-Bold', size: 60, color: '#FFFFFF', align: 'center', fallback: '' }
    },
    content_rating: {
        label: 'Content Rating',
        condition: 'contentRating IS NOT NULL',
        background: { enabled: true, color: '#000000CC', width: 230, height: 110, borderRadius: 14 },
        icon: { enabled: false, type: 'image', path: '', width: 70, height: 70, side: 'left' },
        text: { enabled: true, value: '{{contentRating}}', font: 'DejaVuSans-Bold', size: 60, color: '#FFFFFF', align: 'center', fallback: '' }
    },
    status: {
        label: 'Show Status',
        condition: 'status IS NOT NULL',
        background: { enabled: true, color: '#000000CC', width: 270, height: 110, borderRadius: 14 },
        icon: { enabled: false, type: 'image', path: '', width: 70, height: 70, side: 'left' },
        text: { enabled: true, value: '{{status}}', font: 'DejaVuSans-Bold', size: 60, color: '#FFFFFF', align: 'center', fallback: '' }
    },
    versions: {
        label: 'Versions / Duplicates',
        condition: 'versionCount > 1',
        background: { enabled: true, color: '#000000CC', width: 270, height: 110, borderRadius: 14 },
        icon: { enabled: true, type: 'image', path: '/logos/misc/copy.png', width: 60, height: 60, side: 'left' },
        text: { enabled: true, value: '{{versionCount}}', font: 'DejaVuSans-Bold', size: 60, color: '#FFFFFF', align: 'left', fallback: '' }
    },
    custom: {
        label: 'Custom Badge',
        condition: '',
        background: { enabled: true, color: '#000000CC', width: 345, height: 140, borderRadius: 18 },
        icon: { enabled: false, type: 'image', path: '', width: 90, height: 90, side: 'left' },
        text: { enabled: true, value: 'Custom Text', font: 'DejaVuSans-Bold', size: 60, color: '#FFFFFF', align: 'center', fallback: '' }
    },
    file_match: {
        label: 'File Match',
        condition: '',
        background: { enabled: true, color: '#000000CC', width: 300, height: 110, borderRadius: 14 },
        icon: { enabled: false, type: 'image', path: '', width: 70, height: 70, side: 'left' },
        text: { enabled: true, value: '', font: 'DejaVuSans-Bold', size: 60, color: '#FFFFFF', align: 'center', fallback: '' },
        filenameMatch: { searchTerm: '', displayText: '', useIcon: false }
    }
};

// ═══════════════════════════════════════════════════════════
//  BACKGROUND PANEL — default structure + helpers
// ═══════════════════════════════════════════════════════════

function backgroundPanelDefaults(overrides) {
    return Object.assign({
        type: 'background_panel', label: 'Tray',
        x: 9, y: 754, width: 180, height: 58, opacity: 1.0,
        borderEnabled: true, borderColor: '#ffffff', borderOpacity: 0.08, borderWidth: 1,
        borderRadius: 8,
        bgType: 'solid', bgColor: '#000000', bgOpacity: 0.8,
        bgColor2: '#000000', bgGradientAngle: 135,
        bgPadding: 0,
    }, overrides || {});
}

function addBackgroundPanel(x, y) {
    const panel = backgroundPanelDefaults({ x, y,
        id: Date.now() + Math.random() });
    badges.push(panel);
    updateBadgesList();
    selectBadge(panel.id);
    renderCanvas();
}

function populatePanelProperties(badge) {
    document.getElementById('pan-x').value           = badge.x;
    document.getElementById('pan-y').value           = badge.y;
    document.getElementById('pan-width').value       = badge.width  || 180;
    document.getElementById('pan-height').value      = badge.height || 58;
    const opPct = Math.round((badge.opacity ?? 1.0) * 100);
    document.getElementById('pan-opacity').value     = opPct;
    document.getElementById('pan-opacity-val').textContent = opPct + '%';
    // Border
    document.getElementById('pan-border-enabled').checked = badge.borderEnabled ?? true;
    document.getElementById('pan-border-color').value     = badge.borderColor   ?? '#ffffff';
    document.getElementById('pan-border-width').value     = badge.borderWidth   ?? 1;
    const bOpPct = Math.round((badge.borderOpacity ?? 0.08) * 100);
    document.getElementById('pan-border-opacity').value   = bOpPct;
    document.getElementById('pan-border-opacity-val').textContent = bOpPct + '%';
    document.getElementById('pan-border-radius').value    = badge.borderRadius  ?? 8;
    // Background
    document.getElementById('pan-bg-type').value    = badge.bgType    ?? 'solid';
    document.getElementById('pan-bg-color').value   = badge.bgColor   ?? '#000000';
    document.getElementById('pan-bg-color2').value  = badge.bgColor2  ?? '#000000';
    document.getElementById('pan-bg-angle').value   = badge.bgGradientAngle ?? 135;
    const bgOpPct = Math.round((badge.bgOpacity ?? 0.8) * 100);
    document.getElementById('pan-bg-opacity').value = bgOpPct;
    document.getElementById('pan-bg-opacity-val').textContent = bgOpPct + '%';
    document.getElementById('pan-bg-padding').value = badge.bgPadding ?? 0;
    _panUpdateBgGradientVisibility();
    document.querySelectorAll('.properties-panel input[type="range"]').forEach(_syncRangeVal);
}

function _panUpdateBgGradientVisibility() {
    const isGrad = document.getElementById('pan-bg-type').value === 'gradient';
    document.getElementById('pan-bg-color2-wrap').style.display = isGrad ? '' : 'none';
    document.getElementById('pan-bg-angle-wrap').style.display  = isGrad ? '' : 'none';
}

function updateSelectedPanel() {
    const badge = badges.find(b => b.id === selectedBadgeId);
    if (!badge || badge.type !== 'background_panel') return;
    badge.x             = parseInt(document.getElementById('pan-x').value)      || 0;
    badge.y             = parseInt(document.getElementById('pan-y').value)      || 0;
    badge.width         = parseInt(document.getElementById('pan-width').value)  || 180;
    badge.height        = parseInt(document.getElementById('pan-height').value) || 58;
    badge.opacity       = parseFloat(document.getElementById('pan-opacity').value) / 100;
    badge.borderEnabled = document.getElementById('pan-border-enabled').checked;
    badge.borderColor   = document.getElementById('pan-border-color').value;
    badge.borderWidth   = parseInt(document.getElementById('pan-border-width').value) || 1;
    badge.borderOpacity = parseFloat(document.getElementById('pan-border-opacity').value) / 100;
    badge.borderRadius  = parseInt(document.getElementById('pan-border-radius').value) ?? 8;
    badge.bgType        = document.getElementById('pan-bg-type').value;
    badge.bgColor       = document.getElementById('pan-bg-color').value;
    badge.bgColor2      = document.getElementById('pan-bg-color2').value;
    badge.bgGradientAngle = parseFloat(document.getElementById('pan-bg-angle').value) || 135;
    badge.bgOpacity     = parseFloat(document.getElementById('pan-bg-opacity').value) / 100;
    badge.bgPadding     = parseInt(document.getElementById('pan-bg-padding').value) || 0;
    _panUpdateBgGradientVisibility();
    updateBadgesList();
    renderCanvas();
}

function setupPanelBindings() {
    const ids = [
        'pan-x','pan-y','pan-width','pan-height',
        'pan-border-enabled','pan-border-color','pan-border-width','pan-border-radius',
        'pan-bg-type','pan-bg-color','pan-bg-color2','pan-bg-angle','pan-bg-padding',
    ];
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('change', updateSelectedPanel);
    });
    ['pan-opacity','pan-border-opacity','pan-bg-opacity'].forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('input', () => {
            const spanId = id + '-val';
            const span = document.getElementById(spanId);
            if (span) span.textContent = el.value + '%';
            updateSelectedPanel();
        });
    });
    const bgType = document.getElementById('pan-bg-type');
    if (bgType) bgType.addEventListener('change', _panUpdateBgGradientVisibility);
}

function renderPanelOnCanvas(ctx, badge, isSelected) {
    const x = badge.x, y = badge.y;
    const W = badge.width  || 180;
    const H = badge.height || 58;
    const R = badge.borderRadius ?? 8;
    const op = badge.opacity ?? 1.0;
    const pad = badge.bgPadding ?? 0;

    ctx.save();
    ctx.globalAlpha = op;

    // Background
    const px = x + pad, py = y + pad, pw = W - pad * 2, ph = H - pad * 2;
    if (badge.bgType === 'gradient') {
        const angle = (badge.bgGradientAngle ?? 135) * Math.PI / 180;
        const cx2 = px + pw / 2, cy2 = py + ph / 2;
        const len = Math.sqrt(pw * pw + ph * ph) / 2;
        const gx1 = cx2 - Math.cos(angle) * len, gy1 = cy2 - Math.sin(angle) * len;
        const gx2 = cx2 + Math.cos(angle) * len, gy2 = cy2 + Math.sin(angle) * len;
        const g = ctx.createLinearGradient(gx1, gy1, gx2, gy2);
        g.addColorStop(0, _hexToRgba(badge.bgColor  ?? '#000000', badge.bgOpacity ?? 0.8));
        g.addColorStop(1, _hexToRgba(badge.bgColor2 ?? '#000000', badge.bgOpacity ?? 0.8));
        ctx.fillStyle = g;
    } else {
        ctx.fillStyle = _hexToRgba(badge.bgColor ?? '#000000', badge.bgOpacity ?? 0.8);
    }
    _rrPath(ctx, px, py, pw, ph, R);
    ctx.fill();

    // Border
    if (badge.borderEnabled !== false) {
        const bw = badge.borderWidth ?? 1;
        const half = bw / 2;
        ctx.strokeStyle = _hexToRgba(badge.borderColor ?? '#ffffff', badge.borderOpacity ?? 0.08);
        ctx.lineWidth   = bw;
        _rrPath(ctx, px + half, py + half, pw - bw, ph - bw, Math.max(0, R - half));
        ctx.stroke();
    }

    // Selection outline
    if (isSelected) {
        ctx.strokeStyle = 'rgba(120,56,255,0.9)';
        ctx.lineWidth   = 2;
        ctx.setLineDash([4, 3]);
        _rrPath(ctx, px - 2, py - 2, pw + 4, ph + 4, R + 2);
        ctx.stroke();
        ctx.setLineDash([]);
    }

    ctx.restore();
}

// ═══════════════════════════════════════════════════════════
//  DESIGNED BADGE — default structure
// ═══════════════════════════════════════════════════════════

function designedBadgeDefaults(overrides) {
    return Object.assign({
        type: 'designed_badge', label: 'Designed Badge',
        x: 20, y: 20, width: 0, height: 0, opacity: 1.0,
        borderRadius: 8,
        borderEnabled: true, borderColor: '#ffffff', borderOpacity: 0.08, borderWidth: 1,
        highlightEnabled: true, highlightOpacity: 0.09,
        bgType: 'solid', bgColor: '#000000', bgOpacity: 0.8,
        bgColor2: '#000000', bgGradientAngle: 135,
        leftEnabled: true, leftWidth: 0, leftPaddingH: 10,
        leftBgColor: '#000000', leftBgOpacity: 0.0,
        leftText: '{{resolution}}', leftFont: 'Bebas Neue', leftFontSize: 18,
        leftColor: '#ffffff', leftOpacity: 0.9, leftFontWeight: 'normal', leftFontStyle: 'normal',
        dividerEnabled: true, dividerColor: '#ffffff', dividerOpacity: 0.07,
        rightEnabled: true, rightPaddingH: 8,
        rightBgType: 'gradient',
        rightBgColor: '#7838ff', rightBgColor2: '#ff6e14',
        rightBgOpacity: 0.15, rightBgGradientAngle: 135,
        autoLayout: true, friendlyResolution: false, resFluid: false, verticalStack: false,
        ratingFormat: 'auto',
        rightLayout: 'stacked',
        rightText1: '{{hdrLine1}}', rightFont1: 'Barlow Condensed', rightFontSize1: 30,
        rightColor1: '#e0e0e0', rightOpacity1: 0.80, rightFontWeight1: 'bold', rightFontStyle1: 'normal',
        rightText2: '{{hdrLine2}}', rightFont2: 'Barlow Condensed', rightFontSize2: 28,
        rightColor2: '#e0e0e0', rightOpacity2: 0.60, rightFontWeight2: 'bold', rightFontStyle2: 'normal',
    }, overrides || {});
}

// Preset text configs — only overrides text/layout fields, leaves style intact
const DB_PRESETS = {
    resolution_hdr: {
        leftText: '{{resolution}}', rightLayout: 'stacked',
        rightText1: '{{hdrLine1}}', rightText2: '{{hdrLine2}}',
        rightTextFallback: '{{definition}}',
    },
    resolution: {
        leftText: '{{resolution}}', rightLayout: 'single',
        rightText1: '{{definition}}', rightText2: '',
    },
    hdr: {
        leftText: '{{hdr}}',        rightLayout: 'single',
        rightText1: '',             rightText2: '',
    },
    audio_codec: {
        leftText: '{{audioCodec}}', rightLayout: 'single',
        rightText1: '',             rightText2: '',
    },
    audio_combo: {
        leftText: '{{audioCodec}}', rightLayout: 'single',
        rightText1: '{{audioChannels}}', rightText2: '',
    },
    audio_stacked: {
        leftText: '',               rightLayout: 'stacked',
        rightText1: '{{audioCodec}}', rightText2: '{{audioChannels}}',
    },
    video_codec: {
        leftText: '{{videoCodec}}', rightLayout: 'single',
        rightText1: '',             rightText2: '',
    },
    format: {
        leftText: '{{format}}',     rightLayout: 'single',
        rightText1: '',             rightText2: '',
    },
    imdb: {
        leftText: 'IMDb',           rightLayout: 'single',
        rightText1: '{{imdbRating}}', rightText2: '',
    },
    tmdb: {
        leftText: 'TMDb',           rightLayout: 'single',
        rightText1: '{{tmdbRating}}', rightText2: '',
    },
    trakt: {
        leftText: 'Trakt',          rightLayout: 'single',
        rightText1: '{{traktRating}}', rightText2: '',
    },
    rt_critics: {
        leftText: 'RT',             rightLayout: 'single',
        rightText1: '{{rtCriticsScore}}%', rightText2: '',
    },
    rt_audience: {
        leftText: 'RT',             rightLayout: 'single',
        rightText1: '{{rtUserScore}}%', rightText2: '',
    },
    network: {
        leftText: '{{network}}',    rightLayout: 'single',
        rightText1: '',             rightText2: '',
    },
    studio: {
        leftText: '{{studio}}',     rightLayout: 'single',
        rightText1: '',             rightText2: '',
    },
    content_rating: {
        leftText: '{{contentRating}}', rightLayout: 'single',
        rightText1: '',                rightText2: '',
    },
    status: {
        leftText: '{{status}}',        rightLayout: 'single',
        rightText1: '',                rightText2: '',
    },
    versions: {
        leftText: '{{versionCount}}',  rightLayout: 'single',
        rightText1: 'Versions',        rightText2: '',
    },
};

function _dbUpdatePresetUI(preset) {
    const isAudio = preset === 'audio_codec' || preset === 'audio_combo' || preset === 'audio_channels';

    // Switch between audio-specific panel and standard panel
    const audioProps = document.getElementById('db-audio-properties');
    const stdProps   = document.getElementById('db-standard-properties');
    if (audioProps) audioProps.style.display = isAudio ? '' : 'none';
    if (stdProps)   stdProps.style.display   = isAudio ? 'none' : '';

    // Within audio panel: show channels corner only for audio_combo
    const audioChSection = document.getElementById('db-audio-channels-section');
    if (audioChSection) audioChSection.style.display = (preset === 'audio_combo') ? '' : 'none';

    // Hide width/height from container for audio (audio panel has its own height field)
    const whRow = document.getElementById('db-wh-row');
    if (whRow) whRow.style.display = isAudio ? 'none' : '';

    // Within standard panel: friendly resolution row — only for resolution-based presets
    const isResolution = ['resolution', 'resolution_hdr'].includes(preset);
    const row = document.getElementById('db-friendly-res-row');
    if (row) row.style.display = isResolution ? '' : 'none';

    // Within standard panel: logo-only row
    const logoOnlyRow = document.getElementById('db-logo-only-row');
    const isNetStudio = preset === 'network' || preset === 'studio';
    if (logoOnlyRow) logoOnlyRow.style.display = isNetStudio ? '' : 'none';
    _dbUpdateLogoVariantRow();
}

function applyDbPreset(presetKey) {
    if (!presetKey || presetKey === 'custom') { _dbUpdatePresetUI('custom'); return; }
    const preset = DB_PRESETS[presetKey];
    if (!preset) return;
    _dbSet('db-left-text',    preset.leftText    ?? '');
    _dbSet('db-right-layout', preset.rightLayout ?? 'single');
    _dbSet('db-right-text1',  preset.rightText1  ?? '');
    _dbSet('db-right-text2',  preset.rightText2  ?? '');
    // audio_stacked: no left segment, right stacked, vertical orientation
    if (presetKey === 'audio_stacked') {
        _dbSet('db-left-enabled', false);
        _dbSet('db-vertical-stack', true);
        _dbSet('db-auto-layout', true);
    }
    // Store fallback directly on the badge object (not a form field)
    const badge = badges.find(b => b.id === selectedBadgeId);
    if (badge) badge.rightTextFallback = preset.rightTextFallback ?? '';
    // sync stacked-row visibility
    const stackedRow = document.getElementById('db-right-text2-row');
    if (stackedRow) stackedRow.style.display = (preset.rightLayout === 'stacked') ? '' : 'none';
    _dbUpdatePresetUI(presetKey);
}

// ═══════════════════════════════════════════════════════════
//  GOOGLE FONTS — curated list + on-demand loader
// ═══════════════════════════════════════════════════════════

const GOOGLE_FONTS = [
    // Display / Impact
    'Anton','Archivo Black','Bebas Neue','Black Han Sans','Black Ops One',
    'Boogaloo','Chakra Petch','Cinzel','Exo','Exo 2','Fjalla One',
    'Francois One','Fredoka','Graduate','Grandstander','Jockey One',
    'Kanit','Kavoon','Lalezar','Lobster','Monoton','Orbitron',
    'Oswald','Permanent Marker','Poller One','Press Start 2P',
    'Rajdhani','Righteous','Russo One','Saira Condensed','Sigmar One',
    'Silkscreen','Squada One','Teko','Titan One','Ultra','Wallpoet',
    // Condensed / Bold sans
    'Arimo','Barlow','Barlow Condensed','Barlow Semi Condensed',
    'Cabin','DM Sans','Figtree','Hind','IBM Plex Sans',
    'Inter','Josefin Sans','Karla','Lato','Manrope',
    'Montserrat','Mulish','Noto Sans','Nunito','Open Sans',
    'Outfit','Overpass','Oxanium','Poppins','Public Sans',
    'Questrial','Quicksand','Readex Pro','Roboto','Roboto Condensed',
    'Rubik','Sora','Space Grotesk','Ubuntu','Urbanist','Work Sans',
    // Serif
    'Crimson Text','DM Serif Display','EB Garamond','Fraunces',
    'Libre Baskerville','Lora','Merriweather','Playfair Display','Spectral',
    // Monospace
    'Courier Prime','DM Mono','Fira Code','IBM Plex Mono','Inconsolata',
    'JetBrains Mono','Oxygen Mono','Roboto Mono','Share Tech Mono',
    'Source Code Pro','Space Mono',
].sort();

const _loadedFonts = new Set(['Arial','Georgia','Verdana','Times New Roman','Courier New']);
// Map of family → Promise so concurrent callers all await the same load
const _fontLoadPromises = new Map();

async function loadGoogleFont(family) {
    if (!family || _loadedFonts.has(family)) return;
    // If already loading, await the same promise (don't double-load)
    if (_fontLoadPromises.has(family)) {
        await _fontLoadPromises.get(family);
        return;
    }
    const promise = (async () => {
        const link = document.createElement('link');
        link.rel  = 'stylesheet';
        link.href = `https://fonts.googleapis.com/css2?family=${encodeURIComponent(family)}:ital,wght@0,400;0,700;1,400;1,700&display=swap`;
        document.head.appendChild(link);
        // Poll until the font is actually available (up to 5s)
        const deadline = Date.now() + 5000;
        while (Date.now() < deadline) {
            try {
                const loaded = await document.fonts.load(`16px "${family}"`);
                if (loaded.length > 0) break;
            } catch (_) { break; }
            await new Promise(r => setTimeout(r, 100));
        }
        _loadedFonts.add(family);
        _fontLoadPromises.delete(family);
        renderCanvas();
    })();
    _fontLoadPromises.set(family, promise);
    await promise;
}

/** Load a PIL/server font by name (e.g. 'DejaVuSans-Bold') via FontFace API. */
async function _ensureFont(fontName) {
    if (_loadedFonts.has(fontName)) return;
    if (_fontLoadPromises.has(fontName)) {
        await _fontLoadPromises.get(fontName);
        return;
    }
    const promise = (async () => {
        const url = `/api/overlays/fonts/${fontName}.ttf`;
        try {
            const face = new FontFace(fontName, `url(${url})`);
            await face.load();
            document.fonts.add(face);
            _loadedFonts.add(fontName);
            _fontLoadPromises.delete(fontName);
            renderCanvas();
        } catch (_) {
            // Font not on server — canvas falls back to browser sans-serif
            _fontLoadPromises.delete(fontName);
        }
    })();
    _fontLoadPromises.set(fontName, promise);
    await promise;
}

/** Build a CSS font string using the badge's stored font name, weight and style. */
function _canvasFontStr(fontName, bold, size, italic) {
    const style  = italic ? 'italic ' : '';
    const weight = bold ? '700' : '400';
    return `${style}${weight} ${size}px "${fontName}", sans-serif`;
}

// Preload the two default designed-badge fonts on page load
function preloadDesignedBadgeFonts() {
    loadGoogleFont('Bebas Neue');
    loadGoogleFont('Barlow Condensed');
    _ensureFont('DejaVuSans-Bold');
    _ensureFont('DejaVuSans');
}

/** Populate all font <select> elements with DejaVu options + full Google Fonts list.
 *  Call once on page load. Call again with a specific id + value to re-select after loading layout. */
function populateFontSelects(selectId, selectedValue) {
    const PIL_FONTS = ['DejaVuSans-Bold', 'DejaVuSans'];
    const ALL_FONTS = [...PIL_FONTS, ...GOOGLE_FONTS];
    const ids = selectId
        ? [selectId]
        : ['text-font', 'db-left-font', 'db-right-font1', 'db-right-font2', 'db-ch-font', 'db-audio-font'];
    ids.forEach(id => {
        const sel = document.getElementById(id);
        if (!sel) return;
        const current = selectedValue || sel.value || '';
        // Build option list; add current value at top if not already in list
        const fontList = (current && !ALL_FONTS.includes(current))
            ? [current, ...ALL_FONTS]
            : ALL_FONTS;
        sel.innerHTML = fontList.map(f =>
            `<option value="${f}"${f === current ? ' selected' : ''}>${f}</option>`
        ).join('');
    });
}

// ═══════════════════════════════════════════════════════════
//  SMART BADGE PRESETS  (PNG asset badges from badge library)
// ═══════════════════════════════════════════════════════════

const SMART_BADGE_SLUGS = {
    sb_audio_codec:    { label: 'Audio Codec',       badge_type: 'audio_codec',    previewW: 130, previewH: 52 },
    sb_audio_combo:    { label: 'Audio + Channels',  badge_type: 'audio_combo',    previewW: 140, previewH: 42 },
    sb_resolution:     { label: 'Resolution',        badge_type: 'resolution',     previewW: 80,  previewH: 42 },
    sb_hdr:            { label: 'HDR Format',        badge_type: 'hdr',            previewW: 80,  previewH: 42 },
    sb_resolution_hdr: { label: 'Resolution + HDR',  badge_type: 'resolution_hdr', previewW: 120, previewH: 42 },
};

// Preloaded sample PNGs for smart badge canvas preview (keyed by badge_type slug)
const smartBadgePreviewImages = {};

// Icon image cache for v2 badge icon/logo preview (keyed by icon path)
const _iconImageCache = {};

/**
 * Return a loaded Image for the given icon path, or null if not yet ready.
 * Kicks off loading on first call; calls onLoad() when the image arrives
 * so the caller can re-render.
 * Paths starting with /logos/ are served via /api/overlays/logos/serve/.
 */
function _getOrLoadIconImage(path, onLoad) {
    if (!path) return null;
    const entry = _iconImageCache[path];
    if (entry) return entry.loaded ? entry.img : null;

    // Convert /logos/... path to a web-servable URL
    let url;
    if (path.startsWith('/logos/')) {
        url = '/api/overlays/logos/serve/' + path.slice('/logos/'.length);
    } else {
        // Filesystem paths that aren't under /logos/ can't be fetched in the browser
        return null;
    }

    const img = new Image();
    _iconImageCache[path] = { img, loaded: false };
    img.onload  = () => { _iconImageCache[path].loaded = true;  onLoad(); };
    img.onerror = () => { _iconImageCache[path].loaded = false; };
    img.src = url;
    return null;
}

/**
 * Returns the /logos/ path for a network or studio name.
 * Tries white variant for network, standard for studio.
 * name must already be interpolated (actual value, not template).
 */
function _logoPathForPreset(preset, name, variant) {
    if (!name) return null;
    if (preset === 'network') {
        const sub = (variant === 'color') ? 'color' : 'white';
        return `/logos/network/${sub}/${name}.png`;
    }
    if (preset === 'studio') {
        const sub = (variant === 'color') ? 'bigger' : 'standard';
        return `/logos/studio/${sub}/${name}.png`;
    }
    return null;
}

function preloadSmartBadgeImages() {
    Object.entries(SMART_BADGE_SLUGS).forEach(([, def]) => {
        const img = new Image();
        img.onload = () => {
            smartBadgePreviewImages[def.badge_type] = img;
            renderCanvas();
        };
        img.src = `/api/overlays/badges/types/${def.badge_type}/sample_asset`;
    });
}

// ═══════════════════════════════════════════════════════════
//  SAMPLE MEDIA DATA (canvas preview interpolation)
// ═══════════════════════════════════════════════════════════

function resolutionToDefinition(res) {
    const r = (res || '').toLowerCase().replace(/\s/g, '');
    if (/^(2160p?|4k|uhd)/.test(r))  return 'UHD';
    if (/^(1440p?|qhd)/.test(r))      return 'QHD';
    if (/^(1080p?|fhd)/.test(r))      return 'FHD';
    if (/^(720p?|hd)/.test(r))        return 'HD';
    return 'SD';
}

let SAMPLE_MEDIA = {
    imdbRating: '8.5', tmdbRating: '8.2', traktRating: '82',
    rtCriticsScore: '95', rtUserScore: '88',
    resolution: '2160p', definition: 'UHD',
    hdr: 'DV', hdrLine1: 'DV', hdrLine2: 'HDR10+',
    audioCodec: 'TrueHD Atmos', audioChannels: '7.1',
    videoCodec: 'HEVC', format: 'BluRay',
    network: 'HBO', studio: 'A24', contentRating: 'R', year: '2023',
    status: 'Returning', versionCount: '2'
};

// ═══════════════════════════════════════════════════════════
//  STATE
// ═══════════════════════════════════════════════════════════

let badges = [];
let selectedBadgeId = null;
let isDragging = false;
let dragOffsetX = 0;
let dragOffsetY = 0;
let currentLayoutId = null;
let posterImage = null;   // loaded poster HTMLImageElement (null = show gradient)
let posterPool = [];      // array of {title, poster_url, tmdb_id, type} from library
let posterPoolIndex = -1; // current position in posterPool (-1 = none loaded)
// Clearlogo cache: keyed by tmdb_id — value is HTMLImageElement|null ('null' = no logo)
const _clearlogoCache = {};
let canvasZoom = 1.0;
let showGrid = false;

// ═══════════════════════════════════════════════════════════
//  INIT
// ═══════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
    setupPaletteDragDrop();
    setupCanvasInteraction();
    populateFontSelects();
    setupPropertyBindings();
    preloadDesignedBadgeFonts();
    renderCanvas();
    loadLayoutFromUrl();
    preloadSmartBadgeImages();
    loadPosterPool();
    document.getElementById('layout-media-type')?.addEventListener('change', loadPosterPool);
    fitZoom();
    window.addEventListener('resize', fitZoom);
    setupPanelBindings();
    setupDesignedBadgeBindings();
    preloadDesignedBadgeFonts();
    _initRangeSliders();

    // Wire preview data controls
    ['pd-resolution','pd-hdr','pd-audio-codec','pd-audio-channels','pd-video-codec','pd-format','pd-content-rating','pd-status'].forEach(id => {
        document.getElementById(id)?.addEventListener('change', syncPreviewData);
    });
    ['pd-network','pd-studio'].forEach(id => {
        document.getElementById(id)?.addEventListener('input', syncPreviewData);
    });

    // Close var legend on backdrop click
    document.getElementById('var-legend-modal')?.addEventListener('click', function(e) {
        if (e.target === this) closeVarLegend();
    });
});

// ═══════════════════════════════════════════════════════════
//  RANGE SLIDER FILL SYNC
// ═══════════════════════════════════════════════════════════

/**
 * Keeps the CSS --val property in sync with each range input's value so the
 * filled-left gradient in tangerine_layout_builder.css tracks correctly.
 * Also re-syncs whenever a slider's value is set programmatically via
 * the _syncRangeVal helper exported below.
 */
function _initRangeSliders() {
    document.querySelectorAll('input[type="range"]').forEach(el => {
        _syncRangeVal(el);
        el.addEventListener('input', () => _syncRangeVal(el));
    });
}

function _syncRangeVal(el) {
    const min = parseFloat(el.min) || 0;
    const max = parseFloat(el.max) || 100;
    const val = parseFloat(el.value);
    el.style.setProperty('--val', ((val - min) / (max - min)) * 100);
}

// ═══════════════════════════════════════════════════════════
//  PREVIEW DATA CONTROLS
// ═══════════════════════════════════════════════════════════

function syncPreviewData() {
    SAMPLE_MEDIA.resolution    = document.getElementById('pd-resolution')?.value     ?? SAMPLE_MEDIA.resolution;
    SAMPLE_MEDIA.definition    = resolutionToDefinition(SAMPLE_MEDIA.resolution);
    const _hdrVal              = document.getElementById('pd-hdr')?.value            ?? SAMPLE_MEDIA.hdr;
    SAMPLE_MEDIA.hdr           = _hdrVal;
    // Split combo HDR values (e.g. "DV HDR10+") into two stacked lines
    const _hdrParts = (_hdrVal || '').split(' ');
    SAMPLE_MEDIA.hdrLine1      = _hdrParts[0] || '';
    SAMPLE_MEDIA.hdrLine2      = _hdrParts.slice(1).join(' ') || '';
    SAMPLE_MEDIA.audioCodec    = document.getElementById('pd-audio-codec')?.value    ?? SAMPLE_MEDIA.audioCodec;
    SAMPLE_MEDIA.audioChannels = document.getElementById('pd-audio-channels')?.value ?? SAMPLE_MEDIA.audioChannels;
    SAMPLE_MEDIA.videoCodec    = document.getElementById('pd-video-codec')?.value    ?? SAMPLE_MEDIA.videoCodec;
    SAMPLE_MEDIA.format        = document.getElementById('pd-format')?.value         ?? SAMPLE_MEDIA.format;
    SAMPLE_MEDIA.network       = document.getElementById('pd-network')?.value        ?? SAMPLE_MEDIA.network;
    SAMPLE_MEDIA.studio        = document.getElementById('pd-studio')?.value         ?? SAMPLE_MEDIA.studio;
    SAMPLE_MEDIA.contentRating = document.getElementById('pd-content-rating')?.value ?? SAMPLE_MEDIA.contentRating;
    SAMPLE_MEDIA.status        = document.getElementById('pd-status')?.value         ?? SAMPLE_MEDIA.status;
    // Re-fetch smart badge (library badge) preview PNGs to match new preview data
    _reloadSmartBadgePreviews();
    renderCanvas();
}

function _reloadSmartBadgePreviews() {
    const params = new URLSearchParams({
        codec:      SAMPLE_MEDIA.audioCodec    || '',
        channels:   SAMPLE_MEDIA.audioChannels || '',
        resolution: SAMPLE_MEDIA.resolution    || '',
        hdr:        SAMPLE_MEDIA.hdr           || '',
    });
    let pending = 0;
    Object.values(SMART_BADGE_SLUGS).forEach(def => {
        const img = new Image();
        pending++;
        img.onload = () => {
            smartBadgePreviewImages[def.badge_type] = img;
            pending--;
            if (pending === 0) renderCanvas();
        };
        img.onerror = () => { pending--; if (pending === 0) renderCanvas(); };
        img.src = `/api/overlays/badges/types/${def.badge_type}/sample_asset?${params}`;
    });
}

function _dbUpdateLogoVariantRow() {
    const row = document.getElementById('db-logo-variant-row');
    if (!row) return;
    const logoOnly = document.getElementById('db-logo-only');
    const logoOnlyRow = document.getElementById('db-logo-only-row');
    const logoOnlyVisible = logoOnlyRow && logoOnlyRow.style.display !== 'none';
    row.style.display = (logoOnlyVisible && logoOnly && logoOnly.checked) ? '' : 'none';

    // Update option labels to match the current preset (network vs studio)
    const variantSel = document.getElementById('db-logo-variant');
    if (!variantSel) return;
    const preset = document.getElementById('db-preset')?.value;
    const opts = variantSel.options;
    if (preset === 'studio') {
        if (opts[0]) { opts[0].value = 'mono';  opts[0].text = 'Standard'; }
        if (opts[1]) { opts[1].value = 'color'; opts[1].text = 'Bigger'; }
    } else {
        if (opts[0]) { opts[0].value = 'mono';  opts[0].text = 'Mono (white)'; }
        if (opts[1]) { opts[1].value = 'color'; opts[1].text = 'Color'; }
    }
}

const _DB_RATING_PRESETS = new Set(['imdb','tmdb','trakt','rt_critics','rt_audience','custom']);
const _DB_STATUS_PRESETS = new Set(['status']);

function _dbUpdatePresetVisibility() {
    const preset = document.getElementById('db-preset')?.value || 'custom';
    const rfRow  = document.getElementById('db-rating-format-row');
    const scRow  = document.getElementById('db-status-color-row');
    const scMap  = document.getElementById('db-status-color-map');
    const scCb   = document.getElementById('db-use-status-colors');
    if (rfRow) rfRow.style.display = _DB_RATING_PRESETS.has(preset) ? '' : 'none';
    const showStatus = _DB_STATUS_PRESETS.has(preset);
    if (scRow) scRow.style.display = showStatus ? '' : 'none';
    if (!showStatus) {
        if (scCb)  scCb.checked = false;
        if (scMap) scMap.style.display = 'none';
    }
}

function togglePreviewData() {
    const body    = document.getElementById('preview-data-body');
    const chevron = document.getElementById('preview-data-chevron');
    if (!body) return;
    const collapsed = body.style.display === 'none';
    body.style.display    = collapsed ? '' : 'none';
    chevron.textContent   = collapsed ? '▲' : '▼';
}

function openVarLegend() {
    const m = document.getElementById('var-legend-modal');
    if (m) m.classList.add('open');
}

function closeVarLegend() {
    const m = document.getElementById('var-legend-modal');
    if (m) m.classList.remove('open');
}


// ═══════════════════════════════════════════════════════════
//  POSTER LOADING
// ═══════════════════════════════════════════════════════════

function loadPosterFile(event) {
    const file = event.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (e) => {
        const img = new Image();
        img.onload = () => {
            posterImage = img;
            renderCanvas();
        };
        img.src = e.target.result;
    };
    reader.readAsDataURL(file);
    // Reset file input so the same file can be re-selected
    event.target.value = '';
}

function resetToGradient() {
    posterImage = null;
    posterPoolIndex = -1;
    updatePosterCounter();
    renderCanvas();
}

async function loadPosterPool() {
    const mediaType = document.getElementById('layout-media-type')?.value || 'both';
    const typeParam = mediaType === 'movie' ? 'movie'
                    : mediaType === 'tv'    ? 'tv'
                    : 'both';
    const textlessParam = (typeof TEXTLESS_POSTERS_ENABLED !== 'undefined' && TEXTLESS_POSTERS_ENABLED) ? '&textless=1' : '';
    try {
        const resp = await fetch(`/api/overlays/preview/posters?type=${typeParam}&limit=20${textlessParam}`);
        const data = await resp.json();
        posterPool = data.posters || [];
        posterPoolIndex = -1;
        updatePosterCounter();
        // Auto-load first poster when no custom poster is currently displayed
        if (posterPool.length > 0 && !posterImage) cyclePoolPoster(1);
    } catch (e) {
        console.warn('Poster pool load failed:', e);
    }
}

function cyclePoolPoster(direction) {
    if (posterPool.length === 0) { loadPosterPool(); return; }
    posterPoolIndex = (posterPoolIndex + direction + posterPool.length) % posterPool.length;
    const entry = posterPool[posterPoolIndex];
    // Clear per-poster clearlogo cache so it re-fetches for the new poster's tmdb_id
    const cacheKey = entry?.tmdb_id ? `${entry.tmdb_id}_${entry.type || 'movie'}` : null;
    if (cacheKey && _clearlogoCache[cacheKey] !== undefined) {
        // Keep it — it's already loaded, no need to re-fetch
    }
    const img = new Image();
    img.onload = () => { posterImage = img; updatePosterCounter(); renderCanvas(); };
    img.onerror = () => {
        // Remove broken entry and try the next one
        posterPool.splice(posterPoolIndex, 1);
        posterPoolIndex = Math.min(posterPoolIndex, posterPool.length - 1);
        if (posterPool.length > 0) cyclePoolPoster(direction);
        else { posterImage = null; updatePosterCounter(); renderCanvas(); }
    };
    img.src = entry.poster_url;
}

function updatePosterCounter() {
    const el = document.getElementById('poster-counter');
    if (!el) return;
    el.textContent = posterPool.length > 0
        ? `${posterPoolIndex < 0 ? '-' : posterPoolIndex + 1} / ${posterPool.length}`
        : '';
}

// ═══════════════════════════════════════════════════════════
//  PALETTE DRAG-AND-DROP
// ═══════════════════════════════════════════════════════════

function setupPaletteDragDrop() {
    const canvas = document.getElementById('preview-canvas');

    document.querySelectorAll('.palette-item').forEach(item => {
        item.addEventListener('dragstart', (e) => {
            e.dataTransfer.setData('badgeType', item.dataset.badgeType);
        });
    });

    const dropZone = document.getElementById('canvas-wrapper');
    dropZone.addEventListener('dragover', (e) => e.preventDefault());
    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        const badgeType = e.dataTransfer.getData('badgeType');
        if (!badgeType) return;
        const rect = canvas.getBoundingClientRect();
        const x = Math.max(0, Math.round((e.clientX - rect.left) * (canvas.width / rect.width)));
        const y = Math.max(0, Math.round((e.clientY - rect.top) * (canvas.height / rect.height)));
        addBadge(badgeType, x, y);
    });
}

// ═══════════════════════════════════════════════════════════
//  CANVAS INTERACTION
// ═══════════════════════════════════════════════════════════

function setupCanvasInteraction() {
    const canvas = document.getElementById('preview-canvas');

    canvas.addEventListener('mousedown', (e) => {
        const { cx, cy } = getCanvasCoords(e);
        const hit = getBadgeAt(cx, cy);
        if (hit) {
            selectBadge(hit.id);
            isDragging = true;
            dragOffsetX = cx - hit.x;
            dragOffsetY = cy - hit.y;
        } else {
            selectBadge(null);
        }
    });

    canvas.addEventListener('mousemove', (e) => {
        const { cx, cy } = getCanvasCoords(e);
        if (!isDragging) {
            canvas.style.cursor = getBadgeAt(cx, cy) ? 'move' : 'default';
            return;
        }
        if (selectedBadgeId === null) return;
        const badge = badges.find(b => b.id === selectedBadgeId);
        if (!badge) return;
        badge.x = Math.max(0, Math.round(cx - dragOffsetX));
        badge.y = Math.max(0, Math.round(cy - dragOffsetY));
        for (const id of ['badge-x', 'sb-x', 'db-x', 'pan-x']) { const el = document.getElementById(id); if (el) el.value = badge.x; }
        for (const id of ['badge-y', 'sb-y', 'db-y', 'pan-y']) { const el = document.getElementById(id); if (el) el.value = badge.y; }
        renderCanvas();
    });

    canvas.addEventListener('mouseup', () => { isDragging = false; });
    canvas.addEventListener('mouseleave', () => { isDragging = false; });
}

function getCanvasCoords(e) {
    const canvas = document.getElementById('preview-canvas');
    const rect = canvas.getBoundingClientRect();
    return {
        cx: (e.clientX - rect.left) * (canvas.width / rect.width),
        cy: (e.clientY - rect.top) * (canvas.height / rect.height)
    };
}

function getBadgeAt(cx, cy) {
    for (let i = badges.length - 1; i >= 0; i--) {
        const b = badges[i];
        let bw, bh;
        if (b.type === 'background_panel') {
            bw = b.width  || 180;
            bh = b.height || 58;
        } else if (b.type === 'designed_badge') {
            bw = b.width  || 180;
            bh = b.height || 36;
        } else if (b.type === 'smart_badge') {
            bw = b._previewW || 130;
            bh = b._previewH || 48;
        } else {
            // For auto-sized (width=0) badges, use the last computed canvas size
            const bgW = b.background?.enabled ? b.background.width  : 0;
            const bgH = b.background?.enabled ? b.background.height : 0;
            bw = bgW || b._autoW || 100;
            bh = bgH || b._autoH || 40;
        }
        if (cx >= b.x && cx <= b.x + bw && cy >= b.y && cy <= b.y + bh) return b;
    }
    return null;
}

// ═══════════════════════════════════════════════════════════
//  BADGE MANAGEMENT
// ═══════════════════════════════════════════════════════════

function addBadgeFromPalette(type) {
    const yOffset = 20 + (badges.length * 70);
    addBadge(type, 20, Math.min(yOffset, 800));
}

function addBadge(type, x, y) {
    if (type === 'background_panel') { addBackgroundPanel(x, y); return; }
    if (type === 'designed_badge') { addDesignedBadge(x, y); return; }
    if (type === 'title_logo') { addTitleLogo(x, y); return; }
    // Route smart badge palette items
    if (SMART_BADGE_SLUGS[type]) {
        addSmartBadge(type, x, y);
        return;
    }
    const preset = BADGE_PRESETS[type];
    if (!preset) return;
    const badge = {
        id: Date.now() + Math.random(),
        type, label: preset.label, x, y,
        condition: preset.condition,
        background: { ...preset.background },
        icon: { ...preset.icon },
        text: { ...preset.text },
        ratingFormat: 'auto',
    };
    badges.push(badge);
    updateBadgesList();
    selectBadge(badge.id);
    renderCanvas();
}

function addSmartBadge(paletteKey, x, y) {
    const def = SMART_BADGE_SLUGS[paletteKey];
    const badge = {
        id: Date.now() + Math.random(),
        type: 'smart_badge',
        badge_type: def.badge_type,
        label: def.label,
        x, y,
        height: null,   // null = native PNG size
        opacity: 1.0,
        styleOverlay: {
            enabled: false,
            bgType: 'solid', bgColor: '#000000', bgColor2: '#000000', bgAngle: 135, bgOpacity: 0.8,
            borderColor: '#ffffff', borderWidth: 1, borderOpacity: 0.08, borderRadius: 8,
            highlightOpacity: 0.09, padding: 8,
        },
        // Kept so hit-detection & canvas sizing work without special-casing everywhere
        _previewW: def.previewW,
        _previewH: def.previewH,
    };
    badges.push(badge);
    updateBadgesList();
    selectBadge(badge.id);
    renderCanvas();
}

function addDesignedBadge(x, y) {
    const badge = designedBadgeDefaults({
        id: Date.now() + Math.random(),
        x: x ?? 20, y: y ?? 20,
    });
    badge.id = Date.now() + Math.random();
    badges.push(badge);
    updateBadgesList();
    selectBadge(badge.id);
    loadGoogleFont(badge.leftFont);
    loadGoogleFont(badge.rightFont1);
    loadGoogleFont(badge.rightFont2);
}

// Scrim effect type toggle
function tlScrimSetMode(mode) {
    const badge = selectedBadgeId != null ? badges.find(b => b.id === selectedBadgeId) : null;
    if (badge && badge.type === 'title_logo') {
        badge.scrimMode = mode;
        _tlScrimSyncModeButtons(mode);
        renderCanvas();
    }
}

function _tlScrimSyncModeButtons(mode) {
    const gBtn  = document.getElementById('tl-scrim-mode-gradient');
    const bBtn  = document.getElementById('tl-scrim-mode-blur');
    const gCtrl = document.getElementById('tl-scrim-gradient-controls');
    const bCtrl = document.getElementById('tl-scrim-blur-controls');
    const active   = 'background:#e07b39;color:#1a1a1a;font-weight:600;';
    const inactive = 'background:#2a2a2a;color:#aaa;font-weight:normal;';
    if (gBtn) gBtn.style.cssText += mode === 'gradient' ? active : inactive;
    if (bBtn) bBtn.style.cssText += mode === 'blur'     ? active : inactive;
    if (gCtrl) gCtrl.style.display = mode === 'gradient' ? '' : 'none';
    if (bCtrl) bCtrl.style.display = mode === 'blur'     ? '' : 'none';
}

// Called by mode toggle buttons in the title logo properties panel
function tlSetMode(mode) {
    const badge = selectedBadgeId != null ? badges.find(b => b.id === selectedBadgeId) : null;
    if (badge && badge.type === 'title_logo') {
        badge.positionMode = mode;
        populateTitleLogoProperties(badge);
        renderCanvas();
    }
}

function addTitleLogo(x, y) {
    const badge = {
        id: Date.now() + Math.random(),
        type: 'title_logo',
        label: 'Title Logo',
        // Default to anchor mode — the organic/dynamic approach
        positionMode: 'anchor',
        // Anchor mode fields
        anchorX: 'center',
        anchorY: 85,        // % from top
        maxWidthPct: 60,    // max % of poster width
        maxHeightPct: 12,   // max % of poster height
        // Pixel mode fields (kept for when user switches mode)
        x: x ?? 20,
        y: y ?? 750,
        width: 300,
        height: 80,
        opacity: 1.0,
        previewFallback: false,
        fontSize: 'auto',
        font: 'DejaVuSans-Bold',
        fontWeight: 'bold',
        color: '#FFFFFFDD',
        borderWidth: 0,
        borderColor: '#000000',
        // Drop shadow
        shadowEnabled: false,
        shadowBlur: 8,
        shadowOpacity: 0.6,
        shadowOffsetX: 0,
        shadowOffsetY: 3,
        shadowColor: '#000000',
        // Poster scrim / blur
        scrimEnabled: false,
        scrimMode: 'gradient',   // 'gradient' | 'blur'
        scrimDirection: 'bottom',
        scrimStart: 55,
        scrimEnd: 100,
        scrimColor: '#000000',
        scrimOpacity: 0.85,
        scrimBlurRadius: 20,
        // Background pill
        pillEnabled: false,
        pillPadding: 12,
        pillRadius: 10,
        pillColor: '#000000',
        pillOpacity: 0.8,
    };
    badges.push(badge);
    updateBadgesList();
    selectBadge(badge.id);
    renderCanvas();
}

function selectBadge(id) {
    selectedBadgeId = id;
    document.querySelectorAll('.element-list-item').forEach(item => {
        item.classList.toggle('selected', item.dataset.badgeId == id);
    });
    const panel      = document.getElementById('badge-properties');
    const sbPanel    = document.getElementById('smart-badge-properties');
    const dbPanel    = document.getElementById('designed-badge-properties');
    const panPanel   = document.getElementById('panel-properties');
    const tlPanel    = document.getElementById('title-logo-properties');
    const noMsg      = document.getElementById('no-selection-message');
    if (id === null) {
        panel.style.display   = 'none';
        sbPanel.style.display = 'none';
        dbPanel.style.display = 'none';
        panPanel.style.display = 'none';
        if (tlPanel) tlPanel.style.display = 'none';
        noMsg.style.display   = 'block';
        renderCanvas();
        return;
    }
    const badge = badges.find(b => b.id === id);
    if (!badge) return;
    noMsg.style.display = 'none';
    panel.style.display    = 'none';
    sbPanel.style.display  = 'none';
    dbPanel.style.display  = 'none';
    panPanel.style.display = 'none';
    if (tlPanel) tlPanel.style.display = 'none';
    if (badge.type === 'background_panel') {
        panPanel.style.display = 'block';
        populatePanelProperties(badge);
    } else if (badge.type === 'smart_badge') {
        sbPanel.style.display = 'block';
        populateSmartBadgeProperties(badge);
    } else if (badge.type === 'designed_badge') {
        dbPanel.style.display = 'block';
        populateDesignedBadgeProperties(badge);
    } else if (badge.type === 'title_logo') {
        if (tlPanel) { tlPanel.style.display = 'block'; populateTitleLogoProperties(badge); }
    } else {
        panel.style.display = 'block';
        populateBadgeProperties(badge);
    }
    renderCanvas();
}

function deleteSelectedBadge() {
    if (selectedBadgeId === null) return;
    showPopup({
        type: 'confirm',
        title: 'Delete Badge',
        message: 'Delete this badge?',
        confirmText: 'Confirm',
        cancelText: 'Cancel',
        onConfirm: function() {
            badges = badges.filter(b => b.id !== selectedBadgeId);
            selectedBadgeId = null;
            document.getElementById('badge-properties').style.display = 'none';
            document.getElementById('smart-badge-properties').style.display = 'none';
            document.getElementById('designed-badge-properties').style.display = 'none';
            document.getElementById('panel-properties').style.display = 'none';
            const _tlp = document.getElementById('title-logo-properties');
            if (_tlp) _tlp.style.display = 'none';
            document.getElementById('no-selection-message').style.display = 'block';
            updateBadgesList();
            renderCanvas();
        }
    });
}

function duplicateSelectedBadge() {
    if (selectedBadgeId === null) return;
    const original = badges.find(b => b.id === selectedBadgeId);
    if (!original) return;
    const copy = JSON.parse(JSON.stringify(original));
    copy.id = Date.now() + Math.random();
    copy.x = original.x + 20;
    copy.y = original.y + 20;
    badges.push(copy);
    updateBadgesList();
    selectBadge(copy.id);
    renderCanvas();
}

function deleteBadgeById(id) {
    showPopup({
        type: 'confirm',
        title: 'Delete Badge',
        message: 'Delete this badge?',
        confirmText: 'Confirm',
        cancelText: 'Cancel',
        onConfirm: function() {
            badges = badges.filter(b => b.id !== id);
            if (selectedBadgeId === id) {
                selectedBadgeId = null;
                document.getElementById('badge-properties').style.display = 'none';
                document.getElementById('no-selection-message').style.display = 'block';
            }
            updateBadgesList();
            renderCanvas();
        }
    });
}

let _dragSrcIdx = null;

function updateBadgesList() {
    const list = document.getElementById('badges-list');
    if (badges.length === 0) {
        list.innerHTML = '<li class="empty-list-msg">No badges yet.<br>Click or drag from above.</li>';
        return;
    }
    list.innerHTML = '';
    badges.forEach((badge, idx) => {
        const item = document.createElement('li');
        item.className = 'element-list-item';
        item.dataset.badgeId = badge.id;
        item.dataset.badgeIdx = idx;
        item.draggable = true;
        if (badge.id === selectedBadgeId) item.classList.add('selected');

        const _listLabel = (badge.type === 'designed_badge')
            ? _dbPresetLabel(badge.badgePreset)
            : (badge.type === 'background_panel' ? 'Tray' : badge.label);

        const pos = badge.type === 'title_logo'
            ? `anchor: ${badge.anchorX ?? 'center'}, ${badge.anchorY ?? 85}%`
            : `(${badge.x}, ${badge.y})`;

        item.innerHTML = `
            <span class="drag-handle" title="Drag to reorder">&#8942;&#8942;</span>
            <div style="flex:1;min-width:0;" onclick="selectBadge(${badge.id})">
                <div class="badge-list-label">${idx + 1}. ${_listLabel}</div>
                <div class="badge-list-type">${badge.type} — ${pos}</div>
            </div>
            <div class="element-list-item-actions">
                <button onclick="deleteBadgeById(${badge.id}); event.stopPropagation();" title="Delete">
                    <i class="fas fa-trash"></i>
                </button>
            </div>`;

        // ── Drag-to-reorder ──────────────────────────────────────────
        item.addEventListener('dragstart', (e) => {
            _dragSrcIdx = idx;
            item.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
        });
        item.addEventListener('dragend', () => {
            item.classList.remove('dragging');
            list.querySelectorAll('.element-list-item').forEach(i => i.classList.remove('drag-over'));
        });
        item.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            list.querySelectorAll('.element-list-item').forEach(i => i.classList.remove('drag-over'));
            item.classList.add('drag-over');
        });
        item.addEventListener('dragleave', () => item.classList.remove('drag-over'));
        item.addEventListener('drop', (e) => {
            e.preventDefault();
            item.classList.remove('drag-over');
            const targetIdx = parseInt(item.dataset.badgeIdx);
            if (_dragSrcIdx === null || _dragSrcIdx === targetIdx) return;
            // Reorder badges array
            const moved = badges.splice(_dragSrcIdx, 1)[0];
            badges.splice(targetIdx, 0, moved);
            _dragSrcIdx = null;
            updateBadgesList();
            renderCanvas();
        });

        list.appendChild(item);
    });
}

// ═══════════════════════════════════════════════════════════
//  PROPERTIES PANEL
// ═══════════════════════════════════════════════════════════

function populateBadgeProperties(badge) {
    document.getElementById('badge-type-chip').textContent = badge.label;
    document.getElementById('badge-x').value = badge.x;
    document.getElementById('badge-y').value = badge.y;
    const typeSelect = document.getElementById('badge-type-select');
    if (typeSelect) typeSelect.value = badge.type;

    // Background
    const bg = badge.background;
    document.getElementById('bg-enabled').checked = bg.enabled;
    document.getElementById('bg-color').value = bg.color.slice(0, 7);
    document.getElementById('bg-color-hex').value = bg.color;
    document.getElementById('bg-width').value = bg.width;
    document.getElementById('bg-height').value = bg.height;
    document.getElementById('bg-radius').value = bg.borderRadius;
    document.getElementById('bg-padding').value = bg.padding ?? 8;
    document.getElementById('bg-border-width').value = bg.borderWidth ?? 0;
    document.getElementById('bg-border-color').value = (bg.borderColor || '#ffffff').slice(0, 7);
    document.getElementById('bg-border-color-hex').value = (bg.borderColor || '#ffffff').slice(0, 7);
    // Opacity: derive from alpha bytes of hex (#RRGGBBAA)
    const hexAlpha = bg.color.length === 9 ? bg.color.slice(7, 9) : 'FF';
    const opacityPct = Math.round(parseInt(hexAlpha, 16) / 255 * 100);
    document.getElementById('bg-opacity').value = opacityPct;
    document.getElementById('bg-opacity-value').textContent = opacityPct + '%';
    syncSectionBody('bg-body', bg.enabled);

    // Icon
    const icon = badge.icon;
    document.getElementById('icon-enabled').checked = icon.enabled;
    document.getElementById('icon-type').value = icon.type || 'image';
    document.getElementById('icon-path').value = icon.path || '';
    document.getElementById('icon-width').value = icon.width;
    document.getElementById('icon-height').value = icon.height;
    document.getElementById('icon-side').value = icon.side || 'left';
    syncSectionBody('icon-body', icon.enabled);

    // Text
    const text = badge.text;
    document.getElementById('text-enabled').checked = text.enabled;
    syncSectionBody('text-body', text.enabled);

    // Vertical stack toggle + gap
    const _stackOn = !!(text.stackEnabled);
    document.getElementById('text-stack-enabled').checked = _stackOn;
    document.getElementById('text-stack-gap').value = text.stackGap ?? 4;
    const _gapRow = document.getElementById('text-stack-gap-row');
    if (_gapRow) _gapRow.style.display = _stackOn ? '' : 'none';

    // Variable selector — strip legacy bare % suffix from RT vars (e.g. {{rtCriticsScore}}%)
    const selectEl = document.getElementById('text-value-select');
    const customEl = document.getElementById('text-value-custom');
    const val = (text.value || '').replace(/(\{\{(?:rtCriticsScore|rtUserScore)\}\})%$/, '$1');
    if (val !== text.value) text.value = val; // migrate in-place
    let matched = false;
    for (const opt of selectEl.options) {
        if (opt.value === val && opt.value !== '__custom__') { matched = true; break; }
    }
    if (matched) {
        selectEl.value = val;
        customEl.style.display = 'none';
    } else {
        selectEl.value = '__custom__';
        customEl.value = val;
        customEl.style.display = 'block';
    }

    const _tf = text.font || 'DejaVuSans-Bold';
    populateFontSelects('text-font', _tf);
    if (GOOGLE_FONTS.includes(_tf)) loadGoogleFont(_tf); else _ensureFont(_tf);
    document.getElementById('text-font-weight').value = text.fontWeight || 'normal';
    document.getElementById('text-font-style').value = text.fontStyle || 'normal';
    document.getElementById('text-size').value = text.size;
    document.getElementById('text-color').value = (text.color || '#FFFFFF').slice(0, 7);
    document.getElementById('text-color-hex').value = text.color || '#FFFFFF';
    document.getElementById('text-align').value = text.align || 'left';
    document.getElementById('text-x-offset').value = text.xOffset ?? 0;
    document.getElementById('text-y-offset').value = text.yOffset ?? 0;
    document.getElementById('text-fallback').value = text.fallback || '';
    const rfEl = document.getElementById('text-rating-format');
    if (rfEl) rfEl.value = badge.ratingFormat || 'auto';
    const puEl = document.getElementById('text-percent-unit');
    if (puEl) puEl.checked = !!(badge.percentUnit);
    _updateRatingFormatVisibility();
    document.querySelectorAll('.properties-panel input[type="range"]').forEach(_syncRangeVal);

    // File Match section — only visible for file_match type
    const fmSection = document.getElementById('file-match-section');
    if (fmSection) {
        const isFileMatch = badge.type === 'file_match';
        fmSection.style.display = isFileMatch ? '' : 'none';
        if (isFileMatch) {
            const fm = badge.filenameMatch || {};
            document.getElementById('fm-search-term').value = fm.searchTerm || '';
            document.getElementById('fm-display-text').value = fm.displayText || '';
            document.getElementById('fm-use-icon').checked = !!fm.useIcon;
        }
    }
}

function populateSmartBadgeProperties(badge) {
    document.getElementById('sb-type-chip').textContent = badge.label;
    document.getElementById('sb-x').value         = badge.x;
    document.getElementById('sb-y').value         = badge.y;
    document.getElementById('sb-badge-type').value = badge.badge_type;
    document.getElementById('sb-height').value    = badge.height || '';
    document.getElementById('sb-opacity').value   = Math.round((badge.opacity ?? 1.0) * 100);

    // Style overlay
    const s = badge.styleOverlay || {};
    const styleOn = s.enabled || false;
    document.getElementById('sb-style-enabled').checked = styleOn;
    syncSectionBody('sb-style-body', styleOn);

    document.getElementById('sb-bg-type').value    = s.bgType    || 'solid';
    document.getElementById('sb-bg-color').value   = (s.bgColor  || '#000000').substring(0, 7);
    document.getElementById('sb-bg-color2').value  = (s.bgColor2 || '#000000').substring(0, 7);
    document.getElementById('sb-bg-angle').value   = s.bgAngle  ?? 135;
    const bgOpPct = Math.round((s.bgOpacity ?? 0.8) * 100);
    document.getElementById('sb-bg-opacity').value = bgOpPct;
    document.getElementById('sb-bg-opacity-val').textContent = bgOpPct + '%';
    document.getElementById('sb-bg-padding').value = s.padding ?? 8;
    document.getElementById('sb-bg-grad-row').style.display = (s.bgType === 'gradient') ? '' : 'none';

    document.getElementById('sb-border-color').value   = (s.borderColor || '#ffffff').substring(0, 7);
    document.getElementById('sb-border-width').value   = s.borderWidth  ?? 1;
    document.getElementById('sb-border-radius').value  = s.borderRadius ?? 8;
    const bOpPct = Math.round((s.borderOpacity ?? 0.08) * 100);
    document.getElementById('sb-border-opacity').value = bOpPct;
    document.getElementById('sb-border-opacity-val').textContent = bOpPct + '%';

    const hlOpPct = Math.round((s.highlightOpacity ?? 0.09) * 100);
    document.getElementById('sb-highlight-opacity').value = hlOpPct;
    document.getElementById('sb-highlight-opacity-val').textContent = hlOpPct + '%';
    document.querySelectorAll('.properties-panel input[type="range"]').forEach(_syncRangeVal);
}

function populateTitleLogoProperties(badge) {
    const g = id => document.getElementById(id);
    if (!g('tl-opacity')) return; // panel not in DOM yet

    // Mode toggle
    const mode = badge.positionMode || 'anchor';
    const anchorBtn = g('tl-mode-anchor');
    const pixelBtn  = g('tl-mode-pixel');
    const anchorDiv = g('tl-anchor-controls');
    const pixelDiv  = g('tl-pixel-controls');
    if (anchorBtn && pixelBtn) {
        const activeStyle   = 'background:#e07b39;color:#1a1a1a;font-weight:600;';
        const inactiveStyle = 'background:#2a2a2a;color:#aaa;font-weight:normal;';
        anchorBtn.style.cssText += mode === 'anchor' ? activeStyle : inactiveStyle;
        pixelBtn.style.cssText  += mode === 'pixel'  ? activeStyle : inactiveStyle;
    }
    if (anchorDiv) anchorDiv.style.display = mode === 'anchor' ? '' : 'none';
    if (pixelDiv)  pixelDiv.style.display  = mode === 'pixel'  ? '' : 'none';

    // Anchor fields
    if (g('tl-anchor-x')) g('tl-anchor-x').value = badge.anchorX ?? 'center';
    const ay = badge.anchorY ?? 85;
    if (g('tl-anchor-y')) { g('tl-anchor-y').value = ay; }
    if (g('tl-anchor-y-val')) g('tl-anchor-y-val').textContent = ay + '%';
    const mw = badge.maxWidthPct ?? 60;
    const mh = badge.maxHeightPct ?? 12;
    if (g('tl-max-width-pct'))  { g('tl-max-width-pct').value = mw; }
    if (g('tl-max-w-val'))  g('tl-max-w-val').textContent  = mw + '%';
    if (g('tl-max-height-pct')) { g('tl-max-height-pct').value = mh; }
    if (g('tl-max-h-val')) g('tl-max-h-val').textContent = mh + '%';

    // Pixel fields
    if (g('tl-x')) g('tl-x').value      = badge.x      ?? 20;
    if (g('tl-y')) g('tl-y').value      = badge.y      ?? 750;
    if (g('tl-width'))  g('tl-width').value  = badge.width  ?? 300;
    if (g('tl-height')) g('tl-height').value = badge.height ?? 80;

    const opPct = Math.round((badge.opacity ?? 1.0) * 100);
    g('tl-opacity').value = opPct;
    if (g('tl-opacity-val')) g('tl-opacity-val').textContent = opPct + '%';

    if (g('tl-preview-fallback')) g('tl-preview-fallback').checked = badge.previewFallback ?? false;

    // Font size
    const fs = badge.fontSize ?? 'auto';
    const fsSelect = g('tl-font-size');
    const fsCustomRow = g('tl-font-size-custom-row');
    const fsCustom = g('tl-font-size-custom');
    if (fsSelect) {
        const knownVals = Array.from(fsSelect.options).map(o => o.value);
        if (fs === 'auto' || knownVals.includes(String(fs))) {
            fsSelect.value = String(fs);
        } else {
            fsSelect.value = 'custom';
            if (fsCustom) fsCustom.value = fs;
        }
        if (fsCustomRow) fsCustomRow.style.display = fsSelect.value === 'custom' ? '' : 'none';
    }

    if (g('tl-font-weight')) g('tl-font-weight').value = badge.fontWeight ?? 'bold';
    const fullColor = badge.color || '#FFFFFFDD';
    if (g('tl-color'))     g('tl-color').value     = fullColor.slice(0, 7);
    if (g('tl-color-hex')) g('tl-color-hex').value = fullColor;
    if (g('tl-border-width')) g('tl-border-width').value = badge.borderWidth ?? 0;
    const bc = badge.borderColor || '#000000';
    if (g('tl-border-color'))     g('tl-border-color').value     = bc.slice(0, 7);
    if (g('tl-border-color-hex')) g('tl-border-color-hex').value = bc;

    // Scrim / blur
    if (g('tl-scrim-enabled'))    g('tl-scrim-enabled').checked   = badge.scrimEnabled    ?? false;
    const scrimMode = badge.scrimMode || 'gradient';
    _tlScrimSyncModeButtons(scrimMode);
    if (g('tl-scrim-direction'))  g('tl-scrim-direction').value   = badge.scrimDirection  ?? 'bottom';
    const ss = badge.scrimStart ?? 55;
    const se = badge.scrimEnd   ?? 100;
    const so = Math.round((badge.scrimOpacity ?? 0.85) * 100);
    const sbr = badge.scrimBlurRadius ?? 20;
    if (g('tl-scrim-start'))       { g('tl-scrim-start').value = ss; }
    if (g('tl-scrim-start-val'))   g('tl-scrim-start-val').textContent   = ss + '%';
    if (g('tl-scrim-end'))         { g('tl-scrim-end').value   = se; }
    if (g('tl-scrim-end-val'))     g('tl-scrim-end-val').textContent     = se + '%';
    if (g('tl-scrim-opacity'))     { g('tl-scrim-opacity').value = so; }
    if (g('tl-scrim-opacity-val')) g('tl-scrim-opacity-val').textContent = so + '%';
    if (g('tl-scrim-blur-radius')) { g('tl-scrim-blur-radius').value = sbr; }
    if (g('tl-scrim-blur-val'))    g('tl-scrim-blur-val').textContent    = sbr + 'px';
    const sc2 = (badge.scrimColor || '#000000').slice(0, 7);
    if (g('tl-scrim-color'))     g('tl-scrim-color').value     = sc2;
    if (g('tl-scrim-color-hex')) g('tl-scrim-color-hex').value = sc2;

    // Drop shadow
    if (g('tl-shadow-enabled')) g('tl-shadow-enabled').checked = badge.shadowEnabled ?? false;
    if (g('tl-shadow-blur'))    g('tl-shadow-blur').value       = badge.shadowBlur    ?? 8;
    const shOpPct = Math.round((badge.shadowOpacity ?? 0.6) * 100);
    if (g('tl-shadow-opacity'))     g('tl-shadow-opacity').value     = shOpPct;
    if (g('tl-shadow-opacity-val')) g('tl-shadow-opacity-val').textContent = shOpPct + '%';
    if (g('tl-shadow-offset-x')) g('tl-shadow-offset-x').value = badge.shadowOffsetX ?? 0;
    if (g('tl-shadow-offset-y')) g('tl-shadow-offset-y').value = badge.shadowOffsetY ?? 3;
    const sc = badge.shadowColor || '#000000';
    if (g('tl-shadow-color'))     g('tl-shadow-color').value     = sc.slice(0, 7);
    if (g('tl-shadow-color-hex')) g('tl-shadow-color-hex').value = sc;

    // Background pill
    if (g('tl-pill-enabled')) g('tl-pill-enabled').checked = badge.pillEnabled ?? false;
    if (g('tl-pill-padding')) g('tl-pill-padding').value   = badge.pillPadding  ?? 12;
    if (g('tl-pill-radius'))  g('tl-pill-radius').value    = badge.pillRadius   ?? 10;
    const pc = (badge.pillColor || '#000000').slice(0, 7);
    if (g('tl-pill-color'))     g('tl-pill-color').value     = pc;
    if (g('tl-pill-color-hex')) g('tl-pill-color-hex').value = pc;
    const pillOpPct = Math.round((badge.pillOpacity ?? 0.8) * 100);
    if (g('tl-pill-opacity'))     g('tl-pill-opacity').value     = pillOpPct;
    if (g('tl-pill-opacity-val')) g('tl-pill-opacity-val').textContent = pillOpPct + '%';
}

function updateSelectedTitleLogo() {
    if (selectedBadgeId === null) return;
    const badge = badges.find(b => b.id === selectedBadgeId);
    if (!badge || badge.type !== 'title_logo') return;
    const g = id => document.getElementById(id);
    if (!g('tl-opacity')) return;

    badge.positionMode = badge.positionMode || 'anchor';

    // Anchor fields
    badge.anchorX      = g('tl-anchor-x')?.value   || 'center';
    badge.anchorY      = parseInt(g('tl-anchor-y')?.value)       ?? 85;
    badge.maxWidthPct  = parseInt(g('tl-max-width-pct')?.value)  ?? 60;
    badge.maxHeightPct = parseInt(g('tl-max-height-pct')?.value) ?? 12;

    // Pixel fields
    badge.x      = parseInt(g('tl-x')?.value)      || 20;
    badge.y      = parseInt(g('tl-y')?.value)       || 750;
    badge.width  = parseInt(g('tl-width')?.value)   || 0;
    badge.height = parseInt(g('tl-height')?.value)  || 0;

    badge.opacity        = (parseInt(g('tl-opacity').value) || 100) / 100;
    badge.previewFallback = g('tl-preview-fallback')?.checked ?? false;

    const fsSelect = g('tl-font-size');
    if (fsSelect) {
        if (fsSelect.value === 'auto') badge.fontSize = 'auto';
        else if (fsSelect.value === 'custom') badge.fontSize = parseInt(g('tl-font-size-custom')?.value) || 32;
        else badge.fontSize = parseInt(fsSelect.value) || 32;
    }

    badge.fontWeight  = g('tl-font-weight')?.value || 'bold';
    badge.color       = g('tl-color-hex')?.value   || '#FFFFFFDD';
    badge.borderWidth = parseInt(g('tl-border-width')?.value) || 0;
    badge.borderColor = g('tl-border-color-hex')?.value || '#000000';

    // Scrim / blur
    badge.scrimEnabled    = g('tl-scrim-enabled')?.checked    ?? false;
    badge.scrimMode       = badge.scrimMode || 'gradient'; // set by tlScrimSetMode
    badge.scrimDirection  = g('tl-scrim-direction')?.value    || 'bottom';
    badge.scrimStart      = parseInt(g('tl-scrim-start')?.value)        ?? 55;
    badge.scrimEnd        = parseInt(g('tl-scrim-end')?.value)          ?? 100;
    badge.scrimOpacity    = (parseInt(g('tl-scrim-opacity')?.value)     || 85) / 100;
    badge.scrimBlurRadius = parseInt(g('tl-scrim-blur-radius')?.value)  || 20;
    badge.scrimColor      = (g('tl-scrim-color-hex')?.value || '#000000').slice(0, 7);

    // Drop shadow
    badge.shadowEnabled  = g('tl-shadow-enabled')?.checked ?? false;
    badge.shadowBlur     = parseInt(g('tl-shadow-blur')?.value)     || 8;
    badge.shadowOpacity  = (parseInt(g('tl-shadow-opacity')?.value) || 60) / 100;
    badge.shadowOffsetX  = parseInt(g('tl-shadow-offset-x')?.value) || 0;
    badge.shadowOffsetY  = parseInt(g('tl-shadow-offset-y')?.value) || 3;
    badge.shadowColor    = g('tl-shadow-color-hex')?.value || '#000000';

    // Background pill
    badge.pillEnabled = g('tl-pill-enabled')?.checked ?? false;
    badge.pillPadding = parseInt(g('tl-pill-padding')?.value) || 12;
    badge.pillRadius  = parseInt(g('tl-pill-radius')?.value)  || 10;
    badge.pillColor   = (g('tl-pill-color-hex')?.value || '#000000').slice(0, 7);
    badge.pillOpacity = (parseInt(g('tl-pill-opacity')?.value) || 80) / 100;

    renderCanvas();
}

function updateSelectedSmartBadge() {
    if (selectedBadgeId === null) return;
    const badge = badges.find(b => b.id === selectedBadgeId);
    if (!badge || badge.type !== 'smart_badge') return;

    badge.x          = parseInt(document.getElementById('sb-x').value) || 0;
    badge.y          = parseInt(document.getElementById('sb-y').value) || 0;
    badge.badge_type = document.getElementById('sb-badge-type').value;
    const hVal       = parseInt(document.getElementById('sb-height').value);
    badge.height     = isNaN(hVal) ? null : hVal;
    badge.opacity    = (parseInt(document.getElementById('sb-opacity').value) || 100) / 100;

    // Style overlay
    badge.styleOverlay = {
        enabled:          document.getElementById('sb-style-enabled').checked,
        bgType:           document.getElementById('sb-bg-type').value,
        bgColor:          document.getElementById('sb-bg-color').value,
        bgColor2:         document.getElementById('sb-bg-color2').value,
        bgAngle:          parseInt(document.getElementById('sb-bg-angle').value)   || 135,
        bgOpacity:        (parseInt(document.getElementById('sb-bg-opacity').value)        || 0) / 100,
        borderColor:      document.getElementById('sb-border-color').value,
        borderWidth:      parseInt(document.getElementById('sb-border-width').value)  || 1,
        borderOpacity:    (parseInt(document.getElementById('sb-border-opacity').value)    || 0) / 100,
        borderRadius:     parseInt(document.getElementById('sb-border-radius').value) || 0,
        highlightOpacity: (parseInt(document.getElementById('sb-highlight-opacity').value) || 0) / 100,
    };
    const _spp = parseInt(document.getElementById('sb-bg-padding').value, 10);
    badge.styleOverlay.padding = isNaN(_spp) ? 8 : Math.max(0, _spp);

    // Update label from badge_type
    const typeLabels = {
        audio_codec: 'Audio Codec', audio_channels: 'Audio Channels',
        audio_combo: 'Audio + Channels', hdr: 'HDR Format',
        resolution: 'Resolution', resolution_hdr: 'Resolution + HDR',
    };
    badge.label = typeLabels[badge.badge_type] || badge.badge_type;
    document.getElementById('sb-type-chip').textContent = badge.label;

    updateBadgesList();
    renderCanvas();
}

function updateSelectedBadge() {
    if (selectedBadgeId === null) return;
    const badge = badges.find(b => b.id === selectedBadgeId);
    if (!badge) return;

    badge.x = parseInt(document.getElementById('badge-x').value) || 0;
    badge.y = parseInt(document.getElementById('badge-y').value) || 0;

    badge.background.enabled = document.getElementById('bg-enabled').checked;
    badge.background.color = document.getElementById('bg-color-hex').value;
    badge.background.width  = Math.max(0, parseInt(document.getElementById('bg-width').value,  10) || 0);
    badge.background.height = Math.max(0, parseInt(document.getElementById('bg-height').value, 10) || 0);
    badge.background.borderRadius = parseInt(document.getElementById('bg-radius').value) || 0;
    const _bgp = parseInt(document.getElementById('bg-padding').value, 10);
    badge.background.padding = isNaN(_bgp) ? 8 : Math.max(0, _bgp);
    badge.background.borderWidth = Math.max(0, parseInt(document.getElementById('bg-border-width').value, 10) || 0);
    badge.background.borderColor = document.getElementById('bg-border-color-hex').value || document.getElementById('bg-border-color').value;

    badge.icon.enabled = document.getElementById('icon-enabled').checked;
    badge.icon.type = document.getElementById('icon-type').value;
    badge.icon.path = document.getElementById('icon-path').value;
    badge.icon.width  = Math.max(0, parseInt(document.getElementById('icon-width').value,  10) || 0);
    badge.icon.height = Math.max(0, parseInt(document.getElementById('icon-height').value, 10) || 0);
    badge.icon.side = document.getElementById('icon-side').value;

    badge.text.enabled = document.getElementById('text-enabled').checked;
    badge.text.stackEnabled = document.getElementById('text-stack-enabled').checked;
    badge.text.stackGap = parseInt(document.getElementById('text-stack-gap').value) || 4;
    const sel = document.getElementById('text-value-select').value;
    badge.text.value = (sel === '__custom__')
        ? document.getElementById('text-value-custom').value
        : sel;
    badge.text.font = document.getElementById('text-font').value;
    badge.text.fontWeight = document.getElementById('text-font-weight').value;
    badge.text.fontStyle = document.getElementById('text-font-style').value;
    badge.text.size = parseInt(document.getElementById('text-size').value) || 26;
    badge.text.color = document.getElementById('text-color-hex').value;
    badge.text.align = document.getElementById('text-align').value;
    badge.text.xOffset = parseInt(document.getElementById('text-x-offset').value) || 0;
    badge.text.yOffset = parseInt(document.getElementById('text-y-offset').value) || 0;
    badge.text.fallback = document.getElementById('text-fallback').value;
    const _rfEl = document.getElementById('text-rating-format');
    if (_rfEl) badge.ratingFormat = _rfEl.value;
    const _puEl = document.getElementById('text-percent-unit');
    badge.percentUnit = _puEl ? _puEl.checked : false;
    // condition is kept from preset — not user-editable

    // File Match properties
    if (badge.type === 'file_match') {
        if (!badge.filenameMatch) badge.filenameMatch = {};
        badge.filenameMatch.searchTerm  = (document.getElementById('fm-search-term')?.value || '').trim();
        badge.filenameMatch.displayText = (document.getElementById('fm-display-text')?.value || '').trim();
        badge.filenameMatch.useIcon     = !!(document.getElementById('fm-use-icon')?.checked);
    }

    updateBadgesList();
    renderCanvas();
}

function syncSectionBody(bodyId, enabled) {
    document.getElementById(bodyId).classList.toggle('section-body-disabled', !enabled);
}

// Show Rating Format row only when a rating variable is selected;
// show % unit checkbox only when format = percentage (and hide + uncheck for decimal).
const _TEXT_RATING_VARS = new Set([
    '{{imdbRating}}', '{{tmdbRating}}', '{{traktRating}}',
    '{{rtCriticsScore}}', '{{rtUserScore}}'
]);
function _updateRatingFormatVisibility() {
    const selVal  = document.getElementById('text-value-select')?.value || '';
    const isRating = _TEXT_RATING_VARS.has(selVal);
    const fmtRow  = document.getElementById('text-rating-format-row');
    const pctRow  = document.getElementById('text-percent-unit-row');
    const fmtSel  = document.getElementById('text-rating-format');
    const pctCb   = document.getElementById('text-percent-unit');
    if (fmtRow) fmtRow.style.display = isRating ? '' : 'none';
    if (!isRating) {
        if (pctRow) pctRow.style.display = 'none';
        return;
    }
    const fmt = fmtSel?.value || 'auto';
    const showPct = fmt === 'percentage';
    if (pctRow) pctRow.style.display = showPct ? 'flex' : 'none';
    if (!showPct && pctCb) pctCb.checked = false;
}

// ═══════════════════════════════════════════════════════════
//  PROPERTY BINDINGS
// ═══════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════
//  DESIGNED BADGE — properties panel population + bindings
// ═══════════════════════════════════════════════════════════

function _dbSet(id, val) {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === 'checkbox') el.checked = !!val;
    else if (el.type === 'range' || el.type === 'number') el.value = val ?? '';
    else el.value = val ?? '';
    // sync paired display spans
    const span = document.getElementById(id + '-val');
    if (span) span.textContent = el.value;
}

function populateDesignedBadgeProperties(b) {
    // Badge type preset
    _dbSet('db-preset', b.badgePreset ?? 'custom');
    _dbUpdatePresetUI(b.badgePreset ?? 'custom');
    _dbSet('db-logo-only', b.logoOnly ?? false);
    _dbSet('db-logo-variant', b.logoVariant ?? 'mono');
    _dbUpdateLogoVariantRow();
    _dbUpdatePresetVisibility();
    // Position & container
    _dbSet('db-x', b.x); _dbSet('db-y', b.y);
    const wAuto = !b.width, hAuto = !b.height;
    _dbSet('db-width-auto',  wAuto); _dbSet('db-height-auto', hAuto);
    _dbSet('db-width',  b.width  || 180); _dbSet('db-height', b.height || 58);
    const wEl = document.getElementById('db-width'),  hEl = document.getElementById('db-height');
    if (wEl) { wEl.classList.toggle('is-auto', wAuto); wEl.disabled = wAuto; }
    if (hEl) { hEl.classList.toggle('is-auto', hAuto); hEl.disabled = hAuto; }
    _dbSet('db-opacity', Math.round((b.opacity ?? 1) * 100));
    document.getElementById('db-opacity-val').textContent = Math.round((b.opacity ?? 1) * 100) + '%';
    _dbSet('db-border-radius', b.borderRadius);
    // Border
    _dbSet('db-border-enabled', b.borderEnabled);
    _dbSet('db-border-color', b.borderColor);
    _dbSet('db-border-opacity', Math.round((b.borderOpacity ?? 0.08) * 100));
    document.getElementById('db-border-opacity-val').textContent = Math.round((b.borderOpacity ?? 0.08) * 100) + '%';
    _dbSet('db-border-width', b.borderWidth);
    // Highlight
    _dbSet('db-highlight-enabled', b.highlightEnabled);
    _dbSet('db-highlight-opacity', Math.round((b.highlightOpacity ?? 0.09) * 100));
    document.getElementById('db-highlight-opacity-val').textContent = Math.round((b.highlightOpacity ?? 0.09) * 100) + '%';
    // Base background
    _dbSet('db-bg-type', b.bgType);
    _dbSet('db-bg-color', b.bgColor); _dbSet('db-bg-opacity', Math.round((b.bgOpacity ?? 0.8) * 100));
    document.getElementById('db-bg-opacity-val').textContent = Math.round((b.bgOpacity ?? 0.8) * 100) + '%';
    _dbSet('db-bg-padding', b.bgPadding ?? 0);
    _dbSet('db-bg-color2', b.bgColor2); _dbSet('db-bg-angle', b.bgGradientAngle);
    const bgGradRow = document.getElementById('db-bg-grad-row');
    if (bgGradRow) bgGradRow.style.display = b.bgType === 'gradient' ? '' : 'none';
    // Left segment
    _dbSet('db-left-enabled', b.leftEnabled);
    const leftAuto = !b.leftWidth;
    _dbSet('db-left-width-auto', leftAuto);
    _dbSet('db-left-width', b.leftWidth || 60);
    const leftWInput = document.getElementById('db-left-width');
    if (leftWInput) { leftWInput.classList.toggle('is-auto', leftAuto); leftWInput.disabled = leftAuto; }
    _dbSet('db-left-padding', b.leftPaddingH ?? 10);
    _dbSet('db-left-bg-color', b.leftBgColor);
    _dbSet('db-left-bg-opacity', Math.round((b.leftBgOpacity ?? 0) * 100));
    document.getElementById('db-left-bg-opacity-val').textContent = Math.round((b.leftBgOpacity ?? 0) * 100) + '%';
    _dbSet('db-left-text', b.leftText);
    _dbSet('db-friendly-res', b.friendlyResolution ?? false);
    _dbSet('db-res-fluid', b.resFluid ?? false);
    populateFontSelects('db-left-font', b.leftFont ?? 'Bebas Neue');
    loadGoogleFont(b.leftFont ?? 'Bebas Neue');
    _dbSet('db-left-font-size', b.leftFontSize);
    _dbSet('db-left-color', b.leftColor);
    _dbSet('db-left-opacity', Math.round((b.leftOpacity ?? 0.9) * 100));
    document.getElementById('db-left-opacity-val').textContent = Math.round((b.leftOpacity ?? 0.9) * 100) + '%';
    _dbSet('db-left-font-weight', b.leftFontWeight ?? (b.leftBold ? 'bold' : 'normal'));
    _dbSet('db-left-font-style', b.leftFontStyle ?? 'normal');
    // Divider
    _dbSet('db-div-enabled', b.dividerEnabled);
    _dbSet('db-div-color', b.dividerColor);
    _dbSet('db-div-opacity', Math.round((b.dividerOpacity ?? 0.07) * 100));
    document.getElementById('db-div-opacity-val').textContent = Math.round((b.dividerOpacity ?? 0.07) * 100) + '%';
    // Right segment
    _dbSet('db-right-enabled', b.rightEnabled);
    _dbSet('db-right-bg-type', b.rightBgType);
    _dbSet('db-right-bg-color', b.rightBgColor);
    _dbSet('db-right-bg-color2', b.rightBgColor2);
    _dbSet('db-right-bg-opacity', Math.round((b.rightBgOpacity ?? 0.15) * 100));
    document.getElementById('db-right-bg-opacity-val').textContent = Math.round((b.rightBgOpacity ?? 0.15) * 100) + '%';
    _dbSet('db-right-bg-angle', b.rightBgGradientAngle);
    _dbSet('db-right-padding', b.rightPaddingH ?? 8);
    _dbSet('db-rating-format', b.ratingFormat ?? 'auto');
    // Status color coding
    const scm = b.statusColorMap || {};
    _dbSet('db-use-status-colors', b.useStatusColors ?? false);
    _dbSet('db-sc-returning', scm.Returning ?? '#1a7f37');
    _dbSet('db-sc-airing',    scm.Airing    ?? '#0969da');
    _dbSet('db-sc-ended',     scm.Ended     ?? '#57606a');
    _dbSet('db-sc-canceled',  scm.Canceled  ?? '#cf222e');
    { const el = document.getElementById('db-status-color-map');
      if (el) el.style.display = (b.useStatusColors ?? false) ? '' : 'none'; }
    _dbSet('db-auto-layout', b.autoLayout ?? true);
    _dbSet('db-vertical-stack', b.verticalStack ?? false);
    _dbSet('db-right-layout', b.rightLayout);
    const stackedRow = document.getElementById('db-right-text2-row');
    if (stackedRow) stackedRow.style.display = b.rightLayout === 'stacked' ? '' : 'none';
    // Right text 1
    _dbSet('db-right-text1', b.rightText1);
    populateFontSelects('db-right-font1', b.rightFont1 ?? 'Barlow Condensed');
    loadGoogleFont(b.rightFont1 ?? 'Barlow Condensed');
    _dbSet('db-right-font-size1', b.rightFontSize1);
    _dbSet('db-right-color1', b.rightColor1);
    _dbSet('db-right-opacity1', Math.round((b.rightOpacity1 ?? 0.92) * 100));
    document.getElementById('db-right-opacity1-val').textContent = Math.round((b.rightOpacity1 ?? 0.92) * 100) + '%';
    _dbSet('db-right-font-weight1', b.rightFontWeight1 ?? (b.rightBold1 ? 'bold' : 'normal'));
    _dbSet('db-right-font-style1', b.rightFontStyle1 ?? 'normal');
    // Right text 2
    _dbSet('db-right-text2', b.rightText2);
    populateFontSelects('db-right-font2', b.rightFont2 ?? 'Barlow Condensed');
    loadGoogleFont(b.rightFont2 ?? 'Barlow Condensed');
    _dbSet('db-right-font-size2', b.rightFontSize2);
    _dbSet('db-right-color2', b.rightColor2);
    _dbSet('db-right-opacity2', Math.round((b.rightOpacity2 ?? 0.92) * 100));
    document.getElementById('db-right-opacity2-val').textContent = Math.round((b.rightOpacity2 ?? 0.92) * 100) + '%';
    _dbSet('db-right-font-weight2', b.rightFontWeight2 ?? (b.rightBold2 ? 'bold' : 'normal'));
    _dbSet('db-right-font-style2', b.rightFontStyle2 ?? 'normal');
    // Channels corner (audio_combo)
    populateFontSelects('db-ch-font', b.chFont ?? 'Barlow Condensed');
    loadGoogleFont(b.chFont ?? 'Barlow Condensed');
    _dbSet('db-ch-font-size', b.chFontSize ?? 9);
    _dbSet('db-ch-font-weight', b.chFontWeight ?? 'normal');
    _dbSet('db-ch-font-style', b.chFontStyle ?? 'normal');
    _dbSet('db-ch-color', b.chColor ?? '#ffffff');
    _dbSet('db-ch-opacity', Math.round((b.chOpacity ?? 0.28) * 100));
    const chOpSpan = document.getElementById('db-ch-opacity-val');
    if (chOpSpan) chOpSpan.textContent = Math.round((b.chOpacity ?? 0.28) * 100) + '%';
    _dbSet('db-ch-position', b.chPosition ?? 'top-right');
    // Audio-specific panel fields
    const audioWAuto = !b.width;
    _dbSet('db-audio-width-auto', audioWAuto);
    _dbSet('db-audio-width', b.width || 120);
    const audioWEl = document.getElementById('db-audio-width');
    if (audioWEl) { audioWEl.classList.toggle('is-auto', audioWAuto); audioWEl.disabled = audioWAuto; }
    _dbSet('db-audio-height',            b.height || 36);
    _dbSet('db-audio-radius',            b.borderRadius ?? 8);
    _dbSet('db-audio-align',             b.audioAlign ?? 'center');
    _dbSet('db-audio-bg-opacity',        Math.round((b.bgOpacity ?? 0.73) * 100));
    const audioBgSpan = document.getElementById('db-audio-bg-opacity-val');
    if (audioBgSpan) audioBgSpan.textContent = Math.round((b.bgOpacity ?? 0.73) * 100) + '%';
    const audioFluid = b.audioFluid ?? true;
    _dbSet('db-audio-fluid',             audioFluid);
    _dbSet('db-audio-padding',           b.audioPad ?? 8);
    _dbSet('db-audio-left-pct',          b.audioLeftPct ?? 45);
    _dbSet('db-audio-brand-size',        b.leftFontSize ?? 14);
    _dbSet('db-audio-variant-size',      b.rightFontSize1 ?? 15);
    populateFontSelects('db-audio-font', b.audioFont ?? 'Barlow Condensed');
    loadGoogleFont(b.audioFont ?? 'Barlow Condensed');
    _dbSet('db-audio-font-weight',       b.audioFontWeight ?? 'bold');
    _dbSet('db-audio-font-style',        b.audioFontStyle ?? 'normal');
    const fluidCtrl  = document.getElementById('db-audio-fluid-controls');
    const manualCtrl = document.getElementById('db-audio-manual-controls');
    if (fluidCtrl)  fluidCtrl.style.display  = audioFluid ? '' : 'none';
    if (manualCtrl) manualCtrl.style.display = audioFluid ? 'none' : '';
    _dbSet('db-audio-vertical-stack',    b.verticalStack ?? false);
    _dbSet('db-audio-border-enabled',    b.borderEnabled ?? true);
    _dbSet('db-audio-border-color',      b.borderColor ?? '#ffffff');
    _dbSet('db-audio-border-width',      b.borderWidth ?? 1);
    _dbSet('db-audio-border-opacity',    Math.round((b.borderOpacity ?? 0.08) * 100));
    const audioBrSpan = document.getElementById('db-audio-border-opacity-val');
    if (audioBrSpan) audioBrSpan.textContent = Math.round((b.borderOpacity ?? 0.08) * 100) + '%';
    _dbSet('db-audio-highlight-enabled', b.highlightEnabled ?? true);
    _dbSet('db-audio-highlight-opacity', Math.round((b.highlightOpacity ?? 0.09) * 100));
    const audioHlSpan = document.getElementById('db-audio-highlight-opacity-val');
    if (audioHlSpan) audioHlSpan.textContent = Math.round((b.highlightOpacity ?? 0.09) * 100) + '%';
    document.querySelectorAll('.properties-panel input[type="range"]').forEach(_syncRangeVal);
}

const _DB_PRESET_LABELS = {
    custom:          'Custom Badge',
    resolution_hdr:  'Resolution + HDR',
    resolution:      'Resolution',
    hdr:             'HDR Format',
    audio_codec:     'Audio Codec',
    audio_channels:  'Audio Channels',
    audio_combo:     'Audio Codec + Channels',
    audio_stacked:   'Audio Stacked',
    video_codec:     'Video Codec',
    format:          'Format / Source',
    imdb:            'IMDb Rating',
    tmdb:            'TMDb Rating',
    trakt:           'Trakt Rating',
    rt_critics:      'RT Critics',
    rt_audience:     'RT Audience',
    network:         'Network',
    studio:          'Studio',
    content_rating:  'Content Rating',
    status:          'Show Status',
    versions:        'Versions',
};

function _dbPresetLabel(preset) {
    return _DB_PRESET_LABELS[preset] || 'Designed Badge';
}

function _readDB(id, defaultVal) {
    const el = document.getElementById(id);
    if (!el) return defaultVal;
    if (el.type === 'checkbox') return el.checked;
    if (el.type === 'number' || el.type === 'range') return parseFloat(el.value);
    return el.value;
}

function updateSelectedDesignedBadge() {
    const badge = badges.find(b => b.id === selectedBadgeId);
    if (!badge || badge.type !== 'designed_badge') return;

    badge.badgePreset  = _readDB('db-preset', 'custom');
    badge.label        = _dbPresetLabel(badge.badgePreset);
    badge.logoOnly     = _readDB('db-logo-only', false);
    badge.logoVariant  = _readDB('db-logo-variant', 'mono');
    badge.autoLayout    = _readDB('db-auto-layout', true);
    badge.verticalStack = _readDB('db-vertical-stack', false);
    badge.x = parseInt(_readDB('db-x', 20));
    badge.y = parseInt(_readDB('db-y', 20));
    badge.width  = _readDB('db-width-auto',  true) ? 0 : (parseInt(_readDB('db-width',  180)) || 180);
    badge.height = _readDB('db-height-auto', true) ? 0 : (parseInt(_readDB('db-height', 58))  || 58);
    badge.opacity = _readDB('db-opacity', 100) / 100;
    badge.borderRadius = parseInt(_readDB('db-border-radius', 8));
    // Border
    badge.borderEnabled = _readDB('db-border-enabled', true);
    badge.borderColor   = _readDB('db-border-color', '#ffffff');
    badge.borderOpacity = _readDB('db-border-opacity', 8) / 100;
    badge.borderWidth   = parseInt(_readDB('db-border-width', 1));
    // Highlight
    badge.highlightEnabled = _readDB('db-highlight-enabled', true);
    badge.highlightOpacity = _readDB('db-highlight-opacity', 9) / 100;
    // Base background
    badge.bgType   = _readDB('db-bg-type', 'solid');
    badge.bgColor  = _readDB('db-bg-color', '#ffffff');
    badge.bgOpacity = _readDB('db-bg-opacity', 3) / 100;
    badge.bgPadding = Math.max(0, parseInt(_readDB('db-bg-padding', 0)) || 0);
    badge.bgColor2 = _readDB('db-bg-color2', '#ffffff');
    badge.bgGradientAngle = parseFloat(_readDB('db-bg-angle', 135));
    const bgGradRow = document.getElementById('db-bg-grad-row');
    if (bgGradRow) bgGradRow.style.display = badge.bgType === 'gradient' ? '' : 'none';
    // Left segment
    badge.leftEnabled    = _readDB('db-left-enabled', true);
    const leftAuto = _readDB('db-left-width-auto', true);
    badge.leftWidth      = leftAuto ? 0 : (parseInt(_readDB('db-left-width', 60)) || 60);
    badge.leftPaddingH   = Math.max(0, parseInt(_readDB('db-left-padding', 10)) || 0);
    badge.leftBgColor    = _readDB('db-left-bg-color', '#000000');
    badge.leftBgOpacity  = _readDB('db-left-bg-opacity', 0) / 100;
    badge.leftText          = _readDB('db-left-text', '');
    badge.friendlyResolution = _readDB('db-friendly-res', false);
    badge.resFluid           = _readDB('db-res-fluid', false);
    badge.leftFont          = _readDB('db-left-font', 'Bebas Neue');
    badge.leftFontSize   = parseInt(_readDB('db-left-font-size', 18));
    badge.leftColor      = _readDB('db-left-color', '#ffffff');
    badge.leftOpacity    = _readDB('db-left-opacity', 90) / 100;
    badge.leftFontWeight = _readDB('db-left-font-weight', 'normal');
    badge.leftFontStyle  = _readDB('db-left-font-style', 'normal');
    // Divider
    badge.dividerEnabled = _readDB('db-div-enabled', true);
    badge.dividerColor   = _readDB('db-div-color', '#ffffff');
    badge.dividerOpacity = _readDB('db-div-opacity', 7) / 100;
    // Right segment
    badge.rightEnabled       = _readDB('db-right-enabled', true);
    badge.rightBgType        = _readDB('db-right-bg-type', 'gradient');
    badge.rightBgColor       = _readDB('db-right-bg-color', '#7838ff');
    badge.rightBgColor2      = _readDB('db-right-bg-color2', '#ff6e14');
    badge.rightBgOpacity     = _readDB('db-right-bg-opacity', 15) / 100;
    badge.rightBgGradientAngle = parseFloat(_readDB('db-right-bg-angle', 135));
    badge.rightPaddingH      = Math.max(0, parseInt(_readDB('db-right-padding', 8)) || 0);
    badge.ratingFormat       = _readDB('db-rating-format', 'auto');
    // Status color coding
    badge.useStatusColors = _readDB('db-use-status-colors', false);
    badge.statusColorMap  = {
        Returning: _readDB('db-sc-returning', '#1a7f37'),
        Airing:    _readDB('db-sc-airing',    '#0969da'),
        Ended:     _readDB('db-sc-ended',      '#57606a'),
        Canceled:  _readDB('db-sc-canceled',   '#cf222e'),
    };
    badge.rightLayout        = _readDB('db-right-layout', 'stacked');
    const stackedRow = document.getElementById('db-right-text2-row');
    if (stackedRow) stackedRow.style.display = badge.rightLayout === 'stacked' ? '' : 'none';
    badge.rightText1    = _readDB('db-right-text1', '');
    badge.rightFont1    = _readDB('db-right-font1', 'Barlow Condensed');
    badge.rightFontSize1 = parseInt(_readDB('db-right-font-size1', 30));
    badge.rightColor1      = _readDB('db-right-color1', '#e0e0e0');
    badge.rightOpacity1    = _readDB('db-right-opacity1', 80) / 100;
    badge.rightFontWeight1 = _readDB('db-right-font-weight1', 'bold');
    badge.rightFontStyle1  = _readDB('db-right-font-style1', 'normal');
    badge.rightText2    = _readDB('db-right-text2', '');
    badge.rightFont2    = _readDB('db-right-font2', 'Barlow Condensed');
    badge.rightFontSize2 = parseInt(_readDB('db-right-font-size2', 28));
    badge.rightColor2      = _readDB('db-right-color2', '#e0e0e0');
    badge.rightOpacity2    = _readDB('db-right-opacity2', 60) / 100;
    badge.rightFontWeight2 = _readDB('db-right-font-weight2', 'bold');
    badge.rightFontStyle2  = _readDB('db-right-font-style2', 'normal');
    // Channels corner (audio_combo)
    badge.chFont       = _readDB('db-ch-font', 'Barlow Condensed');
    badge.chFontSize   = parseInt(_readDB('db-ch-font-size', 9));
    badge.chFontWeight = _readDB('db-ch-font-weight', 'normal');
    badge.chFontStyle  = _readDB('db-ch-font-style', 'normal');
    badge.chColor      = _readDB('db-ch-color', '#ffffff');
    badge.chOpacity  = _readDB('db-ch-opacity', 28) / 100;
    badge.chPosition = _readDB('db-ch-position', 'top-right');
    // Audio-specific panel — read from dedicated fields when audio preset
    const isAudioPreset = ['audio_codec', 'audio_combo', 'audio_channels'].includes(badge.badgePreset);
    if (isAudioPreset) {
        badge.height           = parseInt(_readDB('db-audio-height', 36)) || 36;
        badge.borderRadius     = parseInt(_readDB('db-audio-radius', 8));
        badge.audioAlign       = _readDB('db-audio-align', 'center');
        badge.bgOpacity        = _readDB('db-audio-bg-opacity', 73) / 100;
        badge.audioFluid       = _readDB('db-audio-fluid', true);
        badge.audioPad         = parseInt(_readDB('db-audio-padding', 8));
        badge.audioLeftPct     = parseInt(_readDB('db-audio-left-pct', 45));
        badge.leftFontSize     = parseInt(_readDB('db-audio-brand-size', 14));
        badge.rightFontSize1   = parseInt(_readDB('db-audio-variant-size', 15));
        badge.audioFont        = _readDB('db-audio-font', 'Barlow Condensed');
        badge.audioFontWeight  = _readDB('db-audio-font-weight', 'bold');
        badge.audioFontStyle   = _readDB('db-audio-font-style', 'normal');
        badge.verticalStack    = _readDB('db-audio-vertical-stack', false);
        // Toggle fluid/manual controls visibility
        const _fluidCtrl  = document.getElementById('db-audio-fluid-controls');
        const _manualCtrl = document.getElementById('db-audio-manual-controls');
        if (_fluidCtrl)  _fluidCtrl.style.display  = badge.audioFluid ? '' : 'none';
        if (_manualCtrl) _manualCtrl.style.display = badge.audioFluid ? 'none' : '';
        badge.borderEnabled    = _readDB('db-audio-border-enabled', true);
        badge.borderColor      = _readDB('db-audio-border-color', '#ffffff');
        badge.borderWidth      = parseInt(_readDB('db-audio-border-width', 1));
        badge.borderOpacity    = _readDB('db-audio-border-opacity', 8) / 100;
        badge.highlightEnabled = _readDB('db-audio-highlight-enabled', true);
        badge.highlightOpacity = _readDB('db-audio-highlight-opacity', 9) / 100;
        const audioWidthAuto   = _readDB('db-audio-width-auto', true);
        badge.width            = audioWidthAuto ? 0 : (parseInt(_readDB('db-audio-width', 120)) || 120);
        const audioWInput = document.getElementById('db-audio-width');
        if (audioWInput) { audioWInput.classList.toggle('is-auto', audioWidthAuto); audioWInput.disabled = audioWidthAuto; }
    }

    loadGoogleFont(badge.leftFont);
    loadGoogleFont(badge.rightFont1);
    loadGoogleFont(badge.rightFont2);
    loadGoogleFont(badge.chFont);
    loadGoogleFont(badge.audioFont ?? 'Barlow Condensed');
    renderCanvas();
}

function setupDesignedBadgeBindings() {
    const panel = document.getElementById('designed-badge-properties');
    if (!panel) return;

    // Opacity sliders → display spans
    const sliders = [
        ['db-opacity',                'db-opacity-val',                '%', 1],
        ['db-border-opacity',         'db-border-opacity-val',         '%', 1],
        ['db-highlight-opacity',      'db-highlight-opacity-val',      '%', 1],
        ['db-bg-opacity',             'db-bg-opacity-val',             '%', 1],
        ['db-left-bg-opacity',        'db-left-bg-opacity-val',        '%', 1],
        ['db-left-opacity',           'db-left-opacity-val',           '%', 1],
        ['db-div-opacity',            'db-div-opacity-val',            '%', 1],
        ['db-right-bg-opacity',       'db-right-bg-opacity-val',       '%', 1],
        ['db-right-opacity1',         'db-right-opacity1-val',         '%', 1],
        ['db-right-opacity2',         'db-right-opacity2-val',         '%', 1],
        ['db-ch-opacity',             'db-ch-opacity-val',             '%', 1],
        // Audio-specific sliders
        ['db-audio-bg-opacity',       'db-audio-bg-opacity-val',       '%', 1],
        ['db-audio-border-opacity',   'db-audio-border-opacity-val',   '%', 1],
        ['db-audio-highlight-opacity','db-audio-highlight-opacity-val','%', 1],
    ];
    sliders.forEach(([sliderId, spanId]) => {
        const slider = document.getElementById(sliderId);
        const span   = document.getElementById(spanId);
        if (slider && span) {
            slider.addEventListener('input', () => {
                span.textContent = slider.value + '%';
                updateSelectedDesignedBadge();
            });
        }
    });

    // Preset dropdown — apply preset first, then sync badge data
    const presetEl = document.getElementById('db-preset');
    if (presetEl) {
        presetEl.addEventListener('change', (e) => {
            applyDbPreset(e.target.value);
            _dbUpdatePresetVisibility();
            updateSelectedDesignedBadge();
        });
    }

    // Width / height / left-width / audio-width Auto toggles
    [['db-width-auto',       'db-width'],
     ['db-height-auto',      'db-height'],
     ['db-left-width-auto',  'db-left-width'],
     ['db-audio-width-auto', 'db-audio-width'],
    ].forEach(([cbId, inputId]) => {
        const cb  = document.getElementById(cbId);
        const inp = document.getElementById(inputId);
        if (cb && inp) {
            cb.addEventListener('change', () => {
                inp.classList.toggle('is-auto', cb.checked);
                inp.disabled = cb.checked;
                updateSelectedDesignedBadge();
            });
        }
    });

    // Audio fluid toggle — show/hide fluid vs manual controls
    const fluidCb = document.getElementById('db-audio-fluid');
    if (fluidCb) {
        fluidCb.addEventListener('change', () => {
            const on = fluidCb.checked;
            const fc = document.getElementById('db-audio-fluid-controls');
            const mc = document.getElementById('db-audio-manual-controls');
            if (fc) fc.style.display  = on ? '' : 'none';
            if (mc) mc.style.display = on ? 'none' : '';
            updateSelectedDesignedBadge();
        });
    }

    // Logo-only checkbox — show/hide logo variant selector
    const logoOnlyCb = document.getElementById('db-logo-only');
    if (logoOnlyCb) {
        logoOnlyCb.addEventListener('change', () => {
            _dbUpdateLogoVariantRow();
            updateSelectedDesignedBadge();
        });
    }

    // Status color coding toggle — show/hide color pickers
    const statusColorCb = document.getElementById('db-use-status-colors');
    if (statusColorCb) {
        statusColorCb.addEventListener('change', () => {
            const el = document.getElementById('db-status-color-map');
            if (el) el.style.display = statusColorCb.checked ? '' : 'none';
            updateSelectedDesignedBadge();
        });
    }

    // All other inputs / selects / checkboxes (preset handled above, skip it)
    panel.querySelectorAll('input:not([type=range]), select').forEach(el => {
        if (el.id === 'db-preset') return; // already wired above
        if (el.id === 'db-audio-fluid') return; // already wired above
        if (el.id === 'db-use-status-colors') return; // already wired above
        if (el.id === 'db-logo-only') return; // already wired above
        el.addEventListener('input',  updateSelectedDesignedBadge);
        el.addEventListener('change', updateSelectedDesignedBadge);
    });

    // Load Google Font when any designed badge font select changes
    ['db-left-font', 'db-right-font1', 'db-right-font2', 'db-ch-font', 'db-audio-font'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('change', () => loadGoogleFont(el.value));
    });
}

function setupPropertyBindings() {
    // Badge type dropdown — swap preset while preserving position
    const badgeTypeSelect = document.getElementById('badge-type-select');
    if (badgeTypeSelect) {
        badgeTypeSelect.addEventListener('change', (e) => {
            if (selectedBadgeId === null) return;
            const badge = badges.find(b => b.id === selectedBadgeId);
            if (!badge) return;
            const newType = e.target.value;
            const preset = BADGE_PRESETS[newType];
            if (!preset) return;
            // Preserve position, replace everything else from preset
            const { x, y } = badge;
            Object.assign(badge, {
                type: newType,
                label: preset.label,
                condition: preset.condition,
                background: { ...preset.background },
                icon: { ...preset.icon },
                text: { ...preset.text },
                x, y,
            });
            // Include filenameMatch for file_match type
            if (newType === 'file_match') {
                badge.filenameMatch = preset.filenameMatch ? { ...preset.filenameMatch } : { searchTerm: '', displayText: '', useIcon: false };
            } else {
                delete badge.filenameMatch;
            }
            updateBadgesList();
            populateBadgeProperties(badge);
            renderCanvas();
        });
    }

    // Simple inputs / selects that trigger updateSelectedBadge
    const directIds = [
        'badge-x', 'badge-y',
        'bg-width', 'bg-height', 'bg-radius', 'bg-padding',
        'icon-type', 'icon-path', 'icon-width', 'icon-height', 'icon-side',
        'text-font', 'text-font-weight', 'text-font-style', 'text-size', 'text-align', 'text-x-offset', 'text-y-offset', 'text-fallback',
        'text-value-custom', 'text-stack-gap'
    ];
    directIds.forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('input', updateSelectedBadge);
        if (el.tagName === 'SELECT') el.addEventListener('change', updateSelectedBadge);
    });

    // Load Google Font when text badge font select changes
    const textFontEl = document.getElementById('text-font');
    if (textFontEl) {
        textFontEl.addEventListener('change', () => loadGoogleFont(textFontEl.value));
    }

    // Variable selector: show/hide custom input + rating format visibility
    document.getElementById('text-value-select').addEventListener('change', (e) => {
        const isCustom = e.target.value === '__custom__';
        document.getElementById('text-value-custom').style.display = isCustom ? 'block' : 'none';
        _updateRatingFormatVisibility();
        updateSelectedBadge();
    });

    // Rating format: show/hide percent unit checkbox
    document.getElementById('text-rating-format').addEventListener('change', () => {
        _updateRatingFormatVisibility();
        updateSelectedBadge();
    });

    // Percent unit checkbox
    document.getElementById('text-percent-unit').addEventListener('change', updateSelectedBadge);

    // File Match inputs
    ['fm-search-term', 'fm-display-text'].forEach(id => {
        document.getElementById(id)?.addEventListener('input', updateSelectedBadge);
    });
    document.getElementById('fm-use-icon')?.addEventListener('change', updateSelectedBadge);

    // Enable checkboxes
    const sectionMap = { 'bg-enabled': 'bg-body', 'icon-enabled': 'icon-body', 'text-enabled': 'text-body' };
    Object.entries(sectionMap).forEach(([cbId, bodyId]) => {
        document.getElementById(cbId).addEventListener('change', (e) => {
            syncSectionBody(bodyId, e.target.checked);
            updateSelectedBadge();
        });
    });

    // Background opacity slider
    document.getElementById('bg-opacity').addEventListener('input', (e) => {
        const alpha = Math.round(parseInt(e.target.value) / 100 * 255)
            .toString(16).padStart(2, '0').toUpperCase();
        const hexEl = document.getElementById('bg-color-hex');
        hexEl.value = hexEl.value.slice(0, 7) + alpha;
        document.getElementById('bg-color').value = hexEl.value.slice(0, 7);
        document.getElementById('bg-opacity-value').textContent = e.target.value + '%';
        updateSelectedBadge();
    });

    // Background color picker
    document.getElementById('bg-color').addEventListener('input', (e) => {
        const hexEl = document.getElementById('bg-color-hex');
        const alpha = hexEl.value.length === 9 ? hexEl.value.slice(7) : 'CC';
        hexEl.value = e.target.value + alpha;
        updateSelectedBadge();
    });
    document.getElementById('bg-color-hex').addEventListener('input', (e) => {
        const v = e.target.value;
        if (v.length >= 7) document.getElementById('bg-color').value = v.slice(0, 7);
        if (v.length === 9) {
            const alpha = parseInt(v.slice(7, 9), 16);
            const pct = Math.round(alpha / 255 * 100);
            document.getElementById('bg-opacity').value = pct;
            document.getElementById('bg-opacity-value').textContent = pct + '%';
        }
        updateSelectedBadge();
    });

    // Border color picker
    document.getElementById('bg-border-color').addEventListener('input', (e) => {
        document.getElementById('bg-border-color-hex').value = e.target.value;
        updateSelectedBadge();
    });
    document.getElementById('bg-border-color-hex').addEventListener('input', (e) => {
        if (e.target.value.length >= 7) document.getElementById('bg-border-color').value = e.target.value.slice(0, 7);
        updateSelectedBadge();
    });

    // Text color picker
    document.getElementById('text-color').addEventListener('input', (e) => {
        const hexEl = document.getElementById('text-color-hex');
        const alpha = hexEl.value.length === 9 ? hexEl.value.slice(7) : '';
        hexEl.value = e.target.value + alpha;
        updateSelectedBadge();
    });
    document.getElementById('text-color-hex').addEventListener('input', (e) => {
        if (e.target.value.length >= 7) document.getElementById('text-color').value = e.target.value.slice(0, 7);
        updateSelectedBadge();
    });

    // Vertical stack toggle — show/hide gap field
    document.getElementById('text-stack-enabled').addEventListener('change', (e) => {
        const gapRow = document.getElementById('text-stack-gap-row');
        if (gapRow) gapRow.style.display = e.target.checked ? '' : 'none';
        updateSelectedBadge();
    });

    // Title Logo color pickers (color swatch ↔ hex text, same pattern as text-color)
    const tlColorEl    = document.getElementById('tl-color');
    const tlColorHexEl = document.getElementById('tl-color-hex');
    const tlBorderEl    = document.getElementById('tl-border-color');
    const tlBorderHexEl = document.getElementById('tl-border-color-hex');
    if (tlColorEl && tlColorHexEl) {
        tlColorEl.addEventListener('input', (e) => {
            const alpha = tlColorHexEl.value.length === 9 ? tlColorHexEl.value.slice(7) : 'DD';
            tlColorHexEl.value = e.target.value + alpha;
            updateSelectedTitleLogo();
        });
        tlColorHexEl.addEventListener('input', (e) => {
            if (e.target.value.length >= 7) tlColorEl.value = e.target.value.slice(0, 7);
            updateSelectedTitleLogo();
        });
    }
    if (tlBorderEl && tlBorderHexEl) {
        tlBorderEl.addEventListener('input', (e) => {
            tlBorderHexEl.value = e.target.value;
            updateSelectedTitleLogo();
        });
        tlBorderHexEl.addEventListener('input', (e) => {
            if (e.target.value.length >= 7) tlBorderEl.value = e.target.value.slice(0, 7);
            updateSelectedTitleLogo();
        });
    }

    // Scrim color picker
    const tlScrimEl    = document.getElementById('tl-scrim-color');
    const tlScrimHexEl = document.getElementById('tl-scrim-color-hex');
    if (tlScrimEl && tlScrimHexEl) {
        tlScrimEl.addEventListener('input', (e) => { tlScrimHexEl.value = e.target.value; updateSelectedTitleLogo(); });
        tlScrimHexEl.addEventListener('input', (e) => { if (e.target.value.length >= 7) tlScrimEl.value = e.target.value.slice(0, 7); updateSelectedTitleLogo(); });
    }

    // Shadow color picker
    const tlShadowEl    = document.getElementById('tl-shadow-color');
    const tlShadowHexEl = document.getElementById('tl-shadow-color-hex');
    if (tlShadowEl && tlShadowHexEl) {
        tlShadowEl.addEventListener('input', (e) => { tlShadowHexEl.value = e.target.value; updateSelectedTitleLogo(); });
        tlShadowHexEl.addEventListener('input', (e) => { if (e.target.value.length >= 7) tlShadowEl.value = e.target.value.slice(0, 7); updateSelectedTitleLogo(); });
    }
    // Pill color picker (RGB only — opacity via separate slider)
    const tlPillEl    = document.getElementById('tl-pill-color');
    const tlPillHexEl = document.getElementById('tl-pill-color-hex');
    if (tlPillEl && tlPillHexEl) {
        tlPillEl.addEventListener('input', (e) => { tlPillHexEl.value = e.target.value; updateSelectedTitleLogo(); });
        tlPillHexEl.addEventListener('input', (e) => { if (e.target.value.length >= 7) tlPillEl.value = e.target.value.slice(0, 7); updateSelectedTitleLogo(); });
    }

    // Smart badge property bindings
    ['sb-x', 'sb-y', 'sb-height', 'sb-opacity'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', updateSelectedSmartBadge);
    });
    const sbTypeEl = document.getElementById('sb-badge-type');
    if (sbTypeEl) sbTypeEl.addEventListener('change', updateSelectedSmartBadge);

    // Smart badge style overlay bindings
    document.getElementById('sb-style-enabled')?.addEventListener('change', (e) => {
        syncSectionBody('sb-style-body', e.target.checked);
        updateSelectedSmartBadge();
    });
    document.getElementById('sb-bg-type')?.addEventListener('change', (e) => {
        document.getElementById('sb-bg-grad-row').style.display = e.target.value === 'gradient' ? '' : 'none';
        updateSelectedSmartBadge();
    });
    ['sb-bg-color', 'sb-bg-color2', 'sb-border-color'].forEach(id => {
        document.getElementById(id)?.addEventListener('input', updateSelectedSmartBadge);
    });
    ['sb-bg-angle', 'sb-border-width', 'sb-border-radius', 'sb-bg-padding'].forEach(id => {
        document.getElementById(id)?.addEventListener('input', updateSelectedSmartBadge);
    });
    document.getElementById('sb-bg-opacity')?.addEventListener('input', (e) => {
        document.getElementById('sb-bg-opacity-val').textContent = e.target.value + '%';
        updateSelectedSmartBadge();
    });
    document.getElementById('sb-border-opacity')?.addEventListener('input', (e) => {
        document.getElementById('sb-border-opacity-val').textContent = e.target.value + '%';
        updateSelectedSmartBadge();
    });
    document.getElementById('sb-highlight-opacity')?.addEventListener('input', (e) => {
        document.getElementById('sb-highlight-opacity-val').textContent = e.target.value + '%';
        updateSelectedSmartBadge();
    });
}

// ═══════════════════════════════════════════════════════════
//  ZOOM + GRID
// ═══════════════════════════════════════════════════════════

function adjustZoom(delta) {
    canvasZoom = Math.round(Math.max(0.25, Math.min(3.0, canvasZoom + delta)) * 100) / 100;
    applyZoom();
}

function applyZoom() {
    const canvas = document.getElementById('preview-canvas');
    if (!canvas) return;
    canvas.style.width = Math.round(600 * canvasZoom) + 'px';
    canvas.style.height = 'auto';
    const label = document.getElementById('zoom-label');
    if (label) label.textContent = Math.round(canvasZoom * 100) + '%';
}

function fitZoom() {
    const container = document.querySelector('.canvas-container');
    if (!container) return;
    const availW = container.clientWidth  - 40;
    const availH = container.clientHeight - 40;
    if (availW <= 0 || availH <= 0) return;
    const fit = Math.min(availW / 600, availH / 900, 0.71);
    canvasZoom = Math.max(0.25, parseFloat(fit.toFixed(2)));
    applyZoom();
}

function toggleGrid() {
    showGrid = !showGrid;
    const btn = document.getElementById('grid-toggle-btn');
    if (btn) {
        btn.style.background = showGrid ? '#007bff' : '';
        btn.style.color      = showGrid ? '#fff' : '';
        btn.style.borderColor = showGrid ? '#0069d9' : '';
    }
    renderCanvas();
}

function drawGrid(ctx, canvas) {
    ctx.save();
    // Minor lines: vertical every 25px, horizontal every 50px
    ctx.strokeStyle = 'rgba(255,255,255,0.35)';
    ctx.lineWidth = 0.5;
    for (let x = 25; x < canvas.width; x += 25) {
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, canvas.height); ctx.stroke();
    }
    for (let y = 50; y < canvas.height; y += 50) {
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(canvas.width, y); ctx.stroke();
    }
    // Major lines: vertical every 50px, horizontal every 100px
    ctx.strokeStyle = 'rgba(255,255,255,0.6)';
    ctx.lineWidth = 1;
    for (let x = 50; x < canvas.width; x += 50) {
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, canvas.height); ctx.stroke();
    }
    for (let y = 100; y < canvas.height; y += 100) {
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(canvas.width, y); ctx.stroke();
    }
    // Accent lines: x=200 and x=400 (third-column guides) — bright white
    ctx.strokeStyle = 'rgba(255,255,255,1)';
    ctx.lineWidth = 1.5;
    for (const ax of [200, 400]) {
        ctx.beginPath(); ctx.moveTo(ax, 0); ctx.lineTo(ax, canvas.height); ctx.stroke();
    }
    // Coordinate labels: vertical every 50px, horizontal every 100px
    ctx.fillStyle = 'rgba(255,255,255,0.55)';
    ctx.font = '9px monospace';
    ctx.textBaseline = 'top';
    ctx.textAlign = 'left';
    for (let x = 50; x < canvas.width; x += 50) {
        ctx.fillText(x, x + 2, 2);
    }
    for (let y = 100; y < canvas.height; y += 100) {
        ctx.fillText(y, 2, y + 2);
    }
    ctx.restore();
}

window.adjustZoom  = adjustZoom;
window.fitZoom     = fitZoom;
window.toggleGrid  = toggleGrid;

// ═══════════════════════════════════════════════════════════
//  CANVAS RENDERING
// ═══════════════════════════════════════════════════════════

function drawBackground(ctx, canvas) {
    if (posterImage) {
        // Letterbox / fill the canvas with the loaded poster
        const iw = posterImage.naturalWidth  || posterImage.width;
        const ih = posterImage.naturalHeight || posterImage.height;
        const scale = Math.max(canvas.width / iw, canvas.height / ih);
        const dw = iw * scale;
        const dh = ih * scale;
        const dx = (canvas.width  - dw) / 2;
        const dy = (canvas.height - dh) / 2;
        ctx.drawImage(posterImage, dx, dy, dw, dh);
    } else {
        // Gradient placeholder
        const grad = ctx.createLinearGradient(0, 0, 0, canvas.height);
        grad.addColorStop(0, '#1a1a2e');
        grad.addColorStop(0.5, '#16213e');
        grad.addColorStop(1, '#0f3460');
        ctx.fillStyle = grad;
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = 'rgba(255,255,255,0.06)';
        ctx.fillRect(40, 60, canvas.width - 80, canvas.height - 200);
        ctx.fillStyle = 'rgba(255,255,255,0.8)';
        ctx.font = 'bold 34px Arial';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText('Sample Poster', canvas.width / 2, canvas.height / 2 - 16);
        ctx.font = '15px Arial';
        ctx.fillStyle = 'rgba(255,255,255,0.4)';
        ctx.fillText('Load a poster or drag badges onto the canvas', canvas.width / 2, canvas.height / 2 + 18);
        ctx.textBaseline = 'alphabetic';
    }
}

function renderCanvas() {
    const canvas = document.getElementById('preview-canvas');
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    drawBackground(ctx, canvas);
    if (showGrid) drawGrid(ctx, canvas);
    badges.forEach(badge => renderBadgeOnCanvas(ctx, badge, badge.id === selectedBadgeId));
}

function renderBadgeOnCanvas(ctx, badge, isSelected) {
    if (badge.type === 'background_panel') {
        renderPanelOnCanvas(ctx, badge, isSelected);
        return;
    }
    if (badge.type === 'designed_badge') {
        renderDesignedBadgeOnCanvas(ctx, badge, isSelected);
        return;
    }
    if (badge.type === 'smart_badge') {
        renderSmartBadgeOnCanvas(ctx, badge, isSelected);
        return;
    }
    if (badge.type === 'title_logo') {
        renderTitleLogoOnCanvas(ctx, badge, isSelected);
        return;
    }
    // For file_match: inject the display text (or search term) so the preview
    // shows how the badge will look when it matches. Style is controlled by the
    // existing Text/Value section — only the match logic is in File Match section.
    if (badge.type === 'file_match') {
        const fm = badge.filenameMatch || {};
        const previewText = fm.displayText || fm.searchTerm || 'Match';
        badge = { ...badge, text: { ...badge.text, value: previewText } };
    }
    const { x, y, background: bg, icon, text } = badge;
    const pad = bg.padding ?? 8;
    const isVertStack = !!(text.enabled && text.stackEnabled);

    // Ensure badge font is loaded; use Google Fonts CDN for Google fonts, local server for DejaVu/PIL fonts
    if (text.enabled && text.font) {
        if (GOOGLE_FONTS.includes(text.font)) loadGoogleFont(text.font);
        else _ensureFont(text.font);
    }

    // Compute dimensions — font size and icon size are independent, neither clamps the other
    const fontSize = text.enabled ? (text.size || 24) : 24;
    const explicitIconH = icon.enabled ? (icon.height || 0) : 0;

    let totalW, totalH;
    if (!bg.enabled) {
        // No background: size from actual content, not hardcoded 40
        const contentH = Math.max(fontSize, explicitIconH || (isVertStack ? 32 : 0));
        totalH = isVertStack ? Math.max(80, contentH + (icon.enabled ? (text.stackGap ?? 4) + fontSize : 0)) : contentH;
        totalW = 100;
    } else if (!bg.width || !bg.height) {
        if (!bg.height) {
            if (isVertStack) {
                const estIconH = icon.enabled ? (icon.height || 32) : 0;
                const gap = (icon.enabled && text.value) ? (text.stackGap ?? 4) : 0;
                totalH = Math.max(40, estIconH + gap + fontSize + 2 * pad);
            } else {
                // Horizontal: height = max(font, explicit icon height) + padding
                const contentH = icon.enabled && explicitIconH ? Math.max(fontSize, explicitIconH) : fontSize;
                totalH = Math.max(24, contentH + 2 * pad);
            }
        } else {
            totalH = bg.height;
        }
        if (bg.width) {
            totalW = bg.width;
        } else {
            const _tFont = _canvasFontStr(text.font || 'DejaVuSans-Bold', text.fontWeight === 'bold', fontSize, text.fontStyle === 'italic');
            if (isVertStack) {
                // Width = widest of icon or text
                const estIconW = icon.enabled ? (icon.width || (icon.height ? Math.round((icon.height || 32) * 1.5) : 48)) : 0;
                let textW = 0;
                if (text.enabled && text.value) {
                    const displayText = interpolateSample(text.value, text.fallback, badge.ratingFormat, badge.percentUnit);
                    if (displayText) {
                        ctx.save();
                        ctx.font = _tFont;
                        textW = Math.ceil(ctx.measureText(displayText).width);
                        ctx.restore();
                    }
                }
                totalW = Math.max(40, Math.max(estIconW, textW) + 2 * pad);
            } else {
                let contentW = 2 * pad;
                if (icon.enabled) {
                    const estIconW = icon.width || (icon.height ? Math.round(icon.height * 1.5) : 36);
                    contentW += estIconW + 6;
                }
                if (text.enabled && text.value) {
                    const displayText = interpolateSample(text.value, text.fallback, badge.ratingFormat, badge.percentUnit);
                    if (displayText) {
                        ctx.save();
                        ctx.font = _tFont;
                        contentW += Math.ceil(ctx.measureText(displayText).width);
                        ctx.restore();
                    }
                }
                totalW = Math.max(40, contentW);
            }
        }
        badge._autoW = totalW; badge._autoH = totalH;
    } else {
        totalW = bg.width; totalH = bg.height;
        badge._autoW = undefined; badge._autoH = undefined;
    }

    // Selection outline
    if (isSelected) {
        ctx.save();
        ctx.strokeStyle = '#4da6ff';
        ctx.lineWidth = 2;
        ctx.setLineDash([5, 4]);
        ctx.strokeRect(x - 4, y - 4, totalW + 8, totalH + 8);
        ctx.setLineDash([]);
        ctx.restore();
    }

    // Background
    if (bg.enabled) {
        ctx.save();
        ctx.fillStyle = hexToRgba(bg.color);
        drawRoundedRect(ctx, x, y, totalW, totalH, bg.borderRadius || 0);
        if (bg.borderWidth > 0) {
            ctx.strokeStyle = bg.borderColor || '#ffffff';
            ctx.lineWidth = bg.borderWidth;
            drawRoundedRectStroke(ctx, x, y, totalW, totalH, bg.borderRadius || 0);
        }
        ctx.restore();
    }

    if (isVertStack) {
        // ── Vertical stack: icon top, text bottom ────────────────────────────
        const iconImg = (icon.enabled && icon.path) ? _getOrLoadIconImage(icon.path, renderCanvas) : null;
        let iW = icon.enabled ? (icon.width || 0) : 0;
        let iH = icon.enabled ? (icon.height || 0) : 0;
        if (icon.enabled && (!iW || !iH)) {
            if (iconImg && iconImg.naturalWidth > 0) {
                const aspect = iconImg.naturalWidth / iconImg.naturalHeight;
                if (!iW && !iH) { iH = Math.min(totalH / 2 - pad, 36); iW = Math.round(iH * aspect); }
                else if (!iW)   { iW = Math.round(iH * aspect); }
                else             { iH = Math.round(iW / aspect); }
            } else {
                iW = iW || 36; iH = iH || 24;
            }
        }

        const displayText = (text.enabled && text.value)
            ? interpolateSample(text.value, text.fallback, badge.ratingFormat, badge.percentUnit) : '';
        const fontSize = text.enabled ? (text.size || 24) : 24; // not clamped by badge height

        // Calculate vertical split
        const gap = (icon.enabled && displayText) ? (text.stackGap ?? 4) : 0;
        const totalContentH = (icon.enabled ? iH : 0) + gap + (displayText ? fontSize : 0);
        let curY = y + (totalH - totalContentH) / 2;

        // Draw icon (centred horizontally)
        if (icon.enabled) {
            const iX = x + Math.round((totalW - iW) / 2);
            const iYv = Math.round(curY);
            if (iconImg) {
                ctx.save();
                ctx.drawImage(iconImg, iX, iYv, iW, iH);
                ctx.restore();
            } else {
                ctx.save();
                ctx.strokeStyle = 'rgba(200,200,200,0.6)';
                ctx.lineWidth = 1;
                ctx.setLineDash([3, 3]);
                ctx.strokeRect(iX, iYv, iW, iH);
                ctx.setLineDash([]);
                ctx.fillStyle = 'rgba(180,180,180,0.15)';
                ctx.fillRect(iX, iYv, iW, iH);
                ctx.fillStyle = 'rgba(200,200,200,0.8)';
                ctx.font = '9px Arial';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText('LOGO', iX + iW / 2, iYv + iH / 2);
                ctx.restore();
            }
            curY += iH + gap;
        }

        // Draw text (centred horizontally below icon)
        if (displayText) {
            ctx.save();
            ctx.fillStyle = hexToRgba(text.color || '#FFFFFF');
            ctx.font = _canvasFontStr(text.font || 'DejaVuSans-Bold', text.fontWeight === 'bold', fontSize, text.fontStyle === 'italic');
            const tm = ctx.measureText(displayText);
            const baseline = curY + tm.actualBoundingBoxAscent;
            ctx.textBaseline = 'alphabetic';
            ctx.textAlign = 'center';
            ctx.fillText(displayText, x + totalW / 2, baseline);
            ctx.restore();
        }

    } else {
        // ── Horizontal (normal) layout ────────────────────────────────────────
        let textXLeft = x + pad;
        let textXRight = x + totalW - pad;

        if (icon.enabled) {
            const iconImg = icon.path ? _getOrLoadIconImage(icon.path, renderCanvas) : null;
            let iW = icon.width  || 0;
            let iH = icon.height || 0;
            if (!iW || !iH) {
                if (iconImg && iconImg.naturalWidth > 0) {
                    const aspect = iconImg.naturalWidth / iconImg.naturalHeight;
                    if (!iW && !iH) { iH = Math.min(totalH - 2 * pad, 36); iW = Math.round(iH * aspect); }
                    else if (!iW)   { iW = Math.round(iH * aspect); }
                    else             { iH = Math.round(iW / aspect); }
                } else {
                    iW = iW || 36; iH = iH || 24;
                }
            }
            const iY = y + Math.round((totalH - iH) / 2);
            let iX;
            if (icon.side === 'none') {
                // Independent positioning — icon centered, no text anchor adjustment
                iX = x + Math.round((totalW - iW) / 2);
            } else if (icon.side === 'right') {
                iX = x + totalW - iW - pad;
                textXRight = iX - 6;
            } else {
                iX = x + pad;
                textXLeft = iX + iW + 6;
            }
            if (iconImg) {
                ctx.save();
                ctx.drawImage(iconImg, iX, iY, iW, iH);
                ctx.restore();
            } else {
                ctx.save();
                ctx.strokeStyle = 'rgba(200,200,200,0.6)';
                ctx.lineWidth = 1;
                ctx.setLineDash([3, 3]);
                ctx.strokeRect(iX, iY, iW, iH);
                ctx.setLineDash([]);
                ctx.fillStyle = 'rgba(180,180,180,0.15)';
                ctx.fillRect(iX, iY, iW, iH);
                ctx.fillStyle = 'rgba(200,200,200,0.8)';
                ctx.font = '9px Arial';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText('LOGO', iX + iW / 2, iY + iH / 2);
                ctx.restore();
            }
        }

        if (text.enabled && text.value) {
            const displayText = interpolateSample(text.value, text.fallback, badge.ratingFormat, badge.percentUnit);
            if (displayText) {
                const fontSize = text.size || 24; // not clamped by badge height
                const align = text.align || 'left';
                const xOff = text.xOffset || 0;
                const yOff = text.yOffset || 0;
                ctx.save();
                ctx.fillStyle = hexToRgba(text.color || '#FFFFFF');
                ctx.font = _canvasFontStr(text.font || 'DejaVuSans-Bold', text.fontWeight === 'bold', fontSize, text.fontStyle === 'italic');
                const tm = ctx.measureText(displayText);
                const baseline = y + (totalH - tm.actualBoundingBoxAscent - tm.actualBoundingBoxDescent) / 2 + tm.actualBoundingBoxAscent + yOff;
                ctx.textBaseline = 'alphabetic';
                ctx.textAlign = 'center';
                if (align === 'none') {
                    // Pure offset mode — anchor from badge center, X/Y offset is the only control
                    ctx.fillText(displayText, x + totalW / 2 + xOff, baseline);
                } else if (align === 'center') {
                    ctx.fillText(displayText, x + totalW / 2 + xOff, baseline);
                } else if (align === 'right') {
                    ctx.textAlign = 'right';
                    ctx.fillText(displayText, textXRight + xOff, baseline);
                } else {
                    ctx.textAlign = 'left';
                    ctx.fillText(displayText, textXLeft + xOff, baseline);
                }
                ctx.restore();
            }
        }
    }

    // Badge label (selected only)
    if (isSelected) {
        ctx.save();
        ctx.fillStyle = 'rgba(0,123,255,0.85)';
        ctx.font = '10px Arial';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';
        ctx.fillText(badge.label, x, y + totalH + 3);
        ctx.restore();
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────

function _hexToRgba(hex, opacity) {
    const h = hex.replace('#', '');
    const r = parseInt(h.slice(0, 2), 16);
    const g = parseInt(h.slice(2, 4), 16);
    const b = parseInt(h.slice(4, 6), 16);
    return `rgba(${r},${g},${b},${opacity})`;
}

function _gradientPts(x, y, w, h, angle) {
    const rad = (angle - 90) * Math.PI / 180;
    const cx  = x + w / 2, cy = y + h / 2;
    const len = Math.sqrt(w * w + h * h) / 2;
    return [cx - Math.cos(rad) * len, cy - Math.sin(rad) * len,
            cx + Math.cos(rad) * len, cy + Math.sin(rad) * len];
}

function _rrPath(ctx, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);   ctx.arcTo(x + w, y,     x + w, y + r,     r);
    ctx.lineTo(x + w, y + h - r); ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
    ctx.lineTo(x + r, y + h);   ctx.arcTo(x,     y + h, x,     y + h - r, r);
    ctx.lineTo(x, y + r);       ctx.arcTo(x,     y,     x + r, y,         r);
    ctx.closePath();
}

function _dbFont(bold, size, family, italic) {
    const style = italic ? 'italic ' : '';
    return `${style}${bold ? 'bold ' : ''}${size}px "${family}", Arial, sans-serif`;
}

// ── Audio codec liquid-glass badge canvas renderer ────────────────────────

// Helper: set ctx.letterSpacing if supported (Chromium/modern browsers)
function _ctxLetterSpacing(ctx, em) {
    try { ctx.letterSpacing = em; } catch(e) {}
}

// Binary-search the largest font size (px) where text fits within maxW x maxH.
// fontFn(size) → canvas font string. Pass maxH=Infinity to ignore height constraint.
function _fitFontSize(ctx, text, fontFn, maxW, maxH, minSize, maxSize) {
    let lo = minSize, hi = maxSize, best = minSize;
    while (lo <= hi) {
        const mid = Math.floor((lo + hi) / 2);
        ctx.font = fontFn(mid);
        const m = ctx.measureText(text);
        const w = m.width;
        const h = (m.actualBoundingBoxAscent ?? mid) + (m.actualBoundingBoxDescent ?? 0);
        if (w <= maxW && (maxH === Infinity || h <= maxH)) {
            best = mid; lo = mid + 1;
        } else {
            hi = mid - 1;
        }
    }
    return best;
}

// Draw the SVG-matched icon for icon-bearing codec types.
// cx/cy = center of icon area, size = reference height (badge H)
function _drawAudioCodecIcon(ctx, codecType, cx, cy, size) {
    const s = size / 104; // scale factor relative to SVG viewBox height
    ctx.save();
    ctx.lineCap = 'round';

    if (codecType === 'aac') {
        // WiFi-style arcs + dot — from audio-aac.svg
        // Arc centres at SVG (42,58) → offset relative to icon centre
        const ax = cx + (42 - 42) * s; // already centred
        const ay = cy + (58 - 52) * s; // 52 = vertical centre of SVG text area
        const sw = 2.5 * s;
        // Outer arc
        ctx.beginPath();
        ctx.arc(ax, ay, 16 * s, Math.PI, 0, false);
        ctx.strokeStyle = 'rgba(255,255,255,0.28)';
        ctx.lineWidth = sw;
        ctx.stroke();
        // Middle arc
        ctx.beginPath();
        ctx.arc(ax, ay, 11 * s, Math.PI, 0, false);
        ctx.strokeStyle = 'rgba(255,255,255,0.68)';
        ctx.lineWidth = sw;
        ctx.stroke();
        // Inner arc
        ctx.beginPath();
        ctx.arc(ax, ay, 6 * s, Math.PI, 0, false);
        ctx.strokeStyle = 'rgba(255,255,255,0.9)';
        ctx.lineWidth = sw;
        ctx.stroke();
        // Dot
        ctx.beginPath();
        ctx.arc(ax, ay + 4 * s, 3 * s, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(255,255,255,0.72)';
        ctx.fill();

    } else if (codecType === 'flac') {
        // Equalizer bars — from audio-flac.svg
        // Bar x positions from SVG: 26,32.5,39,45.5,52,58.5,65 (icon centre ~45.5)
        // Bar heights: 8,16,12,24,14,18,7
        const bars = [
            { x: 26, h: 8  },
            { x: 32.5, h: 16 },
            { x: 39, h: 12 },
            { x: 45.5, h: 24 },
            { x: 52, h: 14 },
            { x: 58.5, h: 18 },
            { x: 65, h: 7  },
        ];
        const barW = 4 * s;
        bars.forEach(bar => {
            const bx = cx + (bar.x - 45.5) * s;
            const bh = bar.h * s;
            const by = cy - bh / 2;
            ctx.beginPath();
            ctx.roundRect ? ctx.roundRect(bx, by, barW, bh, 2 * s)
                          : ctx.rect(bx, by, barW, bh);
            ctx.fillStyle = 'rgba(52,215,115,0.92)';
            ctx.fill();
        });

    } else if (codecType === 'pcm') {
        // Two triangles (up + down) — from audio-pcm.svg
        // Up triangle: points 35,36  26,48  44,48  (centre x=35, SVG centre y=52)
        const tx = cx;
        const upTop = cy + (36 - 52) * s;
        const upBot = cy + (48 - 52) * s;
        const upL   = cx + (26 - 35) * s;
        const upR   = cx + (44 - 35) * s;
        ctx.beginPath();
        ctx.moveTo(tx, upTop);
        ctx.lineTo(upL, upBot);
        ctx.lineTo(upR, upBot);
        ctx.closePath();
        ctx.fillStyle = 'rgba(255,255,255,0.65)';
        ctx.fill();
        // Down triangle: points 35,68  26,56  44,56
        const dnBot = cy + (68 - 52) * s;
        const dnTop = cy + (56 - 52) * s;
        ctx.beginPath();
        ctx.moveTo(tx, dnBot);
        ctx.lineTo(upL, dnTop);
        ctx.lineTo(upR, dnTop);
        ctx.closePath();
        ctx.fillStyle = 'rgba(255,255,255,0.3)';
        ctx.fill();

    } else if (codecType === 'mp3') {
        // Music note: ellipse + stem + curve — from audio-mp3.svg
        // Ellipse cx=33,cy=61, stem to (41,40), curve to ~(52,45)
        // Shift so centre ~= (37,52)
        const mx = cx + (33 - 37) * s;
        const my = cy + (61 - 52) * s;
        const sw = 2.5 * s;
        // Ellipse (note head)
        ctx.beginPath();
        ctx.ellipse(mx, my, 8 * s, 6 * s, 0, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(255,255,255,0.55)';
        ctx.lineWidth = sw;
        ctx.stroke();
        // Stem
        const stemX = cx + (41 - 37) * s;
        ctx.beginPath();
        ctx.moveTo(stemX, my);
        ctx.lineTo(stemX, cy + (40 - 52) * s);
        ctx.strokeStyle = 'rgba(255,255,255,0.55)';
        ctx.lineWidth = sw;
        ctx.stroke();
        // Curve at top of stem
        ctx.beginPath();
        ctx.moveTo(stemX, cy + (40 - 52) * s);
        ctx.bezierCurveTo(
            cx + (45 - 37) * s, cy + (36 - 52) * s,
            cx + (51 - 37) * s, cy + (41 - 52) * s,
            cx + (52 - 37) * s, cy + (45 - 52) * s
        );
        ctx.strokeStyle = 'rgba(255,255,255,0.3)';
        ctx.lineWidth = 2 * s;
        ctx.stroke();

    } else if (codecType === 'opus') {
        // Target/circle icon — from audio-opus.svg
        // Large circle r=14, small dot r=4, centre (40,52)
        const ox = cx;
        const oy = cy;
        // Outer ring
        ctx.beginPath();
        ctx.arc(ox, oy, 14 * s, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(255,255,255,0.42)';
        ctx.lineWidth = 3.5 * s;
        ctx.stroke();
        // Inner dot
        ctx.beginPath();
        ctx.arc(ox, oy, 4 * s, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(255,255,255,0.38)';
        ctx.fill();
    }

    ctx.restore();
}

function renderAudioCodecBadgeOnCanvas(ctx, b, isSelected) {
    const x = b.x ?? 20, y = b.y ?? 20;
    const op = b.opacity ?? 1.0;
    const R  = b.borderRadius ?? 8;

    // Detect codec from sample data
    const codecRaw = (SAMPLE_MEDIA.audioCodec || '').toLowerCase();
    // Channel count only shown for audio_combo preset
    const channels = (b.badgePreset === 'audio_combo') ? (SAMPLE_MEDIA.audioChannels || '') : '';

    // Color scheme tuples: [rightBgCss, rightTextCss]
    const CYAN   = ['rgba(0,155,255,0.11)',  'rgba(0,220,255,0.92)'];
    const ORANGE = ['rgba(255,130,20,0.14)', 'rgba(255,138,28,0.95)'];
    const GREEN  = ['rgba(45,200,90,0.09)',  'rgba(52,215,115,0.92)'];
    const BLUE   = ['rgba(50,130,255,0.09)', 'rgba(55,145,255,0.92)'];
    const PLAIN  = ['rgba(255,255,255,0.03)','rgba(255,255,255,0.9)'];
    const GREEN_FLAC = 'rgba(62,222,112,0.92)';

    // Codec type tags used for layout decisions
    let codecType = 'generic'; // 'dts-brand', 'dts-hd', 'dts-hd-ma', 'dts-hd-hra', 'dolby-label', 'dd-plus', 'truehd-atmos', 'truehd', 'flac', 'mp3', 'generic'
    let brand, variant, scheme;
    const c = codecRaw;

    // Dolby — most specific first
    if (c.includes('truehd') && c.includes('atmos'))
        { codecType = 'truehd-atmos'; brand = 'TRUEHD'; variant = 'ATMOS'; scheme = CYAN; }
    else if (c.includes('truehd'))
        { codecType = 'truehd'; brand = 'TRUEHD'; variant = ''; scheme = PLAIN; }
    else if ((c.includes('eac3') || c.includes('dd+')) && c.includes('atmos'))
        { codecType = 'dd-plus'; brand = 'DD+'; variant = 'ATMOS'; scheme = CYAN; }
    else if (c.includes('eac3') || c.includes('dd+'))
        { codecType = 'dolby-label'; brand = 'DOLBY'; variant = 'DIGITAL+'; scheme = PLAIN; }
    else if (c.includes('atmos'))
        { codecType = 'dolby-label'; brand = 'DOLBY'; variant = 'ATMOS'; scheme = CYAN; }
    else if (c.includes('ac3') || (c.includes('dolby') && c.includes('digital')) || c === 'dd')
        { codecType = 'dolby-label'; brand = 'DOLBY'; variant = 'DIGITAL'; scheme = PLAIN; }
    // DTS — most specific first
    else if (c.includes('dts-x') || c.includes('dts:x') || c.includes('dtsx'))
        { codecType = 'dts-brand'; brand = 'dts'; variant = 'X'; scheme = ORANGE; }
    else if ((c.includes('dts') && c.includes('hd') && c.includes('ma')) || c.includes('dts-hd ma') || c.includes('master audio'))
        { codecType = 'dts-hd-ma'; brand = 'dts'; variant = 'MA'; scheme = GREEN; }
    else if ((c.includes('dts') && c.includes('hra')) || c.includes('dts-hd hra') || c.includes('highres') || c.includes('high-res'))
        { codecType = 'dts-hd-hra'; brand = 'dts'; variant = 'HRA'; scheme = BLUE; }
    else if (c.includes('dts-hd') || c.includes('dts hd'))
        { codecType = 'dts-hd'; brand = 'dts'; variant = ''; scheme = PLAIN; }
    else if (c.includes('dts-es') || c.includes('dts es'))
        { codecType = 'dts-brand'; brand = 'dts'; variant = 'ES'; scheme = PLAIN; }
    else if (c.includes('dts'))
        { codecType = 'dts-brand'; brand = 'dts'; variant = ''; scheme = PLAIN; }
    // Lossless / other
    else if (c.includes('flac'))
        { codecType = 'flac'; brand = 'FLAC'; variant = ''; scheme = PLAIN; }
    else if (c.includes('pcm'))
        { codecType = 'pcm';  brand = 'PCM';  variant = ''; scheme = PLAIN; }
    else if (c.includes('aac'))
        { codecType = 'aac';  brand = 'AAC';  variant = ''; scheme = PLAIN; }
    else if (c.includes('mp3'))
        { codecType = 'mp3';  brand = 'mp3';  variant = ''; scheme = PLAIN; }
    else if (c.includes('opus'))
        { codecType = 'opus'; brand = 'OPUS'; variant = ''; scheme = PLAIN; }
    else {
        renderDesignedBadgeOnCanvas(ctx, b, isSelected);
        return;
    }

    // ── Font size: fluid (auto-fit) or manual ─────────────────────────────
    const fluidMode  = b.audioFluid ?? true;
    const pad        = fluidMode ? (b.audioPad ?? 8) : 10;
    const leftPct    = (b.audioLeftPct ?? 45) / 100;
    const chFs       = b.chFontSize ?? 9;
    const chFontFamily = b.chFont ? `"${b.chFont}", sans-serif` : '"Barlow Condensed", sans-serif';
    const chFontBold   = b.chFontWeight === 'bold';
    const chFontItalic = b.chFontStyle === 'italic';
    const _chFontStr   = () => `${chFontItalic ? 'italic ' : ''}${chFontBold ? 'bold ' : ''}${chFs}px ${chFontFamily}`;
    const chColorHex = b.chColor ?? '#ffffff';
    const chOpacity  = b.chOpacity ?? 0.28;
    const chPosition = b.chPosition ?? 'top-right';

    // We need H first to compute fluid sizes — compute it early
    const rawH = b.height || 0;
    const H = (rawH && String(rawH).toLowerCase() !== 'auto') ? rawH : Math.max(20, 36);

    let brandFs, variantFs;
    if (fluidMode) {
        // Target text pixel heights as fraction of the slot height
        // Slot heights depend on layout:
        //   - single segment: full H
        //   - two-segment horizontal: both use H
        //   - vertical stack: topH = H/2, botH = H/2
        //   - DTS-HD MA/HRA horizontal: both use H (stacked words each get ~H*0.4)
        const _noStackVariantsFluid = ['X', 'ES'];
        const isVert = !!(b.verticalStack && variant && !_noStackVariantsFluid.includes(variant));
        const slotH  = H - pad * 2;
        const isDTSHDType = (codecType === 'dts-hd-ma' || codecType === 'dts-hd-hra');
        const isVertDTSHD = isVert && isDTSHDType;

        const _bFont = (sz) => {
            const isDTS = ['dts-brand','dts-hd-ma','dts-hd-hra','dts-hd'].includes(codecType);
            const w = (isDTS || codecType === 'mp3') ? 900 : 800;
            const s = (isDTS || codecType === 'mp3') ? 'italic ' : '';
            return `${s}${w} ${sz}px "Barlow Condensed", sans-serif`;
        };
        const _vFont = (sz) => `800 ${sz}px "Barlow Condensed", sans-serif`;

        if (isDTSHDType) {
            const leftSlotH  = isVertDTSHD ? H / 2 - pad : slotH;
            const rightSlotH = isVertDTSHD ? H / 2 - pad : slotH;
            // Vertical: single-line "MASTER AUDIO" fits 72% of half-height slot
            // Horizontal: stacked — each word fits 45% of full-height slot
            const _varSample = isVertDTSHD
                ? (codecType === 'dts-hd-ma' ? 'MASTER AUDIO' : 'HIGH-RES AUDIO')
                : (codecType === 'dts-hd-ma' ? 'MASTER' : 'HIGH-RES');
            const _hdVarFrac = isVertDTSHD ? 0.72 : 0.45;
            const _hdVarMaxW = b.width ? Math.max(20, b.width * (1 - leftPct) - pad * 2) : Infinity;
            brandFs   = _fitFontSize(ctx, 'dts', _bFont, Infinity, leftSlotH * 0.72, 6, 120);
            variantFs = _fitFontSize(ctx, _varSample, _vFont, _hdVarMaxW, rightSlotH * _hdVarFrac, 6, 120);
        } else if (codecType === 'dolby-label') {
            // DOLBY sub-label (30% slot), variant uses same 0.72 fraction as all other codecs
            brandFs   = _fitFontSize(ctx, 'DOLBY', _bFont, Infinity, slotH * 0.30, 6, 120);
            const _dolbyRightMaxW = b.width ? Math.max(20, b.width * (1 - leftPct) - pad * 2) : Infinity;
            variantFs = _fitFontSize(ctx, variant || 'DIGITAL', _vFont, _dolbyRightMaxW, slotH * 0.72, 6, 120);
        } else if (codecType === 'truehd-atmos' || codecType === 'truehd') {
            const vSlotH = isVert ? H / 2 - pad : slotH;
            brandFs   = _fitFontSize(ctx, 'TRUEHD', _bFont, Infinity, vSlotH * 0.72, 6, 120);
            variantFs = _fitFontSize(ctx, 'ATMOS',  _vFont, Infinity, vSlotH * 0.72, 6, 120);
        } else if (codecType === 'dts-brand') {
            // Only use half-height slot if there's actually a variant to stack
            const vSlotH = isVert ? H / 2 - pad : slotH;
            const _dtsHasVariant = !!(variant);
            const _dtsBrandMaxW  = b.width ? Math.max(20, (_dtsHasVariant ? b.width * leftPct : b.width) - pad * 2) : Infinity;
            const _dtsVarMaxW    = (b.width && _dtsHasVariant) ? Math.max(20, b.width * (1 - leftPct) - pad * 2) : Infinity;
            // dts italic uses 0.85 since italic condensed appears visually shorter; variant uses same 0.72
            brandFs   = _fitFontSize(ctx, 'dts', _bFont, _dtsBrandMaxW, vSlotH * 0.85, 6, 120);
            variantFs = _fitFontSize(ctx, variant || 'X', _vFont, _dtsVarMaxW, vSlotH * 0.72, 6, 120);
        } else if (codecType === 'dts-hd') {
            // Binary-search brandFs so the full "dts -HD" group fits within available width
            const _dtsHdAvailW = b.width ? Math.max(20, b.width - pad * 2) : Infinity;
            if (_dtsHdAvailW === Infinity) {
                brandFs = _fitFontSize(ctx, 'dts', _bFont, Infinity, slotH * 0.85, 6, 120);
            } else {
                let lo = 6, hi = 120, best = 6;
                while (lo <= hi) {
                    const mid = Math.floor((lo + hi) / 2);
                    ctx.font = _bFont(mid);
                    const dw = ctx.measureText('dts').width;
                    ctx.font = `700 ${Math.round(mid * 0.45)}px "Barlow Condensed", sans-serif`;
                    const hw = ctx.measureText('-HD').width;
                    if (dw + 2 + hw <= _dtsHdAvailW && mid <= slotH * 0.85) { best = mid; lo = mid + 1; }
                    else hi = mid - 1;
                }
                brandFs = best;
            }
            variantFs = brandFs;
        } else if (codecType === 'dd-plus') {
            const vSlotH = isVert ? H / 2 - pad : slotH;
            brandFs   = _fitFontSize(ctx, 'DD+',   _bFont, Infinity, vSlotH * 0.72, 6, 120);
            variantFs = _fitFontSize(ctx, 'ATMOS', _vFont, Infinity, vSlotH * 0.72, 6, 120);
        } else {
            // Single segment: FLAC, AAC, PCM, MP3, OPUS — fill slot height
            const _singleMaxW = b.width ? Math.max(20, b.width - pad * 2) : Infinity;
            brandFs   = _fitFontSize(ctx, brand || 'FLAC', _bFont, _singleMaxW, slotH * 0.72, 6, 120);
            variantFs = brandFs;
        }
    } else {
        brandFs   = b.leftFontSize   ?? 14;
        variantFs = b.rightFontSize1 ?? 15;
    }

    // ── Font helpers ──────────────────────────────────────────────────────
    const _audioFamily = b.audioFont ?? 'Barlow Condensed';
    const _audioUserW  = (b.audioFontWeight === 'bold') ? 700 : 400;
    const _audioItalic = b.audioFontStyle === 'italic';
    // Returns canvas font string with correct weight/style per codec type for brand text
    const brandFont = (size) => {
        const isDTSType = ['dts-brand','dts-hd-ma','dts-hd-hra','dts-hd'].includes(codecType);
        const isMp3 = codecType === 'mp3';
        // DTS/mp3 always use ultra-heavy italic (visual style requirement); others use user weight
        const w = (isDTSType || isMp3) ? 900 : (_audioUserW > 400 ? 700 : 400);
        const s = ((isDTSType || isMp3) || _audioItalic) ? 'italic ' : '';
        return `${s}${w} ${size}px "${_audioFamily}", sans-serif`;
    };
    const variantFont = (size, weight) => {
        const s = _audioItalic ? 'italic ' : '';
        return `${s}${weight ?? _audioUserW} ${size}px "${_audioFamily}", sans-serif`;
    };
    const subLabelFont = (size) => `700 ${size}px "${_audioFamily}", sans-serif`;

    // ── Measure text for auto-sizing ──────────────────────────────────────
    ctx.save();
    _ctxLetterSpacing(ctx, '0.04em');

    // For complex DTS-HD layouts, measure all parts
    let bw = 0, vw = 0, hdLabelW = 0, hdVariantW = 0;

    if (codecType === 'dts-hd' || codecType === 'dts-hd-ma' || codecType === 'dts-hd-hra') {
        ctx.font = brandFont(brandFs);
        bw = ctx.measureText('dts').width;                             // "dts" italic
        _ctxLetterSpacing(ctx, '0.03em');
        ctx.font = subLabelFont(Math.round(brandFs * 0.45));
        hdLabelW = ctx.measureText('-HD').width;                        // "-HD" small dimmed
        _ctxLetterSpacing(ctx, '0.04em');
        if (codecType !== 'dts-hd') {
            ctx.font = variantFont(variantFs);
            const v1 = codecType === 'dts-hd-ma' ? 'MASTER' : 'HIGH-RES';
            const v2 = 'AUDIO';
            hdVariantW = Math.max(ctx.measureText(v1).width, ctx.measureText(v2).width);
            vw = hdVariantW;
        }
    } else if (codecType === 'dolby-label') {
        // Small "DOLBY" sub-label + large variant
        _ctxLetterSpacing(ctx, '0.08em');
        ctx.font = subLabelFont(Math.round(brandFs * 0.5));
        bw = ctx.measureText('DOLBY').width;
        _ctxLetterSpacing(ctx, '0.04em');
        ctx.font = variantFont(variantFs);
        vw = variant ? ctx.measureText(variant).width : 0;
    } else {
        ctx.font = brandFont(brandFs);
        bw = ctx.measureText(brand).width;
        if (variant) {
            _ctxLetterSpacing(ctx, '0.04em');
            ctx.font = variantFont(variantFs);
            vw = ctx.measureText(variant).width;
        }
    }

    _ctxLetterSpacing(ctx, '0em');
    ctx.font = _chFontStr();
    const chW       = channels ? ctx.measureText(channels).width : 0;
    const chReserve = channels ? chW + 6 : 0;
    ctx.restore();

    // Vertical stack: dts-hd-ma/hra have their own vertical branch and need isVertical=true
    // Exclude only short single-word variants (X, ES) that have no meaningful vertical layout
    const _noStackVariants = ['X', 'ES'];
    const isVertical = !!(b.verticalStack && variant && !_noStackVariants.includes(variant));

    // Icon codecs — reserve left space for drawn icon
    const ICON_TYPES = ['aac', 'flac', 'pcm', 'mp3', 'opus'];
    const hasIcon = ICON_TYPES.includes(codecType);
    // Scale icon area proportionally to badge height (SVG icon area ~= 42px wide in 104px tall badge)
    const iconAreaW = hasIcon ? Math.round(H * (42 / 104)) + 4 : 0;

    // ── Width calculation ─────────────────────────────────────────────────
    let leftW, rightW, W;
    if (isVertical && (codecType === 'dts-hd-ma' || codecType === 'dts-hd-hra')) {
        // Top row: "dts" + "-HD" group width; bottom row: single-line "MASTER AUDIO"/"HIGH-RES AUDIO"
        const _vLabel = codecType === 'dts-hd-ma' ? 'MASTER AUDIO' : 'HIGH-RES AUDIO';
        ctx.save();
        _ctxLetterSpacing(ctx, '0.04em');
        ctx.font = variantFont(variantFs);
        const _vLabelW = ctx.measureText(_vLabel).width;
        ctx.restore();
        const topRowW = bw + 2 + hdLabelW;
        const autoW = Math.max(20, Math.max(topRowW, _vLabelW) + pad * 2);
        W = b.width ? Math.max(autoW, b.width) : autoW;
        leftW = rightW = W;
    } else if (isVertical) {
        const autoW = Math.max(20, Math.max(bw, vw) + pad * 2);
        W = b.width ? Math.max(autoW, b.width) : autoW;
        leftW = rightW = W;
    } else if (codecType === 'dts-hd') {
        // Single segment: "dts" italic + "-HD" small in same pill
        const autoW = Math.max(20, bw + hdLabelW + pad * 2 + 4 + chReserve);
        W = b.width ? Math.max(autoW, b.width) : autoW;
        leftW = W;
        rightW = 0;
    } else if (codecType === 'dts-hd-ma' || codecType === 'dts-hd-hra') {
        leftW  = bw + hdLabelW + pad * 2 + 4;
        rightW = vw + pad * 2 + chReserve;
        const autoW = Math.max(20, leftW + 1 + rightW);
        W = b.width ? Math.max(autoW, b.width) : autoW;
    } else if (codecType === 'dolby-label') {
        leftW  = bw + pad * 2;
        rightW = variant ? (vw + pad * 2 + chReserve) : chReserve;
        const autoW = Math.max(20, leftW + (variant ? 1 + rightW : 0));
        W = b.width ? Math.max(autoW, b.width) : autoW;
    } else {
        leftW  = bw + pad * 2 + iconAreaW;
        rightW = variant ? (vw + pad * 2 + chReserve) : chReserve;
        const autoW = Math.max(20, leftW + (variant ? 1 + rightW : 0));
        W = b.width ? Math.max(autoW, b.width) : autoW;
    }

    // ── Apply Left Seg % only when badge has a fixed width set by user ────
    // Without a fixed width the badge auto-sizes to content, leftPct doesn't apply
    const _isTwoSegH = !isVertical && variant && codecType !== 'dts-hd' &&
        ['dts-hd-ma','dts-hd-hra','dolby-label','truehd-atmos','dd-plus','dts-brand'].includes(codecType);
    if (fluidMode && _isTwoSegH && b.width) {
        leftW = Math.max(10, Math.round(W * leftPct));
    }

    ctx.save();
    ctx.globalAlpha = op;

    // Clip to rounded rect
    _rrPath(ctx, x, y, W, H, R);
    ctx.clip();

    // Base dark background
    const bgOp = b.bgOpacity ?? 0.73;
    ctx.fillStyle = `rgba(0,0,0,${bgOp})`;
    ctx.fillRect(x, y, W, H);

    // FLAC: green tint overlay (matches SVG rgba(45,200,90,0.06))
    if (codecType === 'flac') {
        ctx.fillStyle = 'rgba(45,200,90,0.06)';
        ctx.fillRect(x, y, W, H);
    }

    const align = b.audioAlign ?? 'center';
    ctx.textBaseline = 'middle';

    const textX = (segX, segW) => {
        if (align === 'left')  return segX + pad;
        if (align === 'right') return segX + segW - pad;
        return segX + segW / 2;
    };
    const canvasAlign = align === 'left' ? 'left' : align === 'right' ? 'right' : 'center';

    // ── Per-codec rendering ───────────────────────────────────────────────
    if (isVertical && (codecType === 'dts-hd-ma' || codecType === 'dts-hd-hra')) {
        // ── DTS-HD MA/HRA vertical: top = "dts -HD", bottom = single-line "MASTER AUDIO" / "HIGH-RES AUDIO" ──
        const topH = Math.round(H / 2);
        const botH = H - topH;
        const smallHdSize = Math.round(brandFs * 0.45);
        const varLabel = codecType === 'dts-hd-ma' ? 'MASTER AUDIO' : 'HIGH-RES AUDIO';

        // Top half: "dts" italic + "-HD" small dimmed, centred as a group
        ctx.save();
        _ctxLetterSpacing(ctx, '0.04em');
        ctx.font = brandFont(brandFs);
        const dtsW = ctx.measureText('dts').width;
        _ctxLetterSpacing(ctx, '0.03em');
        ctx.font = subLabelFont(smallHdSize);
        const hdW = ctx.measureText('-HD').width;
        ctx.restore();
        const groupW = dtsW + 2 + hdW;
        const groupX = x + (W - groupW) / 2;
        _ctxLetterSpacing(ctx, '0.04em');
        ctx.font      = brandFont(brandFs);
        ctx.fillStyle = 'rgba(255,255,255,0.9)';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText('dts', groupX, y + topH / 2);
        ctx.save();
        _ctxLetterSpacing(ctx, '0.03em');
        ctx.font      = subLabelFont(smallHdSize);
        ctx.fillStyle = 'rgba(255,255,255,0.38)';
        ctx.textBaseline = 'middle';
        ctx.fillText('-HD', groupX + dtsW + 2, y + topH * 0.65);
        ctx.restore();

        // Divider
        ctx.fillStyle = 'rgba(255,255,255,0.07)';
        ctx.fillRect(x, y + topH, W, 1);
        // Bottom tint
        ctx.fillStyle = scheme[0];
        ctx.fillRect(x, y + topH + 1, W, botH - 1);

        // Bottom half: single-line "MASTER AUDIO" / "HIGH-RES AUDIO"
        _ctxLetterSpacing(ctx, '0.04em');
        ctx.font = variantFont(variantFs);
        ctx.fillStyle = scheme[1];
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(varLabel, x + W / 2, y + topH + botH / 2);
        _ctxLetterSpacing(ctx, '0em');

    } else if (isVertical) {
        // Vertical stack: brand top half, variant bottom half (all other codecs)
        const topH = Math.round(H / 2);
        const botH = H - topH;
        const isDTSType = ['dts-brand'].includes(codecType);
        const brandLS   = (codecType === 'truehd' || codecType === 'truehd-atmos') ? '0.03em' : '0.04em';
        const variantLS = (codecType === 'truehd-atmos') ? '0.176em' : '0.04em';
        const variantW  = isDTSType ? 900 : 800;

        _ctxLetterSpacing(ctx, brandLS);
        ctx.font      = brandFont(brandFs);
        ctx.fillStyle = 'rgba(255,255,255,0.9)';
        ctx.textAlign = canvasAlign;
        ctx.fillText(brand, textX(x, W), y + topH / 2);

        ctx.fillStyle = 'rgba(255,255,255,0.07)';
        ctx.fillRect(x, y + topH, W, 1);
        ctx.fillStyle = scheme[0];
        ctx.fillRect(x, y + topH + 1, W, botH - 1);

        _ctxLetterSpacing(ctx, variantLS);
        ctx.font      = variantFont(variantFs, variantW);
        ctx.fillStyle = scheme[1];
        ctx.textAlign = canvasAlign;
        ctx.fillText(variant, textX(x, W), y + topH + botH / 2);
        _ctxLetterSpacing(ctx, '0em');

    } else if (codecType === 'dts-hd') {
        // ── DTS-HD bare: single pill — "dts" italic + "-HD" small dimmed, centred as a group ──
        const smallHdSize = Math.round(brandFs * 0.45);
        // Centre the dts + -HD group in the full badge width
        const _groupX = x + (W - (bw + 2 + hdLabelW)) / 2;
        _ctxLetterSpacing(ctx, '0.04em');
        ctx.font      = brandFont(brandFs);
        ctx.fillStyle = 'rgba(255,255,255,0.9)';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText('dts', _groupX, y + H / 2);
        ctx.save();
        _ctxLetterSpacing(ctx, '0.03em');
        ctx.font      = subLabelFont(smallHdSize);
        ctx.fillStyle = 'rgba(255,255,255,0.38)';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText('-HD', _groupX + bw + 2, y + H * 0.62);
        ctx.restore();
        _ctxLetterSpacing(ctx, '0em');

    } else if (codecType === 'dts-hd-ma' || codecType === 'dts-hd-hra') {
        // ── DTS-HD MA / HRA horizontal layout ────────────────────────────
        // Left: "dts -HD" group centred in left segment, 2px from divider
        // Right: stacked MASTER/AUDIO or HIGH-RES/AUDIO, centred in right segment, 2px from divider
        const smallHdSize = Math.round(brandFs * 0.45);
        const v1 = codecType === 'dts-hd-ma' ? 'MASTER' : 'HIGH-RES';
        const v2 = 'AUDIO';
        const varColor1 = scheme[1].replace(/[\d.]+\)$/, '0.65)');
        const varColor2 = scheme[1];

        // Divider
        ctx.fillStyle = 'rgba(255,255,255,0.07)';
        ctx.fillRect(x + leftW, y, 1, H);
        // Right tint
        ctx.fillStyle = scheme[0];
        ctx.fillRect(x + leftW + 1, y, W - leftW - 1, H);

        // Left: "dts -HD" group right-anchored 2px from divider
        const _groupW = bw + 2 + hdLabelW;
        const _groupX = x + leftW - 4 - _groupW;
        _ctxLetterSpacing(ctx, '0.04em');
        ctx.font      = brandFont(brandFs);
        ctx.fillStyle = 'rgba(255,255,255,0.9)';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText('dts', _groupX, y + H / 2);
        ctx.save();
        _ctxLetterSpacing(ctx, '0.03em');
        ctx.font      = subLabelFont(smallHdSize);
        ctx.fillStyle = 'rgba(255,255,255,0.38)';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText('-HD', _groupX + bw + 2, y + H * 0.62);
        ctx.restore();

        // Right: stacked variant text, centred 2px from divider
        const actualRightW = W - leftW - 1;
        const rCenterX = x + leftW + 1 + 4 + (actualRightW - 4) / 2;
        _ctxLetterSpacing(ctx, '0.04em');
        ctx.font = variantFont(variantFs);
        const m1 = ctx.measureText(v1);
        const m2 = ctx.measureText(v2);
        const gap = 2;
        const ink1 = m1.actualBoundingBoxAscent + m1.actualBoundingBoxDescent;
        const ink2 = m2.actualBoundingBoxAscent + m2.actualBoundingBoxDescent;
        const totalInk = ink1 + gap + ink2;
        const yTop = y + (H - totalInk) / 2;
        ctx.fillStyle = varColor1;
        ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
        ctx.fillText(v1, rCenterX, yTop + m1.actualBoundingBoxAscent);
        ctx.fillStyle = varColor2;
        ctx.fillText(v2, rCenterX, yTop + ink1 + gap + m2.actualBoundingBoxAscent);
        _ctxLetterSpacing(ctx, '0em');

    } else if (codecType === 'dolby-label') {
        // ── Dolby Digital / Digital+ / Atmos layout ───────────────────────
        // Left: small "DOLBY" sub-label (dimmed, small, wide letter-spacing)
        // Right: large variant text
        const dolbySize   = Math.round(brandFs * 0.5);

        // Left: DOLBY right-anchored 2px from divider
        _ctxLetterSpacing(ctx, '0.08em');
        ctx.font      = subLabelFont(dolbySize);
        ctx.fillStyle = 'rgba(255,255,255,0.45)';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        ctx.fillText('DOLBY', x + leftW - 4, y + H / 2);

        if (variant) {
            ctx.fillStyle = 'rgba(255,255,255,0.07)';
            ctx.fillRect(x + leftW, y, 1, H);
            ctx.fillStyle = scheme[0];
            ctx.fillRect(x + leftW + 1, y, W - leftW - 1, H);

            _ctxLetterSpacing(ctx, '0.04em');
            ctx.font      = variantFont(variantFs);
            ctx.fillStyle = scheme[1];
            ctx.textAlign = 'left';
            ctx.textBaseline = 'middle';
            ctx.fillText(variant, x + leftW + 5, y + H / 2);
        }
        _ctxLetterSpacing(ctx, '0em');

    } else if (codecType === 'truehd-atmos') {
        // ── TrueHD Atmos: two segments horizontal ────────────────────────
        _ctxLetterSpacing(ctx, '0.03em');
        ctx.font      = variantFont(brandFs);
        ctx.fillStyle = 'rgba(255,255,255,0.9)';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        ctx.fillText(brand, x + leftW - 4, y + H / 2);

        if (variant) {
            ctx.fillStyle = 'rgba(255,255,255,0.07)';
            ctx.fillRect(x + leftW, y, 1, H);
            ctx.fillStyle = scheme[0];
            ctx.fillRect(x + leftW + 1, y, W - leftW - 1, H);

            _ctxLetterSpacing(ctx, '0.176em');
            ctx.font      = variantFont(variantFs);
            ctx.fillStyle = scheme[1];
            ctx.textAlign = 'left';
            ctx.fillText(variant, x + leftW + 5, y + H / 2);
        }
        _ctxLetterSpacing(ctx, '0em');

    } else if (codecType === 'truehd') {
        // ── TrueHD solo: single segment weight 800 ────────────────────────
        _ctxLetterSpacing(ctx, '0.03em');
        ctx.font      = variantFont(brandFs);
        ctx.fillStyle = 'rgba(255,255,255,0.9)';
        ctx.textAlign = canvasAlign;
        ctx.textBaseline = 'middle';
        ctx.fillText(brand, textX(x, W), y + H / 2);
        _ctxLetterSpacing(ctx, '0em');

    } else if (codecType === 'dd-plus') {
        // ── DD+ Atmos: "DD+" left (weight 800) + "ATMOS" right (cyan) ────
        _ctxLetterSpacing(ctx, '0.04em');
        ctx.font      = variantFont(brandFs);
        ctx.fillStyle = 'rgba(255,255,255,0.9)';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        ctx.fillText(brand, x + leftW - 4, y + H / 2);

        if (variant) {
            ctx.fillStyle = 'rgba(255,255,255,0.07)';
            ctx.fillRect(x + leftW, y, 1, H);
            ctx.fillStyle = scheme[0];
            ctx.fillRect(x + leftW + 1, y, W - leftW - 1, H);
            ctx.font      = variantFont(variantFs);
            ctx.fillStyle = scheme[1];
            ctx.textAlign = 'left';
            ctx.fillText(variant, x + leftW + 5, y + H / 2);
        }
        _ctxLetterSpacing(ctx, '0em');

    } else if (codecType === 'dts-brand') {
        // ── DTS / DTS-X / DTS-ES: italic 900 brand + optional colored variant ──
        _ctxLetterSpacing(ctx, '0.04em');
        ctx.font      = brandFont(brandFs);
        ctx.fillStyle = 'rgba(255,255,255,0.9)';
        ctx.textAlign = variant ? 'right' : canvasAlign;
        ctx.textBaseline = 'alphabetic';
        const _bm = ctx.measureText(brand);
        const _bInk = _bm.actualBoundingBoxAscent + _bm.actualBoundingBoxDescent;
        ctx.fillText(brand, variant ? x + leftW - 4 : textX(x, W), y + H / 2 + _bm.actualBoundingBoxAscent - _bInk / 2);

        if (variant) {
            ctx.fillStyle = 'rgba(255,255,255,0.07)';
            ctx.fillRect(x + leftW, y, 1, H);
            ctx.fillStyle = scheme[0];
            ctx.fillRect(x + leftW + 1, y, W - leftW - 1, H);
            ctx.font      = `900 ${variantFs}px "Barlow Condensed", sans-serif`;
            ctx.fillStyle = scheme[1];
            ctx.textAlign = 'left';
            ctx.textBaseline = 'alphabetic';
            const _vm = ctx.measureText(variant);
            const _vInk = _vm.actualBoundingBoxAscent + _vm.actualBoundingBoxDescent;
            ctx.fillText(variant, x + leftW + 5, y + H / 2 + _vm.actualBoundingBoxAscent - _vInk / 2);
        }
        _ctxLetterSpacing(ctx, '0em');

    } else if (codecType === 'flac') {
        // ── FLAC: green color, weight 800, equalizer icon ─────────────────
        const iconCX = x + pad + iconAreaW / 2;
        _drawAudioCodecIcon(ctx, 'flac', iconCX, y + H / 2, H);
        _ctxLetterSpacing(ctx, '0.04em');
        ctx.font      = variantFont(brandFs);
        ctx.fillStyle = GREEN_FLAC;
        ctx.textAlign = canvasAlign;
        ctx.textBaseline = 'middle';
        ctx.fillText(brand, textX(x, W), y + H / 2);
        _ctxLetterSpacing(ctx, '0em');

    } else if (codecType === 'mp3') {
        // ── MP3: italic 900, lowercase, music note icon ───────────────────
        const iconCX = x + pad + iconAreaW / 2;
        _drawAudioCodecIcon(ctx, 'mp3', iconCX, y + H / 2, H);
        _ctxLetterSpacing(ctx, '0.04em');
        ctx.font      = brandFont(brandFs);             // italic 900
        ctx.fillStyle = 'rgba(255,255,255,0.9)';
        ctx.textAlign = canvasAlign;
        ctx.textBaseline = 'middle';
        ctx.fillText(brand, textX(x, W), y + H / 2);
        _ctxLetterSpacing(ctx, '0em');

    } else if (codecType === 'aac' || codecType === 'pcm' || codecType === 'opus') {
        // ── AAC / PCM / OPUS: weight 800, each with their drawn icon ──────
        const iconCX = x + pad + iconAreaW / 2;
        _drawAudioCodecIcon(ctx, codecType, iconCX, y + H / 2, H);
        _ctxLetterSpacing(ctx, '0.04em');
        ctx.font      = variantFont(brandFs);
        ctx.fillStyle = 'rgba(255,255,255,0.9)';
        ctx.textAlign = canvasAlign;
        ctx.textBaseline = 'middle';
        ctx.fillText(brand, textX(x, W), y + H / 2);
        _ctxLetterSpacing(ctx, '0em');

    } else {
        // ── Generic fallback: weight 800 ──────────────────────────────────
        _ctxLetterSpacing(ctx, '0.04em');
        ctx.font      = variantFont(brandFs);
        ctx.fillStyle = 'rgba(255,255,255,0.9)';
        ctx.textAlign = canvasAlign;
        ctx.textBaseline = 'middle';
        ctx.fillText(brand, textX(x, W), y + H / 2);
        _ctxLetterSpacing(ctx, '0em');
    }

    // ── Channel count corner ──────────────────────────────────────────────
    if (channels) {
        _ctxLetterSpacing(ctx, '0em');
        ctx.font         = _chFontStr();
        ctx.textBaseline = 'top';
        const r = parseInt(chColorHex.slice(1,3)||'ff',16);
        const g = parseInt(chColorHex.slice(3,5)||'ff',16);
        const bv = parseInt(chColorHex.slice(5,7)||'ff',16);
        ctx.fillStyle = `rgba(${r},${g},${bv},${chOpacity})`;
        const marginX = 5, marginY = 3;
        const isRight  = chPosition.includes('right');
        const isBottom = chPosition.includes('bottom');
        if (isRight) {
            ctx.textAlign = 'right';
            ctx.fillText(channels, x + W - marginX, isBottom ? y + H - marginY - chFs : y + marginY);
        } else {
            ctx.textAlign = 'left';
            ctx.fillText(channels, x + marginX, isBottom ? y + H - marginY - chFs : y + marginY);
        }
    }

    ctx.restore();

    // Border
    if (b.borderEnabled ?? true) {
        ctx.save();
        ctx.globalAlpha = op * (b.borderOpacity ?? 0.08);
        ctx.strokeStyle = b.borderColor ?? '#ffffff';
        ctx.lineWidth   = b.borderWidth  ?? 1;
        _rrPath(ctx, x + 0.5, y + 0.5, W - 1, H - 1, Math.max(0, R - 0.5));
        ctx.stroke();
        ctx.restore();
    }

    // Top highlight
    if (b.highlightEnabled ?? true) {
        ctx.save();
        ctx.globalAlpha = op;
        const hx1 = x + W * 0.12, hx2 = x + W * 0.88;
        const hg  = ctx.createLinearGradient(hx1, y, hx2, y);
        const hlOp = b.highlightOpacity ?? 0.09;
        hg.addColorStop(0,   'rgba(255,255,255,0)');
        hg.addColorStop(0.5, `rgba(255,255,255,${hlOp})`);
        hg.addColorStop(1,   'rgba(255,255,255,0)');
        ctx.fillStyle = hg;
        ctx.fillRect(hx1, y, hx2 - hx1, 1);
        ctx.restore();
    }

    // Selection outline
    if (isSelected) {
        ctx.save();
        ctx.strokeStyle = '#4da6ff';
        ctx.lineWidth   = 1.5;
        ctx.setLineDash([4, 3]);
        ctx.strokeRect(x - 3, y - 3, W + 6, H + 6);
        ctx.setLineDash([]);
        ctx.restore();
    }

    b._previewW = W;
    b._previewH = H;
}

// ── Designed badge vertical-stack renderer ────────────────────────────────
function _renderDesignedBadgeVertical(ctx, b, isSelected,
        { x, y, op, R, lph, rph, bgPad, lTxt, t1, t2, leftOn, rightOn, divOn, layout }) {

    // ── 1. Measure text to auto-size ──────────────────────────────────────
    const lfs  = b.leftFontSize   ?? 18;
    const rfs1 = b.rightFontSize1 ?? 30;
    const rfs2 = b.rightFontSize2 ?? 28;
    const gap  = Math.max(2, b.rightStackGap ?? 4);
    const vPad = 10;  // top/bottom padding per segment

    ctx.save();

    // Measure widths to find badge W
    let measuredLW = 0, measuredRW = 0;
    if (leftOn && lTxt) {
        ctx.font = _dbFont(b.leftFontWeight === 'bold', lfs, b.leftFont ?? 'Bebas Neue', b.leftFontStyle === 'italic');
        measuredLW = Math.ceil(ctx.measureText(lTxt).width) + lph * 2;
    }
    if (rightOn && t1) {
        ctx.font = _dbFont(b.rightFontWeight1 === 'bold', rfs1, b.rightFont1 ?? 'Barlow Condensed', b.rightFontStyle1 === 'italic');
        measuredRW = Math.ceil(ctx.measureText(t1).width) + rph * 2;
        if (layout === 'stacked' && t2) {
            ctx.font = _dbFont(b.rightFontWeight2 === 'bold', rfs2, b.rightFont2 ?? 'Barlow Condensed', b.rightFontStyle2 === 'italic');
            measuredRW = Math.max(measuredRW, Math.ceil(ctx.measureText(t2).width) + rph * 2);
        }
    }
    ctx.restore();

    // Explicit W/H override or auto
    let W = b.width  || 0;
    let H = b.height || 0;

    if (!W) W = Math.max(20, measuredLW, measuredRW);

    // Each segment height
    let topH, botH;
    if (!H) {
        topH = leftOn  ? (lfs  + vPad * 2 + bgPad * 2) : 0;
        const rightH = rightOn ? (layout === 'stacked' && t2 ? rfs1 + gap + rfs2 : rfs1) : 0;
        botH = rightOn ? (rightH + vPad * 2 + bgPad * 2) : 0;
        H = (leftOn ? topH : 0) + (divOn ? 1 : 0) + (rightOn ? botH : 0);
        H = Math.max(20, H);
    } else {
        // Split H proportionally: topH = lfs portion, botH = rest
        const divH = divOn ? 1 : 0;
        topH = leftOn && rightOn ? Math.round(H * 0.45) : H;
        botH = H - (leftOn ? topH : 0) - divH;
    }
    if (!leftOn)  topH = 0;
    if (!rightOn) botH = 0;
    H = (leftOn ? topH : 0) + (divOn ? 1 : 0) + (rightOn ? botH : 0);

    ctx.save();
    ctx.globalAlpha = op;

    // ── 2. Clip & base background ─────────────────────────────────────────
    _rrPath(ctx, x, y, W, H, R);
    ctx.clip();

    if (b.bgType === 'gradient') {
        const [x1, y1, x2, y2] = _gradientPts(x, y, W, H, b.bgGradientAngle ?? 135);
        const g = ctx.createLinearGradient(x1, y1, x2, y2);
        g.addColorStop(0, _hexToRgba(b.bgColor  ?? '#ffffff', b.bgOpacity ?? 0.03));
        g.addColorStop(1, _hexToRgba(b.bgColor2 ?? '#ffffff', b.bgOpacity ?? 0.03));
        ctx.fillStyle = g;
    } else {
        ctx.fillStyle = _hexToRgba(b.bgColor ?? '#ffffff', b.bgOpacity ?? 0.03);
    }
    ctx.fillRect(x, y, W, H);

    // ── 3. Top (left) segment ─────────────────────────────────────────────
    if (leftOn) {
        const lbOp = b.leftBgOpacity ?? 0;
        if (lbOp > 0) {
            ctx.fillStyle = _hexToRgba(b.leftBgColor ?? '#000000', lbOp);
            ctx.fillRect(x, y, W, topH);
        }
        if (lTxt) {
            ctx.font      = _dbFont(b.leftFontWeight === 'bold', lfs, b.leftFont ?? 'Bebas Neue', b.leftFontStyle === 'italic');
            ctx.fillStyle = _hexToRgba(b.leftColor ?? '#ffffff', b.leftOpacity ?? 0.9);
            ctx.textAlign    = 'center';
            ctx.textBaseline = 'alphabetic';
            const lm  = ctx.measureText(lTxt);
            const lBL = y + bgPad + (topH - 2 * bgPad - lm.actualBoundingBoxAscent - lm.actualBoundingBoxDescent) / 2 + lm.actualBoundingBoxAscent;
            ctx.fillText(lTxt, x + W / 2, lBL);
        }
    }

    // ── 4. Horizontal divider ─────────────────────────────────────────────
    let divY = y + (leftOn ? topH : 0);
    if (divOn) {
        ctx.fillStyle = _hexToRgba(b.dividerColor ?? '#ffffff', b.dividerOpacity ?? 0.07);
        ctx.fillRect(x, divY, W, 1);
        divY += 1;
    }

    // ── 5. Bottom (right) segment ─────────────────────────────────────────
    if (rightOn) {
        const ry  = leftOn ? divY : y;
        const rOp = b.rightBgOpacity ?? 0.15;
        if (b.rightBgType === 'gradient') {
            const [gx1, gy1, gx2, gy2] = _gradientPts(x, ry, W, botH, b.rightBgGradientAngle ?? 135);
            const rg = ctx.createLinearGradient(gx1, gy1, gx2, gy2);
            rg.addColorStop(0, _hexToRgba(b.rightBgColor  ?? '#7838ff', rOp));
            rg.addColorStop(1, _hexToRgba(b.rightBgColor2 ?? '#ff6e14', rOp));
            ctx.fillStyle = rg;
        } else {
            ctx.fillStyle = _hexToRgba(b.rightBgColor ?? '#7838ff', rOp);
        }
        ctx.fillRect(x, ry, W, botH);

        ctx.textAlign    = 'center';
        ctx.textBaseline = 'alphabetic';
        const f1 = _dbFont(b.rightFontWeight1 === 'bold', rfs1, b.rightFont1 ?? 'Barlow Condensed', b.rightFontStyle1 === 'italic');
        const c1 = _hexToRgba(b.rightColor1 ?? '#e0e0e0', b.rightOpacity1 ?? 0.80);

        if (layout === 'stacked' && t1 && t2) {
            const f2  = _dbFont(b.rightFontWeight2 === 'bold', rfs2, b.rightFont2 ?? 'Barlow Condensed', b.rightFontStyle2 === 'italic');
            const c2  = _hexToRgba(b.rightColor2 ?? '#e0e0e0', b.rightOpacity2 ?? 0.60);
            ctx.font = f1;
            const m1   = ctx.measureText(t1);
            const ink1 = m1.actualBoundingBoxAscent + m1.actualBoundingBoxDescent;
            ctx.font = f2;
            const m2   = ctx.measureText(t2);
            const ink2 = m2.actualBoundingBoxAscent + m2.actualBoundingBoxDescent;
            const totalInk = ink1 + gap + ink2;
            const yTop = ry + bgPad + (botH - 2 * bgPad - totalInk) / 2;
            ctx.font = f1; ctx.fillStyle = c1;
            ctx.fillText(t1, x + W / 2, yTop + m1.actualBoundingBoxAscent);
            ctx.font = f2; ctx.fillStyle = c2;
            ctx.fillText(t2, x + W / 2, yTop + ink1 + gap + m2.actualBoundingBoxAscent);
        } else if (t1) {
            ctx.font = f1;
            const rm  = ctx.measureText(t1);
            const rBL = ry + bgPad + (botH - 2 * bgPad - rm.actualBoundingBoxAscent - rm.actualBoundingBoxDescent) / 2 + rm.actualBoundingBoxAscent;
            ctx.fillStyle = c1;
            ctx.fillText(t1, x + W / 2, rBL);
        }
    }

    ctx.restore();

    // ── 6. Border ─────────────────────────────────────────────────────────
    if (b.borderEnabled ?? true) {
        ctx.save();
        ctx.globalAlpha = op * (b.borderOpacity ?? 0.08);
        ctx.strokeStyle = b.borderColor ?? '#ffffff';
        ctx.lineWidth   = b.borderWidth ?? 1;
        _rrPath(ctx, x + 0.5, y + 0.5, W - 1, H - 1, Math.max(0, R - 0.5));
        ctx.stroke();
        ctx.restore();
    }

    // ── 7. Top highlight ──────────────────────────────────────────────────
    if (b.highlightEnabled ?? true) {
        ctx.save();
        ctx.globalAlpha = op;
        const hx1 = x + W * 0.12, hx2 = x + W * 0.88;
        const hg  = ctx.createLinearGradient(hx1, y, hx2, y);
        const hlOp = b.highlightOpacity ?? 0.09;
        hg.addColorStop(0,   'rgba(255,255,255,0)');
        hg.addColorStop(0.5, `rgba(255,255,255,${hlOp})`);
        hg.addColorStop(1,   'rgba(255,255,255,0)');
        ctx.fillStyle = hg;
        ctx.fillRect(hx1, y, hx2 - hx1, 1);
        ctx.restore();
    }

    // ── 8. Selection outline ──────────────────────────────────────────────
    if (isSelected) {
        ctx.save();
        ctx.strokeStyle = '#4da6ff';
        ctx.lineWidth   = 1.5;
        ctx.setLineDash([4, 3]);
        ctx.strokeRect(x - 3, y - 3, W + 6, H + 6);
        ctx.setLineDash([]);
        ctx.restore();
    }

    b._previewW = W;
    b._previewH = H;
}

// ── Designed badge canvas renderer ───────────────────────────────────────

function renderDesignedBadgeOnCanvas(ctx, b, isSelected) {
    // Audio codec/combo presets → dedicated liquid-glass renderer
    if (b.badgePreset === 'audio_codec' || b.badgePreset === 'audio_combo') {
        renderAudioCodecBadgeOnCanvas(ctx, b, isSelected);
        return;
    }

    // Logo-only mode for network / studio
    if (b.logoOnly && (b.badgePreset === 'network' || b.badgePreset === 'studio')) {
        const nameVal = interpolateSample(
            b.badgePreset === 'network' ? (b.leftText || '{{network}}') : (b.leftText || '{{studio}}')
        );
        const logoPath = _logoPathForPreset(b.badgePreset, nameVal, b.logoVariant);
        if (logoPath) {
            const logoImg = _getOrLoadIconImage(logoPath, () => renderCanvas());
            const x = b.x ?? 20, y = b.y ?? 20;
            const H = b.height || 58;
            const bgPad = b.bgPadding ?? 0;
            const R = b.borderRadius ?? 8;
            const op = b.opacity ?? 1.0;
            ctx.save();
            ctx.globalAlpha = op;
            if (logoImg) {
                // Scale logo to fit height (minus padding), then constrain to fixed width if set
                let logoH = Math.max(4, H - bgPad * 2);
                let logoW = logoImg.naturalWidth > 0
                    ? Math.round(logoImg.naturalWidth * (logoH / logoImg.naturalHeight))
                    : logoH;
                const fixedW = b.width > 0 ? b.width : 0;
                const maxLogoW = fixedW > 0 ? fixedW - bgPad * 2 : logoW;
                if (logoW > maxLogoW) {
                    logoW = Math.max(4, maxLogoW);
                    logoH = logoImg.naturalHeight > 0
                        ? Math.round(logoImg.naturalHeight * (logoW / logoImg.naturalWidth))
                        : logoH;
                }
                const W = fixedW > 0 ? fixedW : logoW + bgPad * 2;
                // Base background
                _rrPath(ctx, x, y, W, H, R);
                ctx.clip();
                ctx.fillStyle = _hexToRgba(b.bgColor ?? '#000000', b.bgOpacity ?? 0.8);
                ctx.fillRect(x, y, W, H);
                // Center logo horizontally and vertically within badge
                const logoX = x + Math.round((W - logoW) / 2);
                const logoY = y + Math.round((H - logoH) / 2);
                ctx.drawImage(logoImg, logoX, logoY, logoW, logoH);
                ctx.restore();
                // Border
                if (b.borderEnabled ?? true) {
                    ctx.save();
                    ctx.globalAlpha = op * (b.borderOpacity ?? 0.08);
                    ctx.strokeStyle = b.borderColor ?? '#ffffff';
                    ctx.lineWidth   = b.borderWidth ?? 1;
                    _rrPath(ctx, x + 0.5, y + 0.5, W - 1, H - 1, Math.max(0, R - 0.5));
                    ctx.stroke();
                    ctx.restore();
                }
                if (isSelected) {
                    ctx.save();
                    ctx.strokeStyle = '#4da6ff';
                    ctx.lineWidth   = 1.5;
                    ctx.setLineDash([4, 3]);
                    ctx.strokeRect(x - 3, y - 3, W + 6, H + 6);
                    ctx.setLineDash([]);
                    ctx.restore();
                }
                b._previewW = W;
                b._previewH = H;
            } else {
                // Logo not yet loaded — draw placeholder
                const W = b.width || 100;
                _rrPath(ctx, x, y, W, H, R);
                ctx.clip();
                ctx.fillStyle = _hexToRgba(b.bgColor ?? '#000000', b.bgOpacity ?? 0.8);
                ctx.fillRect(x, y, W, H);
                ctx.restore();
                if (isSelected) {
                    ctx.save();
                    ctx.strokeStyle = '#4da6ff';
                    ctx.lineWidth   = 1.5;
                    ctx.setLineDash([4, 3]);
                    ctx.strokeRect(x - 3, y - 3, W + 6, H + 6);
                    ctx.setLineDash([]);
                    ctx.restore();
                }
                b._previewW = W;
                b._previewH = H;
            }
            return;
        }
    }

    const x   = b.x ?? 20,  y = b.y ?? 20;
    const op  = b.opacity ?? 1.0;
    const R   = b.borderRadius ?? 8;
    const rph = b.rightPaddingH ?? 8;
    const bgPad = b.bgPadding ?? 0;

    // ── 1. Pre-resolve all texts ─────────────────────────────────────────
    const _rf = b.ratingFormat;
    let lTxt = interpolateSample(b.leftText ?? '', '', _rf);
    if (b.friendlyResolution) {
        lTxt = lTxt.replace(/\b2160[pP]\b/g, '4K').replace(/\b1440[pP]\b/g, '2K');
    }
    let t1   = interpolateSample(b.rightText1 ?? '', '', _rf);
    let t2   = interpolateSample(b.rightText2 ?? '', '', _rf);

    // Status color override — shallow-clone b so we don't mutate the badge data
    if (b.useStatusColors && b.statusColorMap) {
        const scColor = b.statusColorMap[lTxt] || b.statusColorMap[t1];
        if (scColor) {
            b = Object.assign({}, b, {
                // Apply to whole badge background (works even when right segment is hidden)
                bgType:        'solid',
                bgColor:       scColor,
                bgColor2:      scColor,
                bgOpacity:     b.bgOpacity ?? 0.8,
                // Also apply to right segment background for split-segment layouts
                rightBgType:   'solid',
                rightBgColor:  scColor,
                rightBgColor2: scColor,
                rightBgOpacity: b.rightBgOpacity ?? 0.8,
            });
        }
    }

    // ── 2. Determine effective layout ────────────────────────────────────
    const autoLayout = b.autoLayout ?? false;
    const leftOn     = b.leftEnabled ?? true;
    const lph        = b.leftPaddingH ?? 10;
    let   leftW      = b.leftWidth ?? 60;   // let — may be expanded by auto-size
    let   layout     = b.rightLayout ?? 'stacked';
    let   rightOn    = b.rightEnabled ?? true;

    if (autoLayout) {
        if      (t1 && t2) layout = 'stacked';
        else if (t1)       layout = 'single';
        else if (t2)     { layout = 'single'; t1 = t2; }
        else {
            const fallback = interpolateSample(b.rightTextFallback ?? '', '', _rf);
            if (fallback) { layout = 'single'; t1 = fallback; }
            else            rightOn = false;
        }
    }
    const divOn = (b.dividerEnabled ?? true) && leftOn && rightOn;

    // ── Vertical stack path ───────────────────────────────────────────────
    if (b.verticalStack) {
        _renderDesignedBadgeVertical(ctx, b, isSelected,
            { x, y, op, R, lph, rph, bgPad, lTxt, t1, t2, leftOn, rightOn, divOn, layout });
        return;
    }

    // ── 3. Auto-size W / H ───────────────────────────────────────────────
    let W = b.width  || 0;
    let H = b.height || 0;

    // Always measure left text to ensure it's never clipped, regardless of fixed W
    ctx.save();
    const _maxLfs = b.leftFontSize ?? 18;
    if (!H) {
        const rfs = b.rightFontSize1 ?? 30;
        H = Math.max(leftOn ? _maxLfs : 0, rightOn ? rfs : 0) + 16 + bgPad * 2;
        H = Math.max(16, H);
    }
    // Fluid font: when resFluid is on, fit both left AND right fonts to the badge height.
    // For auto-width badges: both sides expand to fit content at height-fitted font sizes.
    // For fixed-width badges: right content is measured first at its height-fitted size,
    //   then the left font is further shrunk to fit the remaining space — so neither side clips.
    let effectiveLfs  = _maxLfs;
    let effectiveRfs1 = b.rightFontSize1 ?? 30;
    let effectiveRfs2 = b.rightFontSize2 ?? 28;
    if (b.resFluid && leftOn && lTxt) {
        const slotH = H - bgPad * 2 - 8;
        const _lFontFn  = (sz) => _dbFont(b.leftFontWeight === 'bold', sz, b.leftFont ?? 'Bebas Neue', b.leftFontStyle === 'italic');
        const _r1FontFn = (sz) => _dbFont(b.rightFontWeight1 === 'bold', sz, b.rightFont1 ?? 'Barlow Condensed', b.rightFontStyle1 === 'italic');
        const _r2FontFn = (sz) => _dbFont(b.rightFontWeight2 === 'bold', sz, b.rightFont2 ?? 'Barlow Condensed', b.rightFontStyle2 === 'italic');

        // Step 1: fit right fonts to height
        if (rightOn && t1) {
            if (layout === 'stacked' && t2) {
                effectiveRfs1 = _fitFontSize(ctx, t1, _r1FontFn, Infinity, slotH * 0.45, 6, b.rightFontSize1 ?? 30);
                effectiveRfs2 = _fitFontSize(ctx, t2, _r2FontFn, Infinity, slotH * 0.45, 6, b.rightFontSize2 ?? 28);
            } else {
                effectiveRfs1 = _fitFontSize(ctx, t1, _r1FontFn, Infinity, slotH * 0.72, 6, b.rightFontSize1 ?? 30);
            }
        }

        // Step 2: determine max left width.
        //   - Auto-width badge (W=0): no width cap, badge expands to fit both sides
        //   - Fixed-width badge: left gets W minus right content width and divider
        let maxLeftW = Infinity;
        if (W > 0 && rightOn && t1) {
            ctx.font = _dbFont(b.rightFontWeight1 === 'bold', effectiveRfs1, b.rightFont1 ?? 'Barlow Condensed', b.rightFontStyle1 === 'italic');
            let rw = ctx.measureText(t1).width + rph * 2;
            if (layout === 'stacked' && t2) {
                ctx.font = _dbFont(b.rightFontWeight2 === 'bold', effectiveRfs2, b.rightFont2 ?? 'Barlow Condensed', b.rightFontStyle2 === 'italic');
                rw = Math.max(rw, ctx.measureText(t2).width + rph * 2);
            }
            maxLeftW = Math.max(10, W - rw - (divOn ? 1 : 0));
        }

        // Step 3: fit left font to height AND available left width
        effectiveLfs = _fitFontSize(ctx, lTxt, _lFontFn, maxLeftW - lph * 2, slotH * 0.72, 6, _maxLfs);
    }
    if (leftOn && lTxt) {
        ctx.font = _dbFont(b.leftFontWeight === 'bold', effectiveLfs, b.leftFont ?? 'Bebas Neue', b.leftFontStyle === 'italic');
        const measuredL = Math.ceil(ctx.measureText(lTxt).width) + lph * 2;
        leftW = Math.max(leftW, measuredL);
    }
    if (!W) {
        let rw = 0;
        if (rightOn && t1) {
            ctx.font = _dbFont(b.rightFontWeight1 === 'bold', effectiveRfs1, b.rightFont1 ?? 'Barlow Condensed', b.rightFontStyle1 === 'italic');
            rw = ctx.measureText(t1).width + rph * 2;
            if (layout === 'stacked' && t2) {
                ctx.font = _dbFont(b.rightFontWeight2 === 'bold', effectiveRfs2, b.rightFont2 ?? 'Barlow Condensed', b.rightFontStyle2 === 'italic');
                rw = Math.max(rw, ctx.measureText(t2).width + rph * 2);
            }
        }
        W = Math.ceil(leftW + (divOn ? 1 : 0) + rw);
        W = Math.max(20, W);
    }
    ctx.restore();

    ctx.save();
    ctx.globalAlpha = op;

    // ── 4. Clip to rounded rect ──────────────────────────────────────────
    _rrPath(ctx, x, y, W, H, R);
    ctx.clip();

    // ── 5. Base background ───────────────────────────────────────────────
    if (b.bgType === 'gradient') {
        const [x1, y1, x2, y2] = _gradientPts(x, y, W, H, b.bgGradientAngle ?? 135);
        const g = ctx.createLinearGradient(x1, y1, x2, y2);
        g.addColorStop(0, _hexToRgba(b.bgColor  ?? '#ffffff', b.bgOpacity ?? 0.03));
        g.addColorStop(1, _hexToRgba(b.bgColor2 ?? '#ffffff', b.bgOpacity ?? 0.03));
        ctx.fillStyle = g;
    } else {
        ctx.fillStyle = _hexToRgba(b.bgColor ?? '#ffffff', b.bgOpacity ?? 0.03);
    }
    ctx.fillRect(x, y, W, H);

    // ── 6. Left segment ──────────────────────────────────────────────────
    if (leftOn) {
        const lbOp = b.leftBgOpacity ?? 0;
        if (lbOp > 0) {
            ctx.fillStyle = _hexToRgba(b.leftBgColor ?? '#000000', lbOp);
            ctx.fillRect(x, y, leftW, H);
        }
        if (lTxt) {
            ctx.font      = _dbFont(b.leftFontWeight === 'bold', effectiveLfs, b.leftFont ?? 'Bebas Neue', b.leftFontStyle === 'italic');
            ctx.fillStyle = _hexToRgba(b.leftColor ?? '#ffffff', b.leftOpacity ?? 0.9);
            ctx.textAlign = 'center';
            // Ink-based vertical centering (within padded area)
            const lm  = ctx.measureText(lTxt);
            const lBL = y + bgPad + (H - 2 * bgPad - lm.actualBoundingBoxAscent - lm.actualBoundingBoxDescent) / 2 + lm.actualBoundingBoxAscent;
            ctx.textBaseline = 'alphabetic';
            // When there is no right segment, centre text in the full badge width (W)
            // rather than just the leftWidth panel — fixes static-width alignment.
            const textCentreX = (!rightOn && !divOn) ? x + W / 2 : x + leftW / 2;
            ctx.fillText(lTxt, textCentreX, lBL);
        }
    }

    // ── 7. Divider ───────────────────────────────────────────────────────
    let divX = leftOn ? leftW : 0;
    if (divOn) {
        ctx.fillStyle = _hexToRgba(b.dividerColor ?? '#ffffff', b.dividerOpacity ?? 0.07);
        ctx.fillRect(x + divX, y, 1, H);
        divX += 1;
    }

    // ── 8. Right segment ─────────────────────────────────────────────────
    if (rightOn) {
        const rx  = x + divX;
        const rw  = W - divX;
        const rOp = b.rightBgOpacity ?? 0.15;

        if (b.rightBgType === 'gradient') {
            const [gx1, gy1, gx2, gy2] = _gradientPts(rx, y, rw, H, b.rightBgGradientAngle ?? 135);
            const rg = ctx.createLinearGradient(gx1, gy1, gx2, gy2);
            rg.addColorStop(0, _hexToRgba(b.rightBgColor  ?? '#7838ff', rOp));
            rg.addColorStop(1, _hexToRgba(b.rightBgColor2 ?? '#ff6e14', rOp));
            ctx.fillStyle = rg;
        } else {
            ctx.fillStyle = _hexToRgba(b.rightBgColor ?? '#7838ff', rOp);
        }
        ctx.fillRect(rx, y, rw, H);

        ctx.textAlign = 'left';
        const f1 = _dbFont(b.rightFontWeight1 === 'bold', effectiveRfs1, b.rightFont1 ?? 'Barlow Condensed', b.rightFontStyle1 === 'italic');
        const c1 = _hexToRgba(b.rightColor1 ?? '#e0e0e0', b.rightOpacity1 ?? 0.80);

        if (layout === 'stacked') {
            // Ink-based vertical centering — matches Python renderer fix
            const f2  = _dbFont(b.rightFontWeight2 === 'bold', effectiveRfs2, b.rightFont2 ?? 'Barlow Condensed', b.rightFontStyle2 === 'italic');
            const c2  = _hexToRgba(b.rightColor2 ?? '#e0e0e0', b.rightOpacity2 ?? 0.60);
            const gap = Math.max(2, b.rightStackGap ?? 4);

            ctx.font = f1;
            const m1   = ctx.measureText(t1 || 'Ay');
            const asc1 = m1.actualBoundingBoxAscent;
            const ink1 = asc1 + m1.actualBoundingBoxDescent;

            ctx.font = f2;
            const m2   = ctx.measureText(t2 || 'Ay');
            const asc2 = m2.actualBoundingBoxAscent;
            const ink2 = asc2 + m2.actualBoundingBoxDescent;

            const hasT1 = !!t1, hasT2 = !!t2;
            const totalInk = (hasT1 ? ink1 : 0) + (hasT1 && hasT2 ? gap : 0) + (hasT2 ? ink2 : 0);
            const yInkTop  = y + bgPad + (H - 2 * bgPad - totalInk) / 2;

            ctx.textBaseline = 'alphabetic';
            if (hasT1) {
                ctx.font = f1; ctx.fillStyle = c1;
                ctx.fillText(t1, rx + rph, yInkTop + asc1);
            }
            if (hasT2) {
                ctx.font = f2; ctx.fillStyle = c2;
                ctx.fillText(t2, rx + rph, yInkTop + (hasT1 ? ink1 + gap : 0) + asc2);
            }
        } else {
            if (t1) {
                // Ink-based vertical centering for single right text (within padded area)
                ctx.font = f1;
                const rm  = ctx.measureText(t1);
                const rBL = y + bgPad + (H - 2 * bgPad - rm.actualBoundingBoxAscent - rm.actualBoundingBoxDescent) / 2 + rm.actualBoundingBoxAscent;
                ctx.fillStyle    = c1;
                ctx.textBaseline = 'alphabetic';
                ctx.fillText(t1, rx + rph, rBL);
            }
        }
    }

    ctx.restore(); // remove clip

    // ── 9. Border ────────────────────────────────────────────────────────
    if (b.borderEnabled ?? true) {
        ctx.save();
        ctx.globalAlpha = op * (b.borderOpacity ?? 0.08);
        ctx.strokeStyle = b.borderColor ?? '#ffffff';
        ctx.lineWidth   = b.borderWidth ?? 1;
        _rrPath(ctx, x + 0.5, y + 0.5, W - 1, H - 1, Math.max(0, R - 0.5));
        ctx.stroke();
        ctx.restore();
    }

    // ── 10. Top highlight ────────────────────────────────────────────────
    if (b.highlightEnabled ?? true) {
        ctx.save();
        ctx.globalAlpha = op;
        const hx1  = x + W * 0.12, hx2 = x + W * 0.88;
        const hGrd = ctx.createLinearGradient(hx1, y, hx2, y);
        const hlOp = b.highlightOpacity ?? 0.09;
        hGrd.addColorStop(0,   'rgba(255,255,255,0)');
        hGrd.addColorStop(0.5, `rgba(255,255,255,${hlOp})`);
        hGrd.addColorStop(1,   'rgba(255,255,255,0)');
        ctx.fillStyle = hGrd;
        ctx.fillRect(hx1, y, hx2 - hx1, 1);
        ctx.restore();
    }

    // ── 11. Selection outline ────────────────────────────────────────────
    if (isSelected) {
        ctx.save();
        ctx.strokeStyle = '#4da6ff';
        ctx.lineWidth   = 1.5;
        ctx.setLineDash([4, 3]);
        ctx.strokeRect(x - 3, y - 3, W + 6, H + 6);
        ctx.setLineDash([]);
        ctx.restore();
    }

    b._previewW = W;
    b._previewH = H;
}

/**
 * Resolve the bounding box for a title_logo badge on a canvas of given dimensions.
 * Returns { x, y, maxW, maxH } in canvas pixels.
 * In anchor mode x/y is the top-left of where the logo will be placed after alignment.
 * In pixel mode returns the stored pixel values directly.
 */
function _tlResolveBounds(badge, canvasW, canvasH) {
    const mode = badge.positionMode || 'anchor';
    if (mode === 'anchor') {
        const maxW   = Math.round(canvasW * (badge.maxWidthPct  ?? 60) / 100);
        const maxH   = Math.round(canvasH * (badge.maxHeightPct ?? 12) / 100);
        const cy     = Math.round(canvasH * (badge.anchorY ?? 85) / 100);
        // x is resolved after we know actual logo width; use centre of canvas for now
        return { anchorMode: true, anchorX: badge.anchorX || 'center', anchorY: cy, maxW, maxH, canvasW };
    }
    return {
        anchorMode: false,
        x: badge.x ?? 20, y: badge.y ?? 750,
        maxW: badge.width  || 300,
        maxH: badge.height || 80,
    };
}

function renderTitleLogoOnCanvas(ctx, badge, isSelected) {
    const CANVAS_W = 600, CANVAS_H = 900;
    const opacity = badge.opacity ?? 1.0;
    const bounds  = _tlResolveBounds(badge, CANVAS_W, CANVAS_H);

    // For hit-detection we need _autoW/_autoH — set after we know actual rendered size
    const w = bounds.anchorMode ? bounds.maxW : bounds.maxW;
    const h = bounds.anchorMode ? bounds.maxH : bounds.maxH;
    // Resolve x/y for placeholder/fallback drawing
    let px = bounds.anchorMode ? Math.round((CANVAS_W - w) / 2) : (bounds.x ?? 20);
    let py = bounds.anchorMode ? Math.max(0, bounds.anchorY - h / 2) : (bounds.y ?? 750);

    badge._autoW = w;
    badge._autoH = h;
    // Keep x/y synced for dragging in pixel mode
    if (!bounds.anchorMode) { badge._autoX = bounds.x; badge._autoY = bounds.y; }
    else { badge._autoX = px; badge._autoY = py; }

    // Selection outline around the resolved placeholder area
    if (isSelected) {
        ctx.save();
        ctx.strokeStyle = '#4da6ff';
        ctx.lineWidth = 2;
        ctx.setLineDash([5, 4]);
        ctx.strokeRect(px - 4, py - 4, w + 8, h + 8);
        ctx.setLineDash([]);
        ctx.restore();
    }

    // Draw the scrim/blur over the canvas (before logo/text)
    if (badge.scrimEnabled) {
        const dir       = badge.scrimDirection  || 'bottom';
        const startPct  = (badge.scrimStart     ?? 55)   / 100;
        const endPct    = (badge.scrimEnd       ?? 100)  / 100;
        const col       = badge.scrimColor      || '#000000';
        const maxOp     = badge.scrimOpacity    ?? 0.85;
        const scrimMode = badge.scrimMode       || 'gradient';
        const blurR     = badge.scrimBlurRadius ?? 20;

        if (scrimMode === 'gradient') {
            // Linear gradient fade
            let x0, y0, x1, y1;
            if (dir === 'bottom') {
                x0 = 0; y0 = CANVAS_H * (1 - endPct);   x1 = 0; y1 = CANVAS_H * (1 - startPct);
            } else if (dir === 'top') {
                x0 = 0; y0 = CANVAS_H * endPct;          x1 = 0; y1 = CANVAS_H * startPct;
            } else if (dir === 'right') {
                x0 = CANVAS_W * (1 - endPct); y0 = 0;    x1 = CANVAS_W * (1 - startPct); y1 = 0;
            } else {
                x0 = CANVAS_W * endPct; y0 = 0;           x1 = CANVAS_W * startPct; y1 = 0;
            }
            const grad = ctx.createLinearGradient(x0, y0, x1, y1);
            grad.addColorStop(0, `${col}00`);
            grad.addColorStop(1, _hexToRgba(col, maxOp));
            ctx.save();
            ctx.fillStyle = grad;
            ctx.fillRect(0, 0, CANVAS_W, CANVAS_H);
            ctx.restore();

        } else {
            // Blur mode: blur the region from endPct edge inward, feather the transition
            // Determine the region to blur
            let blurX = 0, blurY = 0, blurW = CANVAS_W, blurH = CANVAS_H;
            let featherStart, featherEnd; // in canvas pixels, sharp→blurred

            if (dir === 'bottom') {
                blurY = Math.round(CANVAS_H * (1 - endPct));
                blurH = CANVAS_H - blurY;
                featherStart = Math.round(CANVAS_H * (1 - endPct));
                featherEnd   = Math.round(CANVAS_H * (1 - startPct));
            } else if (dir === 'top') {
                blurY = 0;
                blurH = Math.round(CANVAS_H * endPct);
                featherStart = blurH;
                featherEnd   = Math.round(CANVAS_H * startPct);
            } else if (dir === 'right') {
                blurX = Math.round(CANVAS_W * (1 - endPct));
                blurW = CANVAS_W - blurX;
                featherStart = blurX;
                featherEnd   = Math.round(CANVAS_W * (1 - startPct));
            } else {
                blurX = 0;
                blurW = Math.round(CANVAS_W * endPct);
                featherStart = blurW;
                featherEnd   = Math.round(CANVAS_W * startPct);
            }

            if (blurW > 0 && blurH > 0 && posterImage) {
                // Draw blurred region onto an offscreen canvas
                const offscreen = document.createElement('canvas');
                offscreen.width  = CANVAS_W;
                offscreen.height = CANVAS_H;
                const offCtx = offscreen.getContext('2d');

                // Draw the full poster blurred
                offCtx.filter = `blur(${blurR}px)`;
                const iw = posterImage.naturalWidth  || posterImage.width;
                const ih = posterImage.naturalHeight || posterImage.height;
                const scale = Math.min(CANVAS_W / iw, CANVAS_H / ih);
                const dw = iw * scale, dh = ih * scale;
                const dx = (CANVAS_W - dw) / 2, dy = (CANVAS_H - dh) / 2;
                offCtx.drawImage(posterImage, dx, dy, dw, dh);
                offCtx.filter = 'none';

                // Compose: sharp full canvas already drawn, paste blurred region with feather mask
                ctx.save();
                const featherLen = Math.max(1, featherEnd - featherStart);

                // Create feather gradient mask
                let maskGrad;
                if (dir === 'bottom') {
                    maskGrad = ctx.createLinearGradient(0, featherStart, 0, featherEnd);
                } else if (dir === 'top') {
                    maskGrad = ctx.createLinearGradient(0, featherEnd, 0, featherStart);
                } else if (dir === 'right') {
                    maskGrad = ctx.createLinearGradient(featherStart, 0, featherEnd, 0);
                } else {
                    maskGrad = ctx.createLinearGradient(featherEnd, 0, featherStart, 0);
                }
                maskGrad.addColorStop(0, 'rgba(0,0,0,0)');
                maskGrad.addColorStop(1, 'rgba(0,0,0,1)');

                // Draw feather mask rectangle, then use source-in to clip blurred image
                const tempC = document.createElement('canvas');
                tempC.width = CANVAS_W; tempC.height = CANVAS_H;
                const tempCtx = tempC.getContext('2d');
                // Fill mask
                tempCtx.fillStyle = maskGrad;
                tempCtx.fillRect(0, 0, CANVAS_W, CANVAS_H);
                // Composite blurred image through mask
                tempCtx.globalCompositeOperation = 'source-in';
                tempCtx.drawImage(offscreen, 0, 0);

                ctx.drawImage(tempC, 0, 0);
                ctx.restore();
            }
        }
    }

    // Helper: draw drop shadow behind a bounding rect
    function _drawShadow(fx, fy, dw, dh) {
        if (!badge.shadowEnabled) return;
        const blur    = badge.shadowBlur    ?? 8;
        const shadowOp = badge.shadowOpacity ?? 0.6;
        const ox      = badge.shadowOffsetX ?? 0;
        const oy      = badge.shadowOffsetY ?? 3;
        const col     = badge.shadowColor   || '#000000';
        ctx.save();
        ctx.shadowColor   = col;
        ctx.shadowBlur    = blur;
        ctx.shadowOffsetX = ox;
        ctx.shadowOffsetY = oy;
        ctx.globalAlpha   = shadowOp;
        ctx.fillStyle = col;
        ctx.fillRect(fx, fy, dw, dh);
        ctx.restore();
    }

    // Helper: draw background pill behind a bounding rect
    function _drawPill(fx, fy, dw, dh) {
        if (!badge.pillEnabled) return;
        const pad    = badge.pillPadding ?? 12;
        const radius = badge.pillRadius  ?? 10;
        const color  = badge.pillColor   || '#000000CC';
        const rx = fx - pad, ry = fy - pad;
        const rw = dw + pad * 2, rh = dh + pad * 2;
        ctx.save();
        ctx.fillStyle = hexToRgba(color);
        drawRoundedRect(ctx, rx, ry, rw, rh, radius);
        ctx.restore();
    }

    // Helper: draw logo image respecting anchor/pixel mode
    function _drawLogoImg(img) {
        const aspect = img.naturalWidth / img.naturalHeight;
        // Scale to fit within maxW × maxH
        const s  = Math.min(bounds.maxW / img.naturalWidth, bounds.maxH / img.naturalHeight);
        const dw = Math.max(1, Math.round(img.naturalWidth  * s));
        const dh = Math.max(1, Math.round(img.naturalHeight * s));

        // Resolve final x using anchor
        let fx;
        if (bounds.anchorMode) {
            const ax = bounds.anchorX;
            if      (ax === 'left')   fx = 0;
            else if (ax === 'right')  fx = bounds.canvasW - dw;
            else                      fx = Math.round((bounds.canvasW - dw) / 2); // center
            py = Math.max(0, Math.min(bounds.anchorY - Math.round(dh / 2), CANVAS_H - dh));
        } else {
            fx = bounds.x;
            py = bounds.y;
        }
        fx = Math.max(0, Math.min(fx, CANVAS_W - dw));

        // Pill first (behind everything), then shadow, then image
        _drawPill(fx, py, dw, dh);
        _drawShadow(fx, py, dw, dh);

        ctx.save();
        ctx.globalAlpha = opacity;
        ctx.drawImage(img, fx, py, dw, dh);
        ctx.restore();

        badge._autoW = dw; badge._autoH = dh;
        badge._autoX = fx; badge._autoY = py;
    }

    // Current pool entry for tmdb_id lookup
    const poolEntry = (posterPoolIndex >= 0 && posterPool[posterPoolIndex]) || null;
    const tmdbId = poolEntry?.tmdb_id || null;
    const mType  = poolEntry?.type    || 'movie';

    if (tmdbId && typeof TEXTLESS_POSTERS_ENABLED !== 'undefined' && TEXTLESS_POSTERS_ENABLED) {
        const cacheKey = `${tmdbId}_${mType}`;
        if (_clearlogoCache[cacheKey] === undefined) {
            _clearlogoCache[cacheKey] = 'loading';
            fetch(`/api/overlays/preview/clearlogo?tmdb_id=${tmdbId}&type=${mType}`)
                .then(r => r.json())
                .then(data => {
                    if (data.logo_url) {
                        const img = new Image();
                        img.onload  = () => { _clearlogoCache[cacheKey] = img; renderCanvas(); };
                        img.onerror = () => { _clearlogoCache[cacheKey] = null; renderCanvas(); };
                        img.crossOrigin = 'anonymous';
                        img.src = data.logo_url;
                    } else {
                        _clearlogoCache[cacheKey] = null;
                        renderCanvas();
                    }
                })
                .catch(() => { _clearlogoCache[cacheKey] = null; renderCanvas(); });
            _drawTitleLogoPlaceholder(ctx, px, py, w, h, 'Loading logo…');
            return;
        }

        const cached = _clearlogoCache[cacheKey];

        if (cached === 'loading') {
            _drawTitleLogoPlaceholder(ctx, px, py, w, h, 'Loading logo…');
            return;
        }

        if (badge.previewFallback) {
            _drawTitleLogoTextFallback(ctx, badge, px, py, w, h, opacity, bounds);
            return;
        }

        if (cached instanceof HTMLImageElement) {
            _drawLogoImg(cached);
            return;
        }

        // null → no clearlogo, show text fallback
        _drawTitleLogoTextFallback(ctx, badge, px, py, w, h, opacity, bounds);
        return;
    }

    // Non-textless mode or no pool entry
    if (badge.previewFallback) {
        _drawTitleLogoTextFallback(ctx, badge, px, py, w, h, opacity, bounds);
    } else {
        _drawTitleLogoPlaceholder(ctx, px, py, w, h, 'Title Logo');
    }
}

function _drawTitleLogoPlaceholder(ctx, x, y, w, h, label) {
    ctx.save();
    ctx.strokeStyle = '#2ecc71';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.strokeRect(x, y, w, h);
    ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(46,204,113,0.08)';
    ctx.fillRect(x, y, w, h);
    ctx.fillStyle = '#2ecc71';
    ctx.font = 'bold 12px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, x + w / 2, y + h / 2);
    ctx.restore();
}

function _drawTitleLogoTextFallback(ctx, badge, x, y, w, h, opacity, bounds) {
    const CANVAS_W = 600, CANVAS_H = 900;
    const fontWeight = badge.fontWeight ?? 'bold';
    const color      = badge.color      || '#FFFFFFDD';
    const borderW    = badge.borderWidth ?? 0;
    const borderCol  = badge.borderColor || '#000000';

    const poolEntry = (posterPoolIndex >= 0 && posterPool[posterPoolIndex]) || null;
    const title = poolEntry?.title || 'Title Preview';

    // Resolve font size — 'auto' fits the title into the container width
    const maxW = w - 16;
    const maxH = h -  8;
    let fontSize;
    const rawFs = badge.fontSize;
    if (rawFs === 'auto' || rawFs == null) {
        let lo = 8, hi = Math.min(maxH, 200), best = lo;
        while (lo <= hi) {
            const mid = Math.floor((lo + hi) / 2);
            ctx.font = `${fontWeight} ${mid}px sans-serif`;
            if (ctx.measureText(title).width <= maxW) { best = mid; lo = mid + 1; }
            else { hi = mid - 1; }
        }
        fontSize = best;
    } else {
        fontSize = typeof rawFs === 'number' ? rawFs : (parseInt(rawFs) || 32);
    }

    ctx.save();
    ctx.font = `${fontWeight} ${fontSize}px sans-serif`;
    const tw = ctx.measureText(title).width;
    const th = fontSize; // approx height

    // Resolve horizontal position using anchor
    let cx;
    if (bounds?.anchorMode) {
        const ax = bounds.anchorX || 'center';
        if      (ax === 'left')  cx = Math.round(tw / 2) + 8;
        else if (ax === 'right') cx = CANVAS_W - Math.round(tw / 2) - 8;
        else                     cx = CANVAS_W / 2;
        y = Math.max(0, Math.min(bounds.anchorY, CANVAS_H - fontSize));
    } else {
        cx = x + w / 2;
    }

    // Draw pill and shadow behind text (using approximate bounding box)
    const textLeft = cx - tw / 2;
    if (badge.pillEnabled) {
        const pad = badge.pillPadding ?? 12, rad = badge.pillRadius ?? 10;
        ctx.save();
        ctx.fillStyle = hexToRgba(badge.pillColor || '#000000CC');
        drawRoundedRect(ctx, textLeft - pad, y - th / 2 - pad, tw + pad * 2, th + pad * 2, rad);
        ctx.restore();
    }
    if (badge.shadowEnabled) {
        ctx.save();
        ctx.shadowColor   = badge.shadowColor   || '#000000';
        ctx.shadowBlur    = badge.shadowBlur    ?? 8;
        ctx.shadowOffsetX = badge.shadowOffsetX ?? 0;
        ctx.shadowOffsetY = badge.shadowOffsetY ?? 3;
        ctx.globalAlpha   = badge.shadowOpacity ?? 0.6;
        ctx.fillStyle = badge.shadowColor || '#000000';
        ctx.fillRect(textLeft, y - th / 2, tw, th);
        ctx.restore();
    }

    ctx.globalAlpha = opacity;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';

    if (borderW > 0) {
        ctx.strokeStyle = borderCol;
        ctx.lineWidth   = borderW * 2;
        ctx.lineJoin    = 'round';
        ctx.strokeText(title, cx, y);
    }
    ctx.fillStyle = color;
    ctx.fillText(title, cx, y);
    ctx.restore();
}

function renderSmartBadgeOnCanvas(ctx, badge, isSelected) {
    const { x, y } = badge;
    const opacity = badge.opacity ?? 1.0;
    const previewImg = smartBadgePreviewImages[badge.badge_type];

    // Determine rendered dimensions first (needed for style overlay)
    let renderW, renderH;
    if (previewImg?.complete && previewImg.naturalWidth > 0) {
        renderH = badge.height || badge._previewH || 42;
        renderW = Math.round(previewImg.naturalWidth * (renderH / previewImg.naturalHeight));
    } else {
        renderW = badge._previewW || 130;
        renderH = badge._previewH || 48;
    }

    const style = badge.styleOverlay || {};
    const styleOn = style.enabled || false;
    const R = style.borderRadius ?? 8;
    const sp = styleOn ? Math.max(0, style.padding ?? 8) : 0;

    // Outer frame dimensions when style overlay is active
    const fW = renderW + 2 * sp;
    const fH = renderH + 2 * sp;

    // ── 1. Style overlay: Base Background (behind PNG, grows outward) ────
    if (styleOn) {
        ctx.save();
        ctx.globalAlpha = opacity;
        const bgType = style.bgType || 'solid';
        const bgOp   = style.bgOpacity ?? 0.03;
        if (bgType === 'gradient') {
            const ang  = ((style.bgAngle ?? 135) * Math.PI) / 180;
            const cx   = x + renderW / 2, cy = y + renderH / 2;
            const dist = Math.sqrt(fW * fW + fH * fH) / 2;
            const grad = ctx.createLinearGradient(
                cx - Math.cos(ang) * dist, cy - Math.sin(ang) * dist,
                cx + Math.cos(ang) * dist, cy + Math.sin(ang) * dist);
            grad.addColorStop(0, _hexToRgba(style.bgColor  || '#ffffff', bgOp));
            grad.addColorStop(1, _hexToRgba(style.bgColor2 || '#ffffff', bgOp));
            ctx.fillStyle = grad;
        } else {
            ctx.fillStyle = _hexToRgba(style.bgColor || '#ffffff', bgOp);
        }
        drawRoundedRect(ctx, x - sp, y - sp, fW, fH, R);
        ctx.restore();
    }

    // ── 2. Badge PNG or placeholder ──────────────────────────────────────
    if (previewImg?.complete && previewImg.naturalWidth > 0) {
        ctx.save();
        ctx.globalAlpha = opacity;
        ctx.drawImage(previewImg, x, y, renderW, renderH);
        ctx.restore();
        badge._previewW = renderW;
        badge._previewH = renderH;
    } else {
        // Fallback placeholder pill (shown while images are loading)
        ctx.save();
        ctx.globalAlpha = opacity;

        // Dark pill background
        ctx.fillStyle = 'rgba(6, 6, 15, 0.82)';
        drawRoundedRect(ctx, x, y, renderW, renderH, 8);

        // Cyan accent left bar
        ctx.fillStyle = '#00a2c7';
        ctx.fillRect(x, y, 3, renderH);

        // Label text
        ctx.fillStyle = '#00dcff';
        ctx.font = `bold ${Math.min(13, renderH - 10)}px Arial`;
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(badge.label, x + 10, y + renderH / 2);

        // "PNG" tag top-right
        ctx.fillStyle = 'rgba(0,162,199,0.7)';
        ctx.font = '9px Arial';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'top';
        ctx.fillText('PNG', x + renderW - 5, y + 4);

        ctx.restore();
    }

    // ── 3. Style overlay: Border + Top Highlight (on outer frame) ───────
    if (styleOn) {
        const bw = Math.max(0, style.borderWidth ?? 1);
        if (bw > 0) {
            ctx.save();
            ctx.globalAlpha = opacity;
            ctx.strokeStyle = _hexToRgba(style.borderColor || '#ffffff', style.borderOpacity ?? 0.08);
            ctx.lineWidth = bw;
            const half = bw / 2;
            _rrPath(ctx, x - sp + half, y - sp + half, fW - bw, fH - bw, Math.max(0, R - half));
            ctx.stroke();
            ctx.restore();
        }
        const hlOp = style.highlightOpacity ?? 0.09;
        if (hlOp > 0) {
            ctx.save();
            ctx.globalAlpha = opacity;
            const hx1 = (x - sp) + fW * 0.12, hx2 = (x - sp) + fW * 0.88;
            const hlGrad = ctx.createLinearGradient(hx1, y - sp, hx2, y - sp);
            hlGrad.addColorStop(0,   `rgba(255,255,255,0)`);
            hlGrad.addColorStop(0.5, `rgba(255,255,255,${hlOp})`);
            hlGrad.addColorStop(1,   `rgba(255,255,255,0)`);
            ctx.fillStyle = hlGrad;
            ctx.fillRect(hx1, y - sp, hx2 - hx1, 1);
            ctx.restore();
        }
    }

    // ── 4. Selection outline ─────────────────────────────────────────────
    if (isSelected) {
        ctx.save();
        ctx.strokeStyle = '#4da6ff';
        ctx.lineWidth = 1.5;
        ctx.setLineDash([5, 4]);
        ctx.strokeRect(x - sp - 3, y - sp - 3, fW + 6, fH + 6);
        ctx.setLineDash([]);
        ctx.restore();
    }
}

function drawRoundedRect(ctx, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
    ctx.fill();
}

function drawRoundedRectStroke(ctx, x, y, w, h, r) {
    const half = ctx.lineWidth / 2;
    r = Math.min(r, (w - ctx.lineWidth) / 2, (h - ctx.lineWidth) / 2);
    ctx.beginPath();
    ctx.moveTo(x + half + r, y + half);
    ctx.lineTo(x + w - half - r, y + half);
    ctx.quadraticCurveTo(x + w - half, y + half, x + w - half, y + half + r);
    ctx.lineTo(x + w - half, y + h - half - r);
    ctx.quadraticCurveTo(x + w - half, y + h - half, x + w - half - r, y + h - half);
    ctx.lineTo(x + half + r, y + h - half);
    ctx.quadraticCurveTo(x + half, y + h - half, x + half, y + h - half - r);
    ctx.lineTo(x + half, y + half + r);
    ctx.quadraticCurveTo(x + half, y + half, x + half + r, y + half);
    ctx.closePath();
    ctx.stroke();
}

function hexToRgba(hex) {
    if (!hex || !hex.startsWith('#')) return 'rgba(0,0,0,0.8)';
    hex = hex.replace('#', '');
    if (hex.length === 3) hex = hex.split('').map(c => c + c).join('');
    const r = parseInt(hex.slice(0, 2), 16) || 0;
    const g = parseInt(hex.slice(2, 4), 16) || 0;
    const b = parseInt(hex.slice(4, 6), 16) || 0;
    const a = hex.length === 8 ? (parseInt(hex.slice(6, 8), 16) / 255).toFixed(3) : '1';
    return `rgba(${r},${g},${b},${a})`;
}

// Rating variable names that support format conversion
const RATING_VARS = new Set(['imdbRating','tmdbRating','traktRating','rtCriticsScore','rtUserScore']);

/**
 * Format a raw rating value according to the chosen display format.
 *   'decimal'    → e.g. 8.5  (divide by 10 if value > 10)
 *   'percentage' → e.g. 85   (multiply by 10 if value ≤ 10, round to int)
 *   'auto'       → pass through unchanged (default)
 */
function formatRatingValue(raw, format) {
    if (!format || format === 'auto') return raw;
    const n = parseFloat(raw);
    if (isNaN(n)) return raw;
    if (format === 'decimal') {
        const v = n > 10 ? n / 10 : n;
        return v % 1 === 0 ? v.toFixed(1) : String(parseFloat(v.toFixed(1)));
    }
    if (format === 'percentage') {
        const v = n <= 10 ? Math.round(n * 10) : Math.round(n);
        return String(v);
    }
    return raw;
}

function interpolateSample(template, fallback, ratingFormat, percentUnit) {
    const text = template.replace(/\{\{(\w+)\}\}/g, (_, k) => {
        const val = SAMPLE_MEDIA[k] || '';
        if (ratingFormat && RATING_VARS.has(k)) {
            const formatted = formatRatingValue(val, ratingFormat);
            return (percentUnit && ratingFormat === 'percentage') ? formatted + '%' : formatted;
        }
        return val;
    });
    return text || fallback || '';
}

// ═══════════════════════════════════════════════════════════
//  SAVE / LOAD
// ═══════════════════════════════════════════════════════════

function saveLayout() {
    const name = document.getElementById('layout-name').value.trim();
    if (!name) { showNotification('Please enter a layout name', 'error'); return; }
    if (badges.length === 0) { showNotification('Please add at least one badge', 'error'); return; }

    const payload = {
        name,
        description: document.getElementById('layout-description').value.trim(),
        media_type: document.getElementById('layout-media-type').value,
        layout_json: { version: 2, badges },
    };

    // On create, default to active. On update, do NOT send is_default so the
    // backend leaves the existing active status unchanged.
    if (!currentLayoutId) {
        payload.is_default = true;
    }

    const method = currentLayoutId ? 'PUT' : 'POST';
    const url = currentLayoutId ? `/api/overlays/layouts/${currentLayoutId}` : '/api/overlays/layouts';

    fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                if (!currentLayoutId && data.layout_id) currentLayoutId = data.layout_id;
                showNotification('Layout saved!', 'success');
            } else {
                showNotification('Save failed: ' + (data.error || 'Unknown error'), 'error');
            }
        })
        .catch(e => showNotification('Error: ' + e, 'error'));
}

function loadLayoutFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const layoutId = params.get('layout_id');
    if (!layoutId) return;

    fetch(`/api/overlays/layouts/${layoutId}`)
        .then(r => r.json())
        .then(data => {
            if (!data.layout) return;
            const layout = data.layout;
            document.getElementById('layout-name').value = layout.name || '';
            document.getElementById('layout-description').value = layout.description || '';
            document.getElementById('layout-media-type').value = layout.media_type || 'both';
            if (document.getElementById('layout-active')) document.getElementById('layout-active').checked = layout.is_default || false;

            let lj = layout.layout_json;
            if (typeof lj === 'string') lj = JSON.parse(lj);
            if (lj && lj.badges) {
                badges = lj.badges;
                // Migrate badges saved before ratingFormat was added
                badges.forEach(b => { b.ratingFormat ??= 'auto'; });
            } else {
                showNotification('Legacy layout format — badges not loaded. Rebuild here.', 'error');
                return;
            }
            currentLayoutId = parseInt(layoutId);
            updateBadgesList();
            // Preload all custom fonts used by the loaded badges so the canvas
            // preview matches the Plex overlay from the first render.
            badges.forEach(b => {
                if (b.type === 'designed_badge') {
                    loadGoogleFont(b.leftFont);
                    loadGoogleFont(b.rightFont1);
                    loadGoogleFont(b.rightFont2);
                    if (b.audioFont) loadGoogleFont(b.audioFont);
                }
            });
            renderCanvas();
        })
        .catch(e => console.error('Failed to load layout:', e));
}

function clearCanvas() {
    if (badges.length === 0) return;
    showPopup({
        type: 'confirm',
        title: 'Clear All Badges',
        message: 'Clear all badges?',
        confirmText: 'Confirm',
        cancelText: 'Cancel',
        onConfirm: function() {
            badges = [];
            selectedBadgeId = null;
            document.getElementById('badge-properties').style.display = 'none';
            document.getElementById('no-selection-message').style.display = 'block';
            updateBadgesList();
            renderCanvas();
        }
    });
}

// ═══════════════════════════════════════════════════════════
//  UTILITY
// ═══════════════════════════════════════════════════════════

function showNotification(msg, type) {
    const n = document.createElement('div');
    n.textContent = msg;
    n.style.cssText = [
        'position:fixed', 'top:20px', 'right:20px', 'z-index:9999',
        'padding:12px 20px', 'border-radius:6px', 'font-weight:500',
        'font-size:14px', 'max-width:320px',
        'box-shadow:0 3px 12px rgba(0,0,0,0.25)',
        `background:${type === 'success' ? '#28a745' : '#dc3545'}`,
        'color:white'
    ].join(';');
    document.body.appendChild(n);
    setTimeout(() => { n.style.opacity = '0'; n.style.transition = 'opacity 0.3s'; setTimeout(() => n.remove(), 300); }, 3000);
}

// Expose globals
window.updateSelectedSmartBadge = updateSelectedSmartBadge;
window.cyclePoolPoster = cyclePoolPoster;
window.addBadgeFromPalette = addBadgeFromPalette;
window.selectBadge = selectBadge;
window.deleteSelectedBadge = deleteSelectedBadge;
window.duplicateSelectedBadge = duplicateSelectedBadge;
window.deleteBadgeById = deleteBadgeById;
window.saveLayout = saveLayout;
window.clearCanvas = clearCanvas;
window.renderCanvas = renderCanvas;
window.loadSamplePoster = resetToGradient;   // backward compat
window.loadPosterFile = loadPosterFile;
window.resetToGradient = resetToGradient;

// ── Logo Picker ──────────────────────────────────────────────────────────────

let _logoLibraryData = null;   // cached { categories: [...] }
let _logoActiveTab   = 'all';

function openLogoPicker() {
    const modal = document.getElementById('logo-picker-modal');
    if (!modal) return;
    modal.classList.add('open');

    // Infer default category from current path value
    const curPath = (document.getElementById('icon-path')?.value || '').toLowerCase();
    let hint = null;
    for (const cat of ['rating', 'network', 'studio', 'misc']) {
        if (curPath.includes('/' + cat + '/') || curPath.startsWith(cat + '/')) {
            hint = cat; break;
        }
    }

    document.getElementById('logo-search').value = '';
    _loadLogoLibrary(hint);

    // Close on backdrop click
    modal.onclick = (e) => { if (e.target === modal) closeLogoPicker(); };
}

function closeLogoPicker() {
    const modal = document.getElementById('logo-picker-modal');
    if (modal) modal.classList.remove('open');
}

async function _loadLogoLibrary(defaultCat) {
    const grid = document.getElementById('logo-picker-grid');
    const tabs = document.getElementById('logo-picker-tabs');
    grid.innerHTML = '<p style="color:#666;padding:20px;text-align:center;">Loading logos…</p>';
    tabs.innerHTML = '';

    try {
        if (!_logoLibraryData) {
            const resp = await fetch('/api/overlays/logos');
            if (!resp.ok) throw new Error(resp.statusText);
            _logoLibraryData = await resp.json();
        }
        const data = _logoLibraryData;

        // Build category tabs
        const allCount = data.categories.reduce((n, c) => n + c.groups.reduce((m, g) => m + g.logos.length, 0), 0);
        tabs.innerHTML = `<button class="logo-tab${!defaultCat ? ' active' : ''}" data-cat="all" onclick="switchLogoTab('all')">All <span class="logo-tab-count">${allCount}</span></button>`;
        for (const cat of data.categories) {
            const count = cat.groups.reduce((n, g) => n + g.logos.length, 0);
            const isActive = defaultCat === cat.name;
            tabs.innerHTML += `<button class="logo-tab${isActive ? ' active' : ''}" data-cat="${cat.name}" onclick="switchLogoTab('${cat.name}')">${cat.name} <span class="logo-tab-count">${count}</span></button>`;
        }

        _logoActiveTab = defaultCat || 'all';
        _renderLogoGrid();

    } catch (e) {
        grid.innerHTML = `<p style="color:#f66;padding:20px;text-align:center;">Failed to load logos: ${e.message}</p>`;
    }
}

function switchLogoTab(catName) {
    _logoActiveTab = catName;
    document.querySelectorAll('.logo-tab').forEach(t =>
        t.classList.toggle('active', t.dataset.cat === catName));
    document.getElementById('logo-search').value = '';
    _renderLogoGrid();
}

function filterLogos() {
    _renderLogoGrid();
}

function _renderLogoGrid() {
    const grid = document.getElementById('logo-picker-grid');
    const search = (document.getElementById('logo-search')?.value || '').toLowerCase().trim();
    const data = _logoLibraryData;
    if (!data) return;

    const cats = _logoActiveTab === 'all'
        ? data.categories
        : data.categories.filter(c => c.name === _logoActiveTab);

    let html = '';
    for (const cat of cats) {
        for (const group of cat.groups) {
            const logos = search
                ? group.logos.filter(l => l.name.toLowerCase().includes(search))
                : group.logos;
            if (!logos.length) continue;

            const groupLabel = (group.label !== cat.name)
                ? `${cat.name} / ${group.label}`
                : cat.name;

            html += `<div class="logo-group">
                <div class="logo-group-label">${groupLabel} <span style="opacity:.45">(${logos.length})</span></div>
                <div class="logo-thumbnail-grid">`;

            for (const logo of logos) {
                const userMark = logo.is_user ? '<span class="user-mark">custom</span>' : '';
                const escapedPath = logo.path.replace(/'/g, "\\'");
                html += `<div class="logo-thumb" onclick="selectLogoFromPicker('${escapedPath}')" title="${logo.name}">
                    <img src="${logo.url}" alt="${logo.name}" loading="lazy">
                    <span>${logo.name}</span>${userMark}
                </div>`;
            }
            html += `</div></div>`;
        }
    }

    grid.innerHTML = html || '<p style="color:#666;padding:20px;text-align:center;">No logos found</p>';
}

function selectLogoFromPicker(path) {
    const input = document.getElementById('icon-path');
    if (input) {
        input.value = path;
        input.dispatchEvent(new Event('input',  { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
    }
    closeLogoPicker();
}

async function handleLogoUpload(fileInput) {
    const file = fileInput.files[0];
    if (!file) return;

    // Check if active tab has a subcategory (e.g. "network/color")
    const parts = _logoActiveTab.split('/');
    const cat = parts[0] !== 'all' ? parts[0] : 'custom';
    const sub = parts[1] || '';

    const fd = new FormData();
    fd.append('file', file);
    fd.append('category', cat);
    if (sub) fd.append('subcategory', sub);

    const grid = document.getElementById('logo-picker-grid');
    const prevHTML = grid.innerHTML;
    grid.innerHTML = '<p style="color:#aaa;padding:20px;text-align:center;">Uploading…</p>';

    try {
        const resp = await fetch('/api/overlays/logos/upload', { method: 'POST', body: fd });
        const data = await resp.json();
        if (data.success) {
            _logoLibraryData = null;  // invalidate cache
            await _loadLogoLibrary(_logoActiveTab !== 'all' ? cat : null);
        } else {
            grid.innerHTML = prevHTML;
            showPopup({ type: 'error', title: 'Error', message: 'Upload failed: ' + (data.error || 'unknown error'), autoClose: 4000 });
        }
    } catch (e) {
        grid.innerHTML = prevHTML;
        showPopup({ type: 'error', title: 'Error', message: 'Upload failed: ' + e.message, autoClose: 4000 });
    }
    fileInput.value = '';
}

window.openLogoPicker    = openLogoPicker;
window.closeLogoPicker   = closeLogoPicker;
window.switchLogoTab     = switchLogoTab;
window.filterLogos       = filterLogos;
window.selectLogoFromPicker = selectLogoFromPicker;
window.handleLogoUpload  = handleLogoUpload;
