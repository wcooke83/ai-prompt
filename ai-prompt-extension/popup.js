const browserAPI = typeof browser !== 'undefined' ? browser : chrome;

const statusIndicator = document.getElementById('status-indicator');
const statusText = document.getElementById('status-text');
const providerList = document.getElementById('provider-list');
const portInput = document.getElementById('port');
const saveBtn = document.getElementById('save-btn');
const reconnectBtn = document.getElementById('reconnect-btn');
const reconnectBtnWs = document.getElementById('reconnect-btn-ws');
const connectionDetail = document.getElementById('connection-detail');
const fastReconnectToggle = document.getElementById('fast-reconnect');
const nativeMessagingToggle = document.getElementById('native-messaging');
const websocketSettings = document.getElementById('websocket-settings');
const debugLoggingToggle = document.getElementById('debug-logging');
const keepTabsOpenToggle = document.getElementById('keep-tabs-open');
const hideTabsToggle = document.getElementById('hide-tabs');
const domStabilizeMsInput = document.getElementById('dom-stabilize-ms');
const logsContainer = document.getElementById('logs-container');
const clearLogsBtn = document.getElementById('clear-logs-btn');
const autoScrollBtn = document.getElementById('auto-scroll-btn');

const providerIcons = {
  chatgpt: 'G',
  claude: 'C',
  grok: 'X',
  deepseek: 'D'
};

const providerColors = {
  chatgpt: '#10a37f',
  claude: '#d97706',
  grok: '#1da1f2',
  deepseek: '#0f0f0f'
};

let draggedItem = null;
let autoScroll = true;
let disabledProviders = []; // provider names switched off (mirrors background state)
let pasteProviders = []; // provider names that paste the whole prompt at once (mirrors background state)
let currentPort = 8760; // WebSocket port the background is targeting (mirrors background state)
let currentUseNative = true; // whether native messaging is the preferred transport

// Logging utility for popup
function logToBackground(level, ...args) {
  const message = args.map(arg =>
    typeof arg === 'object' ? JSON.stringify(arg) : String(arg)
  ).join(' ');

  browserAPI.runtime.sendMessage({
    type: 'log',
    source: 'Popup',
    level: level,
    message: message
  }).catch(() => {});
}

// ============ Tab Management ============
const tabs = document.querySelectorAll('.tab');
const tabContents = document.querySelectorAll('.tab-content');

tabs.forEach(tab => {
  tab.addEventListener('click', () => {
    const targetTab = tab.dataset.tab;

    // Update active tab
    tabs.forEach(t => t.classList.remove('active'));
    tab.classList.add('active');

    // Update active content
    tabContents.forEach(content => content.classList.remove('active'));
    document.getElementById(`${targetTab}-tab`).classList.add('active');

    // Load logs when logs tab is opened
    if (targetTab === 'logs') {
      loadLogs();
    }
  });
});

// ============ Logs Management ============
function loadLogs() {
  browserAPI.runtime.sendMessage({ type: 'getLogs' }).then(response => {
    displayLogs(response.logs || []);
  });
}

function displayLogs(logs) {
  if (logs.length === 0) {
    logsContainer.innerHTML = '<div class="empty-logs">No logs yet</div>';
    return;
  }

  logsContainer.innerHTML = '';
  logs.forEach(log => {
    addLogEntry(log);
  });

  if (autoScroll) {
    logsContainer.scrollTop = logsContainer.scrollHeight;
  }
}

function addLogEntry(log) {
  const emptyLogs = logsContainer.querySelector('.empty-logs');
  if (emptyLogs) {
    emptyLogs.remove();
  }

  const entry = document.createElement('div');
  entry.className = 'log-entry';

  entry.innerHTML = `
    <span class="log-timestamp">${log.timestamp}</span>
    <span class="log-source">[${log.source}]</span>
    <span class="log-level ${log.level}">${log.level.toUpperCase()}</span>
    <span class="log-message">${escapeHtml(log.message)}</span>
  `;

  logsContainer.appendChild(entry);

  if (autoScroll) {
    logsContainer.scrollTop = logsContainer.scrollHeight;
  }
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

clearLogsBtn.addEventListener('click', () => {
  browserAPI.runtime.sendMessage({ type: 'clearLogs' }).then(() => {
    logsContainer.innerHTML = '<div class="empty-logs">No logs yet</div>';
  });
});

autoScrollBtn.addEventListener('click', () => {
  autoScroll = !autoScroll;
  autoScrollBtn.textContent = `Auto-scroll: ${autoScroll ? 'ON' : 'OFF'}`;
  autoScrollBtn.className = autoScroll ? '' : 'secondary';
});

// Listen for new logs
browserAPI.runtime.onMessage.addListener((message) => {
  if (message.type === 'newLog') {
    // Only update if logs tab is active
    if (document.querySelector('.tab[data-tab="logs"]').classList.contains('active')) {
      addLogEntry(message.log);
    }
  } else if (message.type === 'connectionState') {
    updateStatus(message.state);
  }
});

// ============ Status & Provider Management ============
function updateStatus(state) {
  statusIndicator.className = 'status-indicator ' + state;

  const statusLabels = {
    connected: 'Connected',
    disconnected: 'Disconnected',
    error: 'Connection Error'
  };

  statusText.textContent = statusLabels[state] || state;
  renderConnectionDetail();
}

// Show what the background is actually trying to reach, so a wrong port (e.g. an
// old 8765 pointing at something that isn't the bridge) is visible at a glance.
function renderConnectionDetail() {
  if (!connectionDetail) return;
  connectionDetail.textContent = currentUseNative
    ? `Native messaging (WebSocket fallback: localhost:${currentPort})`
    : `WebSocket: localhost:${currentPort}`;
}

// Trigger a reconnect and give brief feedback on the clicked button.
function triggerReconnect(btn) {
  browserAPI.runtime.sendMessage({ type: 'reconnect' }).catch(() => {});
  if (!btn) return;
  const prevText = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Reconnecting…';
  setTimeout(() => {
    btn.disabled = false;
    btn.textContent = prevText;
  }, 1500);
}

function renderProviderList(providers, order, disabled = [], paste = []) {
  providerList.innerHTML = '';

  // Sort providers by saved order
  const sortedProviders = [...providers].sort((a, b) => {
    const indexA = order.indexOf(a.name);
    const indexB = order.indexOf(b.name);
    // If not in order array, put at end
    if (indexA === -1) return 1;
    if (indexB === -1) return -1;
    return indexA - indexB;
  });

  sortedProviders.forEach((provider) => {
    const enabled = !disabled.includes(provider.name);
    const pasteOn = paste.includes(provider.name);
    const li = document.createElement('li');
    li.className = 'provider-list-item' + (enabled ? '' : ' provider-disabled');
    li.draggable = true;
    li.dataset.provider = provider.name;

    li.innerHTML = `
      <span class="drag-handle">&#9776;</span>
      <div class="provider-icon" style="background-color: ${providerColors[provider.name] || '#9ca3af'}">
        ${providerIcons[provider.name] || '?'}
      </div>
      <span class="provider-label">${provider.displayName}</span>
      <span class="provider-rank"></span>
      <div class="provider-paste" title="Paste the whole prompt at once (on) or type it key-by-key (off)">
        <span class="paste-icon">📋</span>
        <div class="toggle">
          <input type="checkbox" ${pasteOn ? 'checked' : ''}>
          <span class="toggle-slider"></span>
        </div>
      </div>
      <div class="toggle provider-toggle" title="Enable or disable this provider">
        <input type="checkbox" ${enabled ? 'checked' : ''}>
        <span class="toggle-slider"></span>
      </div>
    `;

    // Drag events
    li.addEventListener('dragstart', handleDragStart);
    li.addEventListener('dragend', handleDragEnd);
    li.addEventListener('dragover', handleDragOver);
    li.addEventListener('dragenter', handleDragEnter);
    li.addEventListener('dragleave', handleDragLeave);
    li.addEventListener('drop', handleDrop);

    // Per-provider paste toggle — keep its interactions from starting a drag on the row
    const pasteWrap = li.querySelector('.provider-paste');
    pasteWrap.draggable = false;
    pasteWrap.addEventListener('mousedown', (e) => e.stopPropagation());
    pasteWrap.addEventListener('click', (e) => e.stopPropagation());
    const pasteInput = pasteWrap.querySelector('input');
    pasteInput.addEventListener('change', () => {
      handleProviderPasteToggle(provider.name, pasteInput.checked);
    });

    // Enable/disable toggle — keep its interactions from starting a drag on the row
    const toggleWrap = li.querySelector('.provider-toggle');
    toggleWrap.draggable = false;
    toggleWrap.addEventListener('mousedown', (e) => e.stopPropagation());
    toggleWrap.addEventListener('click', (e) => e.stopPropagation());
    const toggleInput = toggleWrap.querySelector('input');
    toggleInput.addEventListener('change', () => {
      handleProviderToggle(provider.name, li, toggleInput.checked);
    });

    providerList.appendChild(li);
  });

  updateRankNumbers();
}

function handleProviderToggle(name, li, enabled) {
  li.classList.toggle('provider-disabled', !enabled);
  if (enabled) {
    disabledProviders = disabledProviders.filter(n => n !== name);
  } else if (!disabledProviders.includes(name)) {
    disabledProviders.push(name);
  }
  updateRankNumbers();
  browserAPI.runtime.sendMessage({ type: 'setProviderEnabled', name, enabled });
}

function handleProviderPasteToggle(name, paste) {
  if (paste) {
    if (!pasteProviders.includes(name)) pasteProviders.push(name);
  } else {
    pasteProviders = pasteProviders.filter(n => n !== name);
  }
  browserAPI.runtime.sendMessage({ type: 'setProviderPaste', name, paste });
}

function handleDragStart(e) {
  draggedItem = this;
  this.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', this.dataset.provider);
}

function handleDragEnd(e) {
  this.classList.remove('dragging');
  document.querySelectorAll('.provider-list-item').forEach(item => {
    item.classList.remove('drag-over');
  });
  draggedItem = null;
}

function handleDragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
}

function handleDragEnter(e) {
  e.preventDefault();
  if (this !== draggedItem) {
    this.classList.add('drag-over');
  }
}

function handleDragLeave(e) {
  this.classList.remove('drag-over');
}

function handleDrop(e) {
  e.preventDefault();
  this.classList.remove('drag-over');

  if (draggedItem && this !== draggedItem) {
    const allItems = [...providerList.querySelectorAll('.provider-list-item')];
    const draggedIndex = allItems.indexOf(draggedItem);
    const targetIndex = allItems.indexOf(this);

    if (draggedIndex < targetIndex) {
      this.parentNode.insertBefore(draggedItem, this.nextSibling);
    } else {
      this.parentNode.insertBefore(draggedItem, this);
    }

    // Update rank numbers
    updateRankNumbers();

    // Save new order
    saveProviderOrder();
  }
}

function updateRankNumbers() {
  const items = providerList.querySelectorAll('.provider-list-item');
  let rank = 0;
  items.forEach((item) => {
    const rankEl = item.querySelector('.provider-rank');
    if (!rankEl) return;
    if (item.classList.contains('provider-disabled')) {
      rankEl.textContent = 'Off';
      rankEl.classList.add('off');
    } else {
      rank += 1;
      rankEl.textContent = `#${rank}`;
      rankEl.classList.remove('off');
    }
  });
}

function saveProviderOrder() {
  const items = providerList.querySelectorAll('.provider-list-item');
  const order = Array.from(items).map(item => item.dataset.provider);
  browserAPI.runtime.sendMessage({ type: 'setProviderOrder', order });
}

function updateWebsocketSettingsVisibility(useNative) {
  websocketSettings.style.display = useNative ? 'none' : 'block';
}

// Get initial status
browserAPI.runtime.sendMessage({ type: 'getStatus' }).then(response => {
  currentPort = response.port;
  currentUseNative = response.useNativeMessaging !== false;
  updateStatus(response.state);
  portInput.value = response.port;
  fastReconnectToggle.checked = response.fastReconnect !== false;
  nativeMessagingToggle.checked = response.useNativeMessaging !== false;
  debugLoggingToggle.checked = response.debugLogging === true;
  keepTabsOpenToggle.checked = response.keepTabsOpen === true;
  hideTabsToggle.checked = response.hideTabs === true;
  domStabilizeMsInput.value = response.domStabilizeMs || 3000;
  updateWebsocketSettingsVisibility(response.useNativeMessaging !== false);

  if (response.availableProviders) {
    disabledProviders = response.disabledProviders || [];
    pasteProviders = response.pasteProviders || [];
    renderProviderList(response.availableProviders, response.providerOrder || [], disabledProviders, pasteProviders);
  }
});

// Save port
saveBtn.addEventListener('click', () => {
  const port = parseInt(portInput.value, 10);
  if (port >= 1 && port <= 65535) {
    currentPort = port;
    renderConnectionDetail();
    browserAPI.runtime.sendMessage({ type: 'setPort', port });
  }
});

// Reconnect (main tab button, plus the one in WebSocket settings)
reconnectBtn.addEventListener('click', () => triggerReconnect(reconnectBtn));
if (reconnectBtnWs) {
  reconnectBtnWs.addEventListener('click', () => triggerReconnect(reconnectBtnWs));
}

// Fast reconnect toggle
fastReconnectToggle.addEventListener('change', () => {
  browserAPI.runtime.sendMessage({ type: 'setFastReconnect', value: fastReconnectToggle.checked });
});

// Native messaging toggle
nativeMessagingToggle.addEventListener('change', () => {
  const useNative = nativeMessagingToggle.checked;
  currentUseNative = useNative;
  renderConnectionDetail();
  browserAPI.runtime.sendMessage({ type: 'setUseNativeMessaging', value: useNative });
  updateWebsocketSettingsVisibility(useNative);
});

// Debug logging toggle
debugLoggingToggle.addEventListener('change', () => {
  browserAPI.runtime.sendMessage({ type: 'setDebugLogging', value: debugLoggingToggle.checked });
});

// Keep tabs open toggle
keepTabsOpenToggle.addEventListener('change', () => {
  browserAPI.runtime.sendMessage({ type: 'setKeepTabsOpen', value: keepTabsOpenToggle.checked });
});

// Hide tabs toggle
hideTabsToggle.addEventListener('change', () => {
  browserAPI.runtime.sendMessage({ type: 'setHideTabs', value: hideTabsToggle.checked });
});

// DOM stabilize ms input
domStabilizeMsInput.addEventListener('change', () => {
  const value = parseInt(domStabilizeMsInput.value, 10);
  if (value >= 500 && value <= 10000) {
    browserAPI.runtime.sendMessage({ type: 'setDomStabilizeMs', value });
  }
});
