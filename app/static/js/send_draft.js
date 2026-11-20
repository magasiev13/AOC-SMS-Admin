(() => {
    'use strict';
    const form = document.getElementById('sendForm');
    const key = form?.dataset.draftKey;
    if (!form || !key) return;

    // Save composition settings only. Recipient numbers, test selections,
    // session tokens and reviewed audience snapshots never belong in a draft.
    const fields = ['message_body', 'target', 'event_id', 'schedule_later',
        'schedule_date', 'schedule_time', 'include_unsubscribe'];
    const message = form.elements.namedItem('message_body');
    const revision = document.createElement('input');
    revision.type = 'hidden';
    revision.name = 'draft_revision';
    form.appendChild(revision);
    let lastContent = null;
    function composition() {
        const draft = {};
        for (const name of fields) {
            const field = form.elements.namedItem(name);
            if (field) draft[name] = field.type === 'checkbox' ? field.checked : field.value;
        }
        return draft;
    }
    function save() {
        try {
            if (!message.value.trim()) {
                if (revision.value && JSON.parse(localStorage.getItem(key))?.revision === revision.value) {
                    localStorage.removeItem(key);
                }
                lastContent = null;
                revision.value = '';
                return;
            }
            const draft = composition();
            const content = JSON.stringify(draft);
            // Unchanged controls in this tab must not overwrite another tab's edits.
            if (content === lastContent) return;
            draft.revision = Array.from(crypto.getRandomValues(new Uint8Array(16)),
                byte => byte.toString(16).padStart(2, '0')).join('');
            localStorage.setItem(key, JSON.stringify(draft));
            revision.value = draft.revision;
            lastContent = content;
        } catch (_) {
            // Storage can be disabled. Keep the current form usable and intact.
        }
    }
    function refresh() {
        for (const name of fields) {
            for (const field of form.querySelectorAll(`[name="${name}"]`)) {
                field.dispatchEvent(new Event('input', { bubbles: true }));
                field.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }
    }
    function restore() {
        // Server-rendered validation feedback and explicit event compositions
        // take precedence over a previous browser draft.
        if (message.value.trim()) return;
        let draft;
        try { draft = JSON.parse(localStorage.getItem(key)); } catch (_) { return; }
        if (!draft || typeof draft !== 'object' || typeof draft.message_body !== 'string') return;
        message.value = draft.message_body.slice(0, message.maxLength > 0 ? message.maxLength : 1600);
        for (const name of fields.filter(name => name !== 'message_body')) {
            const field = form.elements.namedItem(name);
            if (!field || !(name in draft)) continue;
            if (field.type === 'checkbox') {
                if (typeof draft[name] === 'boolean') field.checked = draft[name];
            } else if (typeof draft[name] === 'string') {
                if (name === 'target' && !['community', 'event'].includes(draft[name])) continue;
                field.value = draft[name];
            }
        }
        if (form.elements.namedItem('schedule_later')?.checked) {
            const advanced = form.querySelector('.advanced-options');
            if (advanced) advanced.open = true;
        }
        if (/^[0-9a-f]{32}$/.test(draft.revision || '')) {
            revision.value = draft.revision;
            lastContent = JSON.stringify(composition());
        }
        refresh();
    }
    form.addEventListener('input', save);
    form.addEventListener('change', save);
    form.addEventListener('submit', save, true);
    document.getElementById('discardSendDraft')?.addEventListener('click', () => {
        const discardedRevision = revision.value;
        form.reset();
        revision.value = discardedRevision;
        message.value = '';
        form.elements.namedItem('target').value = 'community';
        for (const name of ['event_id', 'schedule_date', 'schedule_time']) {
            const field = form.elements.namedItem(name);
            if (field) field.value = '';
        }
        const scheduleLater = form.elements.namedItem('schedule_later');
        if (scheduleLater) scheduleLater.checked = false;
        const includeUnsubscribe = form.elements.namedItem('include_unsubscribe');
        if (includeUnsubscribe) includeUnsubscribe.checked = true;
        const reviewToken = form.elements.namedItem('review_token');
        if (reviewToken) reviewToken.value = '';
        document.getElementById('sendFormError')?.classList.add('d-none');
        refresh();
        message.focus();
    });
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', restore, { once: true });
    else restore();
})();
