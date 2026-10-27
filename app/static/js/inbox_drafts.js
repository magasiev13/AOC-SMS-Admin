(() => {
    'use strict';
    const meta = document.getElementById('inboxStatusMeta');
    if (!meta) return;
    const prefix = 'twinevia:reply-draft:';
    const context = `${prefix}${meta.dataset.draftContext}:`;
    const key = meta.dataset.threadId ? `${context}${meta.dataset.threadId}` : null;
    const body = document.getElementById('replyBody');
    const form = body?.closest('form');
    const requestKey = form?.querySelector('[name="request_idempotency_key"]');
    const status = document.getElementById('replyDraftStatus');
    const notice = document.getElementById('inboxActivityNotice');
    let stored = null;
    let storageAvailable = true;
    try {
        for (const candidate of Object.keys(sessionStorage)) {
            if (candidate.startsWith(prefix) && !candidate.startsWith(context)) sessionStorage.removeItem(candidate);
        }
        if (key) stored = JSON.parse(sessionStorage.getItem(key) || 'null');
        if (stored && (!Number.isFinite(stored.updatedAt) || Date.now() - stored.updatedAt > 86400000)) {
            sessionStorage.removeItem(key);
            stored = null;
        }
        if (stored?.submittedKey && stored.submittedKey === meta.dataset.acknowledgedKey) {
            sessionStorage.removeItem(key);
            stored = null;
        }
    } catch (_error) {
        storageAvailable = false;
    }
    if (body && typeof stored?.body === 'string') {
        const originalKey = stored.requestKey || stored.submittedKey;
        if (requestKey && typeof originalKey === 'string' && /^[A-Za-z0-9._:-]{1,64}$/.test(originalKey)) requestKey.value = originalKey;
        body.value = stored.body;
        body.dispatchEvent(new Event('input', { bubbles: true }));
    }
    function persist(forgetSubmission = false) {
        if (!body || !key) return;
        if (forgetSubmission && stored) delete stored.submittedKey;
        stored = { ...stored, body: body.value, requestKey: requestKey?.value || '', updatedAt: Date.now() };
        try {
            if (body.value) sessionStorage.setItem(key, JSON.stringify(stored));
            else sessionStorage.removeItem(key);
        } catch (_error) {
            storageAvailable = false;
        }
        if (status) status.textContent = body.value ? (storageAvailable ? 'Draft kept in this tab for up to 24 hours.' : 'Browser draft storage is unavailable. Keep this page open while composing.') : '';
    }
    body?.addEventListener('input', () => persist(true));
    form?.addEventListener('submit', () => {
        stored = { ...stored, submittedKey: form.querySelector('[name="request_idempotency_key"]')?.value || '' };
        persist();
    });
    document.getElementById('discardReplyDraft')?.addEventListener('click', () => {
        if (body?.value && !window.confirm('Discard this draft? An already submitted message may still complete. Review its outcome before starting a replacement.')) return;
        stored = null;
        if (body) body.value = '';
        if (requestKey) requestKey.value = crypto.randomUUID().replaceAll('-', '');
        persist();
        body?.dispatchEvent(new Event('input', { bubbles: true }));
        body?.focus();
    });
    window.addEventListener('pagehide', () => persist());
    document.getElementById('inboxActivityRefresh')?.addEventListener('click', () => persist());
    for (const logout of document.querySelectorAll('form[action]')) {
        if (new URL(logout.action, location.href).pathname !== '/logout') continue;
        logout.addEventListener('submit', () => {
            try {
                for (const candidate of Object.keys(sessionStorage)) if (candidate.startsWith(prefix)) sessionStorage.removeItem(candidate);
                stored = null;
                if (body) body.value = '';
            } catch (_error) { /* Storage is optional; the context still isolates accounts. */ }
        });
    }
    persist();
    let inFlight = false;
    const baselineId = Number(meta.dataset.latestMessageId || 0);
    const baselineCount = Number(meta.dataset.messageCount || 0);
    const baselineDelivery = meta.dataset.deliveryVersion || '';
    async function poll() {
        if (document.hidden || inFlight || !meta.dataset.statusUrl) return;
        inFlight = true;
        try {
            const response = await fetch(meta.dataset.statusUrl, { headers: { Accept: 'application/json' } });
            if (!response.ok) return;
            const payload = await response.json();
            if (Number(payload.latest_message_id || 0) !== baselineId || Number(payload.message_count || 0) !== baselineCount || (payload.delivery_version || '') !== baselineDelivery) {
                notice?.classList.remove('d-none');
            }
        } catch (_error) {
            // An interrupted poll never navigates away or changes composition.
        } finally {
            inFlight = false;
        }
    }
    const interval = window.setInterval(poll, 3000);
    window.addEventListener('pagehide', () => window.clearInterval(interval), { once: true });
    window.addEventListener('pageshow', event => {
        // Revalidate identity and accepted submissions after browser Back;
        // sessionStorage restores only the current authenticated draft context.
        if (event.persisted) window.location.reload();
    });
})();
