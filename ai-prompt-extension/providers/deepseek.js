// DeepSeek Provider Configuration
const DeepSeekProvider = {
  name: 'deepseek',
  displayName: 'DeepSeek',
  hostPatterns: [
    '*://chat.deepseek.com/*'
  ],
  
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
  
  // Extract response text from a response element
  extractResponseText: function(responseElement) {
    // Try to find markdown content first
    for (const selector of this.selectors.markdownContent) {
      const markdown = responseElement.querySelector(selector);
      if (markdown) {
        // Clone to avoid modifying original
        const clone = markdown.cloneNode(true);
        // Remove any UI elements (buttons, etc.)
        clone.querySelectorAll('button, [role="button"]').forEach(el => el.remove());
        const text = clone.textContent.trim();
        if (text) return { text, images: [] };
      }
    }
    
    // Fallback: get all text content
    const clone = responseElement.cloneNode(true);
    // Remove buttons and UI elements
    clone.querySelectorAll('button, [role="button"], svg').forEach(el => el.remove());
    return { text: clone.textContent.trim(), images: [] };
  },
  
  // Detect if streaming is complete
  detectStreamingComplete: function(document) {
    // Check streaming indicators first
    for (const selector of this.selectors.streamingIndicator) {
      if (document.querySelector(selector)) {
        return false;
      }
    }

    // Find latest assistant message
    const messages = document.querySelectorAll('div.ds-message');
    if (messages.length === 0) return false;

    const latestMessage = messages[messages.length - 1];

    // Look for any action buttons after the message (in parent or next sibling)
    const parent = latestMessage.parentElement;
    const actionButtons = parent?.querySelector('div.ds-flex div.ds-icon-button[role="button"]') ||
                          latestMessage.nextElementSibling?.querySelector('div.ds-icon-button[role="button"]');

    return !!actionButtons;
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