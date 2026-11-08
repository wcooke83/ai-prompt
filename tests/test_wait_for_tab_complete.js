#!/usr/bin/env node
/*
 * Regression test: a provider tab that never reports 'complete' must not hang a prompt.
 *
 * ChatGPT sometimes leaves one request open, so Firefox shows the tab as loading forever
 * and tabs.onUpdated never sends status 'complete'. background.js used to wait for it with
 * no limit. Like test_pinned_provider_failover.js, this pulls the REAL waitForTabComplete
 * source out of background.js and runs it in a vm sandbox with a stubbed browserAPI.
 *
 * Run: node tests/test_wait_for_tab_complete.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const BG = path.join(__dirname, '..', 'ai-prompt-extension', 'background.js');

function extractFunction(src, name) {
  const startMarker = `async function ${name}`;
  const start = src.indexOf(startMarker);
  if (start < 0) throw new Error(`could not find ${startMarker}`);
  const rest = src.slice(start);
  const endRel = rest.indexOf('\n}\n');
  if (endRel < 0) throw new Error('could not find end of function');
  return rest.slice(0, endRel + 3);
}

const fnSrc = extractFunction(fs.readFileSync(BG, 'utf8'), 'waitForTabComplete');

function sandbox(tabStatus) {
  const listeners = new Set();
  const ctx = {
    console: { log() {} },
    setTimeout,
    clearTimeout,
    Promise,
    TAB_LOAD_MAX_MS: 20000,
    browserAPI: {
      tabs: {
        get: async () => (tabStatus === 'gone' ? Promise.reject(new Error('no tab')) : { status: tabStatus }),
        onUpdated: {
          addListener: l => listeners.add(l),
          removeListener: l => listeners.delete(l),
        },
      },
    },
  };
  vm.createContext(ctx);
  vm.runInContext(fnSrc, ctx);
  return { ctx, listeners };
}

function settled(promise, ms) {
  return Promise.race([
    promise.then(() => true),
    new Promise(r => setTimeout(() => r(false), ms)),
  ]);
}

(async () => {
  // 1. Tab stuck "loading" forever: resolves once maxMs passes, and unhooks its listener.
  {
    const { ctx, listeners } = sandbox('loading');
    const ok = await settled(ctx.waitForTabComplete(7, 50), 1000);
    assert.strictEqual(ok, true, 'stuck tab must not hang past maxMs');
    assert.strictEqual(listeners.size, 0, 'listener removed after timeout');
  }

  // 2. 'complete' arrives via onUpdated: resolves before the cap.
  {
    const { ctx, listeners } = sandbox('loading');
    const p = ctx.waitForTabComplete(7, 5000);
    await new Promise(r => setTimeout(r, 10));
    for (const l of [...listeners]) l(99, { status: 'complete' }); // other tab: ignored
    assert.strictEqual(await settled(p, 50), false, 'another tab completing does not count');
    for (const l of [...listeners]) l(7, { status: 'complete' });
    assert.strictEqual(await settled(p, 200), true, 'resolves on its own tab completing');
    assert.strictEqual(listeners.size, 0, 'listener removed after complete');
  }

  // 3. Tab already complete before the listener was added: resolves immediately.
  {
    const { ctx } = sandbox('complete');
    assert.strictEqual(await settled(ctx.waitForTabComplete(7, 5000), 200), true,
      'already-complete tab resolves without waiting for the cap');
  }

  // 4. Tab closed meanwhile (tabs.get rejects): still resolves at the cap, no throw.
  {
    const { ctx } = sandbox('gone');
    assert.strictEqual(await settled(ctx.waitForTabComplete(7, 50), 1000), true,
      'closed tab resolves at the cap');
  }

  console.log('waitForTabComplete: 4 scenarios passed');
})().catch(e => { console.error(e); process.exit(1); });
