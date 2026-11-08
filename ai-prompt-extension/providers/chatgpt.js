// ChatGPT Provider - With improved detection
const ChatGPTProvider = {
  name: 'chatgpt',
  displayName: 'ChatGPT',
  hostPatterns: ['*://chatgpt.com/*', '*://chat.openai.com/*'],

  selectors: {
    textarea: [
      '#prompt-textarea',
      // Nov 2026 ChatGPT: the composer is a ProseMirror contenteditable with no id inside
      // <form data-chatgpt-composer>; its placeholder moved to the inner <p>.
      'form[data-chatgpt-composer] div.ProseMirror[contenteditable="true"]',
      'div.ProseMirror[contenteditable="true"][role="textbox"]',
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
      // Nov 2026: <button type="submit" aria-label="Send"> with no data-testid
      'form[data-chatgpt-composer] button[type="submit"][aria-label="Send"]',
      'button[aria-label*="Send"]',
      'form button[type="submit"]'
    ],
    responseContainer: [
      // Nov 2026 ChatGPT: no conversation-turn testids. Each assistant reply is a unit div keyed
      // "<turn>:<n>:assistant" holding an sr-only <h4 data-conversation-role="assistant"> and the
      // markdown. findAllElements uses the first selector that matches anything.
      '[data-content-search-unit-key$=":assistant"]',
      'div:has(> h4[data-conversation-role="assistant"])',
      // Older builds: ChatGPT renders each turn as <section data-testid="conversation-turn-N"> in current
      // builds (older builds used <article>). Match tag-agnostically on the data-testid so the
      // container is found regardless of which wrapper element the live ChatGPT build ships.
      '[data-testid^="conversation-turn-"]'
    ],
    streamingIndicator: [
      'button[aria-label="Stop generating"]',
      'button[aria-label="Stop streaming"]',
      'button[data-testid="stop-button"]',
      'form[data-chatgpt-composer] button[aria-label^="Stop"]'
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

    // Nov 2026 DOM: the assistant units themselves (see selectors.responseContainer)
    for (const sel of this.selectors.responseContainer.slice(0, 2)) {
      const units = document.querySelectorAll(sel);
      if (units.length > 0) {
        console.log('[ChatGPT] Assistant units found:', units.length, 'via', sel);
        return units[units.length - 1];
      }
    }

    // Find all conversation turns (tag-agnostic: current ChatGPT uses <section>, older <article>)
    const articles = document.querySelectorAll('[data-testid^="conversation-turn-"]');
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
    
    // Nov 2026 DOM: the turn wrapper carries data-talvt-turn-state="complete" once the reply has
    // finished; when present it is authoritative. Otherwise look for the turn's action buttons
    // (Copy / Share / More actions sit on the turn, outside the assistant unit).
    const stateEl = latestResponse.closest('[data-talvt-turn-state]');
    if (stateEl) {
      const state = stateEl.getAttribute('data-talvt-turn-state');
      console.log('[ChatGPT] Turn state:', state);
      console.log('[ChatGPT] ═══════════════════════════════════════════════════');
      return state === 'complete';
    }

    // Check for completion buttons
    const hasButtons = this.hasCompletionButtons(latestResponse.closest('[data-turn-key]') || latestResponse);
    console.log('[ChatGPT] Result:', hasButtons ? '✓ COMPLETE' : '✗ NOT COMPLETE');
    console.log('[ChatGPT] ═══════════════════════════════════════════════════');
    return hasButtons;
  },

  extractResponseText: async function(container) {
    console.log('[ChatGPT] ═══════════ EXTRACTING RESPONSE TEXT ═══════════');
    console.log('[ChatGPT] Container:', container.tagName, container.getAttribute('data-testid'));

    // The conversation turn now wraps more than the assistant message: action buttons and,
    // increasingly, a sponsored ad block ("People Data Labs / Sponsored / ...") that carries its
    // own <p> elements. Extracting <p>/<pre> from the whole turn concatenates that ad text onto the
    // answer and corrupts JSON responses. Narrow to the actual assistant message markdown first,
    // falling back progressively so a future DOM tweak degrades instead of breaking.
    // Nov 2026 DOM: the answer is the last [data-markdown-text-style="assistant-message"] root.
    const mdRoots = container.querySelectorAll('[data-markdown-text-style="assistant-message"]');
    const scope = (mdRoots.length > 0 ? mdRoots[mdRoots.length - 1] : null)
      || container.querySelector('[data-message-author-role="assistant"] .markdown')
      || container.querySelector('[data-message-author-role="assistant"]')
      || container;
    if (scope !== container) {
      console.log('[ChatGPT] Narrowed extraction scope to assistant message:', scope.className || scope.tagName);
    }

    // Method 1: Extract code blocks directly from DOM
    // Clicking "Copy code" button doesn't work because synthetic events aren't trusted
    // and the Clipboard API blocks untrusted event access.
    // Nov 2026 code blocks have no <pre>: [data-code-block] > [data-code-block-source] > code,
    // one <span> per line, so read them with innerText to keep the line breaks.
    let codeBlocks = scope.querySelectorAll('pre code');
    let readCode = (code) => code.textContent;
    if (codeBlocks.length === 0) {
      codeBlocks = scope.querySelectorAll('[data-code-block-source] code');
      readCode = (code) => (code.innerText && code.innerText.trim() ? code.innerText : code.textContent);
    }
    console.log('[ChatGPT] Found', codeBlocks.length, 'code blocks');

    if (codeBlocks.length > 0) {
      // If there's exactly one code block, return just its content
      if (codeBlocks.length === 1) {
        const codeText = readCode(codeBlocks[0]);
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
        const codeText = readCode(code);
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
    const paragraphs = scope.querySelectorAll('p');
    console.log('[ChatGPT] Found', paragraphs.length, '<p> elements');

    const textParts = [];
    paragraphs.forEach((p, index) => {
      const text = p.textContent.trim();
      if (text) {
        textParts.push(text);
        console.log(`[ChatGPT] p[${index}]:`, text.substring(0, 100));
      }
    });

    let fullText = textParts.join('\n');
    // No <p> in the answer markdown (e.g. a bare list or heading): fall back to its visible text.
    if (!fullText && mdRoots.length > 0) {
      fullText = (scope.innerText || scope.textContent || '').trim();
      console.log('[ChatGPT] No <p> text; used markdown root text instead');
    }
    console.log('[ChatGPT] Total extracted text length:', fullText.length);
    console.log('[ChatGPT] First 150 chars:', fullText.substring(0, 150));
    console.log('[ChatGPT] ═══════════════════════════════════════════════════');

    return { text: fullText, images: [] };
  }
};

self.ChatGPTProvider = ChatGPTProvider;
// Auto-register in content script context (background/service-worker context registers
// explicitly in background.js instead, once all provider files have loaded)
if (self.ProviderRegistry) {
  console.log('[ChatGPT] Registering provider with ProviderRegistry');
  self.ProviderRegistry.register(ChatGPTProvider);
}