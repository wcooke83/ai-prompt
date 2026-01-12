// ChatGPT Provider - With improved detection
const ChatGPTProvider = {
  name: 'chatgpt',
  displayName: 'ChatGPT',
  hostPatterns: ['*://chatgpt.com/*', '*://chat.openai.com/*'],

  selectors: {
    textarea: [
      '#prompt-textarea',
      'div[contenteditable="true"][id="prompt-textarea"]',
      'div[id="prompt-textarea"]',
      'textarea[data-id="root"]',
      'textarea[placeholder*="Message"]',
      'div[contenteditable="true"][data-placeholder]'
    ],
    sendButton: [
      'button[data-testid="send-button"]',
      'button[data-testid="composer-send-button"]',
      'button[aria-label="Send prompt"]',
      'button[aria-label*="Send"]',
      'form button[type="submit"]'
    ],
    responseContainer: [
      'article[data-testid^="conversation-turn-"]'
    ],
    streamingIndicator: [
      'button[aria-label="Stop generating"]',
      'button[aria-label="Stop streaming"]',
      'button[data-testid="stop-button"]'
    ],
    modelIndicator: [
      'button[data-testid="model-switcher-dropdown-button"]',
      '[data-testid="model-switcher"] span',
      'div[class*="model"] span',
      'button[aria-haspopup="menu"] span'
    ],

    // Upload progress indicators - present while upload is in progress
    uploadProgress: [
      'div[data-testid="file-upload-progress"]',
      'div[class*="progress-bar"]',
      'div[class*="uploading"]',
      'svg.animate-spin'
    ],

    // File preview container - appears when file is uploaded
    uploadedFilePreview: [
      'div[data-testid="composer-attachment"]',
      'div[data-testid="file-thumbnail"]',
      'button[data-testid="composer-attachment-delete"]'
    ]
  },

  detectModel: function(document) {
    for (const selector of this.selectors.modelIndicator) {
      const el = document.querySelector(selector);
      if (el) {
        const text = el.textContent.trim().toLowerCase();
        if (text.includes('gpt-4o')) return 'gpt-4o';
        if (text.includes('gpt-4')) return 'gpt-4';
        if (text.includes('gpt-3.5')) return 'gpt-3.5-turbo';
        if (text.includes('o1')) return 'o1';
        if (text) return text;
      }
    }
    return 'gpt-4o';
  },

  // Find the latest assistant response article
  findLatestAssistantResponse: function(document) {
    console.log('[ChatGPT] Finding latest assistant response...');
    
    // Find all conversation turn articles
    const articles = document.querySelectorAll('article[data-testid^="conversation-turn-"]');
    console.log('[ChatGPT] Total conversation turns found:', articles.length);
    
    if (articles.length === 0) {
      console.log('[ChatGPT] No conversation turns found');
      return null;
    }
    
    // Extract turn numbers and sort to find highest
    let highestTurn = -1;
    let latestArticle = null;
    
    articles.forEach(article => {
      const testId = article.getAttribute('data-testid');
      const match = testId.match(/conversation-turn-(\d+)/);
      if (match) {
        const turnNum = parseInt(match[1], 10);
        console.log('[ChatGPT] Found turn:', turnNum, 'data-turn:', article.getAttribute('data-turn'));
        if (turnNum > highestTurn) {
          highestTurn = turnNum;
          latestArticle = article;
        }
      }
    });
    
    if (!latestArticle) {
      console.log('[ChatGPT] No valid turn numbers found');
      return null;
    }
    
    // Check if it's an assistant response
    const dataTurn = latestArticle.getAttribute('data-turn');
    console.log('[ChatGPT] Latest turn:', highestTurn, 'is assistant:', dataTurn === 'assistant');
    
    if (dataTurn !== 'assistant') {
      console.log('[ChatGPT] Latest turn is not an assistant response');
      return null;
    }
    
    return latestArticle;
  },

  // Check if response has completion buttons
  hasCompletionButtons: function(article) {
    console.log('[ChatGPT] Checking for completion buttons...');
    
    const buttonLabels = [
      'More actions',
      'Switch model',
      'Share',
      'Bad response',
      'Good response',
      'Copy'
    ];
    
    let foundCount = 0;
    const foundButtons = [];
    
    for (const label of buttonLabels) {
      const button = article.querySelector(`button[aria-label="${label}"]`);
      if (button) {
        foundCount++;
        foundButtons.push(label);
      }
    }
    
    console.log('[ChatGPT] Found', foundCount, 'completion buttons:', foundButtons.join(', '));
    return foundCount >= 3;
  },

  detectStreamingComplete: function(document) {
    console.log('[ChatGPT] ═══════════ DETECTING STREAMING COMPLETE ═══════════');
    
    // Check for stop button (still streaming)
    for (const selector of this.selectors.streamingIndicator) {
      if (document.querySelector(selector)) {
        console.log('[ChatGPT] ✗ Stop button found, still streaming');
        console.log('[ChatGPT] ═══════════════════════════════════════════════════');
        return false;
      }
    }
    console.log('[ChatGPT] ✓ No stop button found');
    
    // Find latest assistant response
    const latestResponse = this.findLatestAssistantResponse(document);
    if (!latestResponse) {
      console.log('[ChatGPT] ✗ No assistant response found');
      console.log('[ChatGPT] ═══════════════════════════════════════════════════');
      return false;
    }
    
    // Check for completion buttons
    const hasButtons = this.hasCompletionButtons(latestResponse);
    console.log('[ChatGPT] Result:', hasButtons ? '✓ COMPLETE' : '✗ NOT COMPLETE');
    console.log('[ChatGPT] ═══════════════════════════════════════════════════');
    return hasButtons;
  },

  extractResponseText: async function(container) {
    console.log('[ChatGPT] ═══════════ EXTRACTING RESPONSE TEXT ═══════════');
    console.log('[ChatGPT] Container:', container.tagName, container.getAttribute('data-testid'));

    // Method 1: Extract code blocks directly from DOM
    // Clicking "Copy code" button doesn't work because synthetic events aren't trusted
    // and the Clipboard API blocks untrusted event access
    const codeBlocks = container.querySelectorAll('pre code');
    console.log('[ChatGPT] Found', codeBlocks.length, 'code blocks');

    if (codeBlocks.length > 0) {
      // If there's exactly one code block, return just its content
      if (codeBlocks.length === 1) {
        const codeText = codeBlocks[0].textContent;
        console.log('[ChatGPT] Single code block, length:', codeText.length);
        console.log('[ChatGPT] ═══════════════════════════════════════════════════');
        return { text: codeText, images: [] };
      }

      // Multiple code blocks: combine with markdown formatting
      const parts = [];
      codeBlocks.forEach((code, index) => {
        // Try to get language from class (e.g., "language-json")
        const langMatch = code.className.match(/language-(\w+)/);
        const lang = langMatch ? langMatch[1] : '';
        const codeText = code.textContent;
        console.log(`[ChatGPT] Code block ${index}: ${lang || 'no-lang'}, length: ${codeText.length}`);
        parts.push('```' + lang + '\n' + codeText + '\n```');
      });

      const fullText = parts.join('\n\n');
      console.log('[ChatGPT] Combined code blocks, total length:', fullText.length);
      console.log('[ChatGPT] ═══════════════════════════════════════════════════');
      return { text: fullText, images: [] };
    }

    // Method 2: No code blocks, extract text from paragraphs
    console.log('[ChatGPT] No code blocks, extracting from <p> elements...');
    const paragraphs = container.querySelectorAll('p');
    console.log('[ChatGPT] Found', paragraphs.length, '<p> elements');

    const textParts = [];
    paragraphs.forEach((p, index) => {
      const text = p.textContent.trim();
      if (text) {
        textParts.push(text);
        console.log(`[ChatGPT] p[${index}]:`, text.substring(0, 100));
      }
    });

    const fullText = textParts.join('\n');
    console.log('[ChatGPT] Total extracted text length:', fullText.length);
    console.log('[ChatGPT] First 150 chars:', fullText.substring(0, 150));
    console.log('[ChatGPT] ═══════════════════════════════════════════════════');

    return { text: fullText, images: [] };
  }
};

if (typeof window !== 'undefined') {
  window.ChatGPTProvider = ChatGPTProvider;
  // Auto-register in content script context
  if (window.ProviderRegistry) {
    console.log('[ChatGPT] Registering provider with ProviderRegistry');
    window.ProviderRegistry.register(ChatGPTProvider);
  }
}