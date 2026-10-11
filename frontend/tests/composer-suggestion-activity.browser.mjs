// Run against the existing isolated composer fixture; never a production API.
import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '/opt/browser-mcp/node_modules/playwright-core');
const browser = await chromium.launch({
    headless: true, executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium',
    args: ['--no-sandbox']
});
const output = process.env.SCREENSHOT_DIR || '/tmp/composer-suggestion-activity';
await mkdir(output, { recursive: true });
try {
    for (const [width, height] of [[1280, 800], [390, 844]]) {
        const page = await browser.newPage({ viewport: { width, height }, isMobile: width === 390, hasTouch: width === 390 });
        await page.route(url => url.pathname.startsWith('/api/'), route => route.abort());
        await page.goto(process.env.FIXTURE_URL || 'http://127.0.0.1:5197/tests/composer-suggestion.html');
        await page.waitForFunction(() => !!window.suggestionFixture);
        const ghost = page.locator('.inline-suggestion');
        for (const type of ['turn.started', 'message.user']) {
            await page.evaluate(async () => window.suggestionFixture.offer('Initial eligible reply'));
            await ghost.waitFor();
            assert.equal((await ghost.textContent()).trim(), 'Initial eligible reply');
            if (type === 'turn.started') await page.screenshot({ path: `${output}/activity-${width}x${height}-before.png` });
            await page.evaluate(async type => window.suggestionFixture.activity(type, true), type);
            await ghost.waitFor({ state: 'detached' });
            assert.equal(await page.evaluate(() => window.suggestionFixture.suggestions.accept()), '');
            if (type === 'turn.started') await page.screenshot({ path: `${output}/activity-${width}x${height}-cleared.png` });
            await page.evaluate(async () => window.suggestionFixture.offer('Fresh eligible reply'));
            await ghost.waitFor();
            assert.equal((await ghost.textContent()).trim(), 'Fresh eligible reply');
            if (type === 'turn.started') await page.screenshot({ path: `${output}/activity-${width}x${height}-fresh.png` });
            console.log(`${width}x${height}: ${type} clears hint, fresh eligible hint renders`);
        }
        assert.equal(await page.evaluate(() => window.suggestionFixture.sends), 0);
        await page.close();
    }
} finally {
    await browser.close();
}
