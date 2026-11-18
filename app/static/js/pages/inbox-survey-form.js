function bindSmsStats(textareaId, statsId) {
    const textarea = document.getElementById(textareaId);
    const stats = document.getElementById(statsId);
    if (!textarea || !stats || !window.TwineviaSmsAnalysis) {
        return;
    }

    const updateStats = () => {
        const analysis = window.TwineviaSmsAnalysis.analyzeBody(textarea.value || '');
        const normalizationNote = analysis.normalizationApplied
            ? ` Normalizes ${analysis.replacementCount} punctuation/spacing character(s) before save/send.`
            : '';
        stats.textContent = analysis.segmentCount > 0
            ? `Encoding: ${window.TwineviaSmsAnalysis.formatEncodingLabel(analysis.encoding)}. ${analysis.segmentCount} segment(s). ${analysis.charactersToNextSegment} characters until the next segment.${normalizationNote}`
            : 'Optional message. Leave blank to skip sending this step.';
    };

    textarea.addEventListener('input', updateStats);
    updateStats();
}

const eventModeInputs = document.querySelectorAll('.js-event-link-mode');
const eventModeOptions = document.querySelectorAll('.survey-event-sync__option');
const existingEventFields = document.getElementById('existingEventFields');
const newEventFields = document.getElementById('newEventFields');

function syncEventLinkMode() {
    const selected = document.querySelector('.js-event-link-mode:checked');
    const mode = selected ? selected.value : 'none';

    eventModeOptions.forEach((option) => {
        option.classList.remove('is-selected');
    });
    if (selected) {
        const selectedOption = selected.closest('.survey-event-sync__option');
        if (selectedOption) {
            selectedOption.classList.add('is-selected');
        }
    }

    if (existingEventFields) {
        existingEventFields.classList.toggle('d-none', mode !== 'existing');
    }
    if (newEventFields) {
        newEventFields.classList.toggle('d-none', mode !== 'new');
    }
}

eventModeInputs.forEach((input) => {
    input.addEventListener('change', syncEventLinkMode);
});
syncEventLinkMode();
bindSmsStats('intro_message', 'introMessageStats');
bindSmsStats('completion_message', 'completionMessageStats');
