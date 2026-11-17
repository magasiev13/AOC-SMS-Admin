const pane = document.getElementById('inboxMessagePane');
if (pane) {
    pane.scrollTop = pane.scrollHeight;
}

const inboxLayout = document.querySelector('.inbox-layout');
const inboxThreadList = document.getElementById('inboxThreadList');
const inboxThreadListSkeleton = document.getElementById('inboxThreadListSkeleton');
const inboxMessagePane = document.getElementById('inboxMessagePane');
const inboxMessagePaneSkeleton = document.getElementById('inboxMessagePaneSkeleton');

const showInboxLoadingState = () => {
    if (!inboxLayout) {
        return;
    }
    inboxLayout.classList.add('inbox-layout-loading');
    if (inboxThreadList && inboxThreadListSkeleton) {
        inboxThreadList.classList.add('d-none');
        inboxThreadListSkeleton.classList.remove('d-none');
    }
    if (inboxMessagePane && inboxMessagePaneSkeleton) {
        inboxMessagePane.classList.add('d-none');
        inboxMessagePaneSkeleton.classList.remove('d-none');
    }
};

document.querySelectorAll('[data-inbox-loading-trigger]').forEach((trigger) => {
    trigger.addEventListener('click', showInboxLoadingState);
});

const replyBody = document.getElementById('replyBody');
const replyBodyStats = document.getElementById('replyBodyStats');
if (replyBody && replyBodyStats && window.TwineviaSmsAnalysis) {
    const updateReplyStats = () => {
        const analysis = window.TwineviaSmsAnalysis.analyzeBody(replyBody.value || '');
        const normalizationNote = analysis.normalizationApplied
            ? ` Normalizes ${analysis.replacementCount} punctuation/spacing character(s) before send.`
            : '';
        replyBodyStats.textContent = `Encoding: ${window.TwineviaSmsAnalysis.formatEncodingLabel(analysis.encoding)}. ${analysis.segmentCount} segment(s). ${analysis.charactersToNextSegment} characters until the next segment.${normalizationNote}`;
    };
    replyBody.addEventListener('input', updateReplyStats);
    updateReplyStats();
}

function bindSelectAll(selectAllId, itemSelector, onSelectAllChange) {
    const selectAll = document.getElementById(selectAllId);
    if (!selectAll) {
        return [];
    }

    const items = Array.from(document.querySelectorAll(itemSelector));
    if (!items.length) {
        selectAll.checked = false;
        selectAll.disabled = true;
        return [];
    }

    selectAll.addEventListener('change', () => {
        items.forEach((item) => {
            item.checked = selectAll.checked;
        });
        if (onSelectAllChange) {
            onSelectAllChange();
        }
    });

    items.forEach((item) => {
        item.addEventListener('change', () => {
            selectAll.checked = items.every((entry) => entry.checked);
        });
    });

    return items;
}

const bulkMessageDeleteButton = document.getElementById('bulkMessageDeleteButton');
let messageSelectionItems = [];
if (bulkMessageDeleteButton) {
    const syncBulkMessageDeleteState = () => {
        const hasSelection = messageSelectionItems.some((entry) => entry.checked);
        bulkMessageDeleteButton.disabled = !hasSelection;
    };
    messageSelectionItems = bindSelectAll(
        'selectAllMessages',
        '.js-message-select',
        syncBulkMessageDeleteState
    );
    syncBulkMessageDeleteState();
    messageSelectionItems.forEach((entry) => {
        entry.addEventListener('change', syncBulkMessageDeleteState);
    });
} else {
    messageSelectionItems = bindSelectAll('selectAllMessages', '.js-message-select');
}
