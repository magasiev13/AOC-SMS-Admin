(() => {
  const NORMALIZATION_REPLACEMENTS = new Map([
    ['’', "'"],
    ['‘', "'"],
    ['“', '"'],
    ['”', '"'],
    ['–', '-'],
    ['—', '-'],
    ['…', '...'],
    ['\u00a0', ' '],
    ['\u2007', ' '],
    ['\u2009', ' '],
    ['\u202f', ' '],
    ['•', '-'],
  ]);

  const GSM_7_BASIC_CHARSET = new Set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ !\"#¤%&'()*+,-./0123456789:;<=>?" +
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
  );
  const GSM_7_EXTENDED_CHARSET = new Set("^{}\\[~]|€");

  function normalizeBody(body = '') {
    const replacementMap = new Map();
    let normalizedBody = '';
    for (const char of body) {
      if (!NORMALIZATION_REPLACEMENTS.has(char)) {
        normalizedBody += char;
        continue;
      }
      const replacement = NORMALIZATION_REPLACEMENTS.get(char);
      normalizedBody += replacement;
      const key = `${char}=>${replacement}`;
      replacementMap.set(key, {
        from: char,
        to: replacement,
        count: (replacementMap.get(key)?.count || 0) + 1,
      });
    }

    return {
      normalizedBody,
      normalizationApplied: normalizedBody !== body,
      normalizedCharacterDelta: normalizedBody.length - body.length,
      normalizedCharacterSavings: Math.max(0, body.length - normalizedBody.length),
      replacementCount: Array.from(replacementMap.values()).reduce((sum, entry) => sum + entry.count, 0),
      replacements: Array.from(replacementMap.values()).sort((left, right) => {
        if (left.from === right.from) {
          return left.to.localeCompare(right.to);
        }
        return left.from.localeCompare(right.from);
      }),
    };
  }

  function bodyMetrics(body = '') {
    if (!body) {
      return {
        encoding: 'gsm-7',
        segmentCount: 0,
        charactersUsed: 0,
        charactersToNextSegment: 160,
        segmentLimit: 160,
      };
    }

    let encoding = 'gsm-7';
    let charactersUsed = 0;
    for (const char of body) {
      if (GSM_7_BASIC_CHARSET.has(char)) {
        charactersUsed += 1;
        continue;
      }
      if (GSM_7_EXTENDED_CHARSET.has(char)) {
        charactersUsed += 2;
        continue;
      }
      encoding = 'ucs-2';
      break;
    }

    let singleLimit = 160;
    let multiLimit = 153;
    if (encoding === 'ucs-2') {
      charactersUsed = body.length;
      singleLimit = 70;
      multiLimit = 67;
    }

    let segmentCount = 1;
    let segmentLimit = singleLimit;
    if (charactersUsed > singleLimit) {
      segmentCount = Math.floor((charactersUsed - 1) / multiLimit) + 1;
      segmentLimit = segmentCount * multiLimit;
    }

    return {
      encoding,
      segmentCount,
      charactersUsed,
      charactersToNextSegment: Math.max(0, segmentLimit - charactersUsed),
      segmentLimit,
    };
  }

  function analyzeBody(body = '', { applyNormalization = true } = {}) {
    const originalBody = body || '';
    const normalized = applyNormalization
      ? normalizeBody(originalBody)
      : {
          normalizedBody: originalBody,
          normalizationApplied: false,
          normalizedCharacterDelta: 0,
          normalizedCharacterSavings: 0,
          replacementCount: 0,
          replacements: [],
        };

    const originalMetrics = bodyMetrics(originalBody);
    const normalizedMetrics = bodyMetrics(normalized.normalizedBody);

    return {
      originalBody,
      normalizedBody: normalized.normalizedBody,
      normalizationApplied: normalized.normalizationApplied,
      normalizedCharacterDelta: normalized.normalizedCharacterDelta,
      normalizedCharacterSavings: normalized.normalizedCharacterSavings,
      replacementCount: normalized.replacementCount,
      replacements: normalized.replacements,
      encoding: normalizedMetrics.encoding,
      segmentCount: normalizedMetrics.segmentCount,
      charactersUsed: normalizedMetrics.charactersUsed,
      charactersToNextSegment: normalizedMetrics.charactersToNextSegment,
      segmentLimit: normalizedMetrics.segmentLimit,
      originalEncoding: originalMetrics.encoding,
      originalSegmentCount: originalMetrics.segmentCount,
      originalCharactersUsed: originalMetrics.charactersUsed,
      originalCharactersToNextSegment: originalMetrics.charactersToNextSegment,
      originalSegmentLimit: originalMetrics.segmentLimit,
      normalizedSegmentDelta: normalizedMetrics.segmentCount - originalMetrics.segmentCount,
      segmentsSaved: Math.max(0, originalMetrics.segmentCount - normalizedMetrics.segmentCount),
    };
  }

  function formatEncodingLabel(encoding) {
    return String(encoding || '').toLowerCase() === 'ucs-2' ? 'UCS-2' : 'GSM-7';
  }

  function specialCharacter(body = '') {
    for (const character of normalizeBody(body).normalizedBody) {
      if (!GSM_7_BASIC_CHARSET.has(character) && !GSM_7_EXTENDED_CHARSET.has(character)) {
        return { character, codepoint: `U+${character.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')}` };
      }
    }
    return null;
  }

  function usesUnicodeWriting(body = '') {
    const letters = Array.from(normalizeBody(body).normalizedBody).filter(character => /\p{L}/u.test(character));
    const unicodeLetters = letters.filter(character => !GSM_7_BASIC_CHARSET.has(character) && !GSM_7_EXTENDED_CHARSET.has(character));
    return letters.length > 0 && unicodeLetters.length / letters.length >= 0.2;
  }

  function replaceSpecialCharacters(body = '') {
    // Never transliterate or replace written language with question marks.
    // A substantial Unicode passage should keep even its punctuation intact.
    if (usesUnicodeWriting(body)) return body;
    const normalized = normalizeBody(body).normalizedBody;
    const withoutPictures = normalized
      .replace(/[0-9#*]\uFE0F?\u20E3/gu, '')
      .replace(/(?:\p{Extended_Pictographic}|\p{Regional_Indicator}|\p{Emoji_Modifier})[\uFE0E\uFE0F]?\p{Emoji_Modifier}?[\u{E0020}-\u{E007F}]*(?:\u200D(?:\p{Extended_Pictographic}|\p{Regional_Indicator}|\p{Emoji_Modifier})[\uFE0E\uFE0F]?\p{Emoji_Modifier}?[\u{E0020}-\u{E007F}]*)*/gu, '');
    return withoutPictures === normalized ? normalized : withoutPictures.replace(/[ \t]{2,}/g, ' ').trim();
  }

  window.TwineviaSmsAnalysis = {
    analyzeBody,
    bodyMetrics,
    formatEncodingLabel,
    normalizeBody,
    specialCharacter,
    replaceSpecialCharacters,
    usesUnicodeWriting,
  };
})();
