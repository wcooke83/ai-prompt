// Grok Provider - Full implementation for grok.com
const GrokProvider = {
  name: 'grok',
  displayName: 'Grok',
  hostPatterns: ['*://grok.com/*', '*://grok.x.ai/*', '*://x.com/i/grok*'],

  selectors: {
    textarea: [
      '.query-bar div.tiptap.ProseMirror[contenteditable="true"]',
      'div.tiptap.ProseMirror[contenteditable="true"]',
      'div.ProseMirror[contenteditable="true"]',
      '[contenteditable="true"].ProseMirror',
      '[contenteditable="true"].tiptap'
    ],
    sendButton: [
      'button[type="submit"][aria-label="Submit"]',
      'button[aria-label="Submit"]',
      '.query-bar button[type="submit"]'
    ],
    responseContainer: [
      'div[id^="response-"]',
      'div.message-bubble'
    ],
    streamingIndicator: [
      'button[aria-label="Stop"]',
      'button[aria-label="Stop generating"]'
    ],
    attachButton: [
      'button[aria-label="Attach"]'
    ],
    uploadMenuItem: [
      '[role="menuitem"]'  // We'll filter by text content "Upload a file"
    ],
    markdownContent: [
      'div.response-content-markdown',
      'div.markdown'
    ],
    modelIndicator: [
      'button[aria-label*="model"]',
      '[data-testid="model-selector"]',
      'div[class*="model-select"] span'
    ],

    // Upload progress indicators - present while upload is in progress
    uploadProgress: [
      'div[class*="upload-progress"]',
      'div[class*="uploading"]',
      'svg.animate-spin',
      'div[class*="loading"]'
    ],

    // File preview container - appears when file is uploaded
    uploadedFilePreview: [
      '.query-bar div[class*="attachment"]',
      '.query-bar div[class*="file"]',
      '.query-bar img[src*="blob:"]',
      'div[class*="thumbnail"]'
    ]
  },

  // Detect current model from UI
  detectModel: function(document) {
    for (const selector of this.selectors.modelIndicator) {
      const el = document.querySelector(selector);
      if (el) {
        const text = el.textContent.trim().toLowerCase();
        if (text.includes('grok-2')) return 'grok-2';
        if (text.includes('grok-3')) return 'grok-3';
        if (text) return text;
      }
    }
    return 'grok-2'; // Default assumption
  },

  detectStreamingComplete: function(document, responseElement = null) {
    // Check if stop button is present (still streaming)
    for (const selector of this.selectors.streamingIndicator) {
      if (document.querySelector(selector)) {
        console.log('[Grok] Stop button found, still streaming');
        return false;
      }
    }

    // Use specific response element if provided, otherwise find latest
    let target = responseElement;
    console.log('[Grok] detectStreamingComplete called, responseElement:', !!responseElement, responseElement?.id);

    if (!target) {
      const responses = document.querySelectorAll('div[id^="response-"]');
      console.log('[Grok] No element provided, found', responses.length, 'responses');
      if (responses.length === 0) return false;
      target = responses[responses.length - 1];
    }

    console.log('[Grok] Checking target element:', target.id, 'classes:', target.className.substring(0, 50));

    // Check for action-buttons div with last-response class (only appears after streaming complete)
    const actionButtons = target.querySelector('.action-buttons.last-response');
    console.log('[Grok] Found action-buttons.last-response:', !!actionButtons);

    // Also try just action-buttons and check for last-response separately
    const allActionButtons = target.querySelector('.action-buttons');
    if (allActionButtons) {
      console.log('[Grok] Found action-buttons, has last-response class:', allActionButtons.classList.contains('last-response'));
    }

    if (actionButtons) {
      // Verify it has the completion indicators (Regenerate, Like, Dislike buttons)
      const hasRegenerate = actionButtons.querySelector('button[aria-label="Regenerate"]');
      const hasLike = actionButtons.querySelector('button[aria-label="Like"]');
      console.log('[Grok] Action buttons found - Regenerate:', !!hasRegenerate, 'Like:', !!hasLike);
      if (hasRegenerate && hasLike) {
        return true;
      }
    }

    console.log('[Grok] No action-buttons.last-response found, still streaming');
    return false;
  },

  extractResponseText: function(container) {
    // Extract images first
    const images = [];
    const seenSrcs = new Set(); // Deduplicate images (Grok shows same image multiple times for effects)
    const imageViewers = container.querySelectorAll('[data-testid="image-viewer"]');
    let imagePrompt = ''; // For generated image responses

    imageViewers.forEach(viewer => {
      // Check if this is a generated image response (has aria-label with prompt info)
      const ariaLabel = viewer.getAttribute('aria-label') || '';
      if (ariaLabel.includes('generated images with the prompt')) {
        imagePrompt = ariaLabel;
      }

      // For generated images, look for the main image (z-[200] class)
      // For web images, get all unique images
      viewer.querySelectorAll('img').forEach(img => {
        const src = img.getAttribute('src');
        if (!src || seenSrcs.has(src)) return;

        // Skip blurred/effect images (they have filter styles or lower z-index)
        const parent = img.closest('div[style]');
        if (parent) {
          const style = parent.getAttribute('style') || '';
          if (style.includes('blur') || style.includes('filter')) return;
        }

        seenSrcs.add(src);
        const alt = img.getAttribute('alt') || '';
        // Get source link if available (for web images)
        const sourceLink = img.closest('.group\\/image')?.querySelector('a[href]');
        const sourceUrl = sourceLink?.getAttribute('href') || '';
        images.push({ src, alt, sourceUrl });
      });
    });

    // Extract text, removing image containers
    let text = '';
    const messageBubble = container.querySelector('div.message-bubble');
    if (messageBubble) {
      const markdown = messageBubble.querySelector('div.response-content-markdown') ||
                       messageBubble.querySelector('div.markdown');
      if (markdown) {
        const clone = markdown.cloneNode(true);
        // Remove image viewers, sections, buttons, action-buttons
        clone.querySelectorAll('[data-testid="image-viewer"], section, button, .action-buttons').forEach(el => el.remove());
        text = clone.textContent.trim();
      }
    }

    // Fallback if no text found
    if (!text) {
      const markdown = container.querySelector('div.response-content-markdown') ||
                       container.querySelector('div.markdown');
      if (markdown) {
        const clone = markdown.cloneNode(true);
        clone.querySelectorAll('[data-testid="image-viewer"], section, button, .action-buttons').forEach(el => el.remove());
        text = clone.textContent.trim();
      }
    }

    // Last resort - check for image-only response
    if (!text) {
      const clone = container.cloneNode(true);
      clone.querySelectorAll('.action-buttons, .order-first, button, section, [data-testid="image-viewer"]').forEach(el => el.remove());
      text = clone.textContent.trim();
    }

    // If still no text but we have images with a prompt, use that
    if (!text && imagePrompt) {
      text = imagePrompt;
    }

    return { text, images };
  }
};

self.GrokProvider = GrokProvider;
// Auto-register in content script context (background/service-worker context registers
// explicitly in background.js instead, once all provider files have loaded)
if (self.ProviderRegistry) {
  console.log('[Grok] Registering provider with ProviderRegistry');
  self.ProviderRegistry.register(GrokProvider);
}
