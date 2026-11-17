(() => {
    const revealTopic = () => {
        let id;
        try { id = decodeURIComponent(window.location.hash.slice(1)); } catch { return; }
        const topic = document.getElementById(id);
        if (topic && topic.matches('details[data-help-topic]')) topic.open = true;
    };
    revealTopic();
    window.addEventListener('hashchange', revealTopic);
})();
