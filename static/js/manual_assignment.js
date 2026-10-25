// Manual file assignment page (templates/manual_assignment.html).
//
// Each target item (movie or episode) gets one file from the torrent. The
// rows, the shared file picker, the unused-files panel and the summary all
// read one state (values: item key -> filename / '' / IGNORE) and are
// redrawn by refresh(). The hidden inputs named by item key are what the
// form posts to confirm_manual_assignment.

const IGNORE = '--ignore--';
const REASONS = {
    se: { text: 'S/E', title: 'Matched by the season and episode number in the file name' },
    absolute: { text: 'Absolute #', title: 'Matched by the absolute episode number in the file name' },
    title: { text: 'Title', title: 'Matched by episode title only, worth a check' },
    movie: { text: 'Name', title: "The file name matches the movie's title and year" },
    largest: { text: 'Largest file', title: 'No file name matched, so the largest file was picked, worth a check' },
    manual: { text: 'Manual', title: 'Picked by you' },
};
const WEAK_REASONS = new Set(['title', 'largest']);
const PICKER_LIMIT = 200;

const data = JSON.parse(document.getElementById('ma-page-data').textContent);
const urls = JSON.parse(document.getElementById('ma-page-urls').textContent);

let showPopup = null;
let POPUP_TYPES = null;
import(urls.notifications).then(module => {
    showPopup = module.showPopup;
    POPUP_TYPES = module.POPUP_TYPES;
}).catch(error => console.error('Error loading notifications module:', error));

// ── State ──
const items = data.items;
const itemsByKey = new Map(items.map(item => [item.key, item]));
const files = data.files;
const filesByName = new Map(files.map(file => [file.filename, file]));
const values = new Map(items.map(item => [item.key, filesByName.has(item.file) ? item.file : '']));
const reasons = new Map(items.map(item => [item.key, values.get(item.key) ? item.reason : '']));
const undoStack = [];
const collapsed = new Set();
let filter = 'all';
let armedFile = null;
let jumpCursor = -1;
let usage = new Map();  // filename -> keys using it

// Seasons in display order; movies sit in one group with no header
const groups = [];
for (const item of items) {
    const id = item.type === 'episode' ? `s${item.season}` : 'movies';
    let group = groups.find(g => g.id === id);
    if (!group) {
        group = { id, season: item.type === 'episode' ? item.season : null, keys: [] };
        groups.push(group);
    }
    group.keys.push(item.key);
}
const orderedKeys = groups.flatMap(group => group.keys);

// ── Helpers ──
function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
}

function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function pad(n, width = 2) {
    return String(n).padStart(width, '0');
}

function itemCode(item) {
    return item.type === 'episode' ? `S${pad(item.season)}E${pad(item.episode)}` : item.title;
}

function formatSize(bytes) {
    const gb = (bytes || 0) / (1024 ** 3);
    return gb >= 1 ? `${gb.toFixed(2)} GB` : `${Math.round((bytes || 0) / (1024 ** 2))} MB`;
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

// How many episodes a file holds: an "E01-E02" or "(001-002)" file may fill
// that many rows without counting as a duplicate
function fileCapacity(file) {
    return Math.max(1, new Set(file.episodes || []).size, new Set(file.absolutes || []).size);
}

// Whether a file's name says it holds this episode
function fileFits(file, item) {
    if (item.type !== 'episode' || file.skip_reason) return false;
    const absolutes = file.absolutes || [];
    if (absolutes.length) return item.absolute != null && absolutes.includes(item.absolute);
    const episodes = file.episodes || [];
    const seasons = file.seasons || [];
    if (seasons.length) return seasons.includes(item.season) && episodes.includes(item.episode);
    return episodes.includes(item.episode) && item.season === 1
        || (item.absolute != null && episodes.includes(item.absolute));
}

function isDuplicate(filename) {
    const keys = usage.get(filename);
    const file = filesByName.get(filename);
    return !!(keys && file && keys.length > fileCapacity(file));
}

function rowState(key) {
    const value = values.get(key);
    if (value === IGNORE) return 'ignored';
    if (!value) return 'empty';
    return isDuplicate(value) ? 'duplicate' : 'set';
}

// ── Undo ──
function snapshot() {
    undoStack.push({ values: new Map(values), reasons: new Map(reasons) });
    if (undoStack.length > 100) undoStack.shift();
}

function undo() {
    const last = undoStack.pop();
    if (!last) return;
    last.values.forEach((v, k) => values.set(k, v));
    last.reasons.forEach((v, k) => reasons.set(k, v));
    refresh();
}

// Every change goes through here: one undo step per call
function change(updates) {
    snapshot();
    for (const [key, value] of updates) {
        values.set(key, value);
        reasons.set(key, value && value !== IGNORE ? 'manual' : '');
    }
    refresh();
}

// ── Rows ──
const listEl = document.getElementById('ma-list');
const rowEls = new Map();
const groupEls = new Map();
const form = document.getElementById('manual-assignment-form');

function buildRows() {
    for (const group of groups) {
        const section = el('section', 'ma-season');
        let countEl = null;
        if (group.season !== null && groups.length > 1) {
            const header = el('button', 'ma-season-header');
            header.type = 'button';
            header.setAttribute('aria-expanded', 'true');
            header.append(el('span', 'ma-season-chevron', '▾'),
                el('span', 'ma-season-title', group.season === 0 ? 'Specials' : `Season ${group.season}`));
            countEl = el('span', 'ma-season-count');
            header.append(countEl);
            header.addEventListener('click', () => {
                if (collapsed.has(group.id)) collapsed.delete(group.id); else collapsed.add(group.id);
                refresh();
            });
            section.append(header);
        }
        const body = el('div', 'ma-season-body');
        section.append(body);
        groupEls.set(group.id, { section, body, countEl });

        for (const key of group.keys) {
            const item = itemsByKey.get(key);
            const row = el('div', 'ma-row');
            row.dataset.key = key;

            const info = el('div', 'ma-item');
            const line = el('div', 'ma-item-line');
            if (item.type === 'episode') {
                line.append(el('span', 'ma-code', itemCode(item)));
                if (item.absolute != null && item.absolute !== item.episode) {
                    line.append(el('span', 'ma-abs', `#${item.absolute}`));
                }
                line.append(el('span', 'ma-ep-title', item.episode_title || ''));
            } else {
                line.append(el('span', 'ma-ep-title', `${item.title} (${item.year})`));
            }
            const badges = el('div', 'ma-badges');
            if (item.in_library) {
                const badge = el('span', 'in-library-badge', 'In library');
                badge.title = 'Already in your library in this version';
                badges.append(badge);
            }
            const reasonEl = el('span', 'ma-reason');
            badges.append(reasonEl);
            info.append(line, badges);

            const fileCell = el('div', 'ma-file');
            const pick = el('button', 'ma-pick');
            pick.type = 'button';
            const pickName = el('span', 'ma-pick-name');
            const pickMeta = el('span', 'ma-pick-meta');
            pick.append(pickName, pickMeta);
            pick.addEventListener('click', event => {
                event.stopPropagation();
                if (armedFile) assignArmed(key); else openPicker(key, pick);
            });
            const input = el('input');
            input.type = 'hidden';
            input.name = key;
            fileCell.append(pick, input);

            const menuBtn = el('button', 'ma-row-menu', '⋯');
            menuBtn.type = 'button';
            menuBtn.title = 'More actions';
            menuBtn.setAttribute('aria-label', `More actions for ${itemCode(item)}`);
            menuBtn.addEventListener('click', event => {
                event.stopPropagation();
                openMenu(key, menuBtn);
            });

            row.append(info, fileCell, menuBtn);
            row.addEventListener('click', event => {
                if (armedFile && !event.target.closest('.ma-row-menu')) assignArmed(key);
            });
            body.append(row);
            rowEls.set(key, { row, reasonEl, pickName, pickMeta, input });
        }
        listEl.append(section);
    }
}

function rowVisible(key, state) {
    const item = itemsByKey.get(key);
    switch (filter) {
        case 'empty': return state === 'empty';
        case 'duplicate': return state === 'duplicate';
        case 'check': return state === 'set' && WEAK_REASONS.has(reasons.get(key));
        case 'library': return item.in_library;
        default: return true;
    }
}

// ── Refresh ──
function computeUsage() {
    usage = new Map();
    for (const key of orderedKeys) {
        const value = values.get(key);
        if (!value || value === IGNORE) continue;
        if (!usage.has(value)) usage.set(value, []);
        usage.get(value).push(key);
    }
}

function refresh() {
    computeUsage();
    const counts = { set: 0, empty: 0, ignored: 0, duplicate: 0, check: 0, library: 0 };
    const states = new Map();
    for (const key of orderedKeys) {
        const state = rowState(key);
        states.set(key, state);
        counts[state] += 1;
        if (state === 'set' && WEAK_REASONS.has(reasons.get(key))) counts.check += 1;
        if (itemsByKey.get(key).in_library) counts.library += 1;
    }
    if (filter !== 'all' && !counts[filter]) filter = 'all';

    for (const group of groups) {
        const { section, body, countEl } = groupEls.get(group.id);
        let filled = 0;
        let shown = 0;
        for (const key of group.keys) {
            const state = states.get(key);
            if (state === 'set' || state === 'duplicate') filled += 1;
            const visible = rowVisible(key, state);
            if (visible) shown += 1;
            drawRow(key, state, visible);
        }
        section.hidden = shown === 0;
        const isCollapsed = collapsed.has(group.id) && filter === 'all';
        body.hidden = isCollapsed;
        if (countEl) {
            countEl.textContent = `${filled} / ${group.keys.length}`;
            countEl.classList.toggle('ma-complete', filled === group.keys.length);
            const header = countEl.parentElement;
            header.setAttribute('aria-expanded', String(!isCollapsed));
            header.classList.toggle('ma-collapsed', isCollapsed);
        }
    }

    drawSummary(counts);
    drawFilters(counts);
    drawFilesPanel();
    drawMissing(states);
    drawArmed();
    document.getElementById('ma-undo').disabled = undoStack.length === 0;
    const anyEmpty = counts.empty > 0;
    document.getElementById('ma-prev').disabled = !anyEmpty;
    document.getElementById('ma-next').disabled = !anyEmpty;
    document.getElementById('ma-ignore-empty').disabled = !anyEmpty;
}

function drawRow(key, state, visible) {
    const { row, reasonEl, pickName, pickMeta, input } = rowEls.get(key);
    const value = values.get(key);
    row.hidden = !visible;
    row.dataset.state = state;
    row.classList.toggle('ma-armable', !!armedFile);
    input.value = value;

    if (value === IGNORE) {
        pickName.textContent = 'Ignored';
        pickMeta.textContent = 'This item will be skipped';
    } else if (!value) {
        pickName.textContent = 'No file';
        pickMeta.textContent = 'Click to choose';
    } else {
        const file = filesByName.get(value);
        pickName.textContent = value;
        const meta = [file.label, formatSize(file.bytes)].filter(Boolean);
        if (state === 'duplicate') {
            const others = usage.get(value).filter(k => k !== key).map(k => itemCode(itemsByKey.get(k)));
            meta.push(`also on ${others.join(', ')}`);
        }
        pickMeta.textContent = meta.join(' · ');
    }
    row.querySelector('.ma-pick').title = value && value !== IGNORE ? filesByName.get(value).path : '';

    const reason = state === 'set' || state === 'duplicate' ? REASONS[reasons.get(key)] : null;
    reasonEl.hidden = !reason;
    if (reason) {
        const item = itemsByKey.get(key);
        reasonEl.textContent = reasons.get(key) === 'title' && item.score ? `${reason.text} ${item.score}%` : reason.text;
        reasonEl.title = reason.title;
        reasonEl.dataset.reason = reasons.get(key);
    }
}

function drawSummary(counts) {
    const summary = document.getElementById('ma-summary');
    const total = orderedKeys.length;
    const filled = counts.set + counts.duplicate;
    const unused = files.filter(f => !usage.has(f.filename) && !f.skip_reason).length;
    summary.replaceChildren();
    const main = el('span', 'ma-summary-main', `${filled} / ${total} matched`);
    main.classList.toggle('ma-complete', filled + counts.ignored === total && !counts.duplicate);
    summary.append(main);
    const parts = [];
    if (counts.empty) parts.push(['ma-warn', `${counts.empty} with no file`]);
    if (counts.ignored) parts.push(['', `${counts.ignored} ignored`]);
    if (counts.duplicate) parts.push(['ma-bad', `${counts.duplicate} sharing a file`]);
    if (counts.check) parts.push(['', `${counts.check} worth a check`]);
    parts.push(['', `${unused} file${unused === 1 ? '' : 's'} unused`]);
    for (const [cls, text] of parts) {
        summary.append(el('span', 'ma-summary-sep', '·'), el('span', cls, text));
    }
}

function drawFilters(counts) {
    const wrap = document.getElementById('ma-filters');
    const options = [
        ['all', 'All', orderedKeys.length],
        ['empty', 'No file', counts.empty],
        ['duplicate', 'Sharing a file', counts.duplicate],
        ['check', 'Worth a check', counts.check],
        ['library', 'In library', counts.library],
    ];
    wrap.replaceChildren();
    for (const [id, label, count] of options) {
        if (id !== 'all' && !count) continue;
        const chip = el('button', 'ma-chip');
        chip.type = 'button';
        chip.setAttribute('aria-pressed', String(filter === id));
        chip.append(el('span', null, label), el('span', 'ma-chip-count', count));
        chip.addEventListener('click', () => {
            filter = id;
            refresh();
        });
        wrap.append(chip);
    }
}

// ── Unused files panel ──
const filesPanel = document.getElementById('ma-files');
let filesPanelOpen = window.matchMedia('(min-width: 901px)').matches;

function fileButton(file) {
    const button = el('button', 'ma-file-chip');
    button.type = 'button';
    button.title = file.path;
    button.setAttribute('aria-pressed', String(armedFile === file.filename));
    button.append(el('span', 'ma-file-name', file.filename));
    const meta = [file.label, formatSize(file.bytes), file.skip_reason && `skipped: ${file.skip_reason}`].filter(Boolean);
    button.append(el('span', 'ma-file-meta', meta.join(' · ')));
    button.addEventListener('click', () => {
        armedFile = armedFile === file.filename ? null : file.filename;
        refresh();
    });
    return button;
}

function drawFilesPanel() {
    const sorted = [...files].sort((a, b) => collator.compare(a.path, b.path));
    const unused = sorted.filter(f => !usage.has(f.filename) && !f.skip_reason);
    const skipped = sorted.filter(f => !usage.has(f.filename) && f.skip_reason);
    filesPanel.replaceChildren();

    const header = el('button', 'ma-files-header');
    header.type = 'button';
    header.setAttribute('aria-expanded', String(filesPanelOpen));
    header.append(el('span', null, 'Unused files'), el('span', 'ma-chip-count', unused.length));
    header.addEventListener('click', () => {
        filesPanelOpen = !filesPanelOpen;
        refresh();
    });
    filesPanel.append(header);
    if (!filesPanelOpen) return;

    const body = el('div', 'ma-files-body');
    if (unused.length) {
        body.append(el('p', 'ma-files-hint', 'Click a file, then click the episode it belongs to.'));
        unused.forEach(file => body.append(fileButton(file)));
    } else {
        body.append(el('p', 'ma-files-hint', 'Every file is in use.'));
    }
    if (skipped.length) {
        const details = el('details', 'ma-files-skipped');
        details.append(el('summary', null, `Skipped by auto-match (${skipped.length})`));
        skipped.forEach(file => details.append(fileButton(file)));
        body.append(details);
    }
    filesPanel.append(body);
}

// ── Click a file, then an episode ──
function drawArmed() {
    const banner = document.getElementById('ma-armed');
    banner.hidden = !armedFile;
    if (!armedFile) return;
    banner.replaceChildren(
        el('span', null, 'Click an episode to give it '),
        el('strong', null, armedFile),
        el('span', null, '. Esc cancels.'));
    const cancel = el('button', 'ma-btn', 'Cancel');
    cancel.type = 'button';
    cancel.addEventListener('click', () => { armedFile = null; refresh(); });
    banner.append(cancel);
}

function assignArmed(key) {
    const filename = armedFile;
    armedFile = null;
    change([[key, filename]]);
    flashRow(key);
}

// ── File picker ──
const picker = el('div', 'ma-picker');
picker.hidden = true;
picker.setAttribute('role', 'dialog');
const pickerSearch = el('input', 'ma-picker-search');
pickerSearch.type = 'search';
pickerSearch.placeholder = 'Search files';
pickerSearch.setAttribute('aria-label', 'Search files');
const pickerList = el('div', 'ma-picker-list');
pickerList.setAttribute('role', 'listbox');
picker.append(pickerSearch, pickerList);
document.body.append(picker);
let pickerKey = null;
let pickerAnchor = null;
let pickerOptions = [];
let pickerActive = 0;

function openPicker(key, anchor) {
    closeMenu();
    pickerKey = key;
    pickerAnchor = anchor;
    pickerSearch.value = '';
    picker.hidden = false;
    drawPicker();
    anchor.scrollIntoView({ block: 'nearest' });
    positionFloating(picker, anchor, 420);
    pickerSearch.focus({ preventScroll: true });
}

function closePicker(restoreFocus = false) {
    if (picker.hidden) return;
    picker.hidden = true;
    if (restoreFocus && pickerAnchor) pickerAnchor.focus();
    pickerKey = null;
}

function drawPicker() {
    const item = itemsByKey.get(pickerKey);
    const current = values.get(pickerKey);
    const query = pickerSearch.value.trim().toLowerCase();
    const sorted = [...files].sort((a, b) => collator.compare(a.path, b.path))
        .filter(f => !query || f.path.toLowerCase().includes(query));
    const fits = sorted.filter(f => fileFits(f, item));
    const fitSet = new Set(fits);
    const unused = sorted.filter(f => !fitSet.has(f) && !usage.has(f.filename));
    const used = sorted.filter(f => !fitSet.has(f) && usage.has(f.filename));

    pickerOptions = [];
    pickerList.replaceChildren();
    const addOption = (value, name, meta, extraClass) => {
        const option = el('div', `ma-option ${extraClass || ''}`);
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', String(value === current));
        option.append(el('span', 'ma-option-name', name));
        if (meta) option.append(el('span', 'ma-option-meta', meta));
        option.addEventListener('mousedown', event => event.preventDefault());
        option.addEventListener('click', () => pickValue(value));
        pickerList.append(option);
        pickerOptions.push({ value, el: option });
    };
    const fileMeta = file => {
        const parts = [file.label, formatSize(file.bytes)];
        const others = (usage.get(file.filename) || []).filter(k => k !== pickerKey);
        if (others.length) parts.push(`used by ${others.map(k => itemCode(itemsByKey.get(k))).join(', ')}`);
        if (file.skip_reason) parts.push(`skipped: ${file.skip_reason}`);
        return parts.filter(Boolean).join(' · ');
    };
    if (!query) {
        addOption('', 'No file', 'Leave empty: skipped on confirm', 'ma-option-plain');
        addOption(IGNORE, 'Ignore this item', 'Skip it on purpose', 'ma-option-plain');
    }
    let rendered = 0;
    for (const [title, list] of [['Matches this episode', fits], ['Unused', unused], ['Used elsewhere', used]]) {
        if (!list.length || rendered >= PICKER_LIMIT) continue;
        pickerList.append(el('div', 'ma-option-group', title));
        for (const file of list) {
            if (rendered >= PICKER_LIMIT) break;
            addOption(file.filename, file.filename, fileMeta(file), fitSet.has(file) ? 'ma-option-fit' : '');
            rendered += 1;
        }
    }
    const total = fits.length + unused.length + used.length;
    if (total > rendered) pickerList.append(el('div', 'ma-option-note', `${total - rendered} more, type to narrow down`));
    if (!total && query) pickerList.append(el('div', 'ma-option-note', 'No file matches'));

    const selected = pickerOptions.findIndex(o => o.value === current);
    pickerActive = query ? 0 : Math.max(0, selected);
    highlightOption();
}

function highlightOption() {
    pickerOptions.forEach((option, i) => option.el.classList.toggle('ma-active', i === pickerActive));
    // Scroll the list only: scrollIntoView would also scroll the page
    const active = pickerOptions[pickerActive];
    if (!active) return;
    const top = active.el.offsetTop;
    const bottom = top + active.el.offsetHeight;
    if (top < pickerList.scrollTop) pickerList.scrollTop = top;
    else if (bottom > pickerList.scrollTop + pickerList.clientHeight) pickerList.scrollTop = bottom - pickerList.clientHeight;
}

function pickValue(value) {
    const key = pickerKey;
    closePicker(true);
    if (values.get(key) !== value) change([[key, value]]);
}

pickerSearch.addEventListener('input', drawPicker);
pickerSearch.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const step = event.key === 'ArrowDown' ? 1 : -1;
        pickerActive = Math.min(pickerOptions.length - 1, Math.max(0, pickerActive + step));
        highlightOption();
    } else if (event.key === 'Enter') {
        event.preventDefault();
        if (pickerOptions[pickerActive]) pickValue(pickerOptions[pickerActive].value);
    } else if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closePicker(true);
    }
});

function positionFloating(node, anchor, preferredWidth) {
    const rect = anchor.getBoundingClientRect();
    const width = Math.min(Math.max(rect.width, preferredWidth), window.innerWidth - 16);
    const left = Math.min(Math.max(8, rect.left), window.innerWidth - width - 8);
    node.style.width = `${width}px`;
    node.style.left = `${left + window.scrollX}px`;
    // Open upwards when it would run off the bottom and there's more room above
    const height = node.offsetHeight;
    const above = rect.bottom + height + 8 > window.innerHeight && rect.top > window.innerHeight - rect.bottom;
    node.style.top = `${(above ? rect.top - height - 4 : rect.bottom + 4) + window.scrollY}px`;
}

// ── Row menu: shift / fill / clear / ignore ──
const menu = el('div', 'ma-menu');
menu.hidden = true;
menu.setAttribute('role', 'menu');
document.body.append(menu);
let menuAnchor = null;

function seasonKeysFrom(key) {
    const group = groups.find(g => g.keys.includes(key));
    return group.keys.slice(group.keys.indexOf(key));
}

function shift(key, direction) {
    const keys = seasonKeysFrom(key);
    const before = keys.map(k => values.get(k));
    // down: every file moves one episode later and this row is left empty;
    // up: every file moves one episode earlier and the season's last row is left empty
    const after = direction > 0 ? ['', ...before.slice(0, -1)] : [...before.slice(1), ''];
    change(keys.map((k, i) => [k, after[i]]));
}

function fillInFileOrder(key) {
    const keys = seasonKeysFrom(key);
    const inRange = new Set(keys);
    const usedOutside = new Set();
    usage.forEach((users, filename) => {
        if (users.some(k => !inRange.has(k))) usedOutside.add(filename);
    });
    const ordered = [...files].filter(f => !f.skip_reason).sort((a, b) => collator.compare(a.path, b.path));
    const startValue = values.get(key);
    let index = ordered.findIndex(f => f.filename === startValue);
    if (index < 0) index = ordered.findIndex(f => !usedOutside.has(f.filename));
    if (index < 0) return;
    const updates = [];
    for (const k of keys) {
        while (index < ordered.length && usedOutside.has(ordered[index].filename)) index += 1;
        updates.push([k, index < ordered.length ? ordered[index].filename : '']);
        index += 1;
    }
    change(updates);
}

function openMenu(key, anchor) {
    closePicker();
    if (!menu.hidden && menuAnchor === anchor) { closeMenu(); return; }
    menuAnchor = anchor;
    const item = itemsByKey.get(key);
    const episode = item.type === 'episode';
    const label = item.season === 0 ? 'specials' : 'season';
    const actions = [
        episode && [`Shift files down one, to end of ${label}`, 'This episode is left empty and every file below moves one episode later.', () => shift(key, 1)],
        episode && [`Shift files up one, to end of ${label}`, "Every file below moves one episode earlier; the last episode is left empty.", () => shift(key, -1)],
        episode && ['Fill from here in file order', "Gives this and the following episodes the pack's files in name order, starting at this episode's file.", () => fillInFileOrder(key)],
        ['Clear', null, () => change([[key, '']])],
        ['Ignore', null, () => change([[key, IGNORE]])],
    ].filter(Boolean);
    menu.replaceChildren();
    for (const [text, hint, run] of actions) {
        const button = el('button', 'ma-menu-item');
        button.type = 'button';
        button.setAttribute('role', 'menuitem');
        button.append(el('span', null, text));
        if (hint) button.append(el('span', 'ma-menu-hint', hint));
        button.addEventListener('click', () => {
            closeMenu(true);
            run();
        });
        menu.append(button);
    }
    menu.hidden = false;
    positionFloating(menu, anchor, 300);
    const rect = anchor.getBoundingClientRect();
    const width = parseFloat(menu.style.width);
    menu.style.left = `${Math.max(8, rect.right - width) + window.scrollX}px`;
    menu.querySelector('button').focus({ preventScroll: true });
}

function closeMenu(restoreFocus = false) {
    if (menu.hidden) return;
    menu.hidden = true;
    if (restoreFocus && menuAnchor) menuAnchor.focus();
}

menu.addEventListener('keydown', event => {
    const buttons = [...menu.querySelectorAll('button')];
    const index = buttons.indexOf(document.activeElement);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const step = event.key === 'ArrowDown' ? 1 : -1;
        buttons[(index + step + buttons.length) % buttons.length].focus();
    } else if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeMenu(true);
    }
});

document.addEventListener('click', event => {
    if (!picker.hidden && !picker.contains(event.target)) closePicker();
    if (!menu.hidden && !menu.contains(event.target)) closeMenu();
});
document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && armedFile) {
        armedFile = null;
        refresh();
    }
});
window.addEventListener('resize', () => { closePicker(); closeMenu(); });

// ── Jump to the next / previous episode with no file ──
function flashRow(key) {
    const { row } = rowEls.get(key);
    row.classList.remove('ma-flash');
    void row.offsetWidth;
    row.classList.add('ma-flash');
}

function revealRow(key) {
    const group = groups.find(g => g.keys.includes(key));
    if (collapsed.delete(group.id) || !rowVisible(key, rowState(key))) {
        if (!rowVisible(key, rowState(key))) filter = 'all';
        refresh();
    }
    const { row } = rowEls.get(key);
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    flashRow(key);
    jumpCursor = orderedKeys.indexOf(key);
}

function jumpEmpty(direction) {
    const n = orderedKeys.length;
    for (let step = 1; step <= n; step += 1) {
        const index = ((jumpCursor + direction * step) % n + n) % n;
        const key = orderedKeys[index];
        if (rowState(key) === 'empty') {
            revealRow(key);
            return;
        }
    }
}

document.getElementById('ma-prev').addEventListener('click', () => jumpEmpty(-1));
document.getElementById('ma-next').addEventListener('click', () => jumpEmpty(1));
document.getElementById('ma-undo').addEventListener('click', undo);
document.getElementById('ma-ignore-empty').addEventListener('click', () => {
    change(orderedKeys.filter(k => rowState(k) === 'empty').map(k => [k, IGNORE]));
});

// ── Episodes with no file ──
function drawMissing(states) {
    const box = document.getElementById('ma-missing');
    const empty = orderedKeys.filter(k => states.get(k) === 'empty' && itemsByKey.get(k).type === 'episode');
    box.hidden = empty.length === 0;
    if (!empty.length) return;
    const unused = files.filter(f => !usage.has(f.filename) && !f.skip_reason).length;
    const noun = `episode${empty.length === 1 ? '' : 's'}`;
    box.replaceChildren(el('span', null, unused
        ? `No file picked for ${empty.length} ${noun}, and ${unused} file${unused === 1 ? ' is' : 's are'} still unused: `
        : `This pack has no file for ${empty.length} ${noun}: `));
    const shown = empty.slice(0, 12);
    shown.forEach((key, i) => {
        const link = el('button', 'ma-link', itemCode(itemsByKey.get(key)));
        link.type = 'button';
        link.addEventListener('click', () => revealRow(key));
        box.append(link);
        if (i < shown.length - 1) box.append(document.createTextNode(', '));
    });
    if (empty.length > shown.length) box.append(document.createTextNode(` and ${empty.length - shown.length} more`));
    box.append(document.createTextNode(unused
        ? '. Leave them empty or ignore them to skip them.'
        : ". They won't be added unless you pick a file."));
}

// ── Episode order ──
async function setupOrderPicker() {
    if (!data.is_show || !data.tmdb_id) return;
    const wrap = document.getElementById('ma-order');
    const select = document.getElementById('ma-order-select');
    const hint = document.getElementById('ma-order-hint');
    try {
        const response = await fetch(`${urls.episode_orders}?tmdb_id=${encodeURIComponent(data.tmdb_id)}`,
            { headers: { 'Accept': 'application/json' } });
        if (!response.ok) return;
        const result = await response.json();
        if (!result.success || !result.orders || result.orders.length < 2) return;
        const current = data.current_order || 'default';
        for (const order of result.orders) {
            const option = el('option', null, order.name);
            option.value = order.id;
            option.selected = order.id === current;
            select.append(option);
        }
        if (result.library_episodes > 0) {
            select.disabled = true;
            hint.textContent = `The show already has ${result.library_episodes} episodes in your library; change its order from the show page.`;
        } else {
            hint.textContent = 'Changing it re-runs the auto-match in that numbering.';
        }
        wrap.hidden = false;
        select.addEventListener('change', () => {
            const picked = select.value;
            const name = select.options[select.selectedIndex].textContent;
            const changed = undoStack.length > 0;
            const run = () => rematch(picked, () => { select.value = current; });
            if (!changed || !showPopup) { run(); return; }
            showPopup({
                type: POPUP_TYPES.CONFIRM,
                title: 'Change episode order?',
                message: `Matching runs again in ${escapeHtml(name)}, and the changes you made on this page are lost.`,
                confirmText: 'Change order',
                onConfirm: run,
                onCancel: () => { select.value = current; },
            });
        });
    } catch (error) {
        console.error('Error loading episode orders:', error);
    }
}

async function rematch(order, revert) {
    Loading.show();
    try {
        const response = await fetch(urls.rematch, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: JSON.stringify({ episode_order: order, context: { ...data.context, video_files: files } }),
        });
        const result = await response.json();
        if (response.ok && result.success) {
            window.location.href = result.redirect_url;
            return;
        }
        Loading.hide();
        revert();
        notify('error', 'Could not change order', result.error || 'Matching in that order failed.');
    } catch (error) {
        Loading.hide();
        revert();
        console.error('Error rematching:', error);
        notify('error', 'Could not change order', 'A network or unexpected error occurred.');
    }
}

function notify(kind, title, message) {
    if (!showPopup) return;
    showPopup({ type: kind === 'error' ? POPUP_TYPES.ERROR : POPUP_TYPES.SUCCESS, title,
                message: escapeHtml(message), autoClose: 5000 });
}

// ── Confirm ──
const submitButton = document.getElementById('confirm-assignment-btn');

async function submitAssignment() {
    submitButton.disabled = true;
    Loading.show();
    try {
        const response = await fetch(form.action, {
            method: 'POST',
            body: new FormData(form),
            headers: { 'Accept': 'application/json' },
        });
        const result = await response.json();
        Loading.hide();
        if (response.ok && result.success) {
            showPopup({
                type: POPUP_TYPES.SUCCESS,
                title: 'Assignment Successful',
                message: escapeHtml(result.message || `Successfully assigned ${result.added_count || 0} item(s).`),
                autoClose: 4000,
                onConfirm: () => { window.location.href = urls.done; },
            });
            setTimeout(() => { window.location.href = urls.done; }, 4000);
        } else {
            notify('error', 'Assignment Failed', result.error || 'An error occurred during assignment.');
            submitButton.disabled = false;
        }
    } catch (error) {
        Loading.hide();
        console.error('Error submitting assignment:', error);
        notify('error', 'Submission Error', 'A network or unexpected error occurred.');
        submitButton.disabled = false;
    }
}

form.addEventListener('submit', event => {
    event.preventDefault();
    refresh();
    const empty = orderedKeys.filter(k => rowState(k) === 'empty').length;
    const shared = [...usage.keys()].filter(isDuplicate).length;
    const problems = [];
    if (empty) problems.push(`${empty} item${empty === 1 ? ' has' : 's have'} no file and will be skipped.`);
    if (shared) problems.push(`${shared} file${shared === 1 ? ' is' : 's are'} given to more episodes than ${shared === 1 ? 'it holds' : 'they hold'}.`);
    if (!problems.length || !showPopup) { submitAssignment(); return; }
    showPopup({
        type: POPUP_TYPES.CONFIRM,
        title: 'Confirm anyway?',
        message: problems.map(escapeHtml).join('<br>'),
        confirmText: 'Confirm anyway',
        cancelText: 'Go back',
        onConfirm: submitAssignment,
    });
});

if (items.length) {
    buildRows();
    refresh();
    setupOrderPicker();
} else {
    document.getElementById('ma-toolbar').hidden = true;
    filesPanel.hidden = true;
}
