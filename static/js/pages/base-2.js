/* Moved from templates/base.html. */
// Reset scroll to top on page load (sticky nav gets cut off by browser scroll restore)
if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
window.scrollTo(0, 0);
// Tangerine nav JS (prefixed st_ to avoid conflicts)
function stToggleDD(id) {
    document.querySelectorAll('.st-nav-item.open').forEach(function(el) {
        if (el.id !== id) el.classList.remove('open');
    });
    var el = document.getElementById(id);
    if (el) el.classList.toggle('open');
}
document.addEventListener('click', function(e) {
    if (!e.target.closest('.st-nav-item')) {
        document.querySelectorAll('.st-nav-item.open').forEach(function(el) { el.classList.remove('open'); });
    }
});
function stToggleDrawer() {
    var d = document.getElementById('st-nav-drawer');
    if (d) d.classList.toggle('open');
}
function stCloseDrawer(e) {
    if (!e.target.closest('.st-nav-drawer-inner')) {
        var d = document.getElementById('st-nav-drawer');
        if (d) d.classList.remove('open');
    }
}
function stToggleAcc(id) {
    var sec = document.getElementById(id);
    if (!sec) return;
    var isOpen = sec.classList.contains('open');
    // Close all sections first
    document.querySelectorAll('.st-nav-acc-section').forEach(function(s) {
        s.classList.remove('open');
    });
    // If it wasn't open, open it; if it was open, leave it closed (toggle)
    if (!isOpen) sec.classList.add('open');
}
// Wire st_ buttons to the original hidden buttons (for program_controls.js compatibility)
document.addEventListener('DOMContentLoaded', function() {
    // Sync notifications button
    var stNotifBtn = document.getElementById('st_notifications_button');
    var origNotifBtn = document.getElementById('notifications_button');
    if (stNotifBtn && origNotifBtn) {
        stNotifBtn.addEventListener('click', function() { origNotifBtn.click(); });
        // Mirror has-notifications class from ALL #notifications_button elements
        var allOrigNotifBtns = document.querySelectorAll('#notifications_button');
        var stNotifCount = document.getElementById('st_notif_count');
        function syncNotifIndicator() {
            var hasIt = Array.from(allOrigNotifBtns).some(function(b) { return b.classList.contains('has-notifications'); });
            stNotifBtn.classList.toggle('has-notifications', hasIt);
            if (stNotifCount) {
                var count = document.querySelectorAll('.notification.unread').length;
                if (count > 0) {
                    stNotifCount.textContent = count > 99 ? '99+' : count;
                    stNotifCount.style.display = '';
                } else {
                    stNotifCount.style.display = 'none';
                }
            }
        }
        allOrigNotifBtns.forEach(function(btn) {
            new MutationObserver(syncNotifIndicator).observe(btn, { attributes: true, attributeFilter: ['class'] });
        });
        // Fallback poll every 500ms in case observer misses initial set
        setInterval(function() { if (!document.hidden) syncNotifIndicator(); }, 500);
    }
    // Sync release notes button
    var stRelBtn = document.getElementById('st_releaseNotesButton');
    var origRelBtn = document.getElementById('releaseNotesButton');
    if (stRelBtn && origRelBtn) {
        stRelBtn.addEventListener('click', function() { origRelBtn.click(); });
    }
    // Sync logout button
    var stLogoutBtn = document.getElementById('st_logout_button');
    var origLogoutBtn = document.getElementById('logout_button');
    if (stLogoutBtn && origLogoutBtn) {
        stLogoutBtn.addEventListener('click', function() { origLogoutBtn.click(); });
    }
    // Sync program control button
    var stProgBtn = document.getElementById('st_programControlButton');
    var origProgBtn = document.getElementById('programControlButton');
    if (stProgBtn && origProgBtn) {
        stProgBtn.addEventListener('click', function() { origProgBtn.click(); });
        // Mirror status updates from original to st_ button
        var observer = new MutationObserver(function() {
            stProgBtn.className = origProgBtn.className.replace(/\bicon-button\b/g, 'st-nav-act-btn')
                .replace(/\bstop-program\b/g, 'st-btn-stop')
                .replace(/\bstart-program\b/g, 'st-btn-start')
                .replace(/\bstarting-program\b/g, 'st-btn-starting')
                .replace(/\bstopping-program\b/g, 'st-btn-stopping');
            stProgBtn.disabled = origProgBtn.disabled;
            stProgBtn.innerHTML = origProgBtn.innerHTML;
        });
        observer.observe(origProgBtn, { attributes: true, childList: true, subtree: true });
    }
    // Drawer search wires to existing nav search form
    var stSearch = document.getElementById('stNavSearch');
    var origSearch = document.getElementById('navSearch');
    if (stSearch && origSearch) {
        stSearch.addEventListener('input', function() { origSearch.value = stSearch.value; });
        stSearch.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                origSearch.value = stSearch.value;
                document.getElementById('nav-search-form').dispatchEvent(new Event('submit'));
            }
        });
    }

    // Active page highlighting + section auto-expand in mobile drawer
    (function() {
        var drawer = document.getElementById('st-nav-drawer');
        if (!drawer) return;

        var currentPath = window.location.pathname;
        var allItems = drawer.querySelectorAll('.st-nav-mob-item');
        var matchedSection = null;

        allItems.forEach(function(item) {
            var href = item.getAttribute('href');
            if (!href) return;
            var linkPath;
            try { linkPath = new URL(href, window.location.origin).pathname; } catch(e) { return; }
            if (linkPath === currentPath) {
                item.classList.add('active');
                var section = item.closest('.st-nav-acc-section');
                if (section) {
                    section.classList.add('open');
                    matchedSection = section.id;
                }
            }
        });

        // If we found the active page, save it; otherwise restore from localStorage
        if (matchedSection) {
            try { localStorage.setItem('st-nav-expanded', matchedSection); } catch(e) {}
        } else {
            try {
                var saved = localStorage.getItem('st-nav-expanded');
                if (saved) {
                    var sec = document.getElementById(saved);
                    if (sec) sec.classList.add('open');
                }
            } catch(e) {}
        }

        // Save section when user manually opens one
        var origStToggleAcc = window.stToggleAcc;
        window.stToggleAcc = function(id) {
            origStToggleAcc(id);
            var sec = document.getElementById(id);
            if (sec && sec.classList.contains('open')) {
                try { localStorage.setItem('st-nav-expanded', id); } catch(e) {}
            }
        };
    })();

    // ── FAB: AI Butler + Help auto-hide ──────────────
    (function() {
        var fabGroup = document.getElementById('stFabGroup');
        if (!fabGroup) return;
        var fabHideTimer;
        function stFabShow() {
            fabGroup.classList.add('fab-visible');
            clearTimeout(fabHideTimer);
            fabHideTimer = setTimeout(function() { fabGroup.classList.remove('fab-visible'); }, 3000);
        }
        document.addEventListener('mousemove', stFabShow);
        document.addEventListener('touchstart', stFabShow);
        document.addEventListener('keydown', stFabShow);
        fabGroup.addEventListener('mouseenter', function() {
            clearTimeout(fabHideTimer);
            fabGroup.classList.add('fab-visible');
        });
        fabGroup.addEventListener('mouseleave', function() {
            fabHideTimer = setTimeout(function() { fabGroup.classList.remove('fab-visible'); }, 1500);
        });
        // Wire AI FAB to existing AI butler toggle
        var fabAI = document.getElementById('stFabAI');
        var origAI = document.getElementById('ai-butler-toggle');
        if (fabAI && origAI) {
            fabAI.addEventListener('click', function() { origAI.click(); });
        }
        // Wire Help FAB to existing help button
        var fabHelp = document.getElementById('stFabHelp');
        var origHelp = document.getElementById('helpButton');
        if (fabHelp && origHelp) {
            fabHelp.addEventListener('click', function() { origHelp.click(); });
        }
    })();

    // ── Task Ticker + Monitor ────────────────────────
    (function() {
        var tickerEl = document.getElementById('stTaskTicker');
        var monitorEl = document.getElementById('stTaskMonitor');
        var expandBtn = document.getElementById('stTickerExpandBtn');
        var tickerName = document.getElementById('stTickerName');
        var tickerStatus = document.getElementById('stTickerStatus');
        var tmScroll = document.getElementById('stTmScroll');
        var navTaskBtn = document.getElementById('stNavTaskBtn');
        if (!tickerEl) return;

        var monitorOpen = false;
        var tickerHideTimer;

        // Translate #taskList DOM (task-item/task-list-header) → st-tm-* HTML
        function stBuildMonitorHTML() {
            var origTaskList = document.getElementById('taskList');
            if (!origTaskList || !origTaskList.children.length) return '<div class="st-tm-empty">Loading tasks…</div>';
            var html = '';
            var nodes = origTaskList.children;
            for (var i = 0; i < nodes.length; i++) {
                var node = nodes[i];
                if (node.classList.contains('task-list-header')) {
                    html += '<div class="st-tm-section">' + node.textContent + '</div>';
                } else if (node.classList.contains('task-item')) {
                    var isRunning = node.classList.contains('running');
                    var nameEl = node.querySelector('.task-name');
                    var timingEls = node.querySelectorAll('.task-timing span');
                    var sub = timingEls[0] ? timingEls[0].textContent : '';
                    var badge = timingEls[1] ? timingEls[1].textContent : '';
                    html += '<div class="st-tm-task' + (isRunning ? ' running' : '') + '">' +
                        '<div class="st-tm-task-name">' + (nameEl ? nameEl.textContent : '') + '</div>' +
                        '<div class="st-tm-task-meta">' +
                        '<span class="st-tm-task-sub">' + sub + '</span>' +
                        '<span class="st-tm-task-badge ' + (isRunning ? 'st-tm-badge-running' : 'st-tm-badge-sched') + '">' + badge + '</span>' +
                        '</div></div>';
                } else if (node.classList.contains('no-tasks') || node.classList.contains('error') || node.classList.contains('loading')) {
                    html += '<div class="st-tm-empty">' + node.textContent + '</div>';
                }
            }
            return html || '<div class="st-tm-empty">No tasks</div>';
        }

        // Called whenever #currentTaskDisplay or #taskList changes
        function stSyncFromDisplay() {
            var origDisplay = document.getElementById('currentTaskDisplay');
            if (!origDisplay) return;
            var nameEl = origDisplay.querySelector('.current-task-name');
            var timeEl = origDisplay.querySelector('.current-task-time');
            var isRunning = origDisplay.classList.contains('running');
            var name = nameEl ? nameEl.textContent.trim() : '';
            var time = timeEl ? timeEl.textContent.trim() : '';

            // Update ticker pill text
            if (tickerName) tickerName.textContent = name || 'Loading…';
            if (tickerStatus) tickerStatus.textContent = time;

            // Show/hide pulsing dot on nav btn
            if (navTaskBtn) {
                navTaskBtn.classList.toggle('no-tasks', !isRunning);
            }

            // Auto-show ticker when a task is running
            if (isRunning && name) {
                tickerEl.classList.add('visible');
                clearTimeout(tickerHideTimer);
            }

            // Refresh monitor panel if open
            if (monitorOpen && tmScroll) {
                tmScroll.innerHTML = stBuildMonitorHTML();
            }
        }

        // Observe #currentTaskDisplay for class/text changes
        var origDisplay = document.getElementById('currentTaskDisplay');
        if (origDisplay) {
            new MutationObserver(stSyncFromDisplay).observe(origDisplay, { attributes: true, childList: true, subtree: true, characterData: true });
        }
        // Observe #taskList for content changes (re-render monitor)
        var origTaskList = document.getElementById('taskList');
        if (origTaskList) {
            new MutationObserver(function() {
                if (monitorOpen && tmScroll) tmScroll.innerHTML = stBuildMonitorHTML();
            }).observe(origTaskList, { childList: true, subtree: true });
        }

        // Nav task btn — toggle ticker
        window.stShowTicker = function() {
            if (tickerEl.classList.contains('visible') && !monitorOpen) {
                tickerEl.classList.remove('visible');
                return;
            }
            tickerEl.classList.add('visible');
            clearTimeout(tickerHideTimer);
            tickerHideTimer = setTimeout(function() {
                if (!monitorOpen) tickerEl.classList.remove('visible');
            }, 8000);
        };

        // Expand/collapse monitor
        window.stToggleTaskMonitor = function() {
            if (!monitorEl) return;
            monitorOpen = !monitorOpen;
            monitorEl.classList.toggle('open', monitorOpen);
            if (expandBtn) expandBtn.classList.toggle('open', monitorOpen);
            if (monitorOpen) {
                clearTimeout(tickerHideTimer);
                tickerEl.classList.add('visible');
                if (tmScroll) tmScroll.innerHTML = stBuildMonitorHTML();
            } else {
                var d = document.getElementById('currentTaskDisplay');
                if (!d || !d.classList.contains('running')) {
                    tickerHideTimer = setTimeout(function() { tickerEl.classList.remove('visible'); }, 4000);
                }
            }
        };

        // Close monitor on outside click
        document.addEventListener('click', function(e) {
            if (!monitorOpen) return;
            if (!e.target.closest('#stTaskMonitor') &&
                !e.target.closest('#stTaskTicker') &&
                !e.target.closest('#stNavTaskBtn')) {
                monitorOpen = false;
                monitorEl.classList.remove('open');
                if (expandBtn) expandBtn.classList.remove('open');
                var d = document.getElementById('currentTaskDisplay');
                if (!d || !d.classList.contains('running')) {
                    tickerHideTimer = setTimeout(function() { tickerEl.classList.remove('visible'); }, 4000);
                }
            }
        });

        // Run once immediately in case task_monitor.js already populated the DOM
        stSyncFromDisplay();

        // Replace "Interval:" with "Intvl:" in task display
        (function() {
            function shortenInterval(root) {
                root.querySelectorAll('.task-timing span, .current-task-time').forEach(function(el) {
                    if (el.textContent.includes('Interval:')) {
                        el.textContent = el.textContent.replace(/Interval:/g, 'Intvl:');
                    }
                });
            }
            var taskListEl = document.getElementById('taskList');
            var taskDisplayEl = document.getElementById('currentTaskDisplay');
            [taskListEl, taskDisplayEl].forEach(function(el) {
                if (!el) return;
                shortenInterval(el);
                new MutationObserver(function() { shortenInterval(el); })
                    .observe(el, { childList: true, subtree: true, characterData: true });
            });
        })();
    })();

});
