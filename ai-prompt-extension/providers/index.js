// Provider registry - aggregates all providers
const providers = {};

// Register a provider
function registerProvider(provider) {
  providers[provider.name] = provider;
}

// Get provider by name
function getProvider(name) {
  return providers[name] || null;
}

// Get all registered providers
function getAllProviders() {
  return Object.values(providers);
}

// Find provider matching a URL
function getProviderForUrl(url) {
  for (const provider of Object.values(providers)) {
    for (const pattern of provider.hostPatterns) {
      // Convert glob pattern to regex
      const regexPattern = pattern
        .replace(/\*/g, '.*')
        .replace(/\//g, '\\/')
        .replace(/\./g, '\\.');
      const regex = new RegExp('^' + regexPattern.replace('\\.\\*', '.*') + '$');
      if (regex.test(url)) {
        return provider;
      }
    }
  }
  return null;
}

// Match URL against glob pattern (simpler check)
function urlMatchesPattern(url, pattern) {
  // Handle *:// prefix
  const urlObj = new URL(url);
  const host = urlObj.hostname;
  const path = urlObj.pathname;

  // Extract host pattern
  const patternMatch = pattern.match(/^\*:\/\/([^/]+)(\/.*)?$/);
  if (!patternMatch) return false;

  const hostPattern = patternMatch[1];
  const pathPattern = patternMatch[2] || '/*';

  // Check host
  const hostRegex = new RegExp('^' + hostPattern.replace(/\./g, '\\.').replace(/\*/g, '.*') + '$');
  if (!hostRegex.test(host)) return false;

  // Check path
  const pathRegex = new RegExp('^' + pathPattern.replace(/\*/g, '.*') + '$');
  return pathRegex.test(path);
}

// Export. Use `self` (not `window`) so this also works in a Chrome MV3 service worker,
// which has no `window` — `self` is the global object in every context (page, content
// script, background page, and service worker) that this file is loaded into.
self.ProviderRegistry = {
  register: registerProvider,
  get: getProvider,
  getAll: getAllProviders,
  getForUrl: getProviderForUrl,
  urlMatchesPattern: urlMatchesPattern,
  providers: providers
};
