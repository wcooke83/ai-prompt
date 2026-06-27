// DeepSeek Provider Configuration
const DeepSeekProvider = {
  name: 'deepseek',
  displayName: 'DeepSeek',
  hostPatterns: [
    '*://chat.deepseek.com/*'
  ],

  // DeepSeek is a reasoning model: it shows a "Thought for N seconds" trace and may run a web search
  // BEFORE it writes the actual answer, stalling the visible text for many seconds. Raise the generic
  // stability-fallback floor well above the 4s default so a reasoning pause is never mistaken for a
  // finished response (the `isLikelyStillGenerating` veto below is the primary guard; this is a backstop).
  stabilityMs: 12000,

  selectors: {
    // Textarea selectors - multiple options for reliability
    textarea: [
      'textarea[placeholder="Message DeepSeek"]',
      'textarea._27c9245.ds-scroll-area',
      'textarea.ds-scroll-area',
      'div.aaff8b8f textarea'
    ],
    
    // Send button selectors - looking for the upload arrow icon
    sendButton: [
      'div.ds-icon-button[role="button"]:not([aria-disabled="true"]) svg path[d^="M8.3125 0.981587"]',
      'div.bf38813a div.ds-icon-button[role="button"]:not(.ds-icon-button--disabled)',
      'div._7436101.ds-icon-button[role="button"]:not([aria-disabled="true"])'
    ],
    
    // Copy button selectors - for the copy icon with "Copy" text
    copyButton: [
      'button.ds-text-button .code-info-button-text',
      'button.ds-text-button:has(.code-info-button-text)',
      'button.ds-atom-button:has(span:contains("Copy"))',
      'button[role="button"].ds-text-button'
    ],
    
    // Attach button - paperclip icon
    attachButton: [
      'div.f02f0e25.ds-icon-button[role="button"]',
      'div.bf38813a div.ds-icon-button[role="button"]'
    ],
    
    // Response container - to detect when AI responds
    responseContainer: [
      'div.ds-message',
      'div[class*="message"]',
      'div.ce41ed1b',
      '[role="article"]'
    ],
    
    // Streaming indicator - to know when response is complete
    streamingIndicator: [
      'div[class*="streaming"]',
      'div[class*="typing"]',
      'button[aria-label*="Stop"]'
    ],
    
    // Markdown content within responses
    markdownContent: [
      'div[class*="markdown"]',
      'div[class*="content"]',
      'div.ds-message-content'
    ],

    // Upload progress indicators - present while upload is in progress
    uploadProgress: [
      'div.ds-progress',
      'div[class*="progress"]',
      'div[class*="loading"]',
      'div[class*="uploading"]'
    ],

    // File preview container - appears when file is uploaded
    uploadedFilePreview: [
      'div.ds-file-card',
      'div[class*="file-card"]',
      'div[class*="attachment"]',
      'div.bf38813a div[class*="file"]'
    ]
  },
  
  // Detect which model is being used
  detectModel: function(document) {
    // DeepSeek typically shows model info in the UI
    // Look for model selector or indicator
    const modelIndicator = document.querySelector('[class*="model"]');
    if (modelIndicator) {
      const text = modelIndicator.textContent.toLowerCase();
      if (text.includes('deepthink')) return 'deepseek-r1';
      if (text.includes('v3')) return 'deepseek-v3';
      if (text.includes('v2.5')) return 'deepseek-v2.5';
    }
    return 'deepseek-chat'; // Default
  },
  
  // Extract response text from a response element.
  // CRITICAL: exclude DeepSeek's "thinking"/reasoning trace so we return the ANSWER, not the
  // chain-of-thought. The reasoning is rendered in its own block (the "Thought for N seconds" panel);
  // the answer renders into a dedicated markdown container that only appears once the answer starts.
  extractResponseText: function(responseElement) {
    // Work on a clone so stripping the reasoning + UI chrome never touches the live page.
    const clone = responseElement.cloneNode(true);
    clone.querySelectorAll('button, [role="button"], svg').forEach(el => el.remove());
    // Drop the reasoning/thinking panel. Its exact class varies across DeepSeek builds, so match
    // defensively on think/reason (the answer container is "markdown"/"ds-markdown", never matched).
    clone
      .querySelectorAll('[class*="think" i], [class*="reason" i], [class*="chain-of-thought" i]')
      .forEach(el => el.remove());

    // Prefer the dedicated answer markdown container (present only once the real answer renders).
    for (const selector of ['div.ds-markdown', ...this.selectors.markdownContent]) {
      const markdown = clone.querySelector(selector);
      if (markdown) {
        const text = (markdown.textContent || '').trim();
        if (text) return { text, images: [] };
      }
    }

    // Fallback: whatever text remains after the reasoning block was stripped.
    return { text: (clone.textContent || '').trim(), images: [] };
  },
  
  // Detect if streaming is complete.
  // Completion is the answer's action toolbar (copy / regenerate) appearing after the message — but
  // ONLY once the model is no longer reasoning/searching (see isLikelyStillGenerating), so the
  // transient state during "thinking" is never read as done.
  detectStreamingComplete: function(document) {
    // Never complete while the model is still thinking / searching / writing.
    if (typeof this.isLikelyStillGenerating === 'function' && this.isLikelyStillGenerating(document)) {
      return false;
    }

    // Explicit streaming indicators (rarely match DeepSeek's div-based controls, but cheap to check).
    for (const selector of this.selectors.streamingIndicator) {
      if (document.querySelector(selector)) {
        return false;
      }
    }

    // Find latest assistant message
    const messages = document.querySelectorAll('div.ds-message');
    if (messages.length === 0) return false;

    const latestMessage = messages[messages.length - 1];

    // Complete once the answer's action toolbar (copy / regenerate) has rendered after the message…
    const parent = latestMessage.parentElement;
    const actionButtons = parent?.querySelector('div.ds-flex div.ds-icon-button[role="button"]') ||
                          latestMessage.nextElementSibling?.querySelector('div.ds-icon-button[role="button"]');
    if (actionButtons) return true;

    // …or once a Copy button is present for the answer (alternate DOM where the toolbar is inline).
    if (typeof this.findCopyButton === 'function' && this.findCopyButton(latestMessage)) return true;

    return false;
  },

  // Is DeepSeek still working (reasoning / searching / writing)? When true, the generic stability
  // fallback in content.js is vetoed so a reasoning pause can't be mistaken for the finished answer.
  //
  // Primary signal (robust, confirmed from real logs): during the "thinking" phase the message has
  // NO answer-markdown container and NO completion toolbar yet — it holds only the reasoning trace.
  // We treat that as "still working" and keep waiting for the answer to render.
  isLikelyStillGenerating: function(document) {
    try {
      // An explicit stop / generating affordance is showing (best-effort; DeepSeek mostly uses divs).
      if (document.querySelector('button[aria-label*="Stop" i], [class*="stop-generat" i]')) {
        return true;
      }

      const messages = document.querySelectorAll('div.ds-message');
      if (messages.length === 0) return false;
      const latest = messages[messages.length - 1];

      const hasToolbar = !!(
        latest.parentElement?.querySelector('div.ds-flex div.ds-icon-button[role="button"]') ||
        latest.nextElementSibling?.querySelector('div.ds-icon-button[role="button"]')
      );
      // The answer renders into a dedicated markdown / code container that does NOT exist while the
      // model is only showing its reasoning. No answer container + no toolbar ⇒ still working.
      const hasAnswer = !!latest.querySelector('div.ds-markdown, div[class*="markdown"], pre code');

      return !hasToolbar && !hasAnswer;
    } catch (e) {
      // Be conservative: if anything goes wrong, don't claim "busy" (let normal detection proceed).
      return false;
    }
  },
  
  // Find copy button specifically for DeepSeek
  findCopyButton: function(responseElement) {
    // Method 1: Find by the specific "Copy" text
    const buttons = responseElement.querySelectorAll('button[role="button"].ds-text-button');
    for (const btn of buttons) {
      const copyText = btn.querySelector('.code-info-button-text');
      if (copyText && copyText.textContent.includes('Copy')) {
        return btn;
      }
    }
    
    // Method 2: Find by SVG path (the copy icon)
    const copyIconPath = responseElement.querySelector('svg path[d^="M6.14923 4.02032"]');
    if (copyIconPath) {
      return copyIconPath.closest('button');
    }
    
    // Method 3: Find button with "Copy" text anywhere
    for (const btn of buttons) {
      if (btn.textContent.includes('Copy')) {
        return btn;
      }
    }
    
    return null;
  }
};

// Register the provider (if using ProviderRegistry)
if (typeof window !== 'undefined' && window.ProviderRegistry) {
  console.log('[DeepSeek] Registering provider with ProviderRegistry');
  window.ProviderRegistry.register(DeepSeekProvider);
}

// Export for use in other files
if (typeof module !== 'undefined' && module.exports) {
  module.exports = DeepSeekProvider;
}