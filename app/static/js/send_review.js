(() => {
    'use strict';
    const form = document.getElementById('sendForm');
    const dialog = document.getElementById('sendReviewModal');
    const confirm = document.getElementById('confirmReviewedSend');
    const error = document.getElementById('sendFormError');
    const sendButton = document.getElementById('sendBtn');
    if (!form || !dialog || !window.bootstrap) return; // Native server review remains available.
    const modal = new bootstrap.Modal(dialog);
    let confirmed = false;
    let pending = false;
    let captured = '';
    function fingerprint() {
        return JSON.stringify(Array.from(new FormData(form)).filter(([name]) => name !== 'review_token'));
    }
    function report(message, loginUrl) {
        error.textContent = message;
        if (loginUrl) {
            const fallback = new URL(form.dataset.loginUrl, window.location.origin);
            let destination = fallback;
            try {
                const candidate = new URL(loginUrl, window.location.origin);
                if (candidate.origin === window.location.origin && candidate.pathname === fallback.pathname) {
                    // Return to the send screen, never a POST-only review URL.
                    candidate.search = fallback.search;
                    destination = candidate;
                }
            } catch (_) { /* Use the server-rendered sign-in link. */ }
            const link = document.createElement('a');
            link.href = destination.href;
            link.textContent = 'Sign in again';
            link.className = 'ms-2';
            error.appendChild(link);
        }
        error.classList.remove('d-none');
        error.scrollIntoView({ block: 'nearest' });
    }
    form.addEventListener('submit', async event => {
        if (confirmed) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        if (pending) return;
        pending = true;
        const buttonContent = sendButton?.innerHTML;
        sendButton?.setAttribute('aria-busy', 'true');
        if (sendButton) {
            sendButton.disabled = true;
            sendButton.textContent = 'Preparing Review…';
        }
        error.classList.add('d-none');
        form.querySelector('[name="review_token"]').value = '';
        captured = fingerprint();
        try {
            const response = await fetch(form.dataset.reviewUrl, { method: 'POST', body: new FormData(form), headers: { Accept: 'application/json' } });
            const contentType = response.headers.get('content-type') || '';
            let review = {};
            if (contentType.includes('application/json') || contentType.includes('+json')) {
                try { review = await response.json(); } catch (_) { /* Show a useful error below. */ }
            }
            if (!review || typeof review !== 'object' || Array.isArray(review)) review = {};
            if (response.status === 401 || (response.redirected && !contentType.includes('json'))) {
                report('You were signed out. Sign in again; your message is saved.', review.login_url || form.dataset.loginUrl);
                return;
            }
            if (response.status === 400 && !review.error) {
                throw new Error('This page has expired. Reload it and review your saved message again.');
            }
            if (!response.ok) throw new Error(review.error || 'The send review is unavailable. Try again.');
            if (!review.review_token) throw new Error('The send review is unavailable. Your message has been kept. Reload the page and try again.');
            if (captured !== fingerprint()) throw new Error('The composition changed while preparing the review. Review it again.');
            for (const element of dialog.querySelectorAll('[data-review]')) element.textContent = String(review[element.dataset.review] ?? '');
            form.querySelector('[name="review_token"]').value = review.review_token;
            confirm.textContent = review.confirmation_label;
            confirm.disabled = false;
            modal.show();
        } catch (failure) {
            report(failure.message || 'The send review is unavailable. Your message has been kept.');
        } finally {
            pending = false;
            sendButton?.removeAttribute('aria-busy');
            if (sendButton) {
                sendButton.innerHTML = buttonContent;
                sendButton.disabled = false;
            }
        }
    }, true);
    confirm.addEventListener('click', () => {
        if (fingerprint() !== captured || !form.checkValidity()) {
            modal.hide();
            form.querySelector('[name="review_token"]').value = '';
            report('The composition changed. Review the message, audience and timing again.');
            return;
        }
        confirm.disabled = true;
        confirmed = true;
        form.requestSubmit();
    });
    dialog.addEventListener('hidden.bs.modal', () => { if (!confirmed) sendButton?.focus(); });
})();
