(() => {
    const textarea = document.getElementById('response_body');
    const stats = document.getElementById('keywordResponseStats');
    if (!textarea || !stats || !window.TwineviaSmsAnalysis) {
        return;
    }

    const updateStats = () => {
        const analysis = window.TwineviaSmsAnalysis.analyzeBody(textarea.value || '');
        const normalizationNote = analysis.normalizationApplied
            ? ` Normalizes ${analysis.replacementCount} punctuation/spacing character(s) before save/send.`
            : '';
        stats.textContent = `Encoding: ${window.TwineviaSmsAnalysis.formatEncodingLabel(analysis.encoding)}. ${analysis.segmentCount} segment(s). ${analysis.charactersToNextSegment} characters until the next segment.${normalizationNote}`;
    };

    textarea.addEventListener('input', updateStats);
    updateStats();
})();
