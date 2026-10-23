/* Moved from templates/calendar_view.html. */
(function () {
    // ── Preloaded data ──────────────────────────────────────────────────────
    const allGroupedEvents = window.pageData.calendar_view.grouped_events_json;
    const allWeeksMeta     = window.pageData.calendar_view.all_weeks_meta;
    const initialOffset    = window.pageData.calendar_view.week_offset;
    const todayISO         = window.pageData.calendar_view.today_date_isoformat;

    // ── State ───────────────────────────────────────────────────────────────
    let currentOffset = initialOffset;
    let mobileDay     = 0;
    let animating     = false;

    // ── Helpers ─────────────────────────────────────────────────────────────
    function weekMeta(offset) {
        return allWeeksMeta.find(w => w.offset === offset);
    }

    function esc(s) {
        return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    function eventCardHTML(ev) {
        const timeStr  = ev.time ? `<div class="cal-adv-event-time">${esc(ev.time)}</div>` : '';
        const typeStr  = ev.type === 'movie' ? 'Movie' : 'TV Episode';
        const statusStr = (ev.display_status || '').replace(/_/g,' ').replace(/\b\w/g, l => l.toUpperCase());
        const slug     = (ev.display_status || '').replace(/_/g,'-');
        // Store click data in data-attributes to avoid inline JSON quoting issues
        return `<div class="cal-adv-event cal-clickable-event event-type-${esc(ev.type)} status-${slug}"
                     style="cursor:pointer"
                     data-status="${esc(ev.display_status||'')}"
                     data-tmdb="${esc(ev.tmdb_id||'')}"
                     data-imdb="${esc(ev.imdb_id||'')}"
                     data-type="${esc(ev.type||'')}"
                     data-season="${ev.season_number != null ? ev.season_number : ''}"
                     data-episode="${ev.episode_number != null ? ev.episode_number : ''}">
            ${timeStr}
            <div class="cal-adv-event-title">${esc(ev.title)}</div>
            <div class="cal-adv-event-meta">
                <span class="cal-adv-event-type">${typeStr}</span>
                <span class="cal-adv-event-status status-${slug}">${statusStr}</span>
            </div>
        </div>`;
    }

    function buildTableHTML(meta) {
        const thead = meta.days.map(d => {
            const todayCls = d.is_today ? ' cal-adv-th-today' : '';
            const pillCls  = d.is_today ? ' cal-today-pill'   : '';
            const monthLbl = d.month_label ? `<span class="cal-th-month">${esc(d.month_label)}</span>` : '';
            return `<th class="cal-adv-th${todayCls}">
                <span class="cal-th-dayname">${esc(d.dayname)}</span>
                <span class="cal-th-daynum${pillCls}">${d.daynum}</span>
                ${monthLbl}
            </th>`;
        }).join('');

        const tcells = meta.days.map(d => {
            const cellCls = d.is_today ? ' cal-adv-cell-today' : '';
            const group   = allGroupedEvents[d.iso];
            let inner     = '<div class="cal-adv-empty"></div>';
            if (group && group.items && group.items.length) {
                const sorted = group.items.slice().sort((a,b) => new Date(a.sort_datetime) - new Date(b.sort_datetime));
                inner = sorted.map(eventCardHTML).join('');
            }
            return `<td class="cal-adv-cell${cellCls}">${inner}</td>`;
        }).join('');

        return `<table class="cal-adv-table">
            <thead><tr>${thead}</tr></thead>
            <tbody><tr class="cal-adv-body-row">${tcells}</tr></tbody>
        </table>`;
    }

    // ── Advanced grid render + slide ────────────────────────────────────────
    function renderAdvGrid(offset, direction) {
        const meta    = weekMeta(offset);
        if (!meta) return;
        const grid    = document.getElementById('cal-adv-grid');
        const viewport= grid && grid.parentNode;
        const titleEl = document.getElementById('cal-adv-title');
        const prevBtn = document.getElementById('cal-adv-prev');
        const nextBtn = document.getElementById('cal-adv-next');
        if (!grid) return;

        if (titleEl) titleEl.textContent = meta.header;
        if (prevBtn) prevBtn.disabled = offset <= -2;
        if (nextBtn) nextBtn.disabled = offset >= 2;

        // Update URL without reload
        const url = new URL(window.location.href);
        url.searchParams.set('week_offset', offset);
        history.replaceState({weekOffset: offset}, '', url.toString());

        if (!direction || animating) {
            grid.innerHTML = buildTableHTML(meta);
            return;
        }

        animating = true;
        const DURATION = 320;

        // Lock viewport height to current content so layout doesn't jump
        viewport.style.height = grid.offsetHeight + 'px';

        // Build incoming panel off-screen
        const incoming = document.createElement('div');
        incoming.className = 'cal-slide-incoming';
        incoming.innerHTML = buildTableHTML(meta);

        // Position both panels: outgoing stays where it is, incoming starts off-screen
        const fromClass = direction === 'next' ? 'cal-slide-from-right' : 'cal-slide-from-left';
        const toClass   = direction === 'next' ? 'cal-slide-to-left'    : 'cal-slide-to-right';

        incoming.classList.add(fromClass);
        grid.classList.add('cal-animating');
        viewport.appendChild(incoming);

        // Force reflow so initial positions are painted before transitions start
        incoming.getBoundingClientRect();

        // Trigger transitions simultaneously
        incoming.classList.remove(fromClass);
        incoming.classList.add('cal-slide-to-center');
        grid.classList.add(toClass);

        setTimeout(() => {
            // Swap: replace grid content, remove incoming, restore layout
            grid.innerHTML = buildTableHTML(meta);
            grid.className = 'cal-adv-grid-inner';
            incoming.remove();
            viewport.style.height = '';
            animating = false;
        }, DURATION);
    }

    function calAdvNav(dir) {
        const next = currentOffset + dir;
        if (next < -2 || next > 2) return;
        currentOffset = next;
        renderAdvGrid(currentOffset, dir > 0 ? 'next' : 'prev');
        // Sync mobile day to same relative position
        calMobileRenderForOffset(currentOffset);
    }

    // ── Mobile ───────────────────────────────────────────────────────────────
    function calMobileRenderForOffset(offset) {
        const meta = weekMeta(offset);
        if (!meta) return;
        // Keep same day-of-week index; clamp
        const iso    = meta.days[mobileDay] ? meta.days[mobileDay].iso : meta.days[0].iso;
        const label  = meta.days[mobileDay] ? meta.days[mobileDay].dayname + ', ' + new Date(iso + 'T12:00:00').toLocaleDateString('en-US',{month:'short',day:'numeric'}) : '';
        _renderMobileDay(iso, label, offset);
    }

    function calMobileRender() {
        calMobileRenderForOffset(currentOffset);
    }

    function _renderMobileDay(iso, label, offset) {
        const title  = document.getElementById('cal-mobile-day-title');
        const body   = document.getElementById('cal-mobile-day-body');
        const prevBtn= document.getElementById('cal-day-prev');
        const nextBtn= document.getElementById('cal-day-next');
        if (!title) return;

        title.textContent = iso === todayISO ? 'Today, ' + label.split(', ').slice(1).join(', ') : label;
        if (prevBtn) prevBtn.disabled = mobileDay === 0 && offset <= -2;
        if (nextBtn) nextBtn.disabled = mobileDay === 6 && offset >= 2;

        const group = allGroupedEvents[iso];
        if (!group || !group.items || !group.items.length) {
            body.innerHTML = '<p class="cal-mobile-no-events">No events</p>';
            return;
        }
        const sorted = group.items.slice().sort((a,b) => new Date(a.sort_datetime) - new Date(b.sort_datetime));
        body.innerHTML = sorted.map(eventCardHTML).join('');
    }

    function calDayNav(dir) {
        const newDay = mobileDay + dir;
        if (newDay < 0) {
            if (currentOffset > -2) { currentOffset--; mobileDay = 6; calMobileRenderForOffset(currentOffset); }
            return;
        }
        if (newDay > 6) {
            if (currentOffset < 2) { currentOffset++; mobileDay = 0; calMobileRenderForOffset(currentOffset); }
            return;
        }
        mobileDay = newDay;
        calMobileRender();
    }

    // ── Event click ─────────────────────────────────────────────────────────
    function calEventClick(status, tmdbId, imdbId, type, seasonNumber, episodeNumber) {
        const collected = ['collected', 'upgrading', 'checking_upgrade'];
        const mediaType = type === 'movie' ? 'movie' : 'tv';
        let params = '';
        if (seasonNumber && type !== 'movie') {
            params = `?season=${seasonNumber}`;
            if (episodeNumber) params += `&episode=${episodeNumber}`;
        }
        if (collected.includes(status)) {
            const libBase = type === 'movie' ? '/library/' : `/library/show/${tmdbId || imdbId}`;
            window.location.href = libBase + params;
        } else if (tmdbId && tmdbId !== 'None' && tmdbId !== 'null') {
            window.location.href = `/discover/details/${tmdbId}/${mediaType}${params}`;
        } else if (imdbId && imdbId !== 'None' && imdbId !== 'null') {
            window.location.href = `/discover/?search=${imdbId}`;
        }
    }
    window.calEventClick = calEventClick;

    // ── View toggle ─────────────────────────────────────────────────────────
    function calSetView(mode) {
        localStorage.setItem('calViewMode', mode);
        document.getElementById('cal-simple-view').style.display  = mode === 'simple'   ? '' : 'none';
        document.getElementById('cal-advanced-view').style.display = mode === 'advanced' ? '' : 'none';
        document.getElementById('cal-toggle-simple').classList.toggle('active',   mode === 'simple');
        document.getElementById('cal-toggle-advanced').classList.toggle('active', mode === 'advanced');
        if (mode === 'advanced') {
            renderAdvGrid(currentOffset, null);
            calMobileRender();
        }
    }
    window.calSetView = calSetView;

    // ── Init ────────────────────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', function () {
        // Advanced nav
        const advPrev = document.getElementById('cal-adv-prev');
        const advNext = document.getElementById('cal-adv-next');
        if (advPrev) advPrev.addEventListener('click', () => calAdvNav(-1));
        if (advNext) advNext.addEventListener('click', () => calAdvNav(1));

        // Delegated click for JS-rendered event cards (survives grid rebuilds)
        const advancedView = document.getElementById('cal-advanced-view');
        if (advancedView) {
            advancedView.addEventListener('click', function(e) {
                const card = e.target.closest('.cal-clickable-event');
                if (!card) return;
                const s  = card.dataset.season;
                const ep = card.dataset.episode;
                calEventClick(
                    card.dataset.status,
                    card.dataset.tmdb,
                    card.dataset.imdb,
                    card.dataset.type,
                    s  ? parseInt(s)  : null,
                    ep ? parseInt(ep) : null
                );
            });
        }

        // Mobile nav
        const dayPrev = document.getElementById('cal-day-prev');
        const dayNext = document.getElementById('cal-day-next');
        if (dayPrev) dayPrev.addEventListener('click', () => calDayNav(-1));
        if (dayNext) dayNext.addEventListener('click', () => calDayNav(1));

        // Start mobile on today if visible in current week
        const meta = weekMeta(currentOffset);
        if (meta) {
            const todayIdx = meta.days.findIndex(d => d.iso === todayISO);
            if (todayIdx >= 0) mobileDay = todayIdx;
        }

        // ── Modal for simple-view grid cells ────────────────────────────────
        const modal        = document.getElementById('day-events-modal');
        const modalOverlay = document.getElementById('day-events-modal-overlay');
        const modalTitle   = document.getElementById('day-events-modal-title');
        const modalBody    = document.getElementById('day-events-modal-body');
        const modalClose   = document.getElementById('day-events-modal-close');
        const noModalMsg   = modal.querySelector('.no-modal-events');

        document.querySelectorAll('.calendar-day-cell').forEach(cell => {
            cell.addEventListener('click', function () {
                const dateISO    = this.dataset.dateiso;
                const displayDate= this.dataset.displaydate;
                modalTitle.textContent = 'Events for ' + displayDate;
                modalBody.innerHTML = '';
                noModalMsg.style.display = 'none';

                const group = allGroupedEvents[dateISO];
                if (group && group.items && group.items.length) {
                    const sorted = group.items.slice().sort((a,b) => new Date(a.sort_datetime) - new Date(b.sort_datetime));
                    const ul = document.createElement('ul');
                    ul.className = 'modal-event-list';
                    sorted.forEach(ev => {
                        const li  = document.createElement('li');
                        const slug= (ev.display_status||'').replace(/_/g,'-');
                        li.className = `modal-event-item event-type-${ev.type} status-${slug}`;
                        const timeStr  = ev.time || '<span class="all-day-indicator">All Day</span>';
                        const typeStr  = ev.type === 'movie' ? 'Movie' : 'TV Episode';
                        const statusStr= (ev.display_status||'').replace(/_/g,' ').replace(/\b\w/g,l=>l.toUpperCase());
                        li.innerHTML = `
                            <div class="modal-event-time">${timeStr}</div>
                            <div class="modal-event-details">
                                <span class="modal-event-title">${esc(ev.title)}</span>
                                <span class="modal-event-category">${typeStr}</span>
                                <span class="modal-event-status-badge status-${slug}">${statusStr}</span>
                            </div>`;
                        ul.appendChild(li);
                    });
                    modalBody.appendChild(ul);
                } else {
                    noModalMsg.style.display = 'block';
                }
                modal.style.display = 'flex';
                modalOverlay.style.display = 'block';
                document.body.classList.add('modal-open');
            });
        });

        function closeModal() {
            modal.style.display = 'none';
            modalOverlay.style.display = 'none';
            document.body.classList.remove('modal-open');
        }
        modalClose.addEventListener('click', closeModal);
        modalOverlay.addEventListener('click', closeModal);
        document.addEventListener('keydown', e => { if (e.key === 'Escape' && modal.style.display === 'flex') closeModal(); });

        // Apply saved view (renders the advanced grid if needed)
        const saved = localStorage.getItem('calViewMode') || 'simple';
        calSetView(saved);
    });
})();
