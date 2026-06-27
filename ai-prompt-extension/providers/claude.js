// Claude Provider
// Best-effort selectors for claude.ai. Claude renders the composer as a ProseMirror contenteditable
// and each assistant turn as a `.font-claude-message` inside a `div[data-is-streaming]` whose
// attribute flips "true"→"false" when generation finishes (a reliable completion signal).
// claude.ai changes its DOM periodically — if extraction breaks, the content script's [diag] logs
// (on the stability fallback / timeout) show the live DOM so these selectors can be corrected.
const ClaudeProvider = {
  name: 'claude',
  displayName: 'Claude',
  hostPatterns: ['*://claude.ai/*'],

  selectors: {
    textarea: [
      'div[contenteditable="true"].ProseMirror',
      'div.ProseMirror[contenteditable="true"]',
      'fieldset div[contenteditable="true"]',
      'div[contenteditable="true"][translate="no"]',
      'div[contenteditable="true"]'
    ],
    sendButton: [
      'button[aria-label="Send message"]',
      'button[aria-label="Send Message"]',
      'button[aria-label*="Send"]',
      'fieldset button[type="submit"]'
    ],
    // Each assistant turn; data-is-streaming flips false when done.
    responseContainer: [
      'div[data-is-streaming]',
      'div.font-claude-message'
    ],
    streamingIndicator: [
      'div[data-is-streaming="true"]',
      'button[aria-label="Stop response"]',
      'button[aria-label*="Stop"]'
    ],
    markdownContent: [
      '.font-claude-message',
      'div[class*="prose"]'
    ],
    modelIndicator: [
      'button[data-testid="model-selector-dropdown"]',
      'button[data-testid="model-selector"]',
      '[data-testid="model-selector"] span',
      'div[class*="model"] span'
    ]
  },

  // Detect current model from UI
  detectModel: function(document) {
    for (const selector of this.selectors.modelIndicator) {
      const el = document.querySelector(selector);
      if (el) {
        const text = el.textContent.trim().toLowerCase();
        if (text.includes('opus')) return 'claude-opus';
        if (text.includes('sonnet')) return 'claude-sonnet';
        if (text.includes('haiku')) return 'claude-haiku';
        if (text) return text;
      }
    }
    return 'claude';
  },

  detectStreamingComplete: function(document) {
    console.log('[Claude] ═══ Detecting streaming complete ═══');
    // Still streaming if a stop button or an actively-streaming turn is present.
    if (document.querySelector('button[aria-label="Stop response"]') ||
        document.querySelector('button[aria-label*="Stop"]')) {
      console.log('[Claude] ✗ Stop button present → still streaming');
      return false;
    }
    if (document.querySelector('div[data-is-streaming="true"]')) {
      console.log('[Claude] ✗ data-is-streaming="true" present → still streaming');
      return false;
    }
    // Complete if at least one finished assistant message exists.
    const done = document.querySelectorAll('div[data-is-streaming="false"]').length > 0 ||
                 document.querySelectorAll('div.font-claude-message').length > 0;
    console.log('[Claude] Result:', done ? '✓ COMPLETE' : '✗ no finished message yet');
    return done;
  },

  extractResponseText: function(container) {
    console.log('[Claude] Extracting from', container.tagName, (container.className || '').toString().substring(0, 60));
    // Prefer the rendered assistant message body.
    const body = container.querySelector('.font-claude-message') || container;

    // JSON answers usually land in a code block — return that verbatim.
    const codeBlocks = body.querySelectorAll('pre code');
    console.log('[Claude] code blocks:', codeBlocks.length);
    if (codeBlocks.length === 1) {
      return { text: codeBlocks[0].textContent, images: [] };
    }
    if (codeBlocks.length > 1) {
      const parts = [];
      codeBlocks.forEach(code => {
        const langMatch = code.className.match(/language-(\w+)/);
        const lang = langMatch ? langMatch[1] : '';
        parts.push('```' + lang + '\n' + code.textContent + '\n```');
      });
      return { text: parts.join('\n\n'), images: [] };
    }

    // Otherwise take the message text, minus UI chrome.
    const clone = body.cloneNode(true);
    clone.querySelectorAll('button, [role="button"], svg').forEach(el => el.remove());
    const text = clone.textContent.trim();
    console.log('[Claude] extracted textLen:', text.length);
    return { text, images: [] };
  }
};

if (typeof window !== 'undefined') {
  window.ClaudeProvider = ClaudeProvider;
  // Auto-register in content script context
  if (window.ProviderRegistry) {
    console.log('[Claude] Registering provider with ProviderRegistry');
    window.ProviderRegistry.register(ClaudeProvider);
  }
}
