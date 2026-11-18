const dashboardSettings = JSON.parse(document.currentScript.dataset.settings);
const messageBody = document.getElementById('message_body');
const charCount = document.getElementById('charCount');
const messageEncoding = document.getElementById('messageEncoding');
const segmentCount = document.getElementById('segmentCount');
const charsToNextSegment = document.getElementById('charsToNextSegment');
const messageNormalizationHint = document.getElementById('messageNormalizationHint');
const messageUnitEstimate = document.getElementById('messageUnitEstimate');
const specialCharacterWarning = document.getElementById('specialCharacterWarning');
const specialCharacterWarningText = document.getElementById('specialCharacterWarningText');
const replaceSpecialCharacters = document.getElementById('replaceSpecialCharacters');
const undoSpecialCharacters = document.getElementById('undoSpecialCharacters');
let replacementUndo = null;
let applyingReplacement = false;
const targetEvent = document.getElementById('targetEvent');
const targetCommunity = document.getElementById('targetCommunity');
const eventSelect = document.getElementById('event_id');
const eventSelectWrapper = document.getElementById('eventSelectWrapper');
const sendForm = document.getElementById('sendForm');
const sendBtn = document.getElementById('sendBtn');
const sendFormError = document.getElementById('sendFormError');
const sendFormStatus = document.getElementById('sendFormStatus');
const scheduleLater = document.getElementById('scheduleLater');
const scheduleWrapper = document.getElementById('scheduleWrapper');
const scheduleDate = document.getElementById('schedule_date');
const scheduleTime = document.getElementById('schedule_time');
const browserTimezoneNotice = document.getElementById('browserTimezoneNotice');
const testMode = document.getElementById('testMode');
const testModeOptions = document.getElementById('testModeOptions');
const testRecipientModeOne = document.getElementById('testRecipientModeOne');
const testRecipientModeAll = document.getElementById('testRecipientModeAll');
const testRecipientPhone = document.getElementById('testRecipientPhone');
const singleTestRecipientWrapper = document.getElementById('singleTestRecipientWrapper');
const includeUnsubscribe = document.getElementById('includeUnsubscribe');
const unsubscribeFooter = dashboardSettings.footer;
const audienceEligibility = document.getElementById('audienceEligibility');
let eligibleRecipientCount = null;
let audiencePreviewSequence = 0;
let audiencePreviewTimer;
let audiencePreviewController;
let audienceSelectionKey = null;
let audienceProfile = null;
let compositionEstimate = null;
let compositionPreviewPending = false;

function audienceSelection() {
    const selection = new URLSearchParams({
        audience_profile: '1',
        csrf_token: sendForm.elements.namedItem('csrf_token').value,
        target: targetEvent.checked ? 'event' : 'community',
    });
    if (targetEvent.checked) selection.set('event_id', eventSelect.value);
    if (testMode?.checked) {
        selection.set('test_mode', 'on');
        selection.set('test_recipient_selection_mode', testRecipientModeAll?.checked ? 'all' : 'one');
        for (const field of ['test_recipient_id', 'test_recipient_phone']) {
            const input = sendForm.elements.namedItem(field);
            if (input && !testRecipientModeAll?.checked) selection.set(field, input.value);
        }
    }
    return selection;
}

function refreshAudiencePreview() {
    const selection = audienceSelection();
    const key = selection.toString();
    // Message, whitespace, STOP footer and timing edits stay in the browser.
    // Only audience selection changes require another eligibility/profile read.
    if (key === audienceSelectionKey) {
        updateMessageStats();
        return;
    }
    audienceSelectionKey = key;
    const sequence = ++audiencePreviewSequence;
    clearTimeout(audiencePreviewTimer);
    audiencePreviewController?.abort();
    audienceProfile = null;
    compositionEstimate = null;
    eligibleRecipientCount = null;
    compositionPreviewPending = true;
    audienceEligibility.textContent = 'Checking audience eligibility…';
    updateMessageStats();
    audiencePreviewTimer = setTimeout(() => loadAudiencePreview(sequence, selection), 750);
}

async function loadAudiencePreview(sequence, selection) {
    audiencePreviewController = new AbortController();
    try {
        const response = await fetch(dashboardSettings.previewUrl, {
            method: 'POST',
            body: selection,
            signal: audiencePreviewController.signal,
            headers: { Accept: 'application/json' },
        });
        if (!response.ok) throw new Error('Audience preview unavailable');
        const preview = await response.json();
        if (sequence !== audiencePreviewSequence) return;
        eligibleRecipientCount = preview.eligible;
        audienceProfile = preview.profile;
        const audienceNote = testMode?.checked ? 'Internal test recipients only. ' : '';
        audienceEligibility.textContent = `${audienceNote}${preview.eligible} eligible recipient(s); ${preview.excluded} excluded by permission, opt-out, suppression or duplicate checks. Eligibility is checked again before sending.`;
    } catch (error) {
        if (sequence !== audiencePreviewSequence || error.name === 'AbortError') return;
        audienceEligibility.textContent = 'Audience preview is unavailable. Eligibility will be checked before this request is saved.';
    }
    compositionPreviewPending = false;
    updateMessageStats();
}

if (browserTimezoneNotice) {
    try {
        const resolvedTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
        const workspaceTimezone = dashboardSettings.timezone;
        const workspaceTimezoneLabel = dashboardSettings.timezoneLabel;
        const canonicalWorkspaceTimezone = new Intl.DateTimeFormat('en-US', { timeZone: workspaceTimezone }).resolvedOptions().timeZone;
        if (resolvedTimezone && resolvedTimezone !== canonicalWorkspaceTimezone) {
            browserTimezoneNotice.textContent = `Your browser uses ${resolvedTimezone}. Enter ${workspaceTimezoneLabel} workspace time for this schedule.`;
            browserTimezoneNotice.classList.remove('d-none');
        }
    } catch (_) { /* The named workspace zone above still applies. */ }
}

function updateMessageStats() {
    const footer = includeUnsubscribe && includeUnsubscribe.checked ? unsubscribeFooter : '';
    const draft = window.TwineviaSmsAnalysis.trimDraft(messageBody.value);
    const analysis = window.TwineviaSmsAnalysis.analyzeBody(`${draft}${footer}`);
    compositionEstimate = audienceProfile === null ? null : window.TwineviaSmsAnalysis.estimateAudience(messageBody.value, audienceProfile, { footer });
    const recipientEstimate = eligibleRecipientCount || 0;
    const minimum = compositionEstimate?.min_segment_count;
    const maximum = compositionEstimate?.max_segment_count;
    const each = minimum === maximum ? String(minimum) : `${minimum}–${maximum}`;
    const special = compositionEstimate?.special_character || window.TwineviaSmsAnalysis.specialCharacter(messageBody.value);
    const usesUnicode = compositionEstimate ? compositionEstimate.encodings.includes('ucs-2') : analysis.encoding === 'ucs-2';
    const characterName = special ? `${special.name || `Unicode character “${special.character}”`} (${special.codepoint})` : 'Personalized contact names';
    const reason = compositionEstimate && recipientEstimate === 0
        ? 'No eligible recipients were found for this audience.'
        : (usesUnicode
            ? `${characterName} ${special ? 'uses' : 'use'} Unicode SMS, which fits fewer characters per segment.`
            : (maximum > 1 ? 'Message length requires multiple SMS segments.' : 'Message length fits one SMS segment per recipient.'));

    charCount.textContent = messageBody.value.length;
    if (segmentCount) {
        segmentCount.textContent = compositionEstimate ? each : (compositionPreviewPending ? '…' : analysis.segmentCount);
    }
    if (messageEncoding) {
        messageEncoding.textContent = compositionEstimate ? compositionEstimate.encodings.map(window.TwineviaSmsAnalysis.formatEncodingLabel).join(' / ') : window.TwineviaSmsAnalysis.formatEncodingLabel(analysis.encoding);
    }
    if (charsToNextSegment) {
        charsToNextSegment.textContent = analysis.charactersToNextSegment;
    }
    if (messageNormalizationHint) {
        if (analysis.normalizationApplied) {
            const segmentNote = analysis.segmentsSaved > 0
                ? ` This saves ${analysis.segmentsSaved} SMS segment(s) per recipient.`
                : '';
            messageNormalizationHint.textContent = `Twinevia will simplify ${analysis.replacementCount} punctuation or spacing character(s) before sending.${segmentNote}`;
        } else {
            messageNormalizationHint.textContent = `Live preview includes the current STOP footer setting.`;
        }
    }
    if (messageUnitEstimate) {
        delete messageUnitEstimate.dataset.totalSegments;
        if (!draft) {
            messageUnitEstimate.textContent = '0 segments each × 0 recipients = 0';
        } else if (compositionEstimate) {
            messageUnitEstimate.dataset.totalSegments = String(compositionEstimate.total_segments);
            messageUnitEstimate.textContent = `${each} segments each × ${recipientEstimate} recipients = ${compositionEstimate.total_segments}. ${reason}`;
        } else if (compositionPreviewPending) {
            messageUnitEstimate.textContent = 'Calculating personalized SMS segments…';
        } else {
            messageUnitEstimate.textContent = 'Personalized SMS estimate is unavailable. Review the message to check its segments.';
        }
    }
    const segmentTotal = document.getElementById('composeSegmentTotal');
    if (segmentTotal) segmentTotal.textContent = !draft ? '0 SMS segments' : compositionEstimate
        ? `${compositionEstimate.total_segments} SMS segments` : 'Checking estimate…';
    const usage = dashboardSettings.usage;
    const cost = document.getElementById('composeCostEstimate');
    if (cost && usage) {
        const total = !draft ? 0 : compositionEstimate?.total_segments;
        if (!usage.billed) cost.textContent = 'Not billed by Twinevia';
        else if (total == null) cost.textContent = '';
        else {
            const extra = Math.max(0, total - usage.remaining) * Number(usage.rate);
            const amount = extra.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
            const label = usage.currency === 'usd' ? '$' + amount : usage.currency.toUpperCase() + ' ' + amount;
            cost.textContent = label + ' extra at today’s allowance';
        }
    }
    const preview = document.getElementById('composePreviewBody');
    if (preview) preview.textContent = draft ? [draft, footer].filter(Boolean).join(' ') : 'Your message will appear here.';
    specialCharacterWarning.classList.toggle('d-none', !draft || !usesUnicode);
    const canReplace = window.TwineviaSmsAnalysis.replaceSpecialCharacters(messageBody.value) !== messageBody.value;
    const unicodeWriting = window.TwineviaSmsAnalysis.usesUnicodeWriting(messageBody.value);
    replaceSpecialCharacters.hidden = !canReplace;
    specialCharacterWarningText.textContent = `${reason} ${unicodeWriting
        ? 'This message uses Unicode letters. Its text is preserved; Unicode SMS fits fewer characters per segment and may use more segments.'
        : (canReplace
            ? 'Replace simplifies punctuation and removes emoji and pictographic symbols from this draft. You can undo the replacement until your next edit.'
            : 'This may come from personalized contact names; the draft replacement does not change contact names.')}`;
}

updateMessageStats();
messageBody.addEventListener('input', () => {
    if (!applyingReplacement) {
        replacementUndo = null;
        undoSpecialCharacters.hidden = true;
    }
    updateMessageStats();
});
sendForm.addEventListener('input', refreshAudiencePreview);
sendForm.addEventListener('change', refreshAudiencePreview);
replaceSpecialCharacters.addEventListener('click', () => {
    replacementUndo = messageBody.value;
    messageBody.value = window.TwineviaSmsAnalysis.replaceSpecialCharacters(messageBody.value);
    applyingReplacement = true;
    messageBody.dispatchEvent(new Event('input', { bubbles: true }));
    messageBody.dispatchEvent(new Event('change', { bubbles: true }));
    applyingReplacement = false;
    undoSpecialCharacters.hidden = false;
    messageBody.focus();
});
undoSpecialCharacters.addEventListener('click', () => {
    if (replacementUndo === null) return;
    messageBody.value = replacementUndo;
    replacementUndo = null;
    applyingReplacement = true;
    messageBody.dispatchEvent(new Event('input', { bubbles: true }));
    messageBody.dispatchEvent(new Event('change', { bubbles: true }));
    applyingReplacement = false;
    undoSpecialCharacters.hidden = true;
    messageBody.focus();
});

function toggleEventSelect() {
    eventSelectWrapper.style.display = targetEvent.checked ? 'block' : 'none';
    refreshAudiencePreview();
}

function toggleSchedule() {
    const isScheduled = scheduleLater.checked;
    scheduleWrapper.style.display = isScheduled ? 'block' : 'none';
    scheduleDate.required = isScheduled;
    scheduleTime.required = isScheduled;
    if (sendBtn.disabled) return;
    if (isScheduled) {
        sendBtn.innerHTML = '<i class="bi bi-clock me-2" aria-hidden="true"></i>Review Schedule';
    } else {
        sendBtn.innerHTML = '<i class="bi bi-send-fill me-2" aria-hidden="true"></i>Review Message';
    }
}

function updateTestModeRecipientControls() {
    if (!testModeOptions) {
        return;
    }
    const selectingOneRecipient = testRecipientModeOne && testRecipientModeOne.checked;
    if (singleTestRecipientWrapper) {
        singleTestRecipientWrapper.style.display = selectingOneRecipient ? 'block' : 'none';
    }
    if (testRecipientPhone) {
        testRecipientPhone.required = Boolean(testMode && testMode.checked && selectingOneRecipient);
    }
    updateMessageStats();
}

function toggleTestModeOptions() {
    if (!testMode || !testModeOptions) {
        return;
    }
    testModeOptions.style.display = testMode.checked ? 'block' : 'none';
    updateTestModeRecipientControls();
    refreshAudiencePreview();
}

targetEvent.addEventListener('change', toggleEventSelect);
targetCommunity.addEventListener('change', toggleEventSelect);
scheduleLater.addEventListener('change', toggleSchedule);
if (eventSelect) {
    eventSelect.addEventListener('change', refreshAudiencePreview);
}
if (includeUnsubscribe) {
    includeUnsubscribe.addEventListener('change', updateMessageStats);
}
if (testMode) {
    testMode.addEventListener('change', toggleTestModeOptions);
}
if (testRecipientModeOne) {
    testRecipientModeOne.addEventListener('change', updateTestModeRecipientControls);
}
if (testRecipientModeAll) {
    testRecipientModeAll.addEventListener('change', updateTestModeRecipientControls);
}
if (testRecipientPhone) {
    testRecipientPhone.addEventListener('change', updateMessageStats);
}
toggleEventSelect();
toggleSchedule();
toggleTestModeOptions();

function setValidationError(message) {
    if (!sendFormError) {
        return;
    }
    if (message) {
        sendFormError.textContent = message;
        sendFormError.classList.remove('d-none');
    } else {
        sendFormError.textContent = '';
        sendFormError.classList.add('d-none');
    }
}

function resetValidationState() {
    messageBody.classList.remove('is-invalid');
    scheduleDate.classList.remove('is-invalid');
    scheduleTime.classList.remove('is-invalid');
    eventSelect.classList.remove('is-invalid');
    if (testRecipientPhone) {
        testRecipientPhone.classList.remove('is-invalid');
    }
    setValidationError('');
    if (sendFormStatus) {
        sendFormStatus.textContent = '';
    }
}

sendForm.addEventListener('submit', function(event) {
    resetValidationState();
    const errors = [];

    if (!messageBody.value.trim()) {
        errors.push('Message body is required.');
        messageBody.classList.add('is-invalid');
    }

    if (targetEvent.checked) {
        if (!eventSelect.value) {
            errors.push('Please select an event for the message.');
            eventSelect.classList.add('is-invalid');
        }
    }

    if (scheduleLater.checked) {
        if (!scheduleDate.value) {
            errors.push('Schedule date is required when scheduling.');
            scheduleDate.classList.add('is-invalid');
        }
        if (!scheduleTime.value) {
            errors.push('Schedule time is required when scheduling.');
            scheduleTime.classList.add('is-invalid');
        }
    }

    if (testMode && testMode.checked && testRecipientPhone && testRecipientModeOne && testRecipientModeOne.checked && !testRecipientPhone.value) {
        errors.push('Choose one saved test recipient before using test mode.');
        testRecipientPhone.classList.add('is-invalid');
    }

    if (errors.length > 0) {
        event.preventDefault();
        setValidationError(errors[0]);
        if (sendFormStatus) {
            sendFormStatus.textContent = 'Please resolve the highlighted errors before sending.';
        }
        return;
    }

    sendBtn.disabled = true;
    if (scheduleLater.checked) {
        sendBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Scheduling...';
        if (sendFormStatus) {
            sendFormStatus.textContent = 'Scheduling your message. Please wait.';
        }
    } else {
        sendBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Sending...';
        if (sendFormStatus) {
            sendFormStatus.textContent = 'Sending your message. Please wait.';
        }
    }
});

sendForm.addEventListener('input', resetValidationState);

// ===== Chart.js Initialization =====

// Chart data from server
const chartData = dashboardSettings.chartData;

// Delivery Trends Chart (Line/Area)
const trendsCtx = document.getElementById('deliveryTrendsChart');
if (trendsCtx && chartData.trends) {
    new Chart(trendsCtx, {
        type: 'line',
        data: {
            labels: chartData.trends.labels,
            datasets: [
                {
                    label: 'Provider Accepted',
                    data: chartData.trends.sent,
                    borderColor: '#10b981',
                    backgroundColor: 'rgba(16, 185, 129, 0.1)',
                    fill: true,
                    tension: 0.4,
                    pointBackgroundColor: '#10b981',
                    pointBorderColor: '#fff',
                    pointBorderWidth: 2,
                    pointRadius: 4,
                    pointHoverRadius: 6
                },
                {
                    label: 'Submission Failed',
                    data: chartData.trends.failed,
                    borderColor: '#ef4444',
                    backgroundColor: 'rgba(239, 68, 68, 0.1)',
                    fill: true,
                    tension: 0.4,
                    pointBackgroundColor: '#ef4444',
                    pointBorderColor: '#fff',
                    pointBorderWidth: 2,
                    pointRadius: 4,
                    pointHoverRadius: 6
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                intersect: false,
                mode: 'index'
            },
            plugins: {
                legend: {
                    position: 'top',
                    labels: {
                        usePointStyle: true,
                        padding: 20,
                        font: { size: 12, weight: '500' }
                    }
                },
                tooltip: {
                    backgroundColor: '#1e293b',
                    titleFont: { size: 13, weight: '600' },
                    bodyFont: { size: 12 },
                    padding: 12,
                    cornerRadius: 8,
                    displayColors: true
                }
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { font: { size: 11 }, color: '#64748b' }
                },
                y: {
                    beginAtZero: true,
                    grid: { color: 'rgba(0,0,0,0.05)' },
                    ticks: { font: { size: 11 }, color: '#64748b', stepSize: 1 }
                }
            }
        }
    });
} else if (trendsCtx) {
    // No data - show empty state
    trendsCtx.parentElement.innerHTML = `
        <div class="empty-state">
            <i class="bi bi-graph-up empty-state__icon" aria-hidden="true"></i>
            <div class="empty-state__title">No Trend Data Yet</div>
            <div class="empty-state__text">Send messages to see submission trends.</div>
        </div>
    `;
}

// Audience Donut Chart
const audienceCtx = document.getElementById('audienceChart');
if (audienceCtx) {
    const communityCount = dashboardSettings.communityCount;
    const eventCount = dashboardSettings.eventCount;
    const unsubCount = dashboardSettings.unsubCount;

    if (communityCount > 0 || eventCount > 0 || unsubCount > 0) {
        new Chart(audienceCtx, {
            type: 'doughnut',
            data: {
                labels: ['Contact List', 'Event Contacts', 'Opt-Outs'],
                datasets: [{
                    data: [communityCount, eventCount, unsubCount],
                    backgroundColor: ['#6366f1', '#8b5cf6', '#e2e8f0'],
                    borderWidth: 0,
                    hoverOffset: 8
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                layout: {
                    padding: 6
                },
                cutout: '65%',
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: '#1e293b',
                        titleFont: { size: 13, weight: '600' },
                        bodyFont: { size: 12 },
                        padding: 12,
                        cornerRadius: 8
                    }
                }
            }
        });
    } else {
        audienceCtx.parentElement.innerHTML = `
            <div class="empty-state">
                <i class="bi bi-people empty-state__icon" aria-hidden="true"></i>
                <div class="empty-state__title">No Recipients Yet</div>
                <div class="empty-state__text">Add contacts or registrations to build your audience.</div>
                <a href="${dashboardSettings.contactsUrl}" class="btn btn-sm btn-outline-primary empty-state__action">Open Contacts</a>
            </div>
        `;
    }
}
