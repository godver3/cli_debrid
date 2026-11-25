// Top bar bug report (admins): the user describes the problem, the report adds
// their version and setup (/logs/api/bug-report/context, nothing personal) and
// links to freshly uploaded, redacted logs: Clyde's (plus the built-in
// mount engine's) and, with an external cli_mount, cli_mount's own. The result
// opens as a prefilled GitHub issue or is copied for Discord.
(function () {
    const GITHUB_NEW_ISSUE = 'https://github.com/godver3/cli_debrid/issues/new';
    const DISCORD_URL = 'https://discord.com/channels/691282102162948107';
    const DISCORD_LIMIT = 2000;
    const GITHUB_URL_LIMIT = 7500;

    let context = null;
    let report = null;

    const $ = id => document.getElementById(id);

    function setStatus(text, isError) {
        const el = $('bugReportStatus');
        el.textContent = text || '';
        el.classList.toggle('error', !!isError);
    }

    function open() {
        $('bugReportPopup').style.display = 'block';
        $('bugReportOverlay').style.display = 'block';
        document.body.classList.add('modal-open');
        showForm();
        loadContext();
        setTimeout(() => $('bugReportSummary').focus(), 50);
    }

    function close() {
        $('bugReportPopup').style.display = 'none';
        $('bugReportOverlay').style.display = 'none';
        document.body.classList.remove('modal-open');
    }

    function showForm() {
        $('bugReportForm').style.display = '';
        $('bugReportResult').style.display = 'none';
        $('bugReportCopied').textContent = '';
    }

    function loadContext() {
        const setup = $('bugReportSetup');
        fetch('/logs/api/bug-report/context')
            .then(r => r.json())
            .then(data => {
                if (!data.success) throw new Error(data.error || 'failed');
                context = data;
                setup.innerHTML = '';
                data.rows.forEach(row => {
                    const line = document.createElement('div');
                    const label = document.createElement('span');
                    label.className = 'bug-report-label';
                    label.textContent = row.label;
                    line.appendChild(label);
                    line.appendChild(document.createTextNode(row.value));
                    setup.appendChild(line);
                });
                const names = { engine: 'built-in mount', climount: 'cli_mount' };
                $('bugReportLogsWhich').textContent = '(' + ['Clyde'].concat((data.log_sources || []).map(k => names[k] || k)).join(' + ') + ')';
            })
            .catch(() => {
                context = null;
                setup.textContent = "Couldn't read your setup; the report will go without it.";
            });
    }

    function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

    // Starts an upload on the existing log-share endpoints and waits for its link.
    async function upload(url, body, label) {
        const start = await fetch(url, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
        }).then(r => r.json());
        if (!start.success || !start.task_id) throw new Error(start.error || `${label}: upload didn't start`);
        for (let i = 0; i < 300; i++) {
            await sleep(2000);
            const st = await fetch(`/logs/api/logs/share/status/${start.task_id}`).then(r => r.json());
            if (st.message) setStatus(`${label}: ${st.message}`);
            if (st.status === 'completed' && st.url) return st.url;
            if (st.status === 'failed') throw new Error(st.error || `${label}: upload failed`);
        }
        throw new Error(`${label}: upload took too long`);
    }

    async function create() {
        const summary = $('bugReportSummary').value.trim();
        const what = $('bugReportWhat').value.trim();
        if (!summary || !what) {
            setStatus('Add a summary and what happened.', true);
            return;
        }
        const button = $('bugReportCreate');
        button.disabled = true;
        const logs = [];
        const failures = [];
        try {
            if ($('bugReportLogs').checked) {
                const sources = (context && context.log_sources) || [];
                // Each log gets its own upload and link, all started together.
                const jobs = [['Clyde logs', '/logs/api/logs/share']];
                if (sources.includes('engine')) jobs.push(['Built-in mount logs', '/logs/api/engine/logs/share']);
                if (sources.includes('climount')) jobs.push(['cli_mount logs', '/logs/api/climount/logs/share']);
                const results = await Promise.allSettled(jobs.map(([label, url]) => upload(url, {}, label)));
                results.forEach((res, i) => {
                    if (res.status === 'fulfilled') logs.push([jobs[i][0], res.value]);
                    else failures.push(res.reason && res.reason.message ? res.reason.message : `${jobs[i][0]}: upload failed`);
                });
            }
            report = { summary, what, steps: $('bugReportSteps').value.trim(), logs };
            $('bugReportOutput').value = githubBody();
            $('bugReportForm').style.display = 'none';
            $('bugReportResult').style.display = '';
            setStatus('');
            $('bugReportCopied').textContent = failures.length ? `Some logs didn't upload: ${failures.join('; ')}` : '';
            $('bugReportCopied').classList.toggle('error', failures.length > 0);
        } finally {
            button.disabled = false;
        }
    }

    function setupLines(prefix) {
        return context ? context.rows.map(r => `${prefix}**${r.label}:** ${r.value}`) : [];
    }

    function githubBody() {
        const r = report;
        const out = ['### What happened', r.what, ''];
        if (r.steps) out.push('### When / how', r.steps, '');
        if (context) out.push('### Setup', ...setupLines('- '), '');
        if (r.logs.length) out.push('### Logs', ...r.logs.map(([label, url]) => `- ${label}: ${url}`), '');
        out.push('<sub>Made with the bug report button in Clyde.</sub>');
        return out.join('\n');
    }

    function discordText() {
        const r = report;
        const head = [`**Bug: ${r.summary}**`];
        const tail = [];
        if (context) tail.push('', ...setupLines('> '));
        if (r.logs.length) tail.push('', ...r.logs.map(([label, url]) => `${label}: <${url}>`));
        const fixed = head.join('\n').length + tail.join('\n').length + 4;
        let body = r.what + (r.steps ? `\n\n**When / how:** ${r.steps}` : '');
        const room = DISCORD_LIMIT - fixed;
        if (body.length > room) body = body.slice(0, Math.max(0, room - 1)) + '…';
        return [...head, body, ...tail].join('\n');
    }

    function copy(text, what) {
        const done = () => { $('bugReportCopied').classList.remove('error'); $('bugReportCopied').textContent = `Copied ${what}.`; };
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
        } else {
            fallbackCopy(text, done);
        }
    }

    // http:// installs have no clipboard API
    function fallbackCopy(text, done) {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); done(); } catch (e) { /* the text is in the box to copy by hand */ }
        document.body.removeChild(ta);
    }

    function openGithub() {
        let body = $('bugReportOutput').value;
        const make = b => `${GITHUB_NEW_ISSUE}?title=${encodeURIComponent(report.summary)}&body=${encodeURIComponent(b)}`;
        let url = make(body);
        if (url.length > GITHUB_URL_LIMIT) {
            // Too long for a link: the issue opens with the title and the body is copied to paste
            copy(body, 'the report (too long for the link; paste it into the issue)');
            url = make('');
        }
        window.open(url, '_blank', 'noopener');
    }

    document.addEventListener('DOMContentLoaded', () => {
        const button = $('st_bugReportButton');
        if (!button || !$('bugReportPopup')) return;
        button.addEventListener('click', open);
        $('bugReportClose').addEventListener('click', close);
        $('bugReportOverlay').addEventListener('click', close);
        $('bugReportCreate').addEventListener('click', create);
        $('bugReportBack').addEventListener('click', showForm);
        $('bugReportGithub').addEventListener('click', openGithub);
        $('bugReportCopyGithub').addEventListener('click', () => copy($('bugReportOutput').value, 'the GitHub report'));
        $('bugReportCopyDiscord').addEventListener('click', () => copy(discordText(), 'the Discord message'));
        $('bugReportDiscord').addEventListener('click', () => window.open(DISCORD_URL, '_blank', 'noopener'));
        document.addEventListener('keydown', e => {
            if (e.key === 'Escape' && $('bugReportPopup').style.display === 'block') close();
        });
    });
})();
