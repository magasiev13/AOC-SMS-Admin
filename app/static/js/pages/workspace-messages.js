(() => {
    const section = document.getElementById('localMessageDraft');
    if (!section) return;
    try {
        const draft = JSON.parse(localStorage.getItem(section.dataset.key));
        section.hidden = typeof draft?.message_body !== 'string' || !draft.message_body.trim();
    } catch (_) {
        // Private browsing and malformed old drafts leave the timeline usable.
    }
})();
