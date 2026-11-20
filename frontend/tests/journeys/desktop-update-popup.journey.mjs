// Exercise the real sidebar and dock with synthetic update status. No action
// requests are allowed through, even if a click regresses into an install.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, showSidebar } from './lib/app.mjs';
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

await runJourney({
	name: 'desktop-update-popup',
	allow: DEV_INSTANCE_NOISE,
	browser: async () => ({ context: { serviceWorkers: 'block' } }),
	cases: [{
		name: 'worker-details',
		async setup({ page, status, variant }) {
			if (variant.viewport === 'phone') await page.emulateMedia({ colorScheme: 'dark' });
			// Show the control in the browser only; the server keeps preview restrictions.
			await page.route('**/api/v1/auth/status', async route => {
				const response = await route.fetch();
				await route.fulfill({ response, json: { ...(await response.json()), preview_access: false } });
			});
			await page.route('**/api/v1/settings/application-updates**', route => {
				const req = route.request();
				return req.method() === 'GET' && new URL(req.url()).pathname.endsWith('/workers')
					? route.fulfill({ json: snapshot }) : route.abort();
			});
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
	}]
});
