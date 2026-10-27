(() => {
    'use strict';
    const overview = document.getElementById('workspaceOverview');
    const wide = window.matchMedia('(min-width: 992px)');
    if (overview) {
        overview.open = wide.matches;
        wide.addEventListener('change', event => { overview.open = event.matches; });
    }
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
    function report(message) {
        error.textContent = message;
        error.classList.remove('d-none');
        error.scrollIntoView({ block: 'nearest' });
    }
    form.addEventListener('submit', async event => {
        if (confirmed) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        if (pending) return;
        pending = true;
        sendButton?.setAttribute('aria-busy', 'true');
        error.classList.add('d-none');
        form.querySelector('[name="review_token"]').value = '';
        captured = fingerprint();
        try {
            const response = await fetch(form.dataset.reviewUrl, { method: 'POST', body: new FormData(form), headers: { Accept: 'application/json' } });
            const review = await response.json();
            if (!response.ok) throw new Error(review.error || 'The send review is unavailable. Try again.');
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
