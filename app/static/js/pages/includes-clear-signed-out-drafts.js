(() => {
    const prefix = document.currentScript.dataset.prefix;
    try {
        for (const key of Object.keys(localStorage)) {
            if (key.startsWith(prefix)) localStorage.removeItem(key);
        }
    } catch (_) { /* Storage may be disabled; sign-out still completes. */ }
})();
