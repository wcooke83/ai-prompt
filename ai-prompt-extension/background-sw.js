// Chrome MV3 entry point. manifest.background.service_worker only accepts a single file (unlike
// MV2's background.scripts array), so load the shared provider registry + background.js in the
// same order Firefox's manifest.ff.json lists them via importScripts (classic worker, shared scope).
importScripts(
  'providers/index.js',
  'providers/chatgpt.js',
  'providers/grok.js',
  'providers/claude.js',
  'providers/deepseek.js',
  'background.js'
);
