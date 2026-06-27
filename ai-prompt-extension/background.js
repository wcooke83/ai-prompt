// Background script - Provider-agnostic WebSocket manager with Native Messaging support

// ============ Centralized Logging System ============
const LOG_BUFFER_SIZE = 3000; // generous so a full prompt→response run (with heartbeats/diagnostics) isn't truncated
const logBuffer = [];

function log(source, level, ...args) {
  const timestamp = new Date().toISOString().substring(11, 19);
  const message = args.map(arg =>
    typeof arg === 'object' ? JSON.stringify(arg) : String(arg)
  ).join(' ');

  const logEntry = {
    timestamp,
    source,
    level,
    message
  };

  logBuffer.push(logEntry);
  if (logBuffer.length > LOG_BUFFER_SIZE) {
    logBuffer.shift();
  }

  // Notify popup if open
  browser.runtime.sendMessage({
    type: 'newLog',
    log: logEntry
  }).catch(() => {});
}

// Hijack console methods to capture all logs
(function() {
  const originalLog = console.log;
  const originalWarn = console.warn;
  const originalError = console.error;

  console.log = function(...args) {
    originalLog.apply(console, args);
    log('Background', 'info', ...args);
  };

  console.warn = function(...args) {
    originalWarn.apply(console, args);
    log('Background', 'warn', ...args);
  };

  console.error = function(...args) {
    originalError.apply(console, args);
    log('Background', 'error', ...args);
  };
})();

// Register providers
ProviderRegistry.register(ChatGPTProvider);
ProviderRegistry.register(GrokProvider);
ProviderRegistry.register(ClaudeProvider);
ProviderRegistry.register(DeepSeekProvider);

// Connection state
let ws = null;
let wsPort = 8760;
let reconnectAttempts = 0;
let reconnectTimeout = null;
let connectionState = 'disconnected';
let activeProvider = null;
let fastReconnect = true; // Default to fast reconnect
let providerOrder = ['chatgpt', 'grok', 'claude', 'deepseek']; // Default order
let useNativeMessaging = true; // Prefer native messaging
let nativePort = null;
let debugLogging = false; // Debug logging for content scripts
let keepTabsOpen = false; // Keep tabs open after response (overrides ephemeral)
let hideTabs = false; // Open provider tabs hidden (active:false + tabs.hide) so they never appear
// Provider names that paste the whole prompt in one go (fast) instead of typing it key-by-key.
// null = not yet loaded -> treated as "all providers paste" (the default). Tracked per provider.
let pasteProviders = null;
let domStabilizeMs = 3000; // Milliseconds of DOM inactivity before extracting response
let disabledProviders = []; // Provider names the user switched off — never auto-selected or used for failover


// Session management: maps session_id -> { tabId, url, provider }
let sessions = {};

// Load saved sessions from storage
browser.storage.local.get('sessions').then(result => {
  if (result.sessions) {
    sessions = result.sessions;
  }
});

// Save sessions to storage
function saveSessions() {
  browser.storage.local.set({ sessions });
}

// Generate unique session ID
function generateSessionId() {
  return 'sess_' + Date.now().toString(36) + Math.random().toString(36).substr(2, 9);
}

// Get new chat URL for provider
function getNewChatUrl(providerName) {
  const urls = {
    'chatgpt': 'https://chatgpt.com/',
    'claude': 'https://claude.ai/new',
    'grok': 'https://grok.com/',
    'deepseek': 'https://chat.deepseek.com/'
  };
  return urls[providerName] || urls['chatgpt'];
}

// Whether the user has left this provider enabled (default: yes, unless explicitly switched off)
function isProviderEnabled(name) {
  return !disabledProviders.includes(name);
}

// Whether this provider should paste the whole prompt at once (default: yes). When off, the content
// script enters the prompt key-by-key. null pasteProviders = default (every provider pastes).
function usePasteFor(name) {
  return pasteProviders === null || pasteProviders.includes(name);
}

// Resolve pasteProviders to a concrete list for the popup/status (coalesces the null default).
function resolvedPasteProviders() {
  return pasteProviders === null ? ProviderRegistry.getAll().map(p => p.name) : pasteProviders;
}

// Get providers sorted by user preference
function getOrderedProviders() {
  const all = ProviderRegistry.getAll();
  return [...all].sort((a, b) => {
    const indexA = providerOrder.indexOf(a.name);
    const indexB = providerOrder.indexOf(b.name);
    if (indexA === -1) return 1;
    if (indexB === -1) return -1;
    return indexA - indexB;
  });
}

// Get first available provider (enabled + has implemented selectors)
function getFirstAvailableProvider() {
  for (const provider of getOrderedProviders()) {
    if (isProviderEnabled(provider.name) && provider.selectors.textarea && provider.selectors.textarea.length > 0) {
      return provider;
    }
  }
  return null;
}

// ============ Native Messaging ============

const NATIVE_HOST_NAME = 'ai_prompt_native';

function connectNative() {
  if (nativePort) {
    return; // Already connected
  }

  try {
    log('Background', 'info', 'Connecting to native host...');
    nativePort = browser.runtime.connectNative(NATIVE_HOST_NAME);

    nativePort.onMessage.addListener((message) => {
      log('Background', 'info', 'Native message received:', message.type);
      if (message.type === 'host_ready') {
        log('Background', 'info', 'Native host ready');
        updateConnectionState('connected');
        // Tell host we're ready
        nativePort.postMessage({ type: 'ready' });
      } else if (message.type === 'prompt') {
        handlePrompt(message).then(result => {
          // Send response back through native port
          nativePort.postMessage(result);
        });
      }
    });

    nativePort.onDisconnect.addListener((p) => {
      const error = p.error || browser.runtime.lastError;
      log('Background', 'warn', 'Native host disconnected:', error?.message || 'unknown');
      nativePort = null;
      updateConnectionState('disconnected');

      // Fall back to WebSocket if native messaging fails (but keep user preference)
      log('Background', 'info', 'Native messaging unavailable, falling back to WebSocket');
      connect();
    });

    // Don't claim 'connected' yet — wait for the host's 'host_ready' message
    // (handled above). If the native host isn't installed, onDisconnect fires
    // and we fall back to WebSocket, which reports the real connection state.
  } catch (e) {
    log('Background', 'error', 'Failed to connect to native host:', e);
    nativePort = null;
    // Fall back to WebSocket (but keep user preference)
    connect();
  }
}

// ============ Load Settings & Initialize ============

// Load saved settings from storage
browser.storage.local.get(['wsPort', 'fastReconnect', 'providerOrder', 'useNativeMessaging', 'debugLogging', 'keepTabsOpen', 'hideTabs', 'usePasteInput', 'pasteProviders', 'domStabilizeMs', 'disabledProviders']).then(result => {
  if (result.wsPort) {
    wsPort = result.wsPort;
  }
  if (result.fastReconnect !== undefined) {
    fastReconnect = result.fastReconnect;
  }
  if (result.providerOrder) {
    providerOrder = result.providerOrder;
  }
  if (result.useNativeMessaging !== undefined) {
    useNativeMessaging = result.useNativeMessaging;
  }
  if (result.debugLogging !== undefined) {
    debugLogging = result.debugLogging;
  }
  if (result.keepTabsOpen !== undefined) {
    keepTabsOpen = result.keepTabsOpen;
  }
  if (result.hideTabs !== undefined) {
    hideTabs = result.hideTabs;
  }
  if (Array.isArray(result.pasteProviders)) {
    pasteProviders = result.pasteProviders;
  } else if (result.usePasteInput !== undefined) {
    // Migrate the old single global toggle: on -> every provider pastes, off -> every provider types.
    pasteProviders = result.usePasteInput ? ProviderRegistry.getAll().map(p => p.name) : [];
    browser.storage.local.set({ pasteProviders });
  } else {
    // No saved preference -> default every provider to paste (one-shot, fast).
    pasteProviders = ProviderRegistry.getAll().map(p => p.name);
    browser.storage.local.set({ pasteProviders });
  }
  if (result.domStabilizeMs !== undefined) {
    domStabilizeMs = result.domStabilizeMs;
  }
  if (Array.isArray(result.disabledProviders)) {
    disabledProviders = result.disabledProviders;
  }

  // Try native messaging first, fall back to WebSocket
  if (useNativeMessaging) {
    connectNative();
  } else {
    connect();
  }
});

function updateConnectionState(state) {
  connectionState = state;
  browser.runtime.sendMessage({
    type: 'connectionState',
    state,
    provider: activeProvider ? activeProvider.name : null
  }).catch(() => {});
}

// Detach handlers and close the current socket so its onclose doesn't fire a
// stray scheduleReconnect() that races with a fresh connect() and leaks sockets.
function closeWs() {
  if (ws) {
    ws.onopen = ws.onclose = ws.onerror = ws.onmessage = null;
    try { ws.close(); } catch (e) {}
    ws = null;
  }
}

function connect() {
  // Already connected or mid-handshake — don't open a second socket.
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  try {
    ws = new WebSocket(`ws://localhost:${wsPort}`);

    ws.onopen = () => {
      log('Background', 'info', 'WebSocket connected');
      reconnectAttempts = 0;
      updateConnectionState('connected');
      detectActiveProvider();
    };

    ws.onclose = () => {
      log('Background', 'info', 'WebSocket disconnected');
      updateConnectionState('disconnected');
      scheduleReconnect();
    };

    ws.onerror = (error) => {
      log('Background', 'error', 'WebSocket error:', error);
      updateConnectionState('error');
    };

    ws.onmessage = async (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type === 'prompt') {
          await handlePrompt(message);
        }
      } catch (e) {
        log('Background', 'error', 'Error processing message:', e);
      }
    };
  } catch (e) {
    log('Background', 'error', 'Failed to create WebSocket:', e);
    scheduleReconnect();
  }
}

function scheduleReconnect() {
  if (reconnectTimeout) clearTimeout(reconnectTimeout);

  // Fast: constant 500ms. Normal: exponential backoff 1s-30s
  let delay;
  if (fastReconnect) {
    delay = 500; // Constant fast polling
  } else {
    delay = Math.min(1000 * Math.pow(2, reconnectAttempts), 30000);
    reconnectAttempts++;
  }

  reconnectTimeout = setTimeout(() => {
    connect();
  }, delay);
}

// Find tabs matching any provider pattern
async function findProviderTabs() {
  const allPatterns = ProviderRegistry.getAll().flatMap(p => p.hostPatterns);
  const tabs = await browser.tabs.query({ url: allPatterns });
  return tabs;
}

// Detect which provider is active based on open tabs
async function detectActiveProvider() {
  const tabs = await findProviderTabs();

  if (tabs.length === 0) {
    activeProvider = null;
    return null;
  }

  // Find first matching provider that has working selectors (in priority order)
  for (const provider of getOrderedProviders()) {
    // Skip providers with empty selectors (not implemented yet)
    if (!provider.selectors.textarea || provider.selectors.textarea.length === 0) {
      continue;
    }
    for (const tab of tabs) {
      for (const pattern of provider.hostPatterns) {
        if (ProviderRegistry.urlMatchesPattern(tab.url, pattern)) {
          activeProvider = provider;
          browser.runtime.sendMessage({
            type: 'providerChanged',
            provider: provider.name,
            displayName: provider.displayName
          }).catch(() => {});
          return { provider, tab };
        }
      }
    }
  }

  activeProvider = null;
  return null;
}

// Apply the "hide automation tabs" setting to a tab: when hideTabs is on, remove it from the tab
// strip via tabs.hide (needs the "tabHide" permission). The tab keeps running and the content script
// still drives it; note some providers may throttle a hidden/background tab. No-op when hideTabs is off.
async function applyTabVisibility(tabId) {
  if (!hideTabs || !tabId) return;
  try {
    if (browser.tabs.hide) {
      await browser.tabs.hide(tabId);
    }
  } catch (e) {
    console.log('tabs.hide failed:', e.message);
  }
}

// Create tab for provider
async function createProviderTab(providerName) {
  const provider = ProviderRegistry.get(providerName);
  if (!provider || !provider.selectors.textarea || provider.selectors.textarea.length === 0) {
    throw new Error(`Provider ${providerName} not available`);
  }

  const newSessionId = generateSessionId();
  const newChatUrl = getNewChatUrl(providerName);
  const newTab = await browser.tabs.create({ url: newChatUrl, active: !hideTabs });
  await applyTabVisibility(newTab.id);

  // Wait for page to load completely
  await new Promise(resolve => {
    const listener = (tabId, info) => {
      if (tabId === newTab.id && info.status === 'complete') {
        browser.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    };
    browser.tabs.onUpdated.addListener(listener);
  });

  // Give page extra time to initialize
  await new Promise(resolve => setTimeout(resolve, 2000));

  // Inject content script into the new tab
  try {
    await browser.tabs.executeScript(newTab.id, { file: 'logging.js' });
    await browser.tabs.executeScript(newTab.id, { file: 'providers/index.js' });
    await browser.tabs.executeScript(newTab.id, { file: 'providers/chatgpt.js' });
    await browser.tabs.executeScript(newTab.id, { file: 'providers/grok.js' });
    await browser.tabs.executeScript(newTab.id, { file: 'providers/claude.js' });
    await browser.tabs.executeScript(newTab.id, { file: 'providers/deepseek.js' });
    await browser.tabs.executeScript(newTab.id, { file: 'content.js' });
  } catch (e) {
    console.log('Content script injection:', e.message);
  }

  // Extra wait for content script to initialize
  await new Promise(resolve => setTimeout(resolve, 500));

  // Store session
  sessions[newSessionId] = {
    tabId: newTab.id,
    url: newChatUrl,
    provider: providerName
  };
  saveSessions();

  return { tab: newTab, provider, sessionId: newSessionId, isNew: true };
}

// Get or create tab for session
async function getOrCreateSessionTab(sessionId, preferredProvider) {
  // If session exists, try to use existing tab
  if (sessionId && sessions[sessionId]) {
    const session = sessions[sessionId];
    const provider = ProviderRegistry.get(session.provider);

    // Check if tab still exists
    try {
      const tab = await browser.tabs.get(session.tabId);
      if (tab) {
        // Tab exists — focus it (or keep it hidden when hideTabs is on)
        if (hideTabs) {
          await applyTabVisibility(tab.id);
        } else {
          await browser.tabs.update(tab.id, { active: true });
        }
        return { tab, provider, sessionId, isNew: false };
      }
    } catch (e) {
      // Tab was closed, try to restore from URL
      if (session.url) {
        const newTab = await browser.tabs.create({ url: session.url, active: !hideTabs });
        await applyTabVisibility(newTab.id);
        // Wait for page to load
        await new Promise(resolve => {
          const listener = (tabId, info) => {
            if (tabId === newTab.id && info.status === 'complete') {
              browser.tabs.onUpdated.removeListener(listener);
              resolve();
            }
          };
          browser.tabs.onUpdated.addListener(listener);
        });
        // Update session with new tab ID
        sessions[sessionId].tabId = newTab.id;
        saveSessions();
        return { tab: newTab, provider: ProviderRegistry.get(session.provider), sessionId, isNew: false };
      }
    }
  }

  // No session - use preferred provider or first in order
  const providerName = preferredProvider || getFirstAvailableProvider()?.name || 'chatgpt';
  return createProviderTab(providerName);
}

// Update session URL after conversation starts
function updateSessionUrl(sessionId, tabId) {
  browser.tabs.get(tabId).then(tab => {
    if (sessions[sessionId] && tab.url !== sessions[sessionId].url) {
      sessions[sessionId].url = tab.url;
      saveSessions();
    }
  }).catch(() => {});
}

// Try to execute prompt on a provider, with failover
async function tryProviderWithFailover(text, request_id, preferredProvider, session_id, ephemeral, attachments = null, triedProviders = []) {
  // If session exists, use that session's provider (no failover for existing sessions)
  if (session_id && sessions[session_id]) {
    const session = sessions[session_id];
    const sessionProvider = ProviderRegistry.get(session.provider);
    if (sessionProvider) {
      console.log(`Using existing session with provider: ${sessionProvider.name}`);
      // Continue with this provider, don't failover
      try {
        const result = await getOrCreateSessionTab(session_id, sessionProvider.name);
        const { tab, provider, sessionId } = result;

        const response = await browser.tabs.sendMessage(tab.id, {
          type: 'executePrompt',
          request_id,
          text,
          attachments,
          debugLogging,
          usePasteInput: usePasteFor(provider.name),
          provider: {
            name: provider.name,
            selectors: provider.selectors
          }
        });

        if (response.success) {
          updateSessionUrl(sessionId, tab.id);
          return {
            success: true,
            text: response.text,
            images: response.images || [],
            provider: provider.name,
            model: response.model || null,
            input_tokens: response.input_tokens || null,
            output_tokens: response.output_tokens || null,
            sessionId: sessionId
          };
        } else {
          return { success: false, error: response.error };
        }
      } catch (e) {
        return { success: false, error: e.message };
      }
    }
  }

  // An explicitly requested provider that the user has disabled is never silently substituted —
  // fail cleanly so callers that run their own fallback (and label results by the provider they
  // asked for, e.g. the securities valuation service) move on to their next choice instead of
  // recording the wrong provider. Auto-selection (no preferred) simply skips disabled ones below.
  // NB: keep the words "extension"/"connect"/"timeout" out of this message — the securities bridge
  // classifies those as a global connection error and aborts its whole run instead of falling back.
  if (preferredProvider && !isProviderEnabled(preferredProvider)) {
    log('Background', 'info', `Requested provider ${preferredProvider} is disabled; not used.`);
    return { success: false, error: `Provider ${preferredProvider} is disabled` };
  }

  // Get ordered list of enabled providers to try for new sessions
  let providersToTry = getOrderedProviders().filter(p =>
    p.selectors.textarea && p.selectors.textarea.length > 0 &&
    isProviderEnabled(p.name) &&
    !triedProviders.includes(p.name)
  );

  // If preferred provider specified and not tried yet, put it first
  if (preferredProvider && !triedProviders.includes(preferredProvider)) {
    const preferred = ProviderRegistry.get(preferredProvider);
    if (preferred && preferred.selectors.textarea && preferred.selectors.textarea.length > 0) {
      providersToTry = [preferred, ...providersToTry.filter(p => p.name !== preferredProvider)];
    }
  }

  if (providersToTry.length === 0) {
    // Distinguish "nothing left to try" from "the user switched everything off".
    const anyEnabled = ProviderRegistry.getAll().some(p =>
      isProviderEnabled(p.name) && p.selectors.textarea && p.selectors.textarea.length > 0
    );
    const error = anyEnabled
      ? 'All providers failed or unavailable'
      : 'All providers are disabled';
    return { success: false, error };
  }

  const providerToTry = providersToTry[0];
  log('Background', 'info', `Trying provider: ${providerToTry.name}`);

  try {
    // Get or create tab for this provider
    const result = await getOrCreateSessionTab(session_id, providerToTry.name);
    const { tab, provider, sessionId } = result;

    // Send prompt to content script
    let response;
    try {
      response = await browser.tabs.sendMessage(tab.id, {
        type: 'executePrompt',
        request_id,
        text,
        attachments,
        debugLogging,
        usePasteInput: usePasteFor(provider.name),
        domStabilizeMs,
        provider: {
          name: provider.name,
          selectors: provider.selectors
        }
      });
    } catch (e) {
      log('Background', 'error', `Provider ${provider.name} content script error: ${e.message}`);
      // Close the failed tab if it's ephemeral or new (unless keepTabsOpen is set)
      if ((ephemeral || result.isNew) && !keepTabsOpen) {
        browser.tabs.remove(tab.id).catch(() => {});
        delete sessions[sessionId];
        saveSessions();
      }
      // Try next provider
      return tryProviderWithFailover(text, request_id, null, null, ephemeral, attachments, [...triedProviders, provider.name]);
    }

    log('Background', 'info', `Provider ${provider.name} response: ${response.success ? 'success (' + (response.text ? response.text.length : 0) + ' chars)' : 'failed: ' + response.error}`);

    if (response.success) {
      // Update session URL after successful prompt
      if (!ephemeral || keepTabsOpen) {
        updateSessionUrl(sessionId, tab.id);
      }

      // Close tab for ephemeral requests (unless keepTabsOpen is set)
      if (ephemeral && !keepTabsOpen && tab.id) {
        setTimeout(() => {
          browser.tabs.remove(tab.id).catch(() => {});
          if (sessions[sessionId]) {
            delete sessions[sessionId];
            saveSessions();
          }
        }, 500);
      }

      return {
        success: true,
        text: response.text,
        images: response.images || [],
        provider: provider.name,
        model: response.model || null,
        input_tokens: response.input_tokens || null,
        output_tokens: response.output_tokens || null,
        sessionId: (ephemeral && !keepTabsOpen) ? null : sessionId
      };
    } else {
      console.log(`Provider ${provider.name} failed: ${response.error}`);
      // Close the failed tab if it's ephemeral or new (unless keepTabsOpen is set)
      if ((ephemeral || result.isNew) && !keepTabsOpen) {
        browser.tabs.remove(tab.id).catch(() => {});
        delete sessions[sessionId];
        saveSessions();
      }
      // Try next provider
      return tryProviderWithFailover(text, request_id, null, null, ephemeral, attachments, [...triedProviders, provider.name]);
    }
  } catch (e) {
    console.log(`Provider ${providerToTry.name} error: ${e.message}`);
    // Try next provider
    return tryProviderWithFailover(text, request_id, null, null, ephemeral, attachments, [...triedProviders, providerToTry.name]);
  }
}

// Handle incoming prompt from WebSocket or Native Messaging
async function handlePrompt(message) {
  const { request_id, text, provider: preferredProvider, session_id, ephemeral, attachments } = message;

  // Ephemeral (close tab after) only when not using JSON and no session provided
  const isEphemeral = ephemeral === true;

  const result = await tryProviderWithFailover(text, request_id, preferredProvider, session_id, isEphemeral, attachments || null);

  const response = {
    type: 'response',
    request_id,
    success: result.success,
    text: result.text || null,
    images: result.images || [],
    provider: result.provider || null,
    model: result.model || null,
    input_tokens: result.input_tokens || null,
    output_tokens: result.output_tokens || null,
    session_id: result.sessionId || null
  };

  if (!result.success) {
    response.error = result.error;
  }

  // For WebSocket, send the result back to the API/bridge.
  if (ws && ws.readyState === WebSocket.OPEN) {
    const payload = JSON.stringify(response);
    log('Background', 'info',
      `Sending response to API: request_id=${request_id}, success=${response.success}, provider=${response.provider || 'none'}, textLen=${response.text ? response.text.length : 0}, payloadBytes=${payload.length}${response.error ? `, error="${response.error}"` : ''}`);
    try {
      ws.send(payload);
      log('Background', 'info', `Response sent for request_id=${request_id}`);
    } catch (e) {
      log('Background', 'error', `ws.send failed for request_id=${request_id}: ${e.message}`);
    }
  } else {
    // The capture may have succeeded but there's nowhere to send it — surface this loudly, it
    // otherwise looks like the whole run silently vanished.
    log('Background', 'warn',
      `Cannot send response for request_id=${request_id}: WebSocket not open (readyState=${ws ? ws.readyState : 'no ws'}). Response dropped (success=${response.success}, textLen=${response.text ? response.text.length : 0}).`);
  }

  // Return response for native messaging
  return response;
}

function sendResponse(request_id, text, success, error = null, provider = null, session_id = null) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    const response = {
      type: 'response',
      request_id,
      text,
      success
    };
    if (error) response.error = error;
    if (provider) response.provider = provider;
    if (session_id) response.session_id = session_id;
    ws.send(JSON.stringify(response));
  }
}

// Handle messages from popup and content scripts
browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'log') {
    // Content script or other scripts sending logs
    log(message.source || 'Content', message.level || 'info', message.message);
    sendResponse({ success: true });
  } else if (message.type === 'getLogs') {
    sendResponse({ logs: logBuffer });
  } else if (message.type === 'clearLogs') {
    logBuffer.length = 0;
    sendResponse({ success: true });
  } else if (message.type === 'getStatus') {
    detectActiveProvider().then(() => {
      sendResponse({
        state: connectionState,
        port: wsPort,
        fastReconnect: fastReconnect,
        useNativeMessaging: useNativeMessaging,
        nativeConnected: nativePort !== null,
        providerOrder: providerOrder,
        debugLogging: debugLogging,
        keepTabsOpen: keepTabsOpen,
        hideTabs: hideTabs,
        pasteProviders: resolvedPasteProviders(),
        domStabilizeMs: domStabilizeMs,
        disabledProviders: disabledProviders,
        provider: activeProvider ? activeProvider.name : null,
        availableProviders: ProviderRegistry.getAll().map(p => ({
          name: p.name,
          displayName: p.displayName
        }))
      });
    });
    return true;
  } else if (message.type === 'setDebugLogging') {
    debugLogging = message.value;
    browser.storage.local.set({ debugLogging });
    sendResponse({ success: true });
  } else if (message.type === 'getDebugLogging') {
    sendResponse({ debugLogging });
  } else if (message.type === 'setKeepTabsOpen') {
    keepTabsOpen = message.value;
    browser.storage.local.set({ keepTabsOpen });
    sendResponse({ success: true });
  } else if (message.type === 'setHideTabs') {
    hideTabs = message.value;
    browser.storage.local.set({ hideTabs });
    sendResponse({ success: true });
  } else if (message.type === 'setProviderPaste') {
    const { name, paste } = message;
    if (pasteProviders === null) pasteProviders = ProviderRegistry.getAll().map(p => p.name);
    if (paste) {
      if (!pasteProviders.includes(name)) pasteProviders = [...pasteProviders, name];
    } else {
      pasteProviders = pasteProviders.filter(n => n !== name);
    }
    browser.storage.local.set({ pasteProviders });
    log('Background', 'info', `Provider ${name} input mode set to ${paste ? 'paste' : 'type'}`);
    sendResponse({ success: true });
  } else if (message.type === 'setDomStabilizeMs') {
    domStabilizeMs = message.value;
    browser.storage.local.set({ domStabilizeMs });
    sendResponse({ success: true });
  } else if (message.type === 'setUseNativeMessaging') {
    useNativeMessaging = message.value;
    browser.storage.local.set({ useNativeMessaging });
    // Disconnect current connections and reconnect with new method
    if (reconnectTimeout) { clearTimeout(reconnectTimeout); reconnectTimeout = null; }
    reconnectAttempts = 0;
    closeWs();
    if (nativePort) {
      try { nativePort.disconnect(); } catch (e) {}
      nativePort = null;
    }
    if (useNativeMessaging) {
      connectNative();
    } else {
      connect();
    }
    sendResponse({ success: true });
  } else if (message.type === 'setPort') {
    wsPort = message.port;
    browser.storage.local.set({ wsPort });
    if (reconnectTimeout) { clearTimeout(reconnectTimeout); reconnectTimeout = null; }
    reconnectAttempts = 0;
    closeWs();
    connect();
    sendResponse({ success: true });
  } else if (message.type === 'setFastReconnect') {
    fastReconnect = message.value;
    browser.storage.local.set({ fastReconnect });
    sendResponse({ success: true });
  } else if (message.type === 'setProviderOrder') {
    providerOrder = message.order;
    browser.storage.local.set({ providerOrder });
    sendResponse({ success: true });
  } else if (message.type === 'setProviderEnabled') {
    const { name, enabled } = message;
    if (enabled) {
      disabledProviders = disabledProviders.filter(n => n !== name);
    } else if (!disabledProviders.includes(name)) {
      disabledProviders = [...disabledProviders, name];
    }
    browser.storage.local.set({ disabledProviders });
    log('Background', 'info', `Provider ${name} ${enabled ? 'enabled' : 'disabled'}`);
    sendResponse({ success: true });
  } else if (message.type === 'reconnect') {
    // Force a clean reconnect on whichever transport is selected. Tear down any
    // existing socket/port and pending retry first so we don't stack connections.
    if (reconnectTimeout) { clearTimeout(reconnectTimeout); reconnectTimeout = null; }
    reconnectAttempts = 0;
    closeWs();
    if (nativePort) { try { nativePort.disconnect(); } catch (e) {} nativePort = null; }
    updateConnectionState('disconnected');
    if (useNativeMessaging) {
      connectNative();
    } else {
      connect();
    }
    sendResponse({ success: true });
  } else if (message.type === 'detectProvider') {
    detectActiveProvider().then(result => {
      sendResponse({
        provider: result ? result.provider.name : null,
        displayName: result ? result.provider.displayName : null
      });
    });
    return true;
  }
  return true;
});

// Listen for tab updates to detect provider changes
browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete') {
    detectActiveProvider();
  }
});

browser.tabs.onRemoved.addListener(() => {
  detectActiveProvider();
});
