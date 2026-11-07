// Regression: rail-owned overlays must not live inside the hidden/inert Chats column.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, seededChatId, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
const mobileShell = process.argv.includes('--mobile-shell');

const workspace = page => page.getByRole('navigation', { name: 'Workspace', exact: true });
const menu = page => page.getByRole('menu', { name: 'More destinations', exact: true });
async function expectClosedPhoneSheets(page) {
	// The phone's panel plane ends ABOVE its tabs: a translated sheet can still
	// paint over them unless it owns its closed visibility (not its old parent).
	await page.locator('.sidebar .sheet').evaluateAll(sheets => Promise.all(
		sheets.flatMap(sheet => sheet.getAnimations().map(animation => animation.finished))
	));
	const sheets = await page.locator('.sidebar .sheet[aria-hidden="true"]').evaluateAll(nodes => nodes.map(sheet => {
		const active = document.activeElement;
		sheet.querySelector('button')?.focus();
		const style = getComputedStyle(sheet), rect = sheet.getBoundingClientRect();
		const hit = document.elementFromPoint(Math.min(innerWidth - 1, Math.max(1, rect.left + rect.width / 2)), Math.min(innerHeight - 1, Math.max(1, rect.top + 12)));
		return { label: sheet.getAttribute('aria-label'), native: sheet.classList.contains('phone'), opacity: style.opacity, visibility: style.visibility, pointerEvents: style.pointerEvents,
			inert: sheet.inert, focusBlocked: document.activeElement === active && !sheet.contains(document.activeElement), hitBlocked: !sheet.contains(hit) };
	}));
	expect(sheets.length === 2, 'both closed navigation sheets remain mounted');
	for (const sheet of sheets) {
		// Native sheets translate beyond their plane; desktop popovers fade in place.
		expect(sheet.native ? sheet.visibility === 'hidden' : Number(sheet.opacity) === 0, `${sheet.label} cannot paint while closed`);
		expect(sheet.inert && sheet.pointerEvents === 'none' && sheet.focusBlocked && sheet.hitBlocked, `${sheet.label} cannot take focus or pointer hits while closed`);
	}
}
// Hover a desktop rail button until its label shows, then hit-test a grid over
// the label. The label is pointer-events:none, so it is made hit-testable only
// for the measurement; that does not change what paints on top.
async function railLabelOnTop(page, button) {
	await button.hover();
	const tip = button.locator('.tip');
	await page.waitForFunction(el => getComputedStyle(el).opacity === '1', await tip.elementHandle());
	return tip.evaluate(el => {
		el.style.pointerEvents = 'auto';
		const r = el.getBoundingClientRect();
		const covered = [];
		for (let x = r.left + 3; x < r.right - 2; x += 8) for (const y of [r.top + 3, r.top + r.height / 2, r.bottom - 3]) {
			const hit = document.elementFromPoint(x, y);
			if (!hit || !el.contains(hit)) covered.push(hit?.closest('[class]')?.className.toString().split(' ')[0] ?? null);
		}
		el.style.pointerEvents = '';
		return { text: el.textContent.trim(), box: { left: r.left, top: r.top, right: r.right, bottom: r.bottom }, covered: [...new Set(covered)] };
	});
}
async function openMore(page, native) {
	if (native) {
		await page.getByRole('button', { name: /^Open menu(?: \(\d+ unread notifications\))?$/ }).click();
		await workspace(page).waitFor();
	} else if (!await workspace(page).isVisible()) {
		await showSidebar(page);
	}
	await workspace(page).getByRole('button', { name: 'More', exact: true }).click();
	await menu(page).waitFor({ timeout: 5000 });
}

await runJourney({
	name: 'rail-more',
	allow: [
		...DEV_INSTANCE_NOISE,
		{
			kind: 'failed_requests', status: 401, url: /\/github\/inbox(\?|$)/,
			reason: 'the dev instance has no GitHub sign-in, so the Pull requests inbox cannot load'
		}
	],
	cases: [{
		// G13: the rail's z-index scopes its hover labels, so the rail itself must
		// sit above the list panel's sticky section header and selection bar.
		name: 'rail-labels-above-list-1440',
		viewports: ['desktop'],
		async setup({ page, status }) {
			await page.setViewportSize({ width: 1440, height: 900 });
			await gotoApp(page, status, `/?session=${seededChatId(status, 'long')}`);
			if (!await page.getByRole('complementary', { name: 'Chats', exact: true }).locator('.panel-plane').isVisible()) {
				await page.getByRole('button', { name: 'Show sidebar', exact: true }).click();
			}
			await workspace(page).waitFor();
			await page.locator('aside.sidebar .col .sect').first().waitFor();
		},
		async act({ page, mark, log }) {
			await mark('every rail label paints over the Chats list beside it');
			const header = await page.locator('aside.sidebar .col .sect').first().boundingBox();
			const buttons = workspace(page).locator('.r-btn:has(.tip)');
			let overHeader = 0;
			for (const button of await buttons.all()) {
				const label = await railLabelOnTop(page, button);
				log(JSON.stringify(label));
				if (label.box.top < header.y + header.height && label.box.bottom > header.y) overHeader++;
				expect(label.covered.length === 0, `the "${label.text}" label is not covered by the list (${JSON.stringify(label.covered)})`);
			}
			expect(overHeader > 0, 'at least one label crosses the sticky section header, so the check covers it');
			await mark('with rows selected, the New label paints over the selection bar');
			const row = await page.locator('aside.sidebar .col .scard').first().boundingBox();
			await page.mouse.move(row.x + 40, row.y + row.height / 2);
			await page.mouse.down();
			await page.locator('aside.sidebar .col.selecting').waitFor({ timeout: 3000 });
			await page.mouse.up();
			const selbar = page.locator('aside.sidebar .selbar');
			await selbar.evaluate(el => Promise.all(el.getAnimations().map(a => a.finished)));
			const bar = await selbar.boundingBox();
			const label = await railLabelOnTop(page, workspace(page).locator('.r-new'));
			log(JSON.stringify({ bar, label }));
			expect(label.box.right > bar.x && label.box.top < bar.y + bar.height, 'the New label crosses the selection bar');
			expect(label.covered.length === 0, `the New label is not covered by the selection bar (${JSON.stringify(label.covered)})`);
			await page.getByRole('button', { name: 'Cancel selection', exact: true }).click();
		}
	}, {
		name: 'medium-768-collapsed-rail',
		viewports: ['desktop'],
		async setup({ page, status }) {
			await gotoApp(page, status);
			await page.setViewportSize({ width: 768, height: 900 });
			await workspace(page).waitFor();
		},
		async act({ page, mark }) {
			await mark('More stays reachable beside the collapsed medium-width panel');
			await openMore(page, false);
			await menu(page).getByRole('menuitem', { name: 'Automations', exact: true }).click();
			await page.getByRole('heading', { name: 'Automations', exact: true }).waitFor();
			await page.getByRole('button', { name: 'Hide sidebar', exact: true }).click();
			await openMore(page, false);
			await menu(page).getByRole('menuitem', { name: 'Docs', exact: true }).click();
			await page.getByRole('heading', { name: 'Docs', exact: true }).waitFor();
			expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'medium rail does not overflow');
		}
	}, ...[false, true].map(phone => {
		const native = phone && mobileShell;
		return {
		name: phone ? `${native ? 'native' : 'responsive'}-drawer-after-feature` : 'docked-after-feature',
		viewports: [phone ? 'phone' : 'desktop'],
		async setup({ page, status }) {
			if (native) {
				await page.goto(new URL('/mobile/', status.urls.preview_listener).href);
				expect(new URL(page.url()).pathname === '/mobile/', 'native evidence uses the real phone bundle');
				await openMore(page, true);
				await menu(page).getByRole('menuitem', { name: 'Pull requests', exact: true }).click();
			} else {
				await gotoApp(page, status);
				await showSidebar(page);
				await workspace(page).getByRole('button', { name: 'Pull requests', exact: true }).click();
				// K1 removes the phone panel heading; P1 gives the detail its state title.
				await page.locator('[data-view-shell="Pull requests"]').filter({ visible: true }).waitFor();
				await page.getByRole('heading', { name: 'Open pull requests', exact: true }).waitFor();
			}
		},
		async act({ page, mark }) {
			for (const label of ['Docs', 'Stats']) {
				await mark(`open More from a feature and navigate to ${label}`);
				await openMore(page, native);
				const item = menu(page).getByRole('menuitem', { name: label, exact: true });
				expect(await item.evaluate(el => !el.closest('[inert], [aria-hidden="true"]')), 'rail menu is outside hidden Chats');
				await item.click();
				await menu(page).waitFor({ state: 'hidden' });
			}
			await openMore(page, native);
			expect(await menu(page).getByRole('menuitem', { name: 'Stats', exact: true }).getAttribute('aria-current') === 'page', 'Stats is the active destination');
			await mark('open and close Customize from the active feature');
			await menu(page).getByRole('menuitem', { name: /Customize the rail/ }).click();
			const dialog = page.getByRole('dialog', { name: 'Customize the rail', exact: true });
			await dialog.waitFor();
			await dialog.getByRole('button', { name: 'Done', exact: true }).click();
			await workspace(page).getByRole('button', { name: 'More', exact: true }).click();
			await menu(page).waitFor();
			await mark('Escape closes the active navigation overlay and returns focus to its launcher');
			await page.keyboard.press('Escape');
			await menu(page).waitFor({ state: 'hidden' });
			// The native shell owns Escape for the entire drawer; desktop closes just More.
			const launcher = native ? page.getByRole('button', { name: /^Open menu(?: \(\d+ unread notifications\))?$/ }) : workspace(page).getByRole('button', { name: 'More', exact: true });
			expect(await launcher.evaluate(el => el === document.activeElement), 'focus returns to the navigation launcher');
		}
	};
	}), ...['dark', 'light'].map(theme => ({
		name: `${mobileShell ? 'native' : 'responsive'}-closed-sheets-${theme}`,
		viewports: ['phone'],
		async setup({ page, status }) {
			await page.addInitScript(value => localStorage.setItem('ai-shuffle-theme', value), theme);
			if (mobileShell) {
				await page.goto(new URL('/mobile/', status.urls.preview_listener).href);
				expect(new URL(page.url()).pathname === '/mobile/', 'native evidence uses the real phone bundle');
			} else await gotoApp(page, status);
			if (mobileShell) {
				await openMore(page, true);
				await menu(page).getByRole('menuitem', { name: 'Pull requests', exact: true }).click();
			} else {
				await showSidebar(page);
				await workspace(page).getByRole('button', { name: 'Pull requests', exact: true }).click();
			}
			if (mobileShell) await page.getByRole('button', { name: /^Open menu(?: \(\d+ unread notifications\))?$/ }).click();
			else await showSidebar(page);
			await workspace(page).waitFor();
		},
		async act({ page, mark }) {
			await mark(`closed sheets are hidden and inert in the ${theme} phone drawer`);
			expect(await page.evaluate(value => document.documentElement.classList.contains('dark') === (value === 'dark'), theme), 'requested theme applied');
			await expectClosedPhoneSheets(page);
			await mark('Filter opens and becomes fully hidden again');
			await page.getByRole('button', { name: 'Filter', exact: true }).click();
			await page.getByRole('dialog', { name: 'Filter chats', exact: true }).waitFor();
			if (mobileShell) await page.getByRole('button', { name: 'Close filter chats', exact: true }).click();
			else await page.keyboard.press('Escape');
			await expectClosedPhoneSheets(page);
			await mark('More still opens from a panel-owning destination');
			await openMore(page, false);
			await menu(page).getByRole('menuitem', { name: 'Docs', exact: true }).click();
			if (mobileShell) await page.getByRole('button', { name: /^Open menu(?: \(\d+ unread notifications\))?$/ }).click();
			else await showSidebar(page);
			await workspace(page).waitFor();
			await expectClosedPhoneSheets(page);
			await openMore(page, false);
			if (mobileShell) await page.getByRole('button', { name: 'Close more destinations', exact: true }).click();
			else await page.keyboard.press('Escape');
			await expectClosedPhoneSheets(page);
			expect(await workspace(page).getByRole('button', { name: 'More', exact: true }).evaluate(el => el === document.activeElement), 'closing More restores its launcher');
			await mark('reduced motion closes without an exit transition');
			await page.emulateMedia({ reducedMotion: 'reduce' });
			await openMore(page, false);
			if (mobileShell) await page.getByRole('button', { name: 'Close more destinations', exact: true }).click();
			else await page.keyboard.press('Escape');
			await expectClosedPhoneSheets(page);
			// The shared app stylesheet uses a 0.01ms !important duration rather
			// than zero; both duration AND visibility delay must be imperceptible.
			expect(await page.locator('.sidebar .sheet').evaluateAll(nodes => nodes.every(sheet => {
				const style = getComputedStyle(sheet);
				return [style.transitionDuration, style.transitionDelay].every(value => value.split(',').every(time => parseFloat(time) <= 0.00001));
			})), 'phone sheets respect reduced motion without a delayed hide');
		}
	}))]
});
