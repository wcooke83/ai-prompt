// Logging script - MUST BE LOADED FIRST to capture all console logs
// This hijacks console methods BEFORE any other scripts load

(function() {
  // Send log to background
  function sendLogToBackground(level, ...args) {
    const message = args.map(arg =>
      typeof arg === 'object' ? JSON.stringify(arg) : String(arg)
    ).join(' ');

    try {
      browser.runtime.sendMessage({
        type: 'log',
        source: 'Content',
        level: level,
        message: message
      }).catch(() => {});
    } catch (e) {
      // Silently fail if browser API not available yet
    }
  }

  // Store original console methods
  const originalLog = console.log;
  const originalWarn = console.warn;
  const originalError = console.error;

  // Hijack console.log
  console.log = function(...args) {
    originalLog.apply(console, args);
    sendLogToBackground('info', ...args);
  };

  // Hijack console.warn
  console.warn = function(...args) {
    originalWarn.apply(console, args);
    sendLogToBackground('warn', ...args);
  };

  // Hijack console.error
  console.error = function(...args) {
    originalError.apply(console, args);
    sendLogToBackground('error', ...args);
  };

  console.log('[Logging] Console hijacking initialized - all logs will be captured');
})();
