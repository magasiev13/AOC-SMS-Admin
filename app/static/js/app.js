document.addEventListener('click', (event) => {
    const confirmTarget = event.target.closest('[data-confirm]');
    if (!confirmTarget) {
        return;
    }

    const message = confirmTarget.dataset.confirm;
    if (!message) {
        return;
    }

    if (!window.confirm(message)) {
        event.preventDefault();
        event.stopPropagation();
    }
});
