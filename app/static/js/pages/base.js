const toastEls = document.querySelectorAll('.app-toast');
toastEls.forEach((toastEl) => {
    const toast = new bootstrap.Toast(toastEl);
    toast.show();
});

const tableLoadingForms = document.querySelectorAll('form[data-table-loading-target]');
tableLoadingForms.forEach((form) => {
    form.addEventListener('submit', () => {
        const targetIds = form.dataset.tableLoadingTarget || '';
        targetIds.split(',').map((id) => id.trim()).filter(Boolean).forEach((id) => {
            const target = document.getElementById(id);
            if (target) {
                target.classList.add('is-loading');
            }
        });
    });
});

const sidebarSearchForms = document.querySelectorAll('form.app-search[action*="/community"]');
sidebarSearchForms.forEach((form) => {
    const searchInput = form.querySelector('input[name="search"]');
    if (!searchInput) {
        return;
    }
    searchInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();
            form.requestSubmit();
        }
    });
});

const iconEls = document.querySelectorAll('i.bi');
iconEls.forEach((iconEl) => {
    iconEl.setAttribute('aria-hidden', 'true');
});

const copyButtons = document.querySelectorAll('[data-copy-text]');
copyButtons.forEach((button) => {
    button.addEventListener('click', async () => {
        const text = button.dataset.copyText || '';
        if (!text) {
            return;
        }

        const originalHtml = button.innerHTML;
        const feedback = button.dataset.copyFeedback || 'Copied';
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(text);
            } else {
                const input = document.createElement('textarea');
                input.value = text;
                input.setAttribute('readonly', '');
                input.style.position = 'absolute';
                input.style.left = '-9999px';
                document.body.appendChild(input);
                input.select();
                document.execCommand('copy');
                document.body.removeChild(input);
            }
            button.innerHTML = feedback;
            window.setTimeout(() => {
                button.innerHTML = originalHtml;
            }, 1500);
        } catch (error) {
            window.prompt('Copy this link:', text);
        }
    });
});

function updateTableScrollHints() {
    const responsiveTables = document.querySelectorAll('.table-responsive');
    responsiveTables.forEach((wrapper) => {
        const isScrollable = wrapper.scrollWidth > wrapper.clientWidth + 1;
        wrapper.classList.toggle('is-scrollable', isScrollable);
    });
}

function initPageActionOverflow() {
    const wrappers = document.querySelectorAll('[data-page-actions]');
    wrappers.forEach((wrapper) => {
        const inline = wrapper.querySelector('[data-page-actions-inline]');
        const overflow = wrapper.querySelector('[data-page-actions-overflow]');
        const toggle = wrapper.querySelector('[data-page-actions-toggle]');
        if (!inline || !overflow || !toggle) {
            return;
        }

        const mediaQuery = window.matchMedia('(max-width: 991.98px)');

        const moveAllInline = () => {
            const moved = Array.from(overflow.children);
            moved.forEach((actionEl) => {
                actionEl.classList.remove('is-overflow-action');
                inline.appendChild(actionEl);
            });
            overflow.hidden = true;
            toggle.hidden = true;
            toggle.setAttribute('aria-expanded', 'false');
        };

        const applyMobile = () => {
            moveAllInline();
            const actions = Array.from(inline.querySelectorAll(':scope > *'));
            if (actions.length <= 2) {
                return;
            }

            let primary = actions.find((actionEl) => actionEl.classList.contains('page-action-primary'));
            if (!primary) {
                primary = actions[0];
                primary.classList.add('page-action-primary');
            }

            const secondaryActions = actions.filter((actionEl) => actionEl !== primary);
            secondaryActions.forEach((actionEl) => {
                actionEl.classList.add('is-overflow-action');
                overflow.appendChild(actionEl);
            });

            toggle.hidden = false;
            overflow.hidden = true;
            toggle.setAttribute('aria-expanded', 'false');
        };

        const syncActions = () => {
            if (mediaQuery.matches) {
                applyMobile();
            } else {
                moveAllInline();
            }
        };

        toggle.addEventListener('click', () => {
            const isHidden = overflow.hidden;
            overflow.hidden = !isHidden;
            toggle.setAttribute('aria-expanded', String(isHidden));
        });

        mediaQuery.addEventListener('change', syncActions);
        syncActions();
    });
}

function initMobileNav() {
    const mobileNav = document.querySelector('[data-mobile-nav]');
    if (!mobileNav) {
        return;
    }

    const topbar = mobileNav.closest('.app-topbar');
    const toggle = topbar?.querySelector('[aria-controls="appMobileNav"]');
    const collapseInstance = bootstrap.Collapse.getOrCreateInstance(mobileNav, { toggle: false });
    let restoreToggleFocus = false;
    const syncOffset = () => {
        if (!topbar) {
            return;
        }
        mobileNav.style.setProperty('--app-mobile-nav-offset', `${Math.ceil(topbar.getBoundingClientRect().height)}px`);
    };
    const syncBodyState = (isOpen) => {
        document.body.classList.toggle('app-mobile-nav-open', isOpen);
        syncOffset();
    };

    mobileNav.addEventListener('show.bs.collapse', () => syncBodyState(true));
    mobileNav.addEventListener('hide.bs.collapse', () => syncBodyState(false));
    mobileNav.addEventListener('hidden.bs.collapse', () => {
        if (restoreToggleFocus) toggle?.focus();
        restoreToggleFocus = false;
    });
    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape' || !document.body.classList.contains('app-mobile-nav-open')) return;
        event.preventDefault();
        restoreToggleFocus = true;
        if (mobileNav.classList.contains('collapsing')) {
            mobileNav.addEventListener('shown.bs.collapse', () => collapseInstance.hide(), { once: true });
        } else {
            collapseInstance.hide();
        }
    });
    mobileNav.addEventListener('click', (event) => {
        if (event.target === mobileNav) {
            collapseInstance.hide();
        }
    });

    mobileNav.querySelectorAll('a.app-nav-link').forEach((linkEl) => {
        linkEl.addEventListener('click', () => collapseInstance.hide());
    });

    window.addEventListener('resize', syncOffset);
    syncOffset();
    syncBodyState(mobileNav.classList.contains('show'));
}

function initRowActionOverflow() {
    const stripActionButtonClasses = (actionEl) => {
        const removableClasses = [
            'btn',
            'btn-sm',
            'btn-outline-primary',
            'btn-outline-secondary',
            'btn-outline-danger',
            'btn-outline-warning',
            'btn-soft-danger',
            'btn-soft-warning',
            'btn-outline-info',
        ];
        actionEl.classList.remove(...removableClasses);
    };

    const actionTone = (actionNode) => {
        const classes = actionNode.className || '';
        if (classes.includes('danger')) {
            return 'danger';
        }
        if (classes.includes('warning')) {
            return 'warning';
        }
        return 'neutral';
    };

    const wrappers = document.querySelectorAll('[data-row-actions]');
    wrappers.forEach((wrapper) => {
        if (wrapper.dataset.rowActionsEnhanced === 'true') {
            return;
        }

        const items = Array.from(wrapper.children).filter((child) => {
            if (!(child instanceof HTMLElement) || child.hidden) {
                return false;
            }
            if (child.classList.contains('row-actions__primary') || child.classList.contains('row-actions__overflow')) {
                return false;
            }
            if (child.matches('form')) {
                return Boolean(child.querySelector('button, a'));
            }
            return child.matches('a, button, .btn-group');
        });

        const overflowMin = Number.parseInt(wrapper.dataset.rowActionsMinOverflow || '3', 10);
        if (items.length < overflowMin) {
            return;
        }

        const primaryCount = Math.max(1, Number.parseInt(wrapper.dataset.rowActionsPrimary || '1', 10));
        const primaryItems = items.slice(0, primaryCount);
        const overflowItems = items.slice(primaryCount);
        if (!overflowItems.length) {
            return;
        }

        wrapper.dataset.rowActionsEnhanced = 'true';
        wrapper.classList.add('row-actions--with-overflow');

        const primaryContainer = document.createElement('div');
        primaryContainer.className = 'row-actions__primary';
        primaryItems.forEach((item) => {
            primaryContainer.appendChild(item);
        });

        const hasSmallButtons = primaryItems.some((item) => {
            if (!(item instanceof HTMLElement)) {
                return false;
            }
            return item.matches('.btn-sm') || Boolean(item.querySelector('.btn-sm'));
        });
        const toggleSizeClass = hasSmallButtons ? ' btn-sm' : '';

        const overflowContainer = document.createElement('div');
        overflowContainer.className = 'dropdown row-actions__overflow';

        const overflowToggle = document.createElement('button');
        overflowToggle.type = 'button';
        overflowToggle.className = `btn${toggleSizeClass} btn-outline-secondary dropdown-toggle row-actions__overflow-toggle`;
        overflowToggle.setAttribute('data-bs-toggle', 'dropdown');
        overflowToggle.setAttribute('aria-expanded', 'false');
        overflowToggle.innerHTML = '<i class="bi bi-three-dots" aria-hidden="true"></i><span class="visually-hidden">More Actions</span>';

        const overflowMenu = document.createElement('div');
        overflowMenu.className = 'dropdown-menu dropdown-menu-end row-actions__overflow-menu';

        overflowItems.forEach((item) => {
            if (item.matches('form')) {
                const formButton = item.querySelector('button');
                if (!formButton) {
                    return;
                }
                const tone = actionTone(formButton);
                stripActionButtonClasses(formButton);
                formButton.classList.add('dropdown-item', 'row-actions__menu-button');
                if (tone === 'danger') {
                    formButton.classList.add('text-danger');
                } else if (tone === 'warning') {
                    formButton.classList.add('text-warning');
                }
                item.classList.add('row-actions__menu-form');
                overflowMenu.appendChild(item);
                return;
            }

            const tone = actionTone(item);
            stripActionButtonClasses(item);
            item.classList.add('dropdown-item', 'row-actions__menu-button');
            if (tone === 'danger') {
                item.classList.add('text-danger');
            } else if (tone === 'warning') {
                item.classList.add('text-warning');
            }
            overflowMenu.appendChild(item);
        });

        overflowContainer.appendChild(overflowToggle);
        overflowContainer.appendChild(overflowMenu);
        wrapper.appendChild(primaryContainer);
        wrapper.appendChild(overflowContainer);
    });
}

updateTableScrollHints();
window.addEventListener('resize', updateTableScrollHints);
initPageActionOverflow();
initRowActionOverflow();
initMobileNav();
