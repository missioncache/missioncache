// The resume lists as rows: Saved and Closed tabs, a search over both, and
// the row actions (copy the command, keep, rename, remove in two clicks).
// The dashboard's Sessions view and the iTerm2 Saved Tabs panel both draw
// with this, each passing how it reads the rows and posts an action.
//
// const view = ResumeRows({
//   ids: {root, error, tabs, sr},         element ids
//   fetchRows: async () => rows,          rows as resume_sessions returns them
//   post: async (sid, action, body) => {} throws Error with a message on failure
//   openRow: async row => {},             optional: adds Open, and a click on the row opens it
//   pin: false,                           optional: adds Pin / Unpin on Saved rows
//   copy: true, pollMs: 15000, onLoaded: () => {},
// });
// view.start() / view.stop() / view.setTab(tab) / view.setQuery(q)
//
// loadError belongs to the last read and the next read clears it. actError
// belongs to the last action and stays until the next one. A rename in
// progress holds the refresh.
(function () {
    const CLAUDE_ICON = `<svg class="rs-icon claude" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;
    const esc = t => String(t ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    // Free text is isolated, so a Hebrew run never drags a time or a
    // separator next to it to the other end of the line.
    const bidi = t => `<bdi dir="auto">${esc(t)}</bdi>`;
    const shortPath = p => String(p || '').replace(/^(\/Users\/[^/]+|\/home\/[^/]+|[A-Za-z]:\\Users\\[^\\]+)/, '~');
    // The table's stamps are local time, "YYYY-MM-DD HH:MM:SS".
    function ago(stamp) {
        const t = stamp ? new Date(String(stamp).replace(' ', 'T')).getTime() : NaN;
        if (!t) return '';
        const min = Math.max(0, Math.floor((Date.now() - t) / 60000));
        if (min < 1) return 'just now';
        if (min < 60) return `${min}m ago`;
        if (min < 48 * 60) return `${Math.floor(min / 60)}h ago`;
        return `${Math.floor(min / 1440)}d ago`;
    }

    window.ResumeRows = function (cfg) {
        const s = { tab: 'saved', q: '', rows: [], loaded: false, timer: null, armed: null, editing: null,
                    loadError: null, actError: null };
        const el = name => document.getElementById(cfg.ids[name]);
        const copy = cfg.copy !== false;

        function matches(r) {
            if (!s.q) return true;
            return [r.name, r.mc_title, r.project_name, r.cwd].some(v => String(v || '').toLowerCase().includes(s.q));
        }
        function button(act, sid, label, extra, title) {
            const tip = title ? ` title="${esc(title)}"` : '';
            return `<button class="rs-act${extra || ''}"${tip} data-act="${act}" data-sid="${sid}" data-focus="${act}:${sid}">${label}</button>`;
        }
        function rowHtml(r) {
            const sid = esc(r.session_id);
            const icon = r.origin === 'missioncache'
                ? '<span class="rs-icon mc" role="img" aria-label="MissionCache project" title="MissionCache project"></span>'
                : CLAUDE_ICON;
            const name = s.editing === r.session_id
                ? `<input class="rs-rename" data-sid="${sid}" data-focus="rename:${sid}" value="${esc(r.name)}" aria-label="New name, Enter saves, Escape cancels"
                     dir="auto" maxlength="200">`
                : `<div class="rs-name" dir="auto">${r.pinned ? '<span class="rs-pin" title="Pinned">📌</span>' : ''}${bidi(r.name)}</div>`;
            const meta = [
                r.project_name ? `<span>${bidi(r.project_name)}</span>` : '',
                `<span class="rs-path" title="${esc(r.cwd)}">${esc(shortPath(r.cwd))}</span>`,
                r.open ? '<span class="rs-badge">open</span>' : `<span title="${esc(r.closed_at || '')}">${esc(ago(r.closed_at))}</span>`,
                r.transcript_gone ? '<span class="rs-badge warn" title="Claude Code deleted the transcript">transcript gone</span>' : '',
            ].join('');
            const armed = s.armed === r.session_id;
            const actions = [
                cfg.openRow && (r.command || r.live) ? button('open', sid, 'Open', ' primary') : '',
                copy && r.command ? button('copy', sid, 'Copy command', '', r.command) : '',
                r.list === 'closed' ? button('keep', sid, 'Keep') : '',
                cfg.pin && r.list === 'saved' ? button(r.pinned ? 'unpin' : 'pin', sid, r.pinned ? 'Unpin' : 'Pin') : '',
                button('rename', sid, 'Rename'),
                button('forget', sid, armed ? 'Remove?' : 'Remove', armed ? ' armed' : ''),
            ].join('');
            return `<div class="rs-row${cfg.openRow ? ' clickable' : ''}" data-sid="${sid}">${icon}<div class="rs-main">${name}<div class="rs-meta">${meta}</div></div><div class="rs-actions">${actions}</div></div>`;
        }
        function render() {
            const root = el('root');
            if (!root) return;
            const err = el('error');
            const error = s.actError || s.loadError;
            err.hidden = !error;
            // A role=alert speaks on every change, so a load error that
            // lasts across polls is written once.
            if (err.textContent !== (error || '')) err.textContent = error || '';
            const saved = s.rows.filter(r => r.list === 'saved' && matches(r));
            const closed = s.rows.filter(r => r.list === 'closed' && matches(r));
            const tabs = el('tabs');
            tabs.hidden = !!s.q;
            const counts = { saved: 0, closed: 0 };
            s.rows.forEach(r => { counts[r.list]++; });
            tabs.querySelectorAll('.tab-btn').forEach(b => {
                const on = b.dataset.tab === s.tab;
                b.classList.toggle('active', on);
                b.setAttribute('aria-selected', on ? 'true' : 'false');
                b.textContent = `${b.dataset.tab === 'saved' ? 'Saved' : 'Closed'} (${counts[b.dataset.tab]})`;
            });
            let html;
            if (!s.loaded) {
                html = s.loadError ? '' : '<div class="empty-state"><div class="empty-state-text">Loading the sessions...</div></div>';
            } else if (s.q) {
                html = (saved.length ? `<div class="rs-heading">Saved</div>${saved.map(rowHtml).join('')}` : '')
                    + (closed.length ? `<div class="rs-heading">Closed</div>${closed.map(rowHtml).join('')}` : '');
                if (!html) html = '<div class="empty-state"><div class="empty-state-text">No session matches.</div></div>';
            } else {
                const rows = s.tab === 'saved' ? saved : closed;
                html = rows.length ? rows.map(rowHtml).join('')
                    : `<div class="empty-state"><div class="empty-state-text">${s.tab === 'saved'
                        ? 'Nothing saved yet. A session bound to a project goes here when it closes, and Keep moves one from Closed.'
                        : 'No closed sessions yet. Every Claude Code session shows up here when it ends.'}</div></div>`;
            }
            // A render replaces every row, so it carries over what the
            // person was doing: the focused button and a name being typed.
            // Only while the page has the keyboard: in an iTerm2 panel,
            // focus() would pull it out of the terminal being typed in.
            const active = document.activeElement;
            const mine = document.hasFocus();
            const focusKey = mine && root.contains(active) ? active.dataset.focus : null;
            const typing = root.querySelector('.rs-rename');
            const typed = typing && typing.dataset.sid === s.editing
                ? { value: typing.value, start: typing.selectionStart, end: typing.selectionEnd, had: active === typing } : null;
            root.innerHTML = html;
            const input = root.querySelector('.rs-rename');
            if (input && typed) {
                input.value = typed.value;
                if (typed.had && mine) { input.focus(); input.setSelectionRange(typed.start, typed.end); }
            } else if (input && mine) {
                input.focus();
                input.select();
            } else if (focusKey) {
                const again = [...root.querySelectorAll('[data-focus]')].find(e => e.dataset.focus === focusKey);
                if (again) again.focus();
            }
        }
        async function load() {
            try {
                s.rows = await cfg.fetchRows();
                s.loaded = true;
                s.loadError = null;
            } catch (e) {
                s.loadError = `Could not load the sessions: ${e.message || e}`;
            }
            render();
            if (cfg.onLoaded) cfg.onLoaded();
        }
        function announce(text) {
            const sr = el('sr');
            if (sr) sr.textContent = text;
        }
        const DONE = { keep: 'Moved {} to Saved', forget: 'Removed {}', pin: 'Pinned {}', unpin: 'Unpinned {}', open: 'Opened {}' };
        async function act(act, sid, button) {
            const row = s.rows.find(r => r.session_id === sid);
            if (!row) return;
            s.actError = null;
            try {
                if (act === 'copy') {
                    await navigator.clipboard.writeText(row.command);
                    button.textContent = 'Copied';
                    announce(`Copied the command for ${row.name}. Paste it in a terminal.`);
                    setTimeout(() => { button.textContent = 'Copy command'; }, 1500);
                    return;
                }
                if (act === 'rename') {
                    s.editing = sid;
                    render();
                    return;
                }
                if (act === 'forget' && s.armed !== sid) {
                    s.armed = sid;
                    render();
                    announce(`Press Remove again to remove ${row.name}`);
                    setTimeout(() => { if (s.armed === sid) { s.armed = null; render(); } }, 3000);
                    return;
                }
                s.armed = null;
                if (button) button.disabled = true;
                if (act === 'open') await cfg.openRow(row);
                else await cfg.post(sid, act);
                announce(DONE[act].replace('{}', row.name));
                await load();
            } catch (e) {
                s.actError = `Could not ${act === 'forget' ? 'remove' : act} ${row.name}: ${e.message || e}`;
                if (button) button.disabled = false;
                render();
            }
        }
        async function renameDone(input, save) {
            if (s.editing !== input.dataset.sid) return;
            s.editing = null;
            s.actError = null;
            if (save) {
                try {
                    await cfg.post(input.dataset.sid, 'rename', { name: input.value });
                    announce('Renamed');
                } catch (e) {
                    s.actError = `Could not rename: ${e.message || e}`;
                }
                await load();
            } else {
                render();
            }
        }
        document.addEventListener('click', e => {
            const root = el('root');
            if (!root || !e.target.closest || !root.contains(e.target)) return;
            const b = e.target.closest('.rs-act');
            if (b) { act(b.dataset.act, b.dataset.sid, b); return; }
            // With Open, a click anywhere else on a row opens it, as in the panel.
            const row = cfg.openRow && !e.target.closest('input') && e.target.closest('.rs-row');
            if (row && row.querySelector('[data-act="open"]')) act('open', row.dataset.sid, row.querySelector('[data-act="open"]'));
        });
        document.addEventListener('keydown', e => {
            const input = e.target.closest && e.target.closest('.rs-rename');
            if (!input || !el('root') || !el('root').contains(input)) return;
            if (e.key === 'Enter') { e.preventDefault(); renameDone(input, true); }
            if (e.key === 'Escape') { e.preventDefault(); renameDone(input, false); }
        });
        return {
            load,
            render,
            start() {
                if (s.timer) return;
                load();
                s.timer = setInterval(() => { if (!document.hidden && !s.editing) load(); }, cfg.pollMs || 15000);
            },
            stop() {
                clearInterval(s.timer);
                s.timer = null;
            },
            setTab(tab) { s.tab = tab; render(); },
            setQuery(q) { s.q = String(q || '').trim().toLowerCase(); render(); },
        };
    };
})();
