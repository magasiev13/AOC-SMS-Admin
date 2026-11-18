(() => {
    const code = document.getElementById('code');
    const fragment = window.location.hash.slice(1);
    if (code && /^[A-Za-z0-9_-]{43}$/.test(fragment)) {
        code.value = fragment;
        history.replaceState(null, '', window.location.pathname);
    }
})();
