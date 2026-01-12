// Claude Provider - Placeholder
const ClaudeProvider = {
  name: 'claude',
  displayName: 'Claude',
  hostPatterns: ['*://claude.ai/*'],

  selectors: {
    textarea: [],      // TODO: Add Claude selectors
    sendButton: [],
    responseContainer: [],
    streamingIndicator: [],
    markdownContent: null,
    modelIndicator: [
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
    return 'claude-sonnet'; // Default assumption
  },

  detectStreamingComplete: function(document) {
    // TODO: Implement for Claude
    return true;
  },

  extractResponseText: function(container) {
    // TODO: Implement for Claude
    return container.textContent.trim();
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
