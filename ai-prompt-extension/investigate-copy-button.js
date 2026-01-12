const { chromium } = require('playwright');
const fs = require('fs');

(async () => {
  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext();
  const page = await context.newPage();

  console.log('Navigating to ChatGPT conversation...');
  await page.goto('https://chatgpt.com/c/69600126-5378-8320-a8fd-0ee55ef1185b', { waitUntil: 'networkidle' });

  // Wait for page to fully load
  await page.waitForTimeout(5000);

  // Take initial screenshot
  await page.screenshot({ path: '/home/wcooke/projects/ai-prompt/ai-prompt-extension/screenshot-initial.png', fullPage: true });
  console.log('Saved initial screenshot');

  // Look for code blocks and copy buttons
  console.log('\n--- Looking for code blocks and copy buttons ---');

  // Find all pre/code elements
  const codeBlocks = await page.$$('pre');
  console.log(`Found ${codeBlocks.length} <pre> elements`);

  // Look for copy buttons with various selectors
  const copyButtonSelectors = [
    'button:has-text("Copy code")',
    'button:has-text("Copy")',
    '[aria-label*="copy" i]',
    '[data-testid*="copy" i]',
    'button svg', // buttons with icons
  ];

  for (const selector of copyButtonSelectors) {
    try {
      const elements = await page.$$(selector);
      console.log(`Selector "${selector}": found ${elements.length} elements`);
    } catch (e) {
      console.log(`Selector "${selector}": error - ${e.message}`);
    }
  }

  // Get the HTML structure around code blocks
  console.log('\n--- HTML Structure around code blocks ---');
  const codeBlockHtml = await page.evaluate(() => {
    const pres = document.querySelectorAll('pre');
    const results = [];
    pres.forEach((pre, i) => {
      // Get parent container
      let container = pre.parentElement;
      for (let j = 0; j < 3 && container; j++) {
        container = container.parentElement;
      }
      if (container) {
        results.push({
          index: i,
          containerHTML: container.outerHTML.substring(0, 3000),
          containerClasses: container.className,
        });
      }
    });
    return results;
  });

  if (codeBlockHtml.length > 0) {
    console.log('First code block container:');
    console.log(codeBlockHtml[0].containerHTML);
  }

  // Look for buttons near code blocks
  console.log('\n--- Buttons near code blocks ---');
  const buttonsInfo = await page.evaluate(() => {
    const pres = document.querySelectorAll('pre');
    const buttons = [];
    pres.forEach((pre, i) => {
      // Look in parent containers for buttons
      let container = pre.parentElement;
      for (let j = 0; j < 5 && container; j++) {
        container = container.parentElement;
        if (!container) break;
        const btns = container.querySelectorAll('button');
        btns.forEach(btn => {
          buttons.push({
            codeBlockIndex: i,
            parentLevel: j + 1,
            innerHTML: btn.innerHTML.substring(0, 500),
            outerHTML: btn.outerHTML.substring(0, 1000),
            textContent: btn.textContent,
            ariaLabel: btn.getAttribute('aria-label'),
            className: btn.className,
            id: btn.id,
          });
        });
        if (btns.length > 0) break; // Stop at first level with buttons
      }
    });
    return buttons;
  });

  console.log(`Found ${buttonsInfo.length} buttons near code blocks:`);
  buttonsInfo.forEach((btn, i) => {
    console.log(`\nButton ${i}:`);
    console.log(`  Text: "${btn.textContent}"`);
    console.log(`  Aria-label: ${btn.ariaLabel}`);
    console.log(`  Classes: ${btn.className}`);
    console.log(`  OuterHTML: ${btn.outerHTML}`);
  });

  // Try to find and examine event listeners on copy buttons
  console.log('\n--- Examining Copy button event listeners ---');
  const copyBtnDetails = await page.evaluate(() => {
    // Look for copy-related buttons
    const allButtons = document.querySelectorAll('button');
    const copyButtons = [];

    allButtons.forEach(btn => {
      const text = btn.textContent.toLowerCase();
      const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
      if (text.includes('copy') || aria.includes('copy')) {
        // Try to get event listener info (limited in browser)
        copyButtons.push({
          outerHTML: btn.outerHTML,
          textContent: btn.textContent,
          ariaLabel: btn.getAttribute('aria-label'),
          onclick: btn.onclick ? btn.onclick.toString() : null,
          hasClickHandler: btn.onclick !== null,
          // Check for React/framework handlers
          reactProps: Object.keys(btn).filter(k => k.startsWith('__react') || k.startsWith('_reactProps')),
        });
      }
    });

    return copyButtons;
  });

  console.log(`Found ${copyBtnDetails.length} copy-related buttons:`);
  copyBtnDetails.forEach((btn, i) => {
    console.log(`\nCopy Button ${i}:`);
    console.log(`  Text: "${btn.textContent}"`);
    console.log(`  Aria-label: ${btn.ariaLabel}`);
    console.log(`  Has onclick: ${btn.hasClickHandler}`);
    console.log(`  React props: ${btn.reactProps.join(', ')}`);
    console.log(`  HTML: ${btn.outerHTML}`);
  });

  // Scroll to first code block and screenshot
  console.log('\n--- Scrolling to code block ---');
  const firstPre = await page.$('pre');
  if (firstPre) {
    await firstPre.scrollIntoViewIfNeeded();
    await page.waitForTimeout(1000);
    await page.screenshot({ path: '/home/wcooke/projects/ai-prompt/ai-prompt-extension/screenshot-code-block.png' });
    console.log('Saved code block screenshot');
  }

  // Try clicking the copy button
  console.log('\n--- Attempting to click Copy button ---');

  // First, look for any button with "Copy" text
  const copyBtn = await page.$('button:has-text("Copy")');
  if (copyBtn) {
    console.log('Found Copy button, attempting click...');

    // Listen for clipboard events
    await page.evaluate(() => {
      window._clipboardData = null;
      document.addEventListener('copy', (e) => {
        window._clipboardData = e.clipboardData ? e.clipboardData.getData('text/plain') : 'clipboard event fired';
        console.log('Copy event fired!');
      });
    });

    // Try clicking
    try {
      await copyBtn.click();
      await page.waitForTimeout(1000);
      console.log('Click executed');

      // Check if anything changed (like button text)
      const btnTextAfter = await copyBtn.textContent();
      console.log(`Button text after click: "${btnTextAfter}"`);

      // Take screenshot after click
      await page.screenshot({ path: '/home/wcooke/projects/ai-prompt/ai-prompt-extension/screenshot-after-click.png' });

    } catch (e) {
      console.log(`Click failed: ${e.message}`);
    }
  } else {
    console.log('No Copy button found with simple selector');
  }

  // Wait a bit for user to observe if needed
  console.log('\nKeeping browser open for 30 seconds for observation...');
  await page.waitForTimeout(30000);

  await browser.close();
  console.log('Done');
})();
