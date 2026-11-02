(() => {
    'use strict';
    const workspace = document.querySelector('[data-event-sections]');
    if (!workspace) return;
    const panels = Array.from(workspace.querySelectorAll('[data-event-panel]'));
    const links = Array.from(workspace.querySelectorAll('[data-event-section-link]'));
    const panelsById = new Map(panels.map(panel => [panel.id, panel]));
    if (!panelsById.has('overview')) return;

    const selectedId = () => {
        let id;
        try { id = decodeURIComponent(window.location.hash.slice(1)); } catch { return 'overview'; }
        if (panelsById.has(id)) return id;
        // Preserve deeper bookmarks to a form, booking or field inside a section.
        const target = id ? document.getElementById(id) : null;
        return target?.closest('[data-event-panel]')?.id || 'overview';
    };

    const revealDetails = (element) => {
        for (let parent = element.parentElement; parent && parent !== workspace; parent = parent.parentElement) {
            if (parent.tagName === 'DETAILS') parent.open = true;
        }
    };

    const showSection = (id, { focus = false } = {}) => {
        const selected = panelsById.get(id) || panelsById.get('overview');
        for (const panel of panels) panel.hidden = panel !== selected;
        for (const link of links) {
            const active = link.hash === `#${selected.id}`;
            link.classList.toggle('active', active);
            if (active) link.setAttribute('aria-current', 'page');
            else link.removeAttribute('aria-current');
        }
        if (focus) {
            const heading = document.getElementById(selected.getAttribute('aria-labelledby'));
            if (heading) {
                heading.tabIndex = -1;
                heading.focus({ preventScroll: true });
            }
        }
        return selected;
    };

    showSection(selectedId());
    workspace.dataset.enhanced = 'true';
    try {
        const initialTarget = document.getElementById(decodeURIComponent(window.location.hash.slice(1)));
        if (initialTarget && workspace.contains(initialTarget)) revealDetails(initialTarget);
    } catch (_) { /* An invalid fragment keeps the default overview usable. */ }

    window.addEventListener('hashchange', () => {
        showSection(selectedId(), { focus: true });
        let target;
        try { target = document.getElementById(decodeURIComponent(window.location.hash.slice(1))); } catch { return; }
        if (target && workspace.contains(target)) {
            revealDetails(target);
            target.scrollIntoView({ block: 'start' });
        }
    });

    // A link to the current section does not trigger hashchange, but still
    // needs to move keyboard focus from navigation into that section.
    workspace.addEventListener('click', event => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        const link = event.target.closest('a[href^="#"]');
        if (!link || !workspace.contains(link) || !panelsById.has(link.hash.slice(1))) return;
        if (link.hash === window.location.hash) showSection(link.hash.slice(1), { focus: true });
    });

    workspace.addEventListener('invalid', event => {
        const panel = event.target.closest('[data-event-panel]');
        if (!panel) return;
        showSection(panel.id);
        if (window.location.hash !== `#${panel.id}`) window.history.replaceState(window.history.state, '', `#${panel.id}`);
        revealDetails(event.target);
    }, true);
})();
