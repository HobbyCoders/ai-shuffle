// Journey: clicking an image inside an inline HTML preview. The app opens a
// data: image in its lightbox, but only when the page does not handle the
// click itself. A page that toggles a gallery on click (an onclick handler, or
// an addEventListener one that changes the document) must keep the click: the
// lightbox opening on top hid the page's change, and closing it showed the
// pair stacked, the bug this guards. A plain image still opens the lightbox,
// also while the page keeps changing something else in tasks of its own.
import { unlinkSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { runJourney } from './runner.mjs';
import { expect, openNewChat, typeAndSend, uniq, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';

// Toggles change only an attribute and an outline, never the layout, so the
// frame keeps its height and a click moves nothing.
const page = `<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;padding:12px;font:14px system-ui}
.pair{display:flex;gap:8px;margin-bottom:12px}
.pair img{flex:1;min-width:0;height:80px;outline:3px solid transparent}
.pair[data-picked] img{outline-color:#e11d48}
</style></head><body>
<div class="pair" id="inline" onclick="this.toggleAttribute('data-picked')">
<img src="${PNG}" alt="Inline before"><img src="${PNG}" alt="Inline after"></div>
<div class="pair" id="listener">
<img src="${PNG}" alt="Listener before"><img src="${PNG}" alt="Listener after"></div>
<div class="pair"><img src="${PNG}" alt="Plain image"></div>
<div class="pair" id="busy"><img src="${PNG}" alt="Busy page image"></div><p id="ticker">0</p>
<script>document.getElementById('listener').addEventListener('click',function(){this.toggleAttribute('data-picked');});
// Something else on the page keeps changing in tasks of its own (a clock, a
// progress counter): from a press on the last image for a moment, one update per task, so
// the click lands while it runs.
var ch=new MessageChannel(),until=0,n=0;ch.port1.onmessage=function(){document.getElementById('ticker').textContent=String(++n);if(Date.now()<until)ch.port2.postMessage(0);};
document.getElementById('busy').addEventListener('pointerdown',function(){var run=Date.now()<until;until=Date.now()+600;if(!run)ch.port2.postMessage(0);});</script>
</body></html>`;

const written = [];

await runJourney({
	name: 'html-preview-image-click',
	cases: [
		{
			name: 'page-handled-and-plain',
			async setup(ctx) {
				const name = `image-click-${uniq()}.html`;
				const file = join(ctx.status.workspace_dir, name);
				writeFileSync(file, page);
				written.push(file);
				await openNewChat(ctx);
				await typeAndSend(ctx.page, `[gallery](/api/files/by-path?path=${encodeURI(file)})`);
				await waitTurnSettled(ctx.page, { text: 'You said' });
				ctx.frameTitle = `HTML preview: ${name}`;
				await ctx.page.locator(`iframe[title="${ctx.frameTitle}"]`).waitFor({ timeout: 30_000 });
				// Count every time the lightbox appears, including one that opens and
				// closes between two polls.
				await ctx.page.evaluate(() => {
					window.__lightboxOpens = 0;
					const seen = new WeakSet();
					new MutationObserver(() => {
						for (const el of document.querySelectorAll('[role="dialog"][aria-label="Media preview"]')) {
							if (!seen.has(el)) { seen.add(el); window.__lightboxOpens += 1; }
						}
					}).observe(document.body, { childList: true, subtree: true });
				});
			},
			async act(ctx) {
				const { page, mark } = ctx;
				const frame = page.frameLocator(`iframe[title="${ctx.frameTitle}"]`);
				const picked = (id) => frame.locator(`#${id}`).evaluate((el) => el.hasAttribute('data-picked'));
				const opens = () => page.evaluate(() => window.__lightboxOpens);
				const lightbox = page.getByRole('dialog', { name: 'Media preview' });

				for (const id of ['inline', 'listener']) {
					const label = id === 'inline' ? 'Inline' : 'Listener';
					await mark(`click a gallery image the page handles (${id})`);
					await frame.getByRole('img', { name: `${label} before` }).click();
					await page.waitForTimeout(800);
					expect(await picked(id), `the page's own click handler ran (${id})`);
					expect((await opens()) === 0, `no lightbox over a click the page handled (${id})`);

					await mark(`click it again to toggle back (${id})`);
					await frame.getByRole('img', { name: `${label} after` }).click();
					await page.waitForTimeout(800);
					expect(!(await picked(id)), `the second click toggled the page back (${id})`);
					expect((await opens()) === 0, `still no lightbox (${id})`);
				}

				await mark('click a plain image');
				await frame.getByRole('img', { name: 'Plain image' }).click();
				await lightbox.waitFor({ timeout: 10_000 });
				expect((await opens()) === 1, 'a plain data: image opens the lightbox once');

				await mark('close the lightbox');
				await page.keyboard.press('Escape');
				await lightbox.waitFor({ state: 'detached', timeout: 10_000 });
				await page.waitForTimeout(800);

				// Changes the page makes in later tasks are not a reply to the click.
				await mark('click a plain image while the page updates something else');
				const ticks = await frame.locator('#ticker').textContent();
				await frame.getByRole('img', { name: 'Busy page image' }).click();
				await lightbox.waitFor({ timeout: 10_000 });
				expect((await frame.locator('#ticker').textContent()) !== ticks, 'the page was updating while the click landed');
				expect((await opens()) === 2, 'the lightbox opens despite the unrelated updates');

				await mark('close the lightbox again');
				await page.keyboard.press('Escape');
				await lightbox.waitFor({ state: 'detached', timeout: 10_000 });
				await page.waitForTimeout(800);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});

for (const file of written) try { unlinkSync(file); } catch { /* already gone */ }
