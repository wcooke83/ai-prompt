#!/usr/bin/env node
/*
 * Regression test for the "provider pin suppresses failover" behaviour.
 *
 * There is no JS test harness in this repo (package.json pulls in playwright only),
 * and making the whole extension background.js require()-able under Node is
 * disproportionate. Instead this test extracts the REAL `tryProviderWithFailover`
 * function source out of ai-prompt-extension/background.js and evaluates it inside a
 * `vm` sandbox whose browser globals (browserAPI, ProviderRegistry, sessions, ...)
 * are stubbed. So it exercises the actual shipped source, and will pick up any future
 * edit to that function.
 *
 * Run: node tests/test_pinned_provider_failover.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const BG = path.join(__dirname, '..', 'ai-prompt-extension', 'background.js');

// --- Extract the tryProviderWithFailover function source ------------------------
function extractFunction(src, name) {
  const startMarker = `async function ${name}`;
  const start = src.indexOf(startMarker);
  if (start < 0) throw new Error(`could not find ${startMarker}`);
  const rest = src.slice(start);
  // The function's closing brace is the only column-0 "}" in its body (verified).
  const endRel = rest.indexOf('\n}\n');
  if (endRel < 0) throw new Error('could not find end of function');
  return rest.slice(0, endRel + 3); // include the trailing "\n}\n"
}

const fnSrc = extractFunction(fs.readFileSync(BG, 'utf8'), 'tryProviderWithFailover');

// --- Build a fresh sandbox per scenario ----------------------------------------
const DEFAULT_ORDER = ['chatgpt', 'grok', 'claude', 'deepseek'];

function makeProviders() {
  const p = {};
  for (const n of DEFAULT_ORDER) p[n] = { name: n, selectors: { textarea: ['sel'] } };
  return p;
}

/*
 * behaviour: map providerName -> { send?: 'ok'|'fail'|'throw', tab?: 'throw' }
 *   send 'ok'    -> sendMessage resolves { success:true, text:'<name> reply' }
 *   send 'fail'  -> sendMessage resolves { success:false, error:'<name> boom' }
 *   send 'throw' -> sendMessage throws (content-script/tab error path)
 *   tab  'throw' -> getOrCreateSessionTab throws (outer catch path)
 * default when a provider is missing from the map: send 'fail'.
 */
function runScenario(behaviour, callArgs) {
  const providers = makeProviders();
  const sentTo = [];     // providers that actually received the payload (sendMessage)
  const tabOpens = [];   // providers a tab was opened / requested for
  let invocations = 0;

  const sandbox = {
    // --- data the function reads ---
    sessions: {},
    keepTabsOpen: false,
    debugLogging: false,
    domStabilizeMs: 0,
    // --- collaborators ---
    ProviderRegistry: {
      get: (n) => providers[n],
      getAll: () => DEFAULT_ORDER.map((n) => providers[n]),
    },
    getOrderedProviders: () => DEFAULT_ORDER.map((n) => providers[n]),
    isProviderEnabled: () => true,
    usePasteFor: () => false,
    updateSessionUrl: () => {},
    saveSessions: () => {},
    log: () => {},
    getOrCreateSessionTab: (session_id, providerName) => {
      invocations++; // guard against runaway loops (checked below)
      tabOpens.push(providerName);
      const b = behaviour[providerName] || { send: 'fail' };
      if (b.tab === 'throw') throw new Error(`${providerName} tab open failed`);
      return Promise.resolve({
        tab: { id: `tab-${providerName}` },
        provider: providers[providerName],
        sessionId: `sess-${providerName}`,
        isNew: true,
      });
    },
    browserAPI: {
      tabs: {
        sendMessage: (tabId, msg) => {
          const name = msg.provider.name;
          sentTo.push(name); // the payload reached this provider
          const b = behaviour[name] || { send: 'fail' };
          if (b.send === 'throw') throw new Error(`${name} content script error`);
          if (b.send === 'ok') return Promise.resolve({ success: true, text: `${name} reply` });
          return Promise.resolve({ success: false, error: `${name} boom` });
        },
        remove: () => Promise.resolve(),
        get: () => Promise.resolve({ url: 'about:blank' }),
      },
    },
    console: { log: () => {} },
    setTimeout: setTimeout,
  };

  const context = vm.createContext(sandbox);
  vm.runInContext(fnSrc, context, { filename: 'background.js:tryProviderWithFailover' });

  return context
    .tryProviderWithFailover(...callArgs)
    .then((result) => {
      assert.ok(invocations <= 8, `runaway recursion: ${invocations} invocations`);
      return { result, sentTo, tabOpens };
    });
}

// Signature: (text, request_id, preferredProvider, session_id, ephemeral, attachments, triedProviders)
const args = (preferred) => ['hello', 'req-1', preferred, null, false, null, []];

// --- Scenarios ------------------------------------------------------------------
async function main() {
  const failures = [];
  const ok = (name) => console.log(`  PASS  ${name}`);
  async function test(name, fn) {
    try {
      await fn();
      ok(name);
    } catch (e) {
      failures.push({ name, e });
      console.log(`  FAIL  ${name}\n        ${e.message}`);
    }
  }

  const PIN_ERR = /pinned provider grok failed; failover suppressed/;

  // (1a) PINNED + success:false  -> site 2 (else branch). Fail cleanly, only grok touched.
  await test('pinned grok returns success:false -> fails, no other provider gets payload', async () => {
    const { result, sentTo } = await runScenario({ grok: { send: 'fail' } }, args('grok'));
    assert.strictEqual(result.success, false, 'expected failure');
    assert.match(result.error, PIN_ERR, `error was: ${result.error}`);
    assert.deepStrictEqual(sentTo, ['grok'], `only grok may receive payload, got: ${sentTo}`);
  });

  // (1b) PINNED + sendMessage throws -> site 1 (inner catch). Fail cleanly, only grok touched.
  await test('pinned grok throws in content script -> fails, no failover', async () => {
    const { result, sentTo } = await runScenario({ grok: { send: 'throw' } }, args('grok'));
    assert.strictEqual(result.success, false);
    assert.match(result.error, PIN_ERR, `error was: ${result.error}`);
    assert.deepStrictEqual(sentTo, ['grok'], `payload went to: ${sentTo}`);
  });

  // (1c) PINNED + tab open throws -> site 3 (outer catch). Fail cleanly, only grok tab opened.
  await test('pinned grok fails to open tab -> fails, no other provider tab opened', async () => {
    const { result, sentTo, tabOpens } = await runScenario({ grok: { tab: 'throw' } }, args('grok'));
    assert.strictEqual(result.success, false);
    assert.match(result.error, PIN_ERR, `error was: ${result.error}`);
    assert.deepStrictEqual(sentTo, [], `no provider should have received payload, got: ${sentTo}`);
    assert.deepStrictEqual(tabOpens, ['grok'], `only grok tab may open, got: ${tabOpens}`);
  });

  // (1d) PINNED provider that succeeds still returns that provider (pin doesn't break the happy path).
  await test('pinned grok succeeds -> returns grok result', async () => {
    const { result, sentTo } = await runScenario({ grok: { send: 'ok' } }, args('grok'));
    assert.strictEqual(result.success, true);
    assert.strictEqual(result.provider, 'grok');
    assert.deepStrictEqual(sentTo, ['grok']);
  });

  // (2a) UNPINNED, all fail -> full chain still walked in default order, then clean failure.
  await test('unpinned, all fail -> failover walks chatgpt->grok->claude->deepseek', async () => {
    const { result, sentTo } = await runScenario(
      { chatgpt: { send: 'fail' }, grok: { send: 'fail' }, claude: { send: 'fail' }, deepseek: { send: 'fail' } },
      args(null)
    );
    assert.strictEqual(result.success, false);
    assert.deepStrictEqual(sentTo, DEFAULT_ORDER, `chain order was: ${sentTo}`);
    assert.match(result.error, /All providers failed or unavailable/, `error: ${result.error}`);
  });

  // (2b) UNPINNED, chatgpt fails then grok succeeds -> failover still reaches a working provider.
  await test('unpinned, chatgpt fails then grok succeeds -> failover recovers', async () => {
    const { result, sentTo } = await runScenario(
      { chatgpt: { send: 'fail' }, grok: { send: 'ok' } },
      args(null)
    );
    assert.strictEqual(result.success, true);
    assert.strictEqual(result.provider, 'grok');
    assert.deepStrictEqual(sentTo, ['chatgpt', 'grok'], `chain was: ${sentTo}`);
  });

  console.log('');
  if (failures.length) {
    console.log(`${failures.length} test(s) FAILED`);
    process.exit(1);
  }
  console.log('all tests passed');
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
