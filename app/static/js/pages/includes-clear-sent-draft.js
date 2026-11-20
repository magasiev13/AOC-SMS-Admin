(() => {
    const { draftKey, acceptedDraftKey } = document.currentScript.dataset;
    const receipt = /^#sent-draft-([0-9a-f]{32})$/.exec(location.hash);
    if (receipt && acceptedDraftKey) {
        history.replaceState(history.state, '', location.pathname + location.search);
    }
    try {
        if (receipt && acceptedDraftKey) {
            const draft = JSON.parse(localStorage.getItem(acceptedDraftKey));
            if (draft?.revision === receipt[1]) localStorage.removeItem(acceptedDraftKey);
        }
        if (draftKey) {
            const legacy = JSON.parse(localStorage.getItem(draftKey));
            if (legacy && !legacy.revision) localStorage.removeItem(draftKey);
        }
    } catch (_) { /* Storage may be unavailable. */ }
})();
