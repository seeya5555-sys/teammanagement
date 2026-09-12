/* Isolated DOM interaction smoke: no server, login, production data or writes.
 * PLAYWRIGHT_MODULE=/path/to/playwright-core node tests/uiux_browser_smoke.cjs
 * TRMT_BROWSER_PATH can select a locally installed Chromium browser.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.TRMT_BROWSER_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  });
  try {
    const page = await browser.newPage();
    const requests = [];
    await page.route('**/*', route => { requests.push(route.request().url()); return route.abort(); });
    for (const remark of ['', 'Inspection follow-up\nSecond line']) {
      await page.setContent(`<table><tbody id="rows"></tbody></table>
        <div id="cs-survey-modal" hidden><h2 id="cs-modal-title"></h2>
        <input id="cs-f-vendor"><input id="cs-f-mgmt"><input id="cs-f-date">
        <textarea id="cs-f-remark"></textarea></div>`);
      // CS starts its network bootstrap in an IIFE. The unchanged renderer/editor
      // definitions before that boundary run against synthetic data only.
      const source = read('static/js/cs.js').split('(async function init() {')[0];
      await page.addScriptTag({ content: source });
      await page.evaluate(remark => {
        document.querySelector('#rows').append(detailRow({
          id: 71, quarter: 3, vendor: 'Fixture', overall_remark: remark, findings: [],
        }, 'Fixture vessel', 19));
      }, remark);
      const button = page.locator('.summary-edit-button');
      assert.match(await button.getAttribute('role'), /^button$/);
      assert.match(await button.textContent(), remark || '메모 추가');
      await button.focus();
      await page.keyboard.press('Enter');
      assert.equal(await page.locator('#cs-survey-modal').isVisible(), true);
      assert.equal(await page.locator('#cs-f-remark').inputValue(), remark);
      assert.deepEqual(await page.evaluate(() => [_modalCtx.vesselId, _modalCtx.quarter, _modalCtx.surveyId]), [19, 3, 71]);
      await page.evaluate(() => closeSurveyModal());
      await button.evaluate(node => {
        const range = document.createRange(); range.selectNodeContents(node);
        const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
        node.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
      });
      assert.equal(await page.locator('#cs-survey-modal').isVisible(), false, 'copy selection must not open the editor');
      await page.evaluate(() => window.getSelection().removeAllRanges());
      await button.click();
      assert.equal(await page.locator('#cs-survey-modal').isVisible(), true);
      // A new document resets global const declarations for the next fixture.
      await page.goto('about:blank');
    }

    for (const remark of ['', 'Vetting follow-up']) {
      await page.setContent(`<table><tbody id="rows"></tbody></table>
        <div id="vt-remark-modal" hidden><div id="vt-remark-subtitle"></div>
        <textarea id="vt-remark-textarea"></textarea></div>`);
      await page.addScriptTag({ content: read('static/js/vt.js') });
      await page.evaluate(remark => document.querySelector('#rows').append(detailRow({
        id: 83, overall_remark: remark, findings: [],
      })), remark);
      const button = page.locator('.summary-edit-button');
      assert.match(await button.getAttribute('role'), /^button$/);
      assert.match(await button.textContent(), remark || '메모 추가');
      await button.focus();
      await page.keyboard.press('Space');
      assert.equal(await page.locator('#vt-remark-modal').isVisible(), true);
      assert.equal(await page.locator('#vt-remark-textarea').inputValue(), remark);
      assert.equal(await page.evaluate(() => _vtRemarkVetting.id), 83);
      await page.evaluate(() => closeRemarkModal());
      await button.evaluate(node => {
        const range = document.createRange(); range.selectNodeContents(node);
        const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
        node.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
      });
      assert.equal(await page.locator('#vt-remark-modal').isVisible(), false, 'copy selection must not open the editor');
      await page.evaluate(() => window.getSelection().removeAllRanges());
      await button.click();
      assert.equal(await page.locator('#vt-remark-modal').isVisible(), true);
      await page.goto('about:blank');
    }

    await page.setContent(`<header class="topnav">
      <div class="nav-group"><button class="nav-trigger">Survey</button>
        <div class="nav-submenu"><a href="#class">Class</a><a href="#vetting">Vetting</a></div></div>
      <div class="nav-group"><button class="nav-trigger">Report</button>
        <div class="nav-submenu"><a href="#dock">Dock</a></div></div>
      </header><button id="outside">Outside</button>`);
    await page.addStyleTag({ content: read('static/css/main.css') });
    const scripts = [...read('templates/base.html').matchAll(/<script>([\s\S]*?)<\/script>/g)];
    const navScript = scripts.find(match => match[1].includes("const groups = Array.from(document.querySelectorAll('.nav-group'))"));
    assert.ok(navScript, 'navigation interaction script must exist');
    await page.addScriptTag({ content: navScript[1] });
    const survey = page.getByRole('button', { name: 'Survey', exact: true });
    await survey.focus();
    await page.keyboard.press('ArrowDown');
    assert.equal(await survey.getAttribute('aria-expanded'), 'true');
    assert.equal(await page.evaluate(() => document.activeElement.textContent), 'Class');
    await page.keyboard.press('Escape');
    assert.equal(await survey.getAttribute('aria-expanded'), 'false');
    assert.equal(await page.evaluate(() => document.activeElement.textContent), 'Survey');
    await survey.click();
    await page.getByRole('button', { name: 'Report', exact: true }).click();
    assert.equal(await survey.getAttribute('aria-expanded'), 'false');
    await page.locator('#outside').focus();
    assert.equal(await page.locator('.nav-group.open').count(), 0);
    assert.deepEqual(requests, [], 'opening an editor/menu must never send a request');
    console.log('PASS: CS/Vetting blank+populated mouse/keyboard editors and copy selection; nav keyboard/focus/state; zero requests');
  } finally {
    // Some macOS Chrome builds leave an exited browser child unreaped. Bound
    // graceful shutdown so a passing read-only smoke cannot hang the runner.
    const closeTimeout = setTimeout(() => {
      console.error('Browser shutdown timed out after assertions completed');
      process.exit(2);
    }, 10000);
    await browser.close();
    clearTimeout(closeTimeout);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
