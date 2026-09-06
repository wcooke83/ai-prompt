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
    
    // Send button selectors. DeepSeek migrated its button markup from "ds-icon-button" to
    // "ds-button" (the old class no longer appears anywhere in the DOM), so match the current
    // circular primary button. The stable SVG-path identifier lives in content.js's
    // clickSendButtonDeepSeek(), which does the actual clicking for this provider.
    sendButton: [
      'div[role="button"].ds-button--primary.ds-button--filled.ds-button--circle',
      'div[role="button"].ds-button--filled.ds-button--circle'
    ],
    
    // Copy button selectors. The copy control is now an icon-only div[role="button"] (no "Copy"
    // text); it's identified by its SVG path (see findCopyButton below). Kept for reference.
    copyButton: [
      'div[role="button"].ds-button svg path[d^="M6.14929 4.02032"]'
    ],
    
    // Attach button - paperclip icon (identified by the paperclip SVG path in
    // uploadFilesDeepSeek; these selectors are fallbacks matching the new ds-button markup)
    attachButton: [
      'div[role="button"].ds-button svg path[d^="M5.5498 9.75V5H"]',
      'div.bf38813a div[role="button"].ds-button--iconLabelPrimary'
    ],
    
    // Response container - to detect when AI responds
    responseContainer: [
      'div.ds-message'
    ],
    
    // Streaming indicator - to know when response is complete
    streamingIndicator: [
      'div[class*="streaming"]',
      'div[class*="typing"]',
      'button[aria-label*="Stop"]'
    ],
    
    // Markdown content within responses. The assistant's dedicated answer container is the ONLY
    // reliable target — `div.ds-markdown` alone is now ambiguous: the reasoning trace is ALSO
    // rendered as ds-markdown and appears BEFORE the answer in the DOM.
    markdownContent: [
      'div.ds-assistant-message-main-content',
      'div.ds-markdown'
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
    clone.querySelectorAll('button, [role="button"], svg, .md-code-block-banner-wrap').forEach(el => el.remove());
    // Drop the reasoning/thinking panel. Its exact class varies across DeepSeek builds, so match
    // defensively on think/reason (the answer container is "markdown"/"ds-markdown", never matched).
    clone
      .querySelectorAll('[class*="think" i], [class*="reason" i], [class*="chain-of-thought" i]')
      .forEach(el => el.remove());

    // Prefer the assistant's dedicated answer container (ds-assistant-message-main-content), which
    // only exists once the real answer renders. `div.ds-markdown` is NOT sufficient — the reasoning
    // trace is now also rendered as ds-markdown and precedes the answer in the DOM, so matching it
    // first returns the chain-of-thought instead of the answer.
    for (const selector of ['div.ds-assistant-message-main-content', ...this.selectors.markdownContent]) {
      const markdown = clone.querySelector(selector);
      if (markdown) {
        const text = (markdown.textContent || '').trim();
        if (text) {
          console.log(`[DeepSeek] extractResponseText matched "${selector}" -> ${text.length} chars, preview: "${text.substring(0, 80).replace(/\s+/g, ' ')}"`);
          return { text, images: [] };
        }
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
    const actionButtons = parent?.querySelector('div.ds-flex div[role="button"].ds-button') ||
                          latestMessage.nextElementSibling?.querySelector('div[role="button"].ds-button');
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
        latest.parentElement?.querySelector('div.ds-flex div[role="button"].ds-button') ||
        latest.nextElementSibling?.querySelector('div[role="button"].ds-button')
      );
      // The answer renders into the dedicated ds-assistant-message-main-content container, which does
      // NOT exist while the model is only showing its reasoning. (The reasoning trace is now also
      // rendered as ds-markdown, so ds-markdown / pre code can't distinguish it from the answer.)
      const hasAnswer = !!latest.querySelector('div.ds-assistant-message-main-content');

      return !hasToolbar && !hasAnswer;
    } catch (e) {
      // Be conservative: if anything goes wrong, don't claim "busy" (let normal detection proceed).
      return false;
    }
  },
  
  // Find copy button specifically for DeepSeek.
  // The copy control is an icon-only div[role="button"] (no "Copy" text) that lives in the action
  // toolbar, rendered as a SIBLING of the ds-message inside its wrapper — so search from the
  // message's parent wrapper rather than the message itself.
  findCopyButton: function(responseElement) {
    const scope = responseElement?.parentElement || responseElement;
    if (!scope) return null;

    // The copy icon path is the only stable identifier (button class names are hashed).
    const copyIconPath = scope.querySelector('svg path[d^="M6.14929 4.02032"]');
    if (copyIconPath) {
      return copyIconPath.closest('div[role="button"]') || copyIconPath.closest('button');
    }

    // Fallback: any button bearing "Copy" text (older layouts).
    for (const btn of scope.querySelectorAll('div[role="button"], button')) {
      if ((btn.textContent || '').includes('Copy')) return btn;
    }

    return null;
  }
};

// Register the provider (if using ProviderRegistry). Auto-register in content script context
// (background/service-worker context registers explicitly in background.js instead, once all
// provider files have loaded)
if (self.ProviderRegistry) {
  console.log('[DeepSeek] Registering provider with ProviderRegistry');
  self.ProviderRegistry.register(DeepSeekProvider);
}

// Export for use in other files
if (typeof module !== 'undefined' && module.exports) {
  module.exports = DeepSeekProvider;
}