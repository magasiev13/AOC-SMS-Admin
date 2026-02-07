if (!window.__dataConfirmHandlerBound) {
    window.__dataConfirmHandlerBound = true;

    document.addEventListener('click', (event) => {
        const confirmTarget = event.target.closest('[data-confirm]');
        if (!confirmTarget) {
            return;
        }

        const message = confirmTarget.dataset.confirm || 'Are you sure?';
        if (!window.confirm(message)) {
            event.preventDefault();
        }
    });
}
