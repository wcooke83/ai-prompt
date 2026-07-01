// Offscreen document — hosts the WebSocket connection to the ai-prompt-api bridge on Chrome.
//
// Exists ONLY because Chrome's MV3 service worker (background-sw.js) is suspended after ~30s of
// inactivity, which drops any socket owned by it with no in-page event to react to. An offscreen
// document is a normal page context — Chrome does not apply that idle-suspension to it — so the
// socket lives here instead and survives service-worker restarts. The service worker drives this
// document with `target: 'offscreen'` messages (connect/disconnect/send/query-status); this
// document pushes socket events back with `target: 'background'` messages, which — per Chrome's
// extension messaging model — wake the service worker even if it had been suspended.
//
// Deliberately minimal: no provider/tab logic here (offscreen documents can't use chrome.tabs or
// chrome.scripting anyway) — that all stays in background.js. This file only ever forwards bytes.

const browserAPI = typeof browser !== 'undefined' ? browser : chrome;

let ws = null;
let wsPort = 8760;
let fastReconnect = true;
let reconnectAttempts = 0;
let reconnectTimeout = null;

function log(level, ...args) {
  const message = args.map(a => (typeof a === 'object' ? JSON.stringify(a) : String(a))).join(' ');
  browserAPI.runtime.sendMessage({ type: 'log', source: 'Offscreen', level, message }).catch(() => {});
}

function notifyBackground(message) {
  browserAPI.runtime.sendMessage({ target: 'background', ...message }).catch(() => {});
}

function isOpen() {
  return !!(ws && ws.readyState === WebSocket.OPEN);
}

function disconnect() {
  if (reconnectTimeout) {
    clearTimeout(reconnectTimeout);
    reconnectTimeout = null;
  }
  if (ws) {
    ws.onopen = ws.onclose = ws.onerror = ws.onmessage = null;
    try { ws.close(); } catch (e) {}
    ws = null;
  }
}

function scheduleReconnect() {
  if (reconnectTimeout) clearTimeout(reconnectTimeout);

  // Fast: constant 500ms. Normal: exponential backoff 1s-30s (mirrors background.js's Firefox path).
  let delay;
  if (fastReconnect) {
    delay = 500;
  } else {
    delay = Math.min(1000 * Math.pow(2, reconnectAttempts), 30000);
    reconnectAttempts++;
  }

  reconnectTimeout = setTimeout(() => connect(), delay);
}

function connect() {
  // Already connected or mid-handshake — don't open a second socket. Safe to call this repeatedly
  // (the service worker does, on every restart) since it's a no-op once a socket is live.
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  try {
    ws = new WebSocket(`ws://localhost:${wsPort}`);

    ws.onopen = () => {
      log('info', 'WebSocket connected');
      reconnectAttempts = 0;
      notifyBackground({ type: 'ws-status', status: 'open' });
    };

    ws.onclose = () => {
      log('info', 'WebSocket disconnected');
      notifyBackground({ type: 'ws-status', status: 'closed' });
      scheduleReconnect();
    };

    ws.onerror = (error) => {
      const detail = (error && error.message) || 'unknown error';
      log('error', 'WebSocket error:', detail);
      notifyBackground({ type: 'ws-status', status: 'error', error: detail });
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        notifyBackground({ type: 'ws-message', data });
      } catch (e) {
        log('error', 'Error parsing message:', e.message);
      }
    };
  } catch (e) {
    log('error', 'Failed to create WebSocket:', e.message);
    scheduleReconnect();
  }
}

browserAPI.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.target !== 'offscreen') return; // not addressed to us (e.g. meant for the service worker)

  if (message.type === 'connect') {
    if (message.wsPort) wsPort = message.wsPort;
    if (typeof message.fastReconnect === 'boolean') fastReconnect = message.fastReconnect;
    reconnectAttempts = 0;
    connect();
    sendResponse({ success: true });
  } else if (message.type === 'disconnect') {
    disconnect();
    sendResponse({ success: true });
  } else if (message.type === 'send') {
    if (isOpen()) {
      try {
        ws.send(message.payload);
        sendResponse({ success: true });
      } catch (e) {
        log('error', 'ws.send failed:', e.message);
        sendResponse({ success: false, error: e.message });
      }
    } else {
      log('warn', 'Cannot send: WebSocket not open.');
      sendResponse({ success: false, error: 'WebSocket not open' });
    }
  } else if (message.type === 'set-fast-reconnect') {
    fastReconnect = message.value;
    sendResponse({ success: true });
  } else if (message.type === 'query-status') {
    sendResponse({ open: isOpen() });
  }
  return true;
});
