const { chromium } = require('playwright');
const path = require('path');

// Use a persistent context to maintain login state
const userDataDir = path.join(process.env.HOME, '.playwright-chatgpt-profile');

(async () => {
  console.log('Launching browser with persistent profile at:', userDataDir);
  console.log('If not logged in, please log in manually and then re-run this script.\n');

  const context = await chromium.launchPersistentContext(userDataDir, {
    headless: false,
    viewport: { width: 1280, height: 900 },
  });

  const page = context.pages()[0] || await context.newPage();

  console.log('Navigating to ChatGPT conversation...');
  await page.goto('https://chatgpt.com/c/69600126-5378-8320-a8fd-0ee55ef1185b', { waitUntil: 'domcontentloaded' });

  // Wait for page to load
  console.log('Waiting for page to stabilize...');
  await page.waitForTimeout(5000);

  // Check if we need to login
  const loginButton = await page.$('button:has-text("Log in"), a:has-text("Log in")');
  if (loginButton) {
    console.log('\n*** LOGIN REQUIRED ***');
    console.log('Please log in to ChatGPT in the browser window.');
    console.log('Waiting 60 seconds for login...\n');
    await page.waitForTimeout(60000);
  }

  // Take screenshot
  await page.screenshot({ path: '/home/wcooke/projects/ai-prompt/ai-prompt-extension/screenshot-logged-in.png', fullPage: true });
  console.log('Saved screenshot-logged-in.png');

  // Wait for code blocks to appear
  console.log('\nLooking for code blocks...');
  try {
    await page.waitForSelector('pre', { timeout: 10000 });
    console.log('Found <pre> elements');
  } catch (e) {
    console.log('No <pre> elements found after waiting');
  }

  // Extensive analysis
  const analysis = await page.evaluate(() => {
    const results = {
      preElements: [],
      codeElements: [],
      allButtons: [],
      copyRelatedElements: [],
      codeBlockContainers: [],
    };

    // Find all pre elements
    document.querySelectorAll('pre').forEach((pre, i) => {
      results.preElements.push({
        index: i,
        className: pre.className,
        textContent: pre.textContent.substring(0, 100) + '...',
        parentClasses: pre.parentElement?.className,
        grandparentClasses: pre.parentElement?.parentElement?.className,
      });
    });

    // Find all code elements
    document.querySelectorAll('code').forEach((code, i) => {
      if (i < 10) { // Limit to first 10
        results.codeElements.push({
          index: i,
          className: code.className,
          textContent: code.textContent.substring(0, 50) + '...',
          inPre: code.closest('pre') !== null,
        });
      }
    });

    // Find ALL buttons and check for copy-related ones
    document.querySelectorAll('button').forEach((btn, i) => {
      const text = btn.textContent.trim();
      const aria = btn.getAttribute('aria-label') || '';
      const title = btn.getAttribute('title') || '';
      const dataTestId = btn.getAttribute('data-testid') || '';

      const isCopyRelated =
        text.toLowerCase().includes('copy') ||
        aria.toLowerCase().includes('copy') ||
        title.toLowerCase().includes('copy') ||
        dataTestId.toLowerCase().includes('copy');

      const btnInfo = {
        index: i,
        text: text.substring(0, 50),
        ariaLabel: aria,
        title: title,
        dataTestId: dataTestId,
        className: btn.className,
        outerHTML: btn.outerHTML.substring(0, 500),
        isCopyRelated: isCopyRelated,
      };

      results.allButtons.push(btnInfo);
      if (isCopyRelated) {
        results.copyRelatedElements.push(btnInfo);
      }
    });

    // Look for code block containers with their headers
    document.querySelectorAll('pre').forEach((pre, i) => {
      // Walk up the DOM to find the container with the header/toolbar
      let container = pre;
      for (let j = 0; j < 10 && container.parentElement; j++) {
        container = container.parentElement;
        const buttons = container.querySelectorAll('button');
        if (buttons.length > 0) {
          results.codeBlockContainers.push({
            preIndex: i,
            level: j,
            containerClass: container.className,
            containerTag: container.tagName,
            containerOuterHTML: container.outerHTML.substring(0, 2000),
            buttons: Array.from(buttons).map(b => ({
              text: b.textContent.substring(0, 30),
              aria: b.getAttribute('aria-label'),
              html: b.outerHTML.substring(0, 300),
            })),
          });
          break;
        }
      }
    });

    return results;
  });

  console.log('\n=== ANALYSIS RESULTS ===\n');
  console.log(`Pre elements: ${analysis.preElements.length}`);
  analysis.preElements.forEach(p => console.log(`  [${p.index}] class="${p.className}" parent="${p.parentClasses}"`));

  console.log(`\nCode elements: ${analysis.codeElements.length}`);
  analysis.codeElements.slice(0, 5).forEach(c => console.log(`  [${c.index}] class="${c.className}" inPre=${c.inPre}`));

  console.log(`\nTotal buttons: ${analysis.allButtons.length}`);
  console.log(`Copy-related buttons: ${analysis.copyRelatedElements.length}`);

  if (analysis.copyRelatedElements.length > 0) {
    console.log('\n=== COPY BUTTONS FOUND ===');
    analysis.copyRelatedElements.forEach((btn, i) => {
      console.log(`\n[Copy Button ${i}]`);
      console.log(`  Text: "${btn.text}"`);
      console.log(`  Aria: "${btn.ariaLabel}"`);
      console.log(`  Title: "${btn.title}"`);
      console.log(`  data-testid: "${btn.dataTestId}"`);
      console.log(`  Class: ${btn.className}`);
      console.log(`  HTML: ${btn.outerHTML}`);
    });
  }

  console.log('\n=== CODE BLOCK CONTAINERS ===');
  analysis.codeBlockContainers.forEach((container, i) => {
    console.log(`\n[Container ${i}] for pre[${container.preIndex}]:`);
    console.log(`  Tag: ${container.containerTag}, Class: ${container.containerClass}`);
    console.log(`  Buttons in container:`);
    container.buttons.forEach((btn, j) => {
      console.log(`    [${j}] text="${btn.text}" aria="${btn.aria}"`);
      console.log(`        html: ${btn.html}`);
    });
    console.log(`  Container HTML (truncated): ${container.containerOuterHTML.substring(0, 500)}`);
  });

  // Now try to click any copy button found
  if (analysis.copyRelatedElements.length > 0) {
    console.log('\n=== ATTEMPTING COPY BUTTON CLICK ===');

    // Construct a selector based on what we found
    const firstCopyBtn = analysis.copyRelatedElements[0];
    let selector = null;

    if (firstCopyBtn.ariaLabel) {
      selector = `button[aria-label="${firstCopyBtn.ariaLabel}"]`;
    } else if (firstCopyBtn.dataTestId) {
      selector = `button[data-testid="${firstCopyBtn.dataTestId}"]`;
    } else if (firstCopyBtn.text.includes('Copy')) {
      selector = 'button:has-text("Copy")';
    }

    if (selector) {
      console.log(`Using selector: ${selector}`);
      const btn = await page.$(selector);
      if (btn) {
        // Monitor console for any errors
        page.on('console', msg => console.log('PAGE LOG:', msg.text()));

        // Get text before click
        const textBefore = await btn.textContent();
        console.log(`Button text before: "${textBefore}"`);

        // Try clicking
        await btn.click();
        await page.waitForTimeout(1000);

        // Get text after click
        const textAfter = await btn.textContent();
        console.log(`Button text after: "${textAfter}"`);

        // Take screenshot after click
        await page.screenshot({ path: '/home/wcooke/projects/ai-prompt/ai-prompt-extension/screenshot-after-copy.png' });
        console.log('Saved screenshot-after-copy.png');
      }
    }
  }

  // Examine how the copy functionality works
  console.log('\n=== INVESTIGATING COPY MECHANISM ===');
  const copyMechanism = await page.evaluate(() => {
    // Check if navigator.clipboard is available
    const clipboardAvailable = 'clipboard' in navigator;
    const writeTextAvailable = clipboardAvailable && 'writeText' in navigator.clipboard;

    // Check clipboard permissions
    let permissionStatus = 'unknown';
    if ('permissions' in navigator) {
      // Can't await in evaluate, so just note it exists
      permissionStatus = 'permissions API available';
    }

    // Look for any clipboard-related code in event listeners
    // This is limited - we can't fully inspect event listeners from JS

    return {
      clipboardAvailable,
      writeTextAvailable,
      permissionStatus,
      documentHasFocus: document.hasFocus(),
      isSecureContext: window.isSecureContext,
    };
  });

  console.log('Clipboard API available:', copyMechanism.clipboardAvailable);
  console.log('writeText available:', copyMechanism.writeTextAvailable);
  console.log('Document has focus:', copyMechanism.documentHasFocus);
  console.log('Is secure context:', copyMechanism.isSecureContext);

  console.log('\n=== KEEPING BROWSER OPEN ===');
  console.log('Browser will stay open for 120 seconds for manual inspection.');
  console.log('You can inspect the Copy button in DevTools to see its event listeners.\n');

  await page.waitForTimeout(120000);

  await context.close();
  console.log('Done');
})();
