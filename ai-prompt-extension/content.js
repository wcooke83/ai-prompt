// Generic content script - receives provider config from background

let currentProvider = null;
let DEBUG = false; // Will be set per-request from background
let USE_PASTE = false; // Will be set per-request from background
let DOM_STABILIZE_MS = 3000; // Will be set per-request from background

// Console hijacking is now handled by logging.js which loads FIRST
// This ensures ALL logs (including from provider scripts) are captured

const log = {
  debug: (...args) => {
    if (DEBUG) {
      console.log('[AI-Automator DEBUG]', ...args);
    }
  },
  info: (...args) => {
    console.log('[AI-Automator]', ...args);
  },
  warn: (...args) => {
    console.warn('[AI-Automator WARN]', ...args);
  },
  error: (...args) => {
    console.error('[AI-Automator ERROR]', ...args);
  },
  group: (label) => {
    if (DEBUG) {
      console.group('[AI-Automator] ' + label);
    }
  },
  groupEnd: () => DEBUG && console.groupEnd(),
  table: (data) => DEBUG && console.table(data),
};

// Token estimation - approximates token count from text
// Uses ~4 characters per token as rough estimate (works for English)
// More accurate would require actual tokenizer, but this gives reasonable estimates
function estimateTokens(text) {
  if (!text) return 0;
  // Average ~4 chars per token for English text
  // Also count words as backup (roughly 0.75 tokens per word)
  const charEstimate = Math.ceil(text.length / 4);
  const wordCount = text.split(/\s+/).filter(w => w.length > 0).length;
  const wordEstimate = Math.ceil(wordCount * 1.3);
  // Return average of both methods for better accuracy
  return Math.ceil((charEstimate + wordEstimate) / 2);
}

// Detect current model from UI using provider's detectModel function
function detectCurrentModel(provider) {
  if (provider && typeof provider.detectModel === 'function') {
    return provider.detectModel(document);
  }
  // Fallback based on provider name
  const providerName = provider?.name || currentProvider?.name;
  const defaults = {
    'chatgpt': 'gpt-4o',
    'grok': 'grok-2',
    'claude': 'claude-sonnet',
    'deepseek': 'deepseek-chat'
  };
  return defaults[providerName] || 'unknown';
}

// Detect provider for this page
function detectProvider() {
  const url = window.location.href;
  for (const provider of ProviderRegistry.getAll()) {
    for (const pattern of provider.hostPatterns) {
      if (ProviderRegistry.urlMatchesPattern(url, pattern)) {
        return provider;
      }
    }
  }
  return null;
}

// Initialize provider on load
currentProvider = detectProvider();
if (currentProvider) {
  console.log(`AI Automator: ${currentProvider.displayName} provider loaded`);
}

// Find element using selector list
function findElement(selectorList) {
  if (!selectorList || !Array.isArray(selectorList)) return null;
  for (const selector of selectorList) {
    const el = document.querySelector(selector);
    if (el) return el;
  }
  return null;
}

// Find element with retry (waits for DOM to settle after re-renders)
async function waitForElement(selectorList, maxAttempts = 10, delayMs = 300) {
  for (let i = 0; i < maxAttempts; i++) {
    const el = findElement(selectorList);
    if (el) return el;
    await sleep(delayMs);
  }
  return null;
}

function findAllElements(selectorList) {
  if (!selectorList || !Array.isArray(selectorList)) return [];
  for (const selector of selectorList) {
    const els = document.querySelectorAll(selector);
    if (els.length > 0) return Array.from(els);
  }
  return [];
}

// Simulate a single keystroke
async function pressKey(element, char, isBackspace = false) {
  const key = isBackspace ? 'Backspace' : char;
  const code = isBackspace ? 'Backspace' : `Key${char.toUpperCase()}`;
  const keyCode = isBackspace ? 8 : char.charCodeAt(0);

  // Dispatch keydown
  element.dispatchEvent(new KeyboardEvent('keydown', {
    key, code, keyCode, charCode: keyCode, which: keyCode,
    bubbles: true, cancelable: true
  }));

  // Dispatch keypress (not for backspace)
  if (!isBackspace) {
    element.dispatchEvent(new KeyboardEvent('keypress', {
      key, code, keyCode, charCode: keyCode, which: keyCode,
      bubbles: true, cancelable: true
    }));
  }

  // Dispatch beforeinput
  element.dispatchEvent(new InputEvent('beforeinput', {
    inputType: isBackspace ? 'deleteContentBackward' : 'insertText',
    data: isBackspace ? null : char,
    bubbles: true, cancelable: true
  }));

  // Execute the action
  if (isBackspace) {
    document.execCommand('delete', false, null);
  } else {
    document.execCommand('insertText', false, char);
  }

  // Dispatch input
  element.dispatchEvent(new InputEvent('input', {
    inputType: isBackspace ? 'deleteContentBackward' : 'insertText',
    data: isBackspace ? null : char,
    bubbles: true, cancelable: true
  }));

  // Dispatch keyup
  element.dispatchEvent(new KeyboardEvent('keyup', {
    key, code, keyCode, charCode: keyCode, which: keyCode,
    bubbles: true, cancelable: true
  }));
}

// Get realistic typing delay (fast typer: 40-120ms, occasional pause)
function getTypingDelay() {
  const rand = Math.random();
  if (rand < 0.03) {
    // 3% chance of a longer pause (thinking)
    return 200 + Math.random() * 300;
  } else if (rand < 0.15) {
    // 12% chance of slightly slower
    return 100 + Math.random() * 80;
  } else {
    // Normal fast typing
    return 40 + Math.random() * 60;
  }
}

// Random typo characters (nearby keys on QWERTY)
const typoMap = {
  'a': ['s', 'q', 'z'], 'b': ['v', 'n', 'g'], 'c': ['x', 'v', 'd'],
  'd': ['s', 'f', 'e'], 'e': ['w', 'r', 'd'], 'f': ['d', 'g', 'r'],
  'g': ['f', 'h', 't'], 'h': ['g', 'j', 'y'], 'i': ['u', 'o', 'k'],
  'j': ['h', 'k', 'u'], 'k': ['j', 'l', 'i'], 'l': ['k', 'o', 'p'],
  'm': ['n', 'j', 'k'], 'n': ['b', 'm', 'h'], 'o': ['i', 'p', 'l'],
  'p': ['o', 'l'], 'q': ['w', 'a'], 'r': ['e', 't', 'f'],
  's': ['a', 'd', 'w'], 't': ['r', 'y', 'g'], 'u': ['y', 'i', 'j'],
  'v': ['c', 'b', 'f'], 'w': ['q', 'e', 's'], 'x': ['z', 'c', 's'],
  'y': ['t', 'u', 'h'], 'z': ['a', 'x']
};

// Simulate keyboard typing for a single character
async function simulateKeyPress(element, char) {
  const keyCode = char.charCodeAt(0);

  const keydownEvent = new KeyboardEvent('keydown', {
    key: char,
    code: `Key${char.toUpperCase()}`,
    keyCode: keyCode,
    which: keyCode,
    bubbles: true,
    cancelable: true
  });

  const keypressEvent = new KeyboardEvent('keypress', {
    key: char,
    code: `Key${char.toUpperCase()}`,
    keyCode: keyCode,
    charCode: keyCode,
    which: keyCode,
    bubbles: true,
    cancelable: true
  });

  const beforeInputEvent = new InputEvent('beforeinput', {
    inputType: 'insertText',
    data: char,
    bubbles: true,
    cancelable: true
  });

  const inputEvent = new InputEvent('input', {
    inputType: 'insertText',
    data: char,
    bubbles: true,
    cancelable: true
  });

  const keyupEvent = new KeyboardEvent('keyup', {
    key: char,
    code: `Key${char.toUpperCase()}`,
    keyCode: keyCode,
    which: keyCode,
    bubbles: true,
    cancelable: true
  });

  element.dispatchEvent(keydownEvent);
  element.dispatchEvent(keypressEvent);
  element.dispatchEvent(beforeInputEvent);

  // Insert text at cursor position
  const selection = window.getSelection();
  if (selection.rangeCount > 0) {
    const range = selection.getRangeAt(0);
    range.deleteContents();
    range.insertNode(document.createTextNode(char));
    range.collapse(false);
  }

  element.dispatchEvent(inputEvent);
  element.dispatchEvent(keyupEvent);
}

// Enhanced clickCopyButtonAndGetContent with extensive logging
async function clickCopyButtonAndGetContent(responseElement, provider = null) {
  console.log('═══════════════════════════════════════════════════════');
  console.log('[AI-Automator] STARTING COPY BUTTON DETECTION');
  console.log('[AI-Automator] Provider:', provider?.name);
  console.log('[AI-Automator] Response element:', responseElement?.tagName, responseElement?.className?.substring(0, 100));
  console.log('[AI-Automator] Response element ID:', responseElement?.id);
  
  let copyButton = null;
  
  // PRIORITY 1: ChatGPT main message copy button (data-testid="copy-turn-action-button")
  if (provider && provider.name === 'chatgpt') {
    console.log('[AI-Automator] --- CHATGPT SPECIFIC DETECTION ---');
    
    // Find agent-turn container
    const agentTurn = responseElement.closest('div.agent-turn');
    console.log('[AI-Automator] Agent-turn container found:', !!agentTurn);
    if (agentTurn) {
      console.log('[AI-Automator] Agent-turn classes:', agentTurn.className);
    }
    
    let searchContainer = agentTurn || responseElement;
    console.log('[AI-Automator] Search container:', searchContainer.tagName, searchContainer.className.substring(0, 100));
    
    // Method 1: Direct search in container
    copyButton = searchContainer.querySelector('button[data-testid="copy-turn-action-button"]');
    console.log('[AI-Automator] Method 1 (direct): copyButton found:', !!copyButton);
    
    // Method 2: Check next sibling
    if (!copyButton && searchContainer.nextElementSibling) {
      console.log('[AI-Automator] Checking next sibling...');
      console.log('[AI-Automator] Next sibling:', searchContainer.nextElementSibling.tagName, searchContainer.nextElementSibling.className.substring(0, 100));
      copyButton = searchContainer.nextElementSibling.querySelector('button[data-testid="copy-turn-action-button"]');
      console.log('[AI-Automator] Method 2 (next sibling): copyButton found:', !!copyButton);
    }
    
    // Method 3: Check up to 3 levels of siblings
    if (!copyButton) {
      console.log('[AI-Automator] Checking following siblings...');
      let sibling = searchContainer.nextElementSibling;
      let depth = 0;
      while (sibling && depth < 3 && !copyButton) {
        console.log(`[AI-Automator] Sibling depth ${depth}:`, sibling.tagName, sibling.className.substring(0, 100));
        copyButton = sibling.querySelector('button[data-testid="copy-turn-action-button"]');
        if (copyButton) {
          console.log(`[AI-Automator] Method 3 (sibling depth ${depth}): copyButton found!`);
        }
        sibling = sibling.nextElementSibling;
        depth++;
      }
    }
    
    // Method 4: Search entire document (last resort)
    if (!copyButton) {
      console.log('[AI-Automator] Searching entire document for action buttons...');
      const allCopyButtons = document.querySelectorAll('button[data-testid="copy-turn-action-button"]');
      console.log('[AI-Automator] Total copy-turn-action buttons found:', allCopyButtons.length);
      
      // Get the last one (most recent)
      if (allCopyButtons.length > 0) {
        copyButton = allCopyButtons[allCopyButtons.length - 1];
        console.log('[AI-Automator] Using last copy-turn-action button');
      }
    }
    
    if (copyButton) {
      console.log('[AI-Automator] ✓ Found ChatGPT main copy button via data-testid');
      console.log('[AI-Automator] Button details:', {
        tagName: copyButton.tagName,
        dataTestId: copyButton.getAttribute('data-testid'),
        ariaLabel: copyButton.getAttribute('aria-label'),
        className: copyButton.className.substring(0, 100),
        textContent: copyButton.textContent.substring(0, 50)
      });
    }
  }
  
  // Generic fallback methods for other providers
  if (!copyButton) {
    console.log('[AI-Automator] Trying generic copy button detection...');
    
    const candidates = responseElement.querySelectorAll('button[aria-label="Copy"], button[aria-label*="copy" i]');
    console.log('[AI-Automator] Generic aria-label candidates found:', candidates.length);
    
    for (const btn of candidates) {
      const text = btn.textContent.toLowerCase();
      console.log('[AI-Automator] Candidate button text:', text.substring(0, 50));
      
      // Skip code block copy buttons
      if (text.includes('copy code') || text.includes('copy to clipboard')) {
        console.log('[AI-Automator] ✗ Skipping code block copy button');
        continue;
      }
      copyButton = btn;
      console.log('[AI-Automator] ✓ Found copy button via aria-label');
      break;
    }
  }
  
  if (!copyButton) {
    console.log('[AI-Automator] ✗ NO COPY BUTTON FOUND');
    console.log('═══════════════════════════════════════════════════════');
    return null;
  }
  
  console.log('[AI-Automator] === STARTING TEXT EXTRACTION ===');
  
  // METHOD 1: Direct DOM extraction (most reliable)
  try {
    console.log('[AI-Automator] Method 1: Direct DOM extraction...');
    
    if (provider && provider.name === 'chatgpt') {
      // Find the full message container
      const messageContainer = responseElement.querySelector('[data-message-author-role="assistant"]') || responseElement;
      console.log('[AI-Automator] Message container:', messageContainer.tagName, messageContainer.className.substring(0, 100));
      
      // Clone and clean
      const clone = messageContainer.cloneNode(true);
      console.log('[AI-Automator] Cloned container, original children:', messageContainer.children.length);
      
      // Remove UI elements
      const uiElements = clone.querySelectorAll('button, svg, [role="button"], [data-testid*="action"], .action-buttons, [data-testid*="turn-action"]');
      console.log('[AI-Automator] Removing', uiElements.length, 'UI elements');
      uiElements.forEach(el => el.remove());
      
      const extractedText = clone.textContent.trim();
      console.log('[AI-Automator] Extracted text length:', extractedText.length);
      console.log('[AI-Automator] First 200 chars:', extractedText.substring(0, 200));
      console.log('[AI-Automator] Last 200 chars:', extractedText.substring(Math.max(0, extractedText.length - 200)));
      
      if (extractedText && extractedText.length > 0) {
        console.log('[AI-Automator] ✓ SUCCESS via DOM extraction');
        console.log('═══════════════════════════════════════════════════════');
        return extractedText;
      }
    }
  } catch (error) {
    console.error('[AI-Automator] DOM extraction error:', error);
  }
  
  // METHOD 2: Click button and read clipboard
  try {
    console.log('[AI-Automator] Method 2: Clipboard API...');
    
    copyButton.focus();
    await sleep(50);
    
    // Full click sequence
    console.log('[AI-Automator] Dispatching click events...');
    ['mousedown', 'mouseup', 'click'].forEach(eventType => {
      copyButton.dispatchEvent(new MouseEvent(eventType, {
        bubbles: true,
        cancelable: true,
        view: window
      }));
      console.log('[AI-Automator] Dispatched:', eventType);
    });
    
    console.log('[AI-Automator] Waiting 500ms for clipboard...');
    await sleep(500);
    
    if (navigator.clipboard && navigator.clipboard.readText) {
      try {
        const text = await navigator.clipboard.readText();
        console.log('[AI-Automator] Clipboard text length:', text?.length);
        console.log('[AI-Automator] First 200 chars:', text?.substring(0, 200));
        console.log('[AI-Automator] Last 200 chars:', text?.substring(Math.max(0, text.length - 200)));
        
        if (text && text.length > 0) {
          console.log('[AI-Automator] ✓ SUCCESS via clipboard');
          console.log('═══════════════════════════════════════════════════════');
          return text;
        }
      } catch (clipboardError) {
        console.error('[AI-Automator] Clipboard read failed:', clipboardError);
      }
    } else {
      console.log('[AI-Automator] Clipboard API not available');
    }
  } catch (error) {
    console.error('[AI-Automator] Clipboard method error:', error);
  }
  
  // METHOD 3: Fallback - all text content
  try {
    console.log('[AI-Automator] Method 3: Fallback extraction...');
    const clone = responseElement.cloneNode(true);
    clone.querySelectorAll('button, svg, [role="button"], [data-testid*="action"], .action-buttons, [data-testid*="turn-action"]').forEach(el => el.remove());
    const fallbackText = clone.textContent.trim();
    
    console.log('[AI-Automator] Fallback text length:', fallbackText.length);
    console.log('[AI-Automator] First 200 chars:', fallbackText.substring(0, 200));
    console.log('[AI-Automator] Last 200 chars:', fallbackText.substring(Math.max(0, fallbackText.length - 200)));
    
    if (fallbackText && fallbackText.length > 0) {
      console.log('[AI-Automator] ✓ SUCCESS via fallback');
      console.log('═══════════════════════════════════════════════════════');
      return fallbackText;
    }
  } catch (error) {
    console.error('[AI-Automator] Fallback extraction error:', error);
  }
  
  console.log('[AI-Automator] ✗ ALL EXTRACTION METHODS FAILED');
  console.log('═══════════════════════════════════════════════════════');
  return null;
}

// Helper function
async function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// Wait for file upload to complete
// Checks for upload progress indicators to disappear and file preview to appear
async function waitForUploadComplete(providerName, selectors, expectedFileCount = 1, timeout = 30000) {
  const startTime = Date.now();
  log.info(`Waiting for ${expectedFileCount} file(s) to finish uploading...`);

  const progressSelectors = selectors.uploadProgress || [];
  const previewSelectors = selectors.uploadedFilePreview || [];

  log.debug('Progress selectors:', progressSelectors);
  log.debug('Preview selectors:', previewSelectors);

  return new Promise((resolve, reject) => {
    const checkUploadStatus = async () => {
      const elapsed = Date.now() - startTime;

      if (elapsed > timeout) {
        log.warn('Upload timeout reached, proceeding anyway');
        resolve(false);
        return;
      }

      // Check if any upload progress indicators are present
      let uploadInProgress = false;
      for (const selector of progressSelectors) {
        const progressEl = document.querySelector(selector);
        if (progressEl && progressEl.offsetParent !== null) {
          log.debug('Upload progress indicator found:', selector);
          uploadInProgress = true;
          break;
        }
      }

      // Check if file preview elements are present
      let previewCount = 0;
      for (const selector of previewSelectors) {
        const previews = document.querySelectorAll(selector);
        if (previews.length > 0) {
          previewCount = previews.length;
          log.debug(`Found ${previewCount} file preview(s) via:`, selector);
          break;
        }
      }

      // Upload is complete when:
      // 1. No progress indicators are visible AND
      // 2. File preview is present (or we've waited long enough after progress ended)
      if (!uploadInProgress && previewCount >= expectedFileCount) {
        log.info(`Upload complete: ${previewCount} file preview(s) found, no progress indicators`);
        // Wait a bit more for UI to stabilize
        await sleep(500);
        resolve(true);
        return;
      }

      // If no progress indicators but also no preview after initial wait, check with mutation observer
      if (!uploadInProgress && previewCount === 0 && elapsed > 2000) {
        log.debug('No progress indicators or previews found, checking DOM for changes...');
      }

      // Continue polling
      setTimeout(checkUploadStatus, 300);
    };

    // Start checking after a brief delay to let UI update
    setTimeout(checkUploadStatus, 200);
  });
}

// Wait for DOM to stabilize (no mutations for specified duration)
async function waitForDomStability(element, stabilizeMs = 3000, maxWaitMs = 30000) {
  console.log(`[AI-Automator] Waiting for DOM stability (${stabilizeMs}ms of no changes, max ${maxWaitMs}ms)...`);

  return new Promise((resolve) => {
    let lastMutationTime = Date.now();
    let resolved = false;
    const startTime = Date.now();

    const observer = new MutationObserver(() => {
      lastMutationTime = Date.now();
      console.log('[AI-Automator] DOM mutation detected, resetting stability timer...');
    });

    observer.observe(element, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true
    });

    const checkStability = () => {
      if (resolved) return;

      const now = Date.now();
      const timeSinceLastMutation = now - lastMutationTime;
      const totalElapsed = now - startTime;

      if (timeSinceLastMutation >= stabilizeMs) {
        resolved = true;
        observer.disconnect();
        console.log(`[AI-Automator] DOM stable for ${timeSinceLastMutation}ms, proceeding with extraction`);
        resolve();
      } else if (totalElapsed >= maxWaitMs) {
        resolved = true;
        observer.disconnect();
        console.log(`[AI-Automator] Max wait time reached (${maxWaitMs}ms), proceeding anyway`);
        resolve();
      } else {
        setTimeout(checkStability, 200);
      }
    };

    // Start checking after initial delay
    setTimeout(checkStability, 200);
  });
}

// Paste text into element (simulates Ctrl+V)
async function typeText(element, text) {
  console.log('[AI-Automator] ========== typeText START ==========');
  console.log('[AI-Automator] Text length:', text.length);
  console.log('[AI-Automator] Text preview:', text.substring(0, 100));
  console.log('[AI-Automator] Element tag:', element.tagName);
  console.log('[AI-Automator] Element id:', element.id);
  console.log('[AI-Automator] Element className:', element.className);
  console.log('[AI-Automator] Element contentEditable:', element.contentEditable);
  console.log('[AI-Automator] USE_PASTE mode:', USE_PASTE);

  element.focus();
  await sleep(100);
  console.log('[AI-Automator] Element focused');

  // Check if this is a contenteditable element (ChatGPT, Claude, etc.)
  const isContentEditable = element.contentEditable === 'true' || element.isContentEditable;
  console.log('[AI-Automator] isContentEditable:', isContentEditable);

  if (isContentEditable) {
    console.log('[AI-Automator] Handling contenteditable element...');

    // Clear any placeholder content
    const placeholder = element.querySelector('p.is-empty, p.is-editor-empty, [data-placeholder]');
    if (placeholder) {
      console.log('[AI-Automator] Found placeholder, clearing...');
      placeholder.textContent = '';
      placeholder.classList.remove('is-empty', 'is-editor-empty');
    }

    // Position cursor
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(element);
    range.collapse(false);
    selection.removeAllRanges();
    selection.addRange(range);
    console.log('[AI-Automator] Cursor positioned');

    // Method 1: Try execCommand insertText (works on most editors)
    console.log('[AI-Automator] Trying execCommand insertText...');
    const insertSuccess = document.execCommand('insertText', false, text);
    console.log('[AI-Automator] execCommand result:', insertSuccess);

    await sleep(100);

    // Check if text was inserted
    const currentContent = element.textContent || element.innerText;
    console.log('[AI-Automator] Current content length:', currentContent.length);
    console.log('[AI-Automator] Current content preview:', currentContent.substring(0, 100));

    if (currentContent.includes(text.substring(0, 20))) {
      console.log('[AI-Automator] ✓ Text inserted via execCommand');
      // Dispatch input event to trigger React state update
      element.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        inputType: 'insertText',
        data: text
      }));
      console.log('[AI-Automator] ========== typeText END ==========');
      return;
    }

    // Method 2: Try paste event
    console.log('[AI-Automator] execCommand failed, trying paste event...');
    const dataTransfer = new DataTransfer();
    dataTransfer.setData('text/plain', text);

    const pasteEvent = new ClipboardEvent('paste', {
      bubbles: true,
      cancelable: true,
      clipboardData: dataTransfer
    });

    element.dispatchEvent(pasteEvent);
    await sleep(100);

    const afterPaste = element.textContent || element.innerText;
    console.log('[AI-Automator] After paste content length:', afterPaste.length);

    if (afterPaste.includes(text.substring(0, 20))) {
      console.log('[AI-Automator] ✓ Text inserted via paste event');
      element.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        inputType: 'insertFromPaste',
        data: text
      }));
      console.log('[AI-Automator] ========== typeText END ==========');
      return;
    }

    // Method 3: Direct DOM manipulation
    console.log('[AI-Automator] Paste failed, trying direct DOM insertion...');
    let targetNode = element.querySelector('p') || element;
    if (targetNode.tagName === 'P' && targetNode.childNodes.length === 0) {
      targetNode.textContent = text;
    } else {
      const textNode = document.createTextNode(text);
      if (targetNode.tagName === 'P') {
        targetNode.innerHTML = '';
        targetNode.appendChild(textNode);
      } else {
        element.innerHTML = '<p>' + text + '</p>';
      }
    }

    await sleep(100);
    console.log('[AI-Automator] After DOM manipulation:', element.textContent.substring(0, 100));

    // Dispatch input event
    element.dispatchEvent(new InputEvent('input', {
      bubbles: true,
      cancelable: true,
      inputType: 'insertText',
      data: text
    }));

    console.log('[AI-Automator] ========== typeText END ==========');
    return;
  }

  // For textarea elements
  if (element.tagName === 'TEXTAREA') {
    console.log('[AI-Automator] Handling TEXTAREA element...');
    element.value = text;
    element.dispatchEvent(new InputEvent('input', {
      bubbles: true,
      cancelable: true,
      inputType: 'insertText',
      data: text
    }));
    console.log('[AI-Automator] Textarea value set, length:', element.value.length);
    console.log('[AI-Automator] ========== typeText END ==========');
    return;
  }

  // Fallback: try various methods
  console.log('[AI-Automator] Fallback: unknown element type');
  document.execCommand('insertText', false, text);
  await sleep(100);
  console.log('[AI-Automator] ========== typeText END ==========');
}

// Click send button
async function clickSendButton(selectors) {
  console.log('[AI-Automator] clickSendButton called');
  console.log('[AI-Automator] Looking for selectors:', selectors.sendButton);

  // Wait for button to appear and become enabled (Grok hides button until text entered)
  let button = null;
  let attempts = 0;

  while (attempts < 50) {
    // Find all matching buttons
    for (const selector of selectors.sendButton) {
      const buttons = document.querySelectorAll(selector);
      if (attempts % 10 === 0) {
        console.log(`[AI-Automator] Attempt ${attempts}: selector "${selector}" found ${buttons.length} buttons`);
      }
      for (const btn of buttons) {
        // Check if button is visible (not in hidden container) and not disabled
        const isHidden = btn.closest('.hidden') || btn.offsetParent === null;
        if (attempts % 10 === 0) {
          console.log(`[AI-Automator] Button: disabled=${btn.disabled}, isHidden=${isHidden}, aria-label=${btn.getAttribute('aria-label')}`);
        }
        if (!isHidden && !btn.disabled) {
          button = btn;
          break;
        }
      }
      if (button) break;
    }

    if (button) break;
    await sleep(100);
    attempts++;
  }

  if (!button) {
    console.error('[AI-Automator] Send button not found after 50 attempts');
    throw new Error('Send button not found or remained disabled');
  }

  console.log('[AI-Automator] Clicking send button:', button.getAttribute('aria-label'));
  button.click();
  await sleep(200);
  console.log('[AI-Automator] Send button clicked');
}

// Check if response is still streaming
function isStreaming(selectors) {
  return findElement(selectors.streamingIndicator) !== null;
}

// Get latest response text and images
async function getLatestResponse(selectors, provider) {
  const responses = findAllElements(selectors.responseContainer);
  if (responses.length === 0) return null;

  const latest = responses[responses.length - 1];

  // Use provider-specific extraction if available
  if (provider && typeof provider.extractResponseText === 'function') {
    const result = await provider.extractResponseText(latest);
    // Normalize to object format
    return typeof result === 'string' ? { text: result, images: [] } : result;
  }

  // Fallback: try markdown content selectors
  const markdownSelectors = Array.isArray(selectors.markdownContent)
    ? selectors.markdownContent
    : [selectors.markdownContent];

  for (const selector of markdownSelectors) {
    if (!selector) continue;
    const markdown = latest.querySelector(selector);
    if (markdown && markdown.textContent.trim()) {
      return { text: markdown.textContent.trim(), images: [] };
    }
  }

  return { text: latest.textContent.trim(), images: [] };
}

// Get response text by element ID
async function getResponseById(responseId, provider) {
  const element = document.getElementById(responseId);
  if (!element) return null;

  // Use provider-specific extraction if available
  if (provider && typeof provider.extractResponseText === 'function') {
    const result = await provider.extractResponseText(element);
    // Normalize to object format
    return typeof result === 'string' ? { text: result, images: [] } : result;
  }

  return { text: element.textContent.trim(), images: [] };
}

// Click "Skip Selection" button if it appears (Grok image selection prompt)
function clickSkipSelectionIfPresent() {
  // Find any button containing "Skip Selection" text
  const buttons = document.querySelectorAll('button');
  for (const btn of buttons) {
    if (btn.textContent.includes('Skip Selection')) {
      console.log('[AI-Automator] Found "Skip Selection" button, clicking...');
      btn.click();
      return true;
    }
  }
  return false;
}

// Convert base64 to File object
function base64ToFile(base64Data, filename, mimeType) {
  const byteCharacters = atob(base64Data);
  const byteNumbers = new Array(byteCharacters.length);
  for (let i = 0; i < byteCharacters.length; i++) {
    byteNumbers[i] = byteCharacters.charCodeAt(i);
  }
  const byteArray = new Uint8Array(byteNumbers);
  const blob = new Blob([byteArray], { type: mimeType });
  return new File([blob], filename, { type: mimeType });
}

// Upload files to Grok using the attach button
async function uploadFilesGrok(attachments, selectors) {
  log.info('uploadFilesGrok called with', attachments.length, 'files');
  log.debug('Selectors received:', JSON.stringify(selectors, null, 2));

  for (let fileIndex = 0; fileIndex < attachments.length; fileIndex++) {
    const attachment = attachments[fileIndex];
    log.group(`Uploading file ${fileIndex + 1}/${attachments.length}: ${attachment.name}`);
    log.debug('File details:', { name: attachment.name, type: attachment.type, dataLength: attachment.data?.length });

    // Wait for attach button to appear (may not be immediately available)
    log.debug('Looking for attach button with selector: button[aria-label="Attach"]');
    let attachButton = null;
    for (let i = 0; i < 30; i++) {
      attachButton = document.querySelector('button[aria-label="Attach"]');
      if (attachButton) {
        log.debug('Found attach button on attempt', i + 1);
        break;
      }
      if (i % 5 === 0) {
        log.debug('Waiting for attach button... attempt', i + 1);
        // Also log all buttons with aria-label for debugging
        const allButtons = document.querySelectorAll('button[aria-label]');
        log.debug('All buttons with aria-label:', Array.from(allButtons).map(b => b.getAttribute('aria-label')));
      }
      await sleep(200);
    }

    if (!attachButton) {
      log.error('Attach button not found after 30 attempts (6 seconds)');
      log.debug('Current page URL:', window.location.href);
      log.debug('Document body snippet:', document.body.innerHTML.substring(0, 500));
      log.groupEnd();
      throw new Error('Attach button not found');
    }

    log.debug('Attach button found:', {
      tagName: attachButton.tagName,
      ariaLabel: attachButton.getAttribute('aria-label'),
      ariaExpanded: attachButton.getAttribute('aria-expanded'),
      dataState: attachButton.getAttribute('data-state'),
      className: attachButton.className,
      disabled: attachButton.disabled,
      visible: attachButton.offsetParent !== null
    });

    log.info('Clicking attach button...');
    // Try multiple click approaches
    attachButton.focus();
    await sleep(50);

    // Dispatch mousedown, mouseup, click sequence
    ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(eventType => {
        attachButton.dispatchEvent(new PointerEvent(eventType, {
            bubbles: true,
            cancelable: true,
            view: window,
            pointerId: 1,
            isPrimary: true
        }));
    });

    log.debug('Click events dispatched, waiting for menu...');
    await sleep(500);

    // Check if menu opened
    log.debug('After click - aria-expanded:', attachButton.getAttribute('aria-expanded'), 'data-state:', attachButton.getAttribute('data-state'));

    // Wait for menu to appear and find "Upload a file" option
    log.debug('Looking for element containing "Upload a file" text');
    let uploadMenuItem = null;
    for (let i = 0; i < 30; i++) {
      // Try multiple approaches to find the upload menu item
      // 1. Look for role="menuitem" containing the text
      const menuItems = document.querySelectorAll('[role="menuitem"]');
      for (const item of menuItems) {
        if (item.textContent.includes('Upload a file')) {
          uploadMenuItem = item;
          log.debug('Found via role="menuitem"');
          break;
        }
      }
      if (uploadMenuItem) break;

      // 2. Look for any div containing "Upload a file" that's clickable
      const allDivs = document.querySelectorAll('div[tabindex], div[role="menuitem"], div.cursor-pointer');
      for (const div of allDivs) {
        if (div.textContent.includes('Upload a file')) {
          uploadMenuItem = div;
          log.debug('Found via div search');
          break;
        }
      }
      if (uploadMenuItem) break;

      // 3. Use TreeWalker to find text node containing "Upload a file"
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
      while (walker.nextNode()) {
        if (walker.currentNode.textContent.includes('Upload a file')) {
          // Get the closest clickable parent
          let parent = walker.currentNode.parentElement;
          while (parent && parent !== document.body) {
            if (parent.getAttribute('role') === 'menuitem' ||
                parent.hasAttribute('tabindex') ||
                parent.classList.contains('cursor-pointer')) {
              uploadMenuItem = parent;
              log.debug('Found via TreeWalker, element:', parent.tagName, parent.className.substring(0, 50));
              break;
            }
            parent = parent.parentElement;
          }
          if (uploadMenuItem) break;
        }
      }
      if (uploadMenuItem) break;

      if (i % 5 === 0) {
        log.debug(`Attempt ${i + 1}: Still looking for "Upload a file"...`);
        // Log what menus/popups are visible
        const menus = document.querySelectorAll('[role="menu"], [data-radix-menu-content], [data-state="open"]');
        log.debug('Visible menus/popups:', menus.length);
      }
      await sleep(100);
    }

    if (!uploadMenuItem) {
      log.error('Upload a file menu item not found after 30 attempts');
      log.debug('Clicking body to close any open menu');
      document.body.click();
      log.groupEnd();
      throw new Error('Upload file menu item not found');
    }

    log.debug('Upload menu item found:', {
      tagName: uploadMenuItem.tagName,
      role: uploadMenuItem.getAttribute('role'),
      className: uploadMenuItem.className.substring(0, 80),
      textContent: uploadMenuItem.textContent.substring(0, 50)
    });

    // Before clicking, find or prepare to intercept the file input
    log.debug('Looking for existing file input');
    let fileInput = document.querySelector('input[type="file"]');
    log.debug('Existing file input:', fileInput ? 'found' : 'not found');

    // Set up observer to catch dynamically created file input
    let foundInput = null;
    log.debug('Setting up MutationObserver for file input');
    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
          if (node.nodeName === 'INPUT' && node.type === 'file') {
            log.debug('MutationObserver: Found file input (direct node)');
            foundInput = node;
          }
          if (node.querySelectorAll) {
            const inputs = node.querySelectorAll('input[type="file"]');
            if (inputs.length > 0) {
              log.debug('MutationObserver: Found file input (in subtree)');
              foundInput = inputs[0];
            }
          }
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });

    log.info('Clicking "Upload a file" menu item...');
    uploadMenuItem.click();
    await sleep(200);

    // Wait for file input to appear
    log.debug('Waiting for file input to appear...');
    for (let i = 0; i < 20; i++) {
      fileInput = foundInput || document.querySelector('input[type="file"]');
      if (fileInput) {
        log.debug('File input found on attempt', i + 1);
        break;
      }
      if (i % 5 === 0) {
        log.debug('Waiting for file input... attempt', i + 1);
      }
      await sleep(100);
    }
    observer.disconnect();

    if (!fileInput) {
      log.error('File input not found after clicking upload');
      log.debug('All inputs on page:', document.querySelectorAll('input').length);
      log.groupEnd();
      throw new Error('File input not found');
    }

    log.debug('File input details:', {
      tagName: fileInput.tagName,
      type: fileInput.type,
      accept: fileInput.accept,
      multiple: fileInput.multiple,
      name: fileInput.name
    });

    log.info('Converting base64 to File object...');
    const file = base64ToFile(attachment.data, attachment.name, attachment.type);
    log.debug('Created File object:', { name: file.name, size: file.size, type: file.type });

    log.info('Setting files on input element...');
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    fileInput.files = dataTransfer.files;
    log.debug('Files set. fileInput.files.length:', fileInput.files.length);

    log.info('Dispatching change event...');
    fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    log.debug('Change event dispatched');

    // Also dispatch input event
    fileInput.dispatchEvent(new Event('input', { bubbles: true }));
    log.debug('Input event dispatched');

    log.info('Waiting for upload to complete...');
    await waitForUploadComplete('grok', selectors, fileIndex + 1, 30000);
    log.groupEnd();
  }

  log.info('All files uploaded successfully');
}

// Upload files to ChatGPT using the composer plus button
async function uploadFilesChatGPT(attachments, selectors) {
  log.info('uploadFilesChatGPT called with', attachments.length, 'files');
  log.debug('Selectors received:', JSON.stringify(selectors, null, 2));

  for (let fileIndex = 0; fileIndex < attachments.length; fileIndex++) {
    const attachment = attachments[fileIndex];
    log.group(`Uploading file ${fileIndex + 1}/${attachments.length}: ${attachment.name}`);
    log.debug('File details:', { name: attachment.name, type: attachment.type, dataLength: attachment.data?.length });

    // Wait for composer plus button to appear
    log.debug('Looking for composer plus button: button#composer-plus-btn');
    let composerButton = null;
    for (let i = 0; i < 30; i++) {
      composerButton = document.querySelector('button#composer-plus-btn');
      if (composerButton) {
        log.debug('Found composer plus button on attempt', i + 1);
        break;
      }
      if (i % 5 === 0) {
        log.debug('Waiting for composer plus button... attempt', i + 1);
        // Log all buttons with data-testid for debugging
        const allButtons = document.querySelectorAll('button[data-testid]');
        log.debug('All buttons with data-testid:', Array.from(allButtons).map(b => b.getAttribute('data-testid')));
      }
      await sleep(200);
    }

    if (!composerButton) {
      log.error('Composer plus button not found after 30 attempts (6 seconds)');
      log.debug('Current page URL:', window.location.href);
      log.debug('Document body snippet:', document.body.innerHTML.substring(0, 500));
      log.groupEnd();
      throw new Error('Composer plus button not found');
    }

    log.debug('Composer plus button found:', {
      tagName: composerButton.tagName,
      ariaLabel: composerButton.getAttribute('aria-label'),
      ariaExpanded: composerButton.getAttribute('aria-expanded'),
      dataState: composerButton.getAttribute('data-state'),
      className: composerButton.className,
      disabled: composerButton.disabled,
      visible: composerButton.offsetParent !== null
    });

    log.info('Clicking composer plus button...');
    composerButton.focus();
    await sleep(50);

    // Dispatch full click sequence
    ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(eventType => {
      composerButton.dispatchEvent(new PointerEvent(eventType, {
        bubbles: true,
        cancelable: true,
        view: window,
        pointerId: 1,
        isPrimary: true
      }));
    });

    log.debug('Click events dispatched, waiting for menu...');
    await sleep(500);

    // Check if menu opened
    log.debug('After click - aria-expanded:', composerButton.getAttribute('aria-expanded'), 'data-state:', composerButton.getAttribute('data-state'));

    // Wait for menu to appear and find "Add photos & files" option
    log.debug('Looking for "Add photos & files" menuitem');
    let uploadMenuItem = null;
    for (let i = 0; i < 30; i++) {
      // Try to find the first menuitem (should be "Add photos & files")
      const menuItems = document.querySelectorAll('div[role="menuitem"]');
      
      if (menuItems.length > 0) {
        // Check first menuitem for "Add photos & files" text
        for (const item of menuItems) {
          if (item.textContent.includes('Add photos') || item.textContent.includes('Add files')) {
            uploadMenuItem = item;
            log.debug('Found via text search in menuitem');
            break;
          }
        }
      }
      
      if (uploadMenuItem) break;

      if (i % 5 === 0) {
        log.debug(`Attempt ${i + 1}: Still looking for "Add photos & files"...`);
        log.debug('Visible menuitems:', menuItems.length);
        if (menuItems.length > 0) {
          log.debug('First menuitem text:', menuItems[0].textContent.substring(0, 50));
        }
      }
      await sleep(100);
    }

    if (!uploadMenuItem) {
      log.error('Add photos & files menu item not found after 30 attempts');
      log.debug('Clicking body to close any open menu');
      document.body.click();
      log.groupEnd();
      throw new Error('Add photos & files menu item not found');
    }

    log.debug('Upload menu item found:', {
      tagName: uploadMenuItem.tagName,
      role: uploadMenuItem.getAttribute('role'),
      className: uploadMenuItem.className.substring(0, 80),
      textContent: uploadMenuItem.textContent.substring(0, 50)
    });

    // Set up observer to catch dynamically created file input
    let foundInput = null;
    log.debug('Setting up MutationObserver for file input');
    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
          if (node.nodeName === 'INPUT' && node.type === 'file') {
            log.debug('MutationObserver: Found file input (direct node)');
            foundInput = node;
          }
          if (node.querySelectorAll) {
            const inputs = node.querySelectorAll('input[type="file"]');
            if (inputs.length > 0) {
              log.debug('MutationObserver: Found file input (in subtree)');
              foundInput = inputs[0];
            }
          }
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });

    log.info('Clicking "Add photos & files" menu item...');
    uploadMenuItem.click();
    await sleep(200);

    // Wait for file input to appear
    log.debug('Waiting for file input to appear...');
    let fileInput = null;
    for (let i = 0; i < 20; i++) {
      fileInput = foundInput || document.querySelector('input[type="file"]');
      if (fileInput) {
        log.debug('File input found on attempt', i + 1);
        break;
      }
      if (i % 5 === 0) {
        log.debug('Waiting for file input... attempt', i + 1);
      }
      await sleep(100);
    }
    observer.disconnect();

    if (!fileInput) {
      log.error('File input not found after clicking upload');
      log.debug('All inputs on page:', document.querySelectorAll('input').length);
      log.groupEnd();
      throw new Error('File input not found');
    }

    log.debug('File input details:', {
      tagName: fileInput.tagName,
      type: fileInput.type,
      accept: fileInput.accept,
      multiple: fileInput.multiple,
      name: fileInput.name
    });

    log.info('Converting base64 to File object...');
    const file = base64ToFile(attachment.data, attachment.name, attachment.type);
    log.debug('Created File object:', { name: file.name, size: file.size, type: file.type });

    log.info('Setting files on input element...');
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    fileInput.files = dataTransfer.files;
    log.debug('Files set. fileInput.files.length:', fileInput.files.length);

    log.info('Dispatching change event...');
    fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    log.debug('Change event dispatched');

    // Also dispatch input event
    fileInput.dispatchEvent(new Event('input', { bubbles: true }));
    log.debug('Input event dispatched');

    log.info('Waiting for upload to complete...');
    await waitForUploadComplete('chatgpt', selectors, fileIndex + 1, 30000);
    log.groupEnd();
  }

  log.info('All files uploaded successfully');
}

// Upload files to DeepSeek
async function uploadFilesDeepSeek(attachments, selectors) {
  log.info('uploadFilesDeepSeek called with', attachments.length, 'files');
  log.debug('Selectors received:', JSON.stringify(selectors, null, 2));

  for (let fileIndex = 0; fileIndex < attachments.length; fileIndex++) {
    const attachment = attachments[fileIndex];
    log.group(`Uploading file ${fileIndex + 1}/${attachments.length}: ${attachment.name}`);
    log.debug('File details:', { name: attachment.name, type: attachment.type, dataLength: attachment.data?.length });

    // Wait for attach button to appear (looking for paperclip icon)
    log.debug('Looking for DeepSeek attach button (paperclip icon)');
    let attachButton = null;
    for (let i = 0; i < 30; i++) {
      // Try multiple approaches to find the paperclip button
      // Method 1: Find by SVG path (most reliable - identifies the actual paperclip icon)
      const paperclipPath = document.querySelector('svg path[d^="M5.5498 9.75V5H"]');
      if (paperclipPath) {
        attachButton = paperclipPath.closest('div[role="button"]');
      }
      
      // Method 2: Find by container class near textarea
      if (!attachButton) {
        attachButton = document.querySelector('div.bf38813a div.ds-icon-button[role="button"]');
      }
      
      // Method 3: Find by specific class pattern
      if (!attachButton) {
        attachButton = document.querySelector('div.f02f0e25.ds-icon-button[role="button"]');
      }
      
      if (attachButton) {
        log.debug('Found attach button on attempt', i + 1);
        break;
      }
      if (i % 5 === 0) {
        log.debug('Waiting for attach button... attempt', i + 1);
        const allButtons = document.querySelectorAll('div[role="button"]');
        log.debug('All div buttons found:', allButtons.length);
      }
      await sleep(200);
    }

    if (!attachButton) {
      log.error('Attach button not found after 30 attempts (6 seconds)');
      log.debug('Current page URL:', window.location.href);
      log.groupEnd();
      throw new Error('Attach button not found');
    }

    log.debug('Attach button found:', {
      tagName: attachButton.tagName,
      role: attachButton.getAttribute('role'),
      className: attachButton.className.substring(0, 80),
      visible: attachButton.offsetParent !== null
    });

    log.info('Clicking attach button...');
    attachButton.focus();
    await sleep(50);

    // Dispatch click sequence
    ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(eventType => {
      attachButton.dispatchEvent(new PointerEvent(eventType, {
        bubbles: true,
        cancelable: true,
        view: window,
        pointerId: 1,
        isPrimary: true
      }));
    });

    log.debug('Click events dispatched, waiting for file input...');
    await sleep(300);

    // For DeepSeek, the file input should appear directly after clicking
    // Set up observer for file input
    let foundInput = null;
    log.debug('Setting up MutationObserver for file input');
    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
          if (node.nodeName === 'INPUT' && node.type === 'file') {
            log.debug('MutationObserver: Found file input');
            foundInput = node;
          }
          if (node.querySelectorAll) {
            const inputs = node.querySelectorAll('input[type="file"]');
            if (inputs.length > 0) {
              log.debug('MutationObserver: Found file input (in subtree)');
              foundInput = inputs[0];
            }
          }
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });

    // Wait for file input
    log.debug('Waiting for file input to appear...');
    let fileInput = null;
    for (let i = 0; i < 20; i++) {
      fileInput = foundInput || document.querySelector('input[type="file"]');
      if (fileInput) {
        log.debug('File input found on attempt', i + 1);
        break;
      }
      if (i % 5 === 0) {
        log.debug('Waiting for file input... attempt', i + 1);
      }
      await sleep(100);
    }
    observer.disconnect();

    if (!fileInput) {
      log.error('File input not found after clicking upload');
      log.groupEnd();
      throw new Error('File input not found');
    }

    log.debug('File input details:', {
      tagName: fileInput.tagName,
      type: fileInput.type,
      accept: fileInput.accept,
      multiple: fileInput.multiple
    });

    log.info('Converting base64 to File object...');
    const file = base64ToFile(attachment.data, attachment.name, attachment.type);
    log.debug('Created File object:', { name: file.name, size: file.size, type: file.type });

    log.info('Setting files on input element...');
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    fileInput.files = dataTransfer.files;
    log.debug('Files set. fileInput.files.length:', fileInput.files.length);

    log.info('Dispatching change event...');
    fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    log.debug('Change event dispatched');

    fileInput.dispatchEvent(new Event('input', { bubbles: true }));
    log.debug('Input event dispatched');

    log.info('Waiting for upload to complete...');
    await waitForUploadComplete('deepseek', selectors, fileIndex + 1, 30000);
    log.groupEnd();
  }

  log.info('All files uploaded successfully');
}

// Wait for Grok response - simple approach: wait for action buttons to appear
async function waitForGrokResponse(provider, existingResponseIds = new Set(), timeout = 120000) {
  console.log('[AI-Automator] waitForGrokResponse started, ignoring', existingResponseIds.size, 'existing responses');
  const startTime = Date.now();

  return new Promise((resolve, reject) => {
    let resolved = false;

    const checkForCompletion = async () => {
      if (resolved) return false;

      // Check for and click "Skip Selection" button if present
      clickSkipSelectionIfPresent();
      // Look for action-buttons with last-response class containing Regenerate button
      const actionButtons = document.querySelector('.action-buttons.last-response button[aria-label="Regenerate"]');

      if (actionButtons) {
        // Found it! Get the parent response element
        const responseContainer = actionButtons.closest('div[id^="response-"]');

        // Skip if this is an existing response from before we sent the prompt
        if (responseContainer && existingResponseIds.has(responseContainer.id)) {
          console.log('[AI-Automator] Ignoring existing response:', responseContainer.id);
          return false;
        }

        console.log('[AI-Automator] Found NEW completed response:', responseContainer?.id);

        if (responseContainer) {
          // Verify it's an AI response (items-start)
          const parent = responseContainer.closest('.flex.flex-col.justify-center');
          if (parent && parent.classList.contains('items-start')) {
            // Extract text and images - use provider method or robust fallback
            let result = null;
            if (provider && typeof provider.extractResponseText === 'function') {
              result = await provider.extractResponseText(responseContainer);
            } else {
              // Robust fallback: get text only from response-content-markdown inside message-bubble
              let text = '';
              const images = [];
              const messageBubble = responseContainer.querySelector('div.message-bubble');
              if (messageBubble) {
                const markdown = messageBubble.querySelector('div.response-content-markdown') ||
                                 messageBubble.querySelector('div.markdown');
                if (markdown) {
                  const clone = markdown.cloneNode(true);
                  clone.querySelectorAll('[data-testid="image-viewer"], section, button, .action-buttons').forEach(el => el.remove());
                  text = clone.textContent.trim();
                }
              }
              // Last resort: clone container, remove non-content elements
              if (!text) {
                const clone = responseContainer.cloneNode(true);
                clone.querySelectorAll('.action-buttons, .order-first, button, section, [data-testid="image-viewer"]').forEach(el => el.remove());
                text = clone.textContent.trim();
              }
              result = { text, images };
            }

            // Handle both old string format and new object format
            const text = typeof result === 'string' ? result : result?.text;
            const images = typeof result === 'object' ? (result?.images || []) : [];

            console.log('[AI-Automator] Response text length:', text?.length, 'images:', images.length);
            if (text && text.length >= 1) {
              resolved = true;
              clearInterval(pollInterval);
              clearTimeout(timeoutId);
              if (observer) observer.disconnect();
              resolve({ text, images });
              return true;
            }
          }
        }
      }
      return false;
    };

    // Poll every 500ms
    const pollInterval = setInterval(async () => {
      if (Date.now() - startTime > timeout) {
        clearInterval(pollInterval);
        return;
      }
      await checkForCompletion();
    }, 500);

    // Timeout handler
    const timeoutId = setTimeout(() => {
      clearInterval(pollInterval);
      if (observer) observer.disconnect();
      console.error('[AI-Automator] Timeout waiting for response');
      reject(new Error('Timeout waiting for response'));
    }, timeout);

    // Also use mutation observer for faster detection
    let observer = null;
    observer = new MutationObserver(async () => {
      if (await checkForCompletion()) {
        observer.disconnect();
      }
    });

    observer.observe(document.body, {
      childList: true,
      subtree: true
    });
  });
}

// Generic wait for response (for other providers) - FIXED VERSION
async function waitForResponse(selectors, provider, initialResponseCount, timeout = 120000) {
  console.log('[AI-Automator] waitForResponse started, initialResponseCount:', initialResponseCount);
  const startTime = Date.now();

  return new Promise((resolve, reject) => {
    let resolved = false;
    let extracting = false;
    
    const checkCompletion = async () => {
      if (resolved || extracting) return false;
      
      const currentResponses = findAllElements(selectors.responseContainer);

      if (currentResponses.length > initialResponseCount) {
        // Check if streaming is complete
        const streamingComplete = provider && typeof provider.detectStreamingComplete === 'function'
          ? provider.detectStreamingComplete(document)
          : !isStreaming(selectors);

        if (streamingComplete) {
          extracting = true;
          const latestResponse = currentResponses[currentResponses.length - 1];

          console.log('[AI-Automator] Response complete, waiting for DOM to stabilize...');
          await waitForDomStability(latestResponse, DOM_STABILIZE_MS, 30000);
          console.log('[AI-Automator] Attempting to extract text...');

          // Use provider-specific extraction
          let result = null;
          if (provider && typeof provider.extractResponseText === 'function') {
            result = await provider.extractResponseText(latestResponse);
          } else {
            result = await getLatestResponse(selectors, provider);
          }
          
          // Check if provider wants us to use clipboard
          if (result && result.useClipboard) {
            console.log('[AI-Automator] Provider requested clipboard extraction...');
            try {
              // Wait a moment for clipboard to be populated
              await sleep(300);
              
              if (navigator.clipboard && navigator.clipboard.readText) {
                const clipboardText = await navigator.clipboard.readText();
                console.log('[AI-Automator] Clipboard text length:', clipboardText?.length);
                
                if (clipboardText && clipboardText.length > 0) {
                  console.log('[AI-Automator] ✓ SUCCESS via clipboard');
                  result = { text: clipboardText, images: [] };
                } else {
                  console.warn('[AI-Automator] Clipboard empty, using fallback');
                  extracting = false;
                  return false;
                }
              } else {
                console.warn('[AI-Automator] Clipboard API not available');
                extracting = false;
                return false;
              }
            } catch (error) {
              console.error('[AI-Automator] Clipboard read failed:', error);
              extracting = false;
              return false;
            }
          }
          
          // Final validation
          if (result && result.text && result.text.length >= 1) {
            clearInterval(pollInterval);
            if (observer) observer.disconnect();
            clearTimeout(timeoutId);
            resolved = true;
            resolve(result);
            return true;
          } else {
            extracting = false;
            console.warn('[AI-Automator] Extracted text is too short or empty, continuing to wait...');
          }
        }
      }
      return false;
    };

    const pollInterval = setInterval(async () => {
      if (Date.now() - startTime > timeout) {
        clearInterval(pollInterval);
        return;
      }
      await checkCompletion();
    }, 500);

    const timeoutId = setTimeout(() => {
      clearInterval(pollInterval);
      if (observer) observer.disconnect();
      
      // Try to get any response on timeout
      console.warn('[AI-Automator] Timeout reached, attempting final extraction...');
      const result = getLatestResponse(selectors, provider);
      if (result && result.text) {
        resolve(result);
      } else {
        reject(new Error('Timeout waiting for response'));
      }
    }, timeout);

    let observer = null;
    if (typeof MutationObserver !== 'undefined') {
      observer = new MutationObserver(async () => {
        await checkCompletion();
      });

      observer.observe(document.body, {
        childList: true,
        subtree: true,
        characterData: true
      });
    }
  });
}

// Execute prompt using provided selectors
async function executePrompt(text, providerConfig, attachments = null, debugLogging = false, usePasteInput = false, domStabilizeMs = 3000) {
  // Set debug mode for this execution
  DEBUG = debugLogging;
  USE_PASTE = usePasteInput;
  DOM_STABILIZE_MS = domStabilizeMs;

  log.info('========== executePrompt START ==========');
  log.info('Provider:', providerConfig.name);
  log.info('Prompt:', text.substring(0, 100) + (text.length > 100 ? '...' : ''));
  log.info('Attachments:', attachments ? attachments.length : 0);
  log.info('Debug logging:', DEBUG ? 'ENABLED' : 'disabled');
  log.info('Use paste:', USE_PASTE ? 'ENABLED' : 'disabled');

  log.debug('Full provider config:', JSON.stringify(providerConfig, null, 2));

  log.debug('Full provider config:', JSON.stringify(providerConfig, null, 2));
  if (attachments) {
    log.debug('Attachment details:', attachments.map(a => ({ name: a.name, type: a.type, dataLength: a.data?.length })));
  }

  const selectors = providerConfig.selectors;
  log.debug('Selectors:', JSON.stringify(selectors, null, 2));

  const provider = ProviderRegistry.get(providerConfig.name);
  log.debug('Provider from registry:', provider ? provider.name : 'NOT FOUND');

  // Detect model before sending prompt
  const model = detectCurrentModel(provider);
  log.info('Detected model:', model);

  // Estimate input tokens
  const inputTokens = estimateTokens(text);
  log.debug('Estimated input tokens:', inputTokens);

  // Wait for textarea with retries (handles DOM re-renders after sending messages)
  log.info('Waiting for textarea...');
  log.debug('Textarea selectors:', selectors.textarea);
  let textarea;
  if (providerConfig.name === 'deepseek') {
    textarea = await waitForTextareaDeepSeek(selectors.textarea, 15, 200);
  } else {
    textarea = await waitForElement(selectors.textarea, 15, 200);
  }
  if (!textarea) {
    log.error('Textarea not found!');
    log.debug('Tried selectors:', selectors.textarea);
    throw new Error(`${providerConfig.name}: Input textarea not found`);
  }
  log.info('Textarea found:', textarea.className.substring(0, 50));
  log.debug('Textarea details:', {
    tagName: textarea.tagName,
    contentEditable: textarea.contentEditable,
    id: textarea.id,
    className: textarea.className
  });

  // Handle file attachments based on provider
  if (attachments && attachments.length > 0) {
    log.info(`Uploading ${attachments.length} attachments for ${providerConfig.name}...`);
    
    try {
      switch (providerConfig.name) {
        case 'grok':
          log.debug('Using uploadFilesGrok');
          await uploadFilesGrok(attachments, selectors);
          break;
        
        case 'chatgpt':
          log.debug('Using uploadFilesChatGPT');
          await uploadFilesChatGPT(attachments, selectors);
          break;
        
        case 'deepseek':
          log.debug('Using uploadFilesDeepSeek');
          await uploadFilesDeepSeek(attachments, selectors);
          break;
        
        default:
          log.warn(`No upload function implemented for provider: ${providerConfig.name}`);
          throw new Error(`File uploads not supported for ${providerConfig.name}`);
      }
      
      log.info('Attachments uploaded successfully');
    } catch (e) {
      log.error('Failed to upload attachments:', e.message);
      log.debug('Error stack:', e.stack);
      throw e;
    }
  }

  // Count existing responses
  let initialResponseCount = 0;
  let existingResponseIds = new Set();
  await sleep(200);

  if (providerConfig.name === 'grok') {
    // For Grok, track existing response IDs
    document.querySelectorAll('div[id^="response-"]').forEach(el => {
      existingResponseIds.add(el.id);
    });
    console.log('[AI-Automator] Existing Grok response IDs:', existingResponseIds.size);
  } else {
    initialResponseCount = findAllElements(selectors.responseContainer).length;
    console.log('[AI-Automator] Initial response count:', initialResponseCount);
  }

  console.log('[AI-Automator] Typing text...');
  if (providerConfig.name === 'deepseek') {
    await typeTextDeepSeek(textarea, text);
  } else {
    await typeText(textarea, text);
  }

  console.log('[AI-Automator] Clicking send button...');
  if (providerConfig.name === 'deepseek') {
    await clickSendButtonDeepSeek(selectors, providerConfig.name);
  } else {
    await clickSendButton(selectors);
  }

  console.log('[AI-Automator] Waiting for response...');

  let response;
  if (providerConfig.name === 'grok') {
    // Use simplified Grok-specific detection, pass existing IDs to ignore
    response = await waitForGrokResponse(provider, existingResponseIds, 120000);
  } else {
    // Use generic detection for other providers - PASS THE PROVIDER OBJECT
    response = await waitForResponse(selectors, provider, initialResponseCount, 120000);
  }

  // Normalize response to object format { text, images }
  const normalizedResponse = (typeof response === 'string' 
    ? { text: response, images: [] } 
    : { text: response?.text || '', images: response?.images || [] }
  );

  // Strip markdown code fences if present (```json at start, ``` at end)
  if (normalizedResponse.text) {
    let cleanedText = normalizedResponse.text.trim();
    
    // Check for ```json or ``` at the start
    if (cleanedText.startsWith('```json')) {
      cleanedText = cleanedText.substring(7).trim(); // Remove ```json and whitespace
    } else if (cleanedText.startsWith('```')) {
      cleanedText = cleanedText.substring(3).trim(); // Remove ``` and whitespace
    }
    
    // Check for ``` at the end
    if (cleanedText.endsWith('```')) {
      cleanedText = cleanedText.substring(0, cleanedText.length - 3).trim();
    }
    
    // Also handle legacy 'json{' prefix (without backticks)
    if (cleanedText.startsWith('json{')) {
      cleanedText = cleanedText.substring(4);
    }
    
    normalizedResponse.text = cleanedText;
  }
      
  console.log('[AI-Automator] Response:', normalizedResponse);

  // Estimate output tokens
  const outputTokens = estimateTokens(normalizedResponse?.text);
  console.log('[AI-Automator] Estimated output tokens:', outputTokens);

  console.log('[AI-Automator] Response received, text length:', normalizedResponse?.text?.length, 'images:', normalizedResponse?.images?.length);
  console.log('[AI-Automator] ========== executePrompt END ==========');

  return {
    ...normalizedResponse,
    model,
    input_tokens: inputTokens,
    output_tokens: outputTokens
  };
}

function getLatestResponseElement(selectors) {
  const responses = findAllElements(selectors.responseContainer);
  if (responses.length === 0) return null;
  return responses[responses.length - 1];
}

// Listen for messages from background script
browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  console.log('[AI-Automator] Received message:', message.type);
  if (message.type === 'executePrompt') {
    console.log('[AI-Automator] Executing prompt for provider:', message.provider?.name);
    console.log('[AI-Automator] Attachments:', message.attachments ? message.attachments.length : 0);
    console.log('[AI-Automator] Debug logging:', message.debugLogging ? 'ENABLED' : 'disabled');
    console.log('[AI-Automator] Use paste:', message.usePasteInput ? 'ENABLED' : 'disabled');
    console.log('[AI-Automator] DOM stabilize ms:', message.domStabilizeMs || 3000);
    executePrompt(message.text, message.provider, message.attachments, message.debugLogging, message.usePasteInput, message.domStabilizeMs || 3000)
      .then(result => {
        console.log('[AI-Automator] Success! Response length:', result?.text?.length, 'images:', result?.images?.length);
        console.log('[AI-Automator] Model:', result?.model, 'Input tokens:', result?.input_tokens, 'Output tokens:', result?.output_tokens);
        sendResponse({
          success: true,
          text: result.text,
          images: result.images || [],
          model: result.model,
          input_tokens: result.input_tokens,
          output_tokens: result.output_tokens
        });
      })
      .catch(error => {
        console.error('[AI-Automator] Error:', error.message);
        sendResponse({ success: false, error: error.message });
      });
    return true;
  } else if (message.type === 'getProviderInfo') {
    sendResponse({
      provider: currentProvider ? currentProvider.name : null,
      displayName: currentProvider ? currentProvider.displayName : null
    });
  }
});

// DeepSeek-specific helper functions for content.js
// Add these to your content.js file or merge with existing functions

// Enhanced findElement that works with DeepSeek's button structure
function findElementDeepSeek(selectorList) {
  if (!selectorList || !Array.isArray(selectorList)) return null;
  
  for (const selector of selectorList) {
    // Special handling for SVG path selectors
    if (selector.includes('svg path[d^=')) {
      const path = document.querySelector(selector);
      if (path) {
        // Find the closest button/clickable element
        return path.closest('div[role="button"]') || path.closest('button');
      }
    } else {
      const el = document.querySelector(selector);
      if (el) return el;
    }
  }
  return null;
}

// Click send button - enhanced for DeepSeek
async function clickSendButtonDeepSeek(selectors, providerName) {
  console.log('[AI-Automator] clickSendButton called for', providerName);
  console.log('[AI-Automator] Looking for selectors:', selectors.sendButton);

  let button = null;
  let attempts = 0;

  while (attempts < 50) {
    // For DeepSeek, need special handling
    if (providerName === 'deepseek') {
      // Method 1: Find by SVG path (upload arrow)
      const uploadArrowPath = document.querySelector('svg path[d^="M8.3125 0.981587"]');
      if (uploadArrowPath) {
        const btn = uploadArrowPath.closest('div[role="button"]');
        if (btn && btn.getAttribute('aria-disabled') !== 'true') {
          button = btn;
        }
      }
      
      // Method 2: Find by class pattern
      if (!button) {
        const buttons = document.querySelectorAll('div._7436101.ds-icon-button[role="button"]');
        for (const btn of buttons) {
          if (btn.getAttribute('aria-disabled') !== 'true' && btn.offsetParent !== null) {
            button = btn;
            break;
          }
        }
      }
      
      // Method 3: Find in the controls container
      if (!button) {
        const container = document.querySelector('div.ec4f5d61, div.bf38813a');
        if (container) {
          const buttons = container.querySelectorAll('div.ds-icon-button[role="button"]');
          for (const btn of buttons) {
            // Skip disabled buttons and the attach button
            if (btn.getAttribute('aria-disabled') !== 'true' && 
                !btn.querySelector('svg path[d^="M5.5498 9.75V5H"]')) {
              button = btn;
              break;
            }
          }
        }
      }
    } else {
      // Standard approach for other providers
      for (const selector of selectors.sendButton) {
        const buttons = document.querySelectorAll(selector);
        if (attempts % 10 === 0) {
          console.log(`[AI-Automator] Attempt ${attempts}: selector "${selector}" found ${buttons.length} buttons`);
        }
        for (const btn of buttons) {
          const isHidden = btn.closest('.hidden') || btn.offsetParent === null;
          const isDisabled = btn.disabled || btn.getAttribute('aria-disabled') === 'true';
          if (attempts % 10 === 0) {
            console.log(`[AI-Automator] Button: disabled=${isDisabled}, isHidden=${isHidden}`);
          }
          if (!isHidden && !isDisabled) {
            button = btn;
            break;
          }
        }
        if (button) break;
      }
    }

    if (button) break;
    await sleep(100);
    attempts++;
  }

  if (!button) {
    console.error('[AI-Automator] Send button not found after 50 attempts');
    throw new Error('Send button not found or remained disabled');
  }

  console.log('[AI-Automator] Found send button:', {
    tagName: button.tagName,
    role: button.getAttribute('role'),
    ariaLabel: button.getAttribute('aria-label'),
    className: button.className.substring(0, 80)
  });

  // For DeepSeek and similar div-based buttons, use pointer events
  if (button.tagName === 'DIV' && button.getAttribute('role') === 'button') {
    console.log('[AI-Automator] Using pointer event sequence for div button');
    button.focus();
    await sleep(50);
    
    ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(eventType => {
      button.dispatchEvent(new PointerEvent(eventType, {
        bubbles: true,
        cancelable: true,
        view: window,
        pointerId: 1,
        isPrimary: true
      }));
    });
  } else {
    // Standard click for regular buttons
    console.log('[AI-Automator] Using standard click');
    button.click();
  }
  
  await sleep(200);
  console.log('[AI-Automator] Send button clicked');
}

// Wait for textarea - enhanced for DeepSeek
async function waitForTextareaDeepSeek(selectorList, maxAttempts = 15, delayMs = 200) {
  for (let i = 0; i < maxAttempts; i++) {
    // Try each selector
    for (const selector of selectorList) {
      const el = document.querySelector(selector);
      if (el) {
        console.log('[AI-Automator] Found textarea:', selector);
        return el;
      }
    }
    
    // Also try finding by placeholder
    const byPlaceholder = document.querySelector('textarea[placeholder*="DeepSeek" i]');
    if (byPlaceholder) {
      console.log('[AI-Automator] Found textarea by placeholder');
      return byPlaceholder;
    }
    
    await sleep(delayMs);
  }
  return null;
}

// Type text into DeepSeek textarea
async function typeTextDeepSeek(element, text) {
  console.log('[AI-Automator] typeTextDeepSeek called, text length:', text.length);
  console.log('[AI-Automator] Element:', element.tagName, element.className.substring(0, 50));
  console.log('[AI-Automator] USE_PASTE mode:', USE_PASTE);

  element.focus();
  await sleep(100);

  if (USE_PASTE) {
    console.log('[AI-Automator] Using paste for DeepSeek...');
    // For DeepSeek textarea, try standard paste first
    const dataTransfer = new DataTransfer();
    dataTransfer.setData('text/plain', text);

    const pasteEvent = new ClipboardEvent('paste', {
      bubbles: true,
      cancelable: true,
      clipboardData: dataTransfer
    });

    element.dispatchEvent(pasteEvent);
    
    // Give paste event time to process
    await sleep(100);

    // If paste didn't work, set value directly
    if (!element.value?.includes(text) && !element.textContent.includes(text)) {
      console.log('[AI-Automator] Paste failed, setting value directly');
      element.value = text;
      
      // Dispatch input event
      element.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        inputType: 'insertText',
        data: text
      }));
    }
    
    console.log('[AI-Automator] Text input complete. Value length:', element.value?.length || element.textContent.length);
    await sleep(100);
  } else {
    console.log('[AI-Automator] Using keystroke simulation for DeepSeek');
    // Keystroke method
    for (const char of text) {
      // Set value one char at a time
      element.value = (element.value || '') + char;
      
      // Dispatch input event for each char
      element.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        inputType: 'insertText',
        data: char
      }));
      
      await sleep(2 + Math.random() * 6);
    }
    
    await sleep(100);
  }
}

// Export functions if needed
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    findElementDeepSeek,
    clickSendButtonDeepSeek,
    waitForTextareaDeepSeek,
    typeTextDeepSeek
  };
}