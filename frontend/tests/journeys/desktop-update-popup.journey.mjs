// Exercise the real sidebar and dock with synthetic update status. No action
// requests are allowed through, even if a click regresses into an install.
import { runJourney } from './runner.mjs';
import { composerControl, expect, gotoApp, seededChatId, showSidebar, waitChatOpen } from './lib/app.mjs';
import { fakeTerminals } from './lib/fake-terminals.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const primaryStatus = 'This staged update needs an installer that can migrate the application files. Settings and chat history are kept.';
const peerStatus = 'The update is staged but cannot restart until the background indexing service finishes validating its cached results.';
const worker = { supported: true, online: true, allow_updates: true };
const snapshot = {
	allow_updates: true, update_all_workers: true,
	workers: [
		{ ...worker, device_id: 'local', device_name: 'Primary workstation', is_self: true,
			phase: 'error', role: 'unknown', label: primaryStatus, error: primaryStatus, remedy: 'installer', data_kept: true },
		{ ...worker, device_id: 'peer', device_name: 'Background indexing workstation', is_self: false,
			phase: 'ready', role: 'staged', label: peerStatus },
		{ ...worker, device_id: 'offline', device_name: 'Build workstation', is_self: false,
			phase: 'idle', online: false, role: 'excluded', label: 'Offline; not included in this update' }
	]
};

const sidebar = page => page.getByRole('complementary', { name: 'Chats', exact: true });
const update = page => sidebar(page).getByRole('button', { name: 'Update needs the latest installer', exact: true });
const tip = page => page.getByRole('tooltip').filter({ hasText: 'Update needs attention' });

async function dragSidebar(page, targetX) {
	const resize = page.getByRole('separator', { name: 'Resize sidebar' });
	const { x, y, width } = await resize.boundingBox();
	await page.mouse.move(x + width / 2, y + 160);
	await page.mouse.down();
	await page.mouse.move(targetX, y + 160, { steps: 4 });
	await page.mouse.up();
}

async function checkTip(page, label, { collapsed = false } = {}) {
	await tip(page).waitFor();
	// Let focus scrolling and ResizeObserver callbacks settle before sampling the box.
	await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
	const box = await tip(page).evaluate(el => {
		const rect = node => {
			const { left, right, top, bottom } = node.getBoundingClientRect();
			return { left, right, top, bottom };
		};
		const aside = document.querySelector('aside.sidebar');
		return {
			card: rect(el), aside: rect(aside), rail: rect(aside.querySelector('nav')), panel: rect(aside.querySelector('.panel-plane')),
			scrollHeight: el.scrollHeight, clientHeight: el.clientHeight,
			nativeTitle: aside.querySelector('.update-button').hasAttribute('title'),
			viewport: { width: innerWidth, height: innerHeight }, scrollWidth: el.scrollWidth, clientWidth: el.clientWidth
		};
	});
	console.log(`${label}: ${JSON.stringify(box)}`);
	if (collapsed) {
		expect(box.aside.right <= box.rail.right && box.card.left >= box.rail.right && box.card.right <= box.viewport.width,
			`${label}: the rail-only tooltip fits beside the rail`);
	} else {
		expect(box.card.left >= box.panel.left && box.card.right <= box.panel.right,
			`${label}: update tooltip must fit inside the expanded sidebar panel`);
	}
	expect(box.card.top >= Math.max(0, box.aside.top) && box.card.bottom <= Math.min(box.aside.bottom, box.viewport.height),
		`${label}: tooltip fits vertically`);
	expect(box.scrollWidth === box.clientWidth, `${label}: worker statuses wrap without horizontal scrolling`);
	expect(await tip(page).getByText(peerStatus, { exact: true }).isVisible(), 'long per-worker status remains visible');
	expect(await update(page).getAttribute('title') === null, 'no duplicate native tooltip');
	expect(await update(page).getAttribute('aria-describedby') === await tip(page).getAttribute('id'), 'custom tooltip describes the trigger');
}

async function fakeUpdates(page, updateSnapshot = snapshot) {
	// Show the control in the browser only; the server keeps preview restrictions.
	await page.route('**/api/v1/auth/status', async route => {
		const response = await route.fetch();
		await route.fulfill({ response, json: { ...(await response.json()), preview_access: false } });
	});
	await page.route('**/api/v1/settings/application-updates**', route => {
		const req = route.request();
		return req.method() === 'GET' && new URL(req.url()).pathname.endsWith('/workers')
			? route.fulfill({ json: updateSnapshot }) : route.abort();
	});
}

await runJourney({
	name: 'desktop-update-popup',
	allow: DEV_INSTANCE_NOISE,
	browser: async () => ({ context: { serviceWorkers: 'block' } }),
	cases: [{
		name: 'worker-details',
		async setup({ page, status, variant }) {
			if (variant.viewport === 'phone') await page.emulateMedia({ colorScheme: 'dark' });
			await fakeUpdates(page);
			await gotoApp(page, status);
			await showSidebar(page);
			if (await page.getByRole('separator', { name: 'Resize sidebar' }).isVisible()) await dragSidebar(page, 390);
			await update(page).waitFor();
		},
		async act({ page, variant }) {
			await update(page).hover();
			await checkTip(page, 'hover');
			await tip(page).hover();
			await page.mouse.move(0, 0);
			await tip(page).waitFor({ state: 'hidden' });
			await update(page).press('Shift+Tab');
			await page.keyboard.press('Tab');
			expect(await update(page).evaluate(el => el === document.activeElement), 'Tab focuses the update button');
			await checkTip(page, 'focus');
			if (variant.viewport === 'desktop') {
				await dragSidebar(page, 280);
				await checkTip(page, 'focused during sidebar resize');
			}
			await page.keyboard.press('Escape');
			await tip(page).waitFor({ state: 'hidden' });

			if (variant.viewport === 'phone') await update(page).tap();
			else await page.keyboard.press('Enter');
			const dialog = page.getByRole('dialog', { name: 'Update needs the latest installer' });
			await dialog.waitFor();
			const { x, y, width, height } = await dialog.boundingBox();
			const viewport = page.viewportSize();
			console.log(`click dialog: ${JSON.stringify({ x, y, width, height })}`);
			expect(x >= 0 && x + width <= viewport.width && y >= 0 && y + height <= viewport.height,
				'existing dock dialog fits the viewport');
			expect(await dialog.getByRole('link', { name: 'Download the latest installer' }).isVisible(), 'installer action is preserved');
			expect(await update(page).getAttribute('aria-controls') === await dialog.getAttribute('id'), 'trigger controls the dialog');
			await page.keyboard.press('Escape');
			await dialog.waitFor({ state: 'hidden' });
			expect(await update(page).evaluate(el => el === document.activeElement), 'Escape leaves focus on the trigger');
			if (variant.viewport === 'desktop') {
				await dragSidebar(page, 320);
				await sidebar(page).getByRole('button', { name: 'Hide sidebar', exact: true }).click();
				await update(page).hover();
				await checkTip(page, 'rail only', { collapsed: true });
				await page.setViewportSize({ width: 790, height: 360 });
				await page.getByRole('button', { name: 'Show sidebar', exact: true }).click();
				await update(page).focus();
				await checkTip(page, 'expanded rail overlay');
				await page.keyboard.press('PageDown');
				expect(await tip(page).evaluate(el => el.scrollTop > 0), 'long details scroll from the focused trigger');
			}
		}
	}, {
		name: 'escape-layering',
		viewports: ['desktop'],
		async setup({ page, status }) {
			await fakeUpdates(page);
			await page.addInitScript(fakeTerminals, {});
			await gotoApp(page, status, `/?session=${encodeURIComponent(seededChatId(status, 'worktree'))}`);
			await waitChatOpen(page, { messagesAtLeast: 2, hold: 1000 });
			await showSidebar(page);
		},
		async act({ page, mark }) {
			const terminal = page.getByRole('complementary', { name: 'Session side panel' }).getByRole('region', { name: 'Terminal' });
			await mark('open the Terminal panel without starting a shell');
			await (await composerControl(page, 'Toggle terminal')).click();
			await terminal.waitFor();
			await update(page).focus();
			await tip(page).waitFor();
			await mark('first Escape dismisses only the update tooltip');
			await page.keyboard.press('Escape');
			await tip(page).waitFor({ state: 'hidden' });
			// Catch delayed dispatch in the panel's whenEscapeUnclaimed handler.
			await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)));
			expect(await terminal.isVisible(), 'first Escape leaves the Terminal panel open');
			await mark('next Escape dismisses the Terminal panel');
			await page.keyboard.press('Escape');
			await terminal.waitFor({ state: 'hidden' });
		}
	}, {
		name: 'slow-pointer-handoff',
		viewports: ['desktop'],
		async setup({ page, status }) {
			await page.setViewportSize({ width: 900, height: 560 });
			await fakeUpdates(page, { ...snapshot, workers: [
				...snapshot.workers,
				...['Testing workstation', 'Rendering workstation', 'Release workstation'].map((device_name, i) => ({
					...worker, device_id: `extra-${i}`, device_name, phase: 'ready', role: 'staged', label: peerStatus
				}))
			] });
			await gotoApp(page, status);
			await showSidebar(page);
			await dragSidebar(page, 280);
		},
		async act({ page, mark }) {
			const button = update(page);
			await button.hover();
			await tip(page).waitFor();
			expect(await button.evaluate(el => el !== document.activeElement), 'pointer handoff does not rely on keyboard focus');
			await mark('the neighboring Away control takes precedence over the hover corridor');
			await sidebar(page).getByRole('switch', { name: 'Away for all chats', exact: true }).hover();
			await tip(page).waitFor({ state: 'hidden', timeout: 1500 });
			await button.hover();
			await tip(page).waitFor();
			const trigger = await button.boundingBox();
			const card = await tip(page).boundingBox();
			const from = { x: trigger.x + trigger.width - 2, y: trigger.y + trigger.height / 2 };
			// Enter the nearest corner through the gap, not over the chat cards below it.
			const to = { x: card.x + 4, y: card.y + card.height - 30 };
			await page.mouse.move(from.x, from.y);
			await mark('move slowly from the trigger across the gap into the scrollable tooltip');
			let openThroughout = true;
			for (let step = 1; step <= 20; step++) {
				const x = from.x + (to.x - from.x) * step / 20;
				const y = from.y + (to.y - from.y) * step / 20;
				await page.mouse.move(x, y);
				if (step === 10) expect(await page.evaluate(({ x, y }) =>
					!document.elementFromPoint(x, y)?.closest('.update-slot'), { x, y }), 'the gap is not an invisible hit target');
				await page.waitForTimeout(40); // exercise a deliberately slow real pointer
				openThroughout &&= await tip(page).isVisible();
			}
			expect(openThroughout, 'tooltip stays open across the whole slow crossing');
			await page.mouse.wheel(0, 250);
			await page.waitForFunction(() => document.querySelector('.update-tooltip')?.scrollTop > 0);
			await mark('leaving the card away from the trigger dismisses it');
			await page.mouse.move(500, 60);
			await tip(page).waitFor({ state: 'hidden' });
		}
	}, ...[
		{ name: 'rounded-corner-handoff', width: 900, height: 560 },
		{ name: 'rounded-corner-narrow-handoff', width: 390, height: 844 }
	].map(({ name, width, height }) => ({
		name,
		viewports: ['desktop'], // Use a mouse in the web UI at both widths.
		async setup({ page, status }) {
			await page.setViewportSize({ width, height });
			await fakeUpdates(page, { ...snapshot, workers: [
				...snapshot.workers,
				...Array.from({ length: 10 }, (_, i) => ({
					...worker, device_id: `testing-${i}`, device_name: `Testing workstation ${i + 1}`,
					phase: 'ready', role: 'staged', label: peerStatus
				}))
			] });
			await gotoApp(page, status);
			await showSidebar(page);
			await sidebar(page).locator('.scard[role="button"]').first().waitFor({ timeout: 5000 });
		},
		async act({ page, mark }) {
			const button = update(page);
			await button.hover();
			await tip(page).waitFor();
			expect(await button.evaluate(el => el !== document.activeElement), 'the tooltip is held only by hover');
			const trigger = await button.boundingBox();
			const card = await tip(page).boundingBox();
			const from = { x: trigger.x + trigger.width / 2, y: trigger.y + trigger.height / 2 };
			const to = { x: card.x + 12, y: card.y + card.height - 15 };
			await mark('move slowly through the rounded corner over a real chat row');
			let crossedExposedCorner = false;
			let openThroughout = true;
			for (let step = 1; step <= 20; step++) {
				const x = Math.floor(from.x + (to.x - from.x) * step / 20);
				const y = Math.floor(from.y + (to.y - from.y) * step / 20);
				await page.mouse.move(x, y);
				if (x >= card.x && y < card.y + card.height && await tip(page).isVisible()) {
					crossedExposedCorner ||= await page.evaluate(({ x, y }) =>
						!!document.elementFromPoint(x, y)?.closest('.scard[role="button"]'), { x, y });
				}
				await page.waitForTimeout(40);
				openThroughout &&= await tip(page).isVisible();
			}
			expect(openThroughout, 'the tooltip stays open across the rounded corner');
			expect(crossedExposedCorner, 'an underlying chat row remains a pointer target in the cut-out');
			expect(await tip(page).evaluate(el => el.matches(':hover')), 'the pointer reaches the tooltip');
			await page.mouse.wheel(0, 250);
			await page.waitForFunction(() => document.querySelector('.update-tooltip')?.scrollTop > 0);
		}
	}))]
});
