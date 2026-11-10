// Journey: the chat's Terminal side panel header at desktop and phone widths
// (#1309). At 390 px the tab strip shared one row with +, search, copy, clear,
// stop, Watch command and the panel's close button, and was the only item that
// could shrink: it ended up 12 px wide with the tab and its close button off
// the panel's left edge. Each step checks the header's real bounds: the
// selected tab's name and close button inside the strip and the panel, every
// header control inside the panel, nothing wider than the panel; a phone puts
// the tabs on a row of their own, the desktop panel keeps one row.
//
// The dev instance refuses terminals to the preview cookie by design, so the
// terminal routes and sockets are answered in the page (lib/fake-terminals.mjs).
// The panel, its stores and xterm are the real ones.
//
// Cases:
//   header   (powershell shells) open the panel, open one terminal, then two more, switch tabs,
//            search, copy, clear, run and stop a command, close a terminal
//            from its tab, Watch command and back, close the panel
//   resize   seven terminals, the last selected, then 390 -> 360 -> 500 -> 390:
//            the selected tab and its close button stay in the strip (#1394)
// In both, the selected tab's name never runs under its close button.
import { DEFAULT_VIEWPORTS, runJourney } from './runner.mjs';
import { composerControl, expect, gotoApp, seededChatId, waitChatOpen } from './lib/app.mjs';
import { fakeTerminals } from './lib/fake-terminals.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

/** Bounds of the header's parts, in CSS pixels. */
async function headerGeometry(page) {
	return page.evaluate(() => {
		const panel = document.querySelector('aside[aria-label="Session side panel"] .tpanel');
		const box = (el) => {
			if (!el) return null;
			const r = el.getBoundingClientRect();
			return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height };
		};
		const bar = panel?.querySelector('.bar');
		const tabs = bar?.querySelector('.tabs');
		const current = tabs?.querySelector('.tab.current');
		const name = (button) => button.getAttribute('aria-label') || button.textContent.trim();
		return {
			panel: box(panel),
			panelOverflow: panel ? panel.scrollWidth - panel.clientWidth : null,
			bar: box(bar),
			barOverflow: bar ? bar.scrollWidth - bar.clientWidth : null,
			tabs: box(tabs),
			tabCount: tabs ? tabs.querySelectorAll('.tab').length : 0,
			current: box(current),
			currentName: box(current?.querySelector('.tab-host') ?? current?.querySelector('.tab-shell')),
			currentNameCut: (() => { const el = current?.querySelector('.tab-host') ?? current?.querySelector('.tab-shell'); return el ? el.scrollWidth - el.clientWidth : null; })(),
			currentClose: box(current?.querySelector('.tab-close')),
			tools: box(bar?.querySelector('.tools')),
			controls: bar ? [...bar.querySelectorAll('button:not([role="tab"]):not(.tab-close)')]
				.filter((b) => !b.closest('.popover')).map((b) => ({ name: name(b), ...box(b) })) : []
		};
	});
}

/** The header fits the panel and the selected tab can be read and closed. */
async function expectUsableHeader(page, variant, what) {
	const g = await headerGeometry(page);
	const where = `${what} at ${variant.viewport}`;
	const inside = (inner, outer) => inner.left >= outer.left - 1 && inner.right <= outer.right + 1;
	expect(g.panel && g.bar, `${where}: the Terminal panel and its header are on screen`);
	expect(g.barOverflow <= 1, `${where}: the header is no wider than the panel (overflow ${g.barOverflow}px)`);
	expect(g.panelOverflow <= 1, `${where}: nothing in the panel is wider than it (overflow ${g.panelOverflow}px)`);
	for (const control of g.controls) {
		expect(inside(control, g.panel), `${where}: "${control.name}" is inside the panel (${JSON.stringify(control)} vs ${JSON.stringify(g.panel)})`);
	}
	expect(g.tabs && g.current, `${where}: the tab strip shows the selected tab (${JSON.stringify(g)})`);
	// The bug: a 12 px strip around a 76-112 px tab.
	expect(g.tabs.width >= g.current.width, `${where}: the strip (${g.tabs.width}px) is at least as wide as the selected tab (${g.current.width}px)`);
	expect(inside(g.currentName, g.tabs) && inside(g.currentName, g.panel), `${where}: the selected tab's name is in view (${JSON.stringify(g.currentName)}, strip ${JSON.stringify(g.tabs)})`);
	expect(inside(g.currentClose, g.tabs) && inside(g.currentClose, g.panel), `${where}: the selected tab's close button is in view (${JSON.stringify(g.currentClose)}, strip ${JSON.stringify(g.tabs)})`);
	expectNameClearOfClose(g, where);
	const phone = variant.viewport !== 'desktop';
	if (phone) {
		// Touch-size targets: the terminal's tools and + are 40 px on a phone, a tab's close 28 px.
		for (const control of g.controls.filter((c) => /^(New terminal|Search output|Copy output|Clear|Stop)$/.test(c.name))) {
			expect(control.width >= 40 && control.height >= 40, `${where}: "${control.name}" keeps a touch-size target (${control.width}x${control.height})`);
		}
		// A phone sizes each tab to its name, so even "powershell" reads in full.
		expect(g.currentNameCut <= 0, `${where}: the selected tab's name is not cut off (${g.currentNameCut}px hidden)`);
		expect(g.currentClose.width >= 28 && g.currentClose.height >= 28, `${where}: the tab's close button keeps its touch size (${g.currentClose.width}x${g.currentClose.height})`);
		expect(g.tabs.top >= g.tools.bottom - 1, `${where}: the tabs have a row of their own under the tools (tabs ${JSON.stringify(g.tabs)}, tools ${JSON.stringify(g.tools)})`);
	} else {
		expect(g.tabs.top < g.tools.bottom && g.tools.top < g.tabs.bottom, `${where}: the default-width panel keeps tabs and tools on one row (tabs ${JSON.stringify(g.tabs)}, tools ${JSON.stringify(g.tools)})`);
	}
	return g;
}

/** The shell or host name ends before the close button: "powershell" ran under it in a 96 px tab. */
function expectNameClearOfClose(g, where) {
	expect(g.currentName.right <= g.currentClose.left + 1, `${where}: the selected tab's name (${JSON.stringify(g.currentName)}) does not run under its close button (${JSON.stringify(g.currentClose)})`);
}

/** The selected tab's name and close button sit inside the strip (#1394), whatever the header's layout. */
async function expectSelectedInView(page, what) {
	const g = await headerGeometry(page);
	const inside = (inner, outer) => inner && outer && inner.left >= outer.left - 1 && inner.right <= outer.right + 1;
	expect(g.panelOverflow <= 1 && g.barOverflow <= 1, `${what}: nothing is wider than the panel (panel ${g.panelOverflow}px, header ${g.barOverflow}px)`);
	expect(inside(g.currentName, g.tabs), `${what}: the selected tab's name is in view (${JSON.stringify(g.currentName)}, strip ${JSON.stringify(g.tabs)})`);
	expect(inside(g.currentClose, g.tabs) && inside(g.currentClose, g.panel), `${what}: the selected tab's close button is in view (${JSON.stringify(g.currentClose)}, strip ${JSON.stringify(g.tabs)})`);
	expectNameClearOfClose(g, what);
	return g;
}

const tabCount = (page, count) =>
	page.waitForFunction((n) => document.querySelectorAll('aside[aria-label="Session side panel"] .tabs .tab').length === n, count, { timeout: 10_000 });

await runJourney({
	name: 'terminal-tabs',
	viewports: [...DEFAULT_VIEWPORTS, 'fold-cover'],
	browser: () => ({ context: { permissions: ['clipboard-read', 'clipboard-write'] } }),
	cases: [
		{
			name: 'header',
			async setup(ctx) {
				const { page, status } = ctx;
				// The longest local shell name (Windows without PowerShell 7).
				await page.addInitScript(fakeTerminals, { defaultShell: 'powershell' });
				await gotoApp(page, status, `/?session=${encodeURIComponent(seededChatId(status, 'worktree'))}`);
				await waitChatOpen(page, { messagesAtLeast: 2, hold: 1000 });
			},
			async act({ page, mark, variant, log }) {
				const side = page.getByRole('complementary', { name: 'Session side panel' });
				const panel = side.getByRole('region', { name: 'Terminal' });
				const tabs = panel.getByRole('tab');
				// Phones tap (touch events from a touch-enabled context); the desktop clicks.
				const press = (locator) => (variant.viewport === 'desktop' ? locator.click() : locator.tap());

				await mark('open the Terminal panel');
				await (await composerControl(page, 'Toggle terminal')).click();
				await panel.waitFor({ timeout: 15_000 });

				await mark('open a terminal');
				await press(panel.getByRole('button', { name: 'Open terminal', exact: true }));
				await tabCount(page, 1);
				await panel.getByRole('button', { name: 'Search output' }).waitFor();
				await page.waitForTimeout(600);
				log(`one terminal: ${JSON.stringify((await expectUsableHeader(page, variant, 'one terminal')).tabs)}`);

				for (const count of [2, 3]) {
					await mark(`open terminal ${count} from +`);
					await press(panel.getByRole('button', { name: 'New terminal', exact: true }));
					await press(panel.locator('.popover').getByRole('button', { name: 'Open terminal', exact: true }));
					await tabCount(page, count);
					await page.waitForTimeout(400);
				}
				log(`three terminals: ${JSON.stringify((await expectUsableHeader(page, variant, 'three terminals')).tabs)}`);

				await mark('switch to the first terminal');
				await press(tabs.first());
				await page.waitForFunction(() => document.querySelector('aside[aria-label="Session side panel"] [role="tab"]')?.getAttribute('aria-selected') === 'true', null, { timeout: 5000 });
				await page.waitForTimeout(300);
				await expectUsableHeader(page, variant, 'after switching tabs');

				await mark('search the output');
				await press(panel.getByRole('button', { name: 'Search output' }));
				const find = panel.getByRole('textbox', { name: 'Find in output' });
				await find.waitFor();
				await find.fill('bash');
				await press(panel.getByRole('button', { name: 'Close search' }));
				await find.waitFor({ state: 'detached', timeout: 5000 });

				await mark('copy and clear the output');
				await press(panel.getByRole('button', { name: 'Copy output' }));
				await page.waitForTimeout(300);
				await press(panel.getByRole('button', { name: 'Clear' }));

				await mark('run a command and stop it');
				const stop = panel.getByRole('button', { name: 'Stop', exact: true });
				await panel.getByRole('textbox', { name: 'Terminal input' }).focus();
				await page.keyboard.type('sleep 30');
				await page.keyboard.press('Enter');
				await page.waitForFunction(() => {
					const b = [...document.querySelectorAll('aside[aria-label="Session side panel"] .tools button')].find((x) => x.getAttribute('aria-label') === 'Stop');
					return b && !b.disabled;
				}, null, { timeout: 5000 });
				await press(stop);
				await page.waitForFunction(() => {
					const b = [...document.querySelectorAll('aside[aria-label="Session side panel"] .tools button')].find((x) => x.getAttribute('aria-label') === 'Stop');
					return b?.disabled;
				}, null, { timeout: 5000 });

				await mark('close the selected terminal from its tab');
				await press(panel.getByRole('button', { name: 'Close terminal 1', exact: true }));
				await tabCount(page, 2);
				await page.waitForTimeout(400);
				await expectUsableHeader(page, variant, 'after closing a terminal');

				await mark('Watch command and back');
				await press(panel.getByRole('button', { name: 'Watch command' }));
				await panel.getByRole('button', { name: 'Back to shells' }).waitFor();
				await tabCount(page, 0);
				await press(panel.getByRole('button', { name: 'Back to shells' }));
				await tabCount(page, 2);
				await page.waitForTimeout(300);
				await expectUsableHeader(page, variant, 'back from Watch command');

				await mark('close the panel');
				await press(side.getByRole('button', { name: 'Close side panel' }));
				await side.waitFor({ state: 'detached', timeout: 5000 });
			}
		},
		{
			// #1394: the strip scrolled the selected tab into view only when the
			// selection changed, so a narrower strip (a phone rotating, the header
			// going from two rows to one) left the tab cut off or out of view.
			name: 'resize',
			viewports: ['phone'],
			async setup(ctx) {
				const { page, status } = ctx;
				await page.addInitScript(fakeTerminals);
				await gotoApp(page, status, `/?session=${encodeURIComponent(seededChatId(status, 'worktree'))}`);
				await waitChatOpen(page, { messagesAtLeast: 2, hold: 1000 });
			},
			async act({ page, mark, log }) {
				const side = page.getByRole('complementary', { name: 'Session side panel' });
				const panel = side.getByRole('region', { name: 'Terminal' });

				await mark('open seven terminals');
				await (await composerControl(page, 'Toggle terminal')).click();
				await panel.waitFor({ timeout: 15_000 });
				await panel.getByRole('button', { name: 'Open terminal', exact: true }).tap();
				await tabCount(page, 1);
				for (let count = 2; count <= 7; count++) {
					await panel.getByRole('button', { name: 'New terminal', exact: true }).tap();
					await panel.locator('.popover').getByRole('button', { name: 'Open terminal', exact: true }).tap();
					await tabCount(page, count);
				}
				await page.waitForFunction(() => document.querySelector('aside[aria-label="Session side panel"] .tab:last-child [role="tab"]')?.getAttribute('aria-selected') === 'true', null, { timeout: 5000 });
				await page.waitForTimeout(400);
				log(`390: ${JSON.stringify((await expectSelectedInView(page, 'seven terminals at 390')).currentClose)}`);

				for (const width of [360, 500, 390]) {
					await mark(`resize to ${width}`);
					await page.setViewportSize({ width, height: 844 });
					await page.waitForTimeout(500);
					const g = await expectSelectedInView(page, `seven terminals, last selected, resized to ${width}`);
					log(`${width}: strip ${JSON.stringify(g.tabs)}, close ${JSON.stringify(g.currentClose)}`);
				}
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
