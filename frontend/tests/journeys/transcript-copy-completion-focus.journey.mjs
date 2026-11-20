// Named running-app check with controlled network status and scoped notification requests in the combined case.
import assert from 'node:assert/strict';
import { runJourney } from './runner.mjs';
import { api, composer, openNewChat, uniq } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
const phases = new WeakMap();
const downloading = { peer: 'peer-a', peer_name: 'ServerWorker', phase: 'downloading', pages_done: 30,
	pages_total: 60, bytes_done: 12000000, bytes_total: 24000000, error: null };
function networkStatus(phase) {
	const copying = phase === 'syncing';
	const neverCopied = phase.startsWith('standalone');
	const transferring = ['ready-download', 'paused', 'standalone-download'].includes(phase);
	const error = phase === 'paused' ? 'HTTP 503' : null;
	return {
		device: { device_id: 'self', fingerprint: '' }, enrolled: true, platform: 'linux', version: '1', instance_id: 'journey',
		network: { network_id: 'n', name: 'Journey', device_id: 'self', fingerprint: '', founder_device_id: 'peer-a',
			generation: 1, key_version: 1, bootstrap_state: copying ? 'syncing' : 'ready', member_count: 2 },
		hub: { linked: true, first_copy: neverCopied ? null : copying ? downloading : { ...downloading, phase: 'done', pages_done: 60, bytes_done: 24000000 },
			transcripts: transferring ? { phase: 'transcripts', peer: 'peer-a', peer_name: 'ServerWorker',
				sessions_done: error ? 576 : 312, sessions_total: 1152, events_copied: 0, error } : null }
	};
}
const button = (page, name) => page.getByRole('button', { name, exact: true });
const chip = (page) => page.getByRole('button', { name: /Restore details/ });
const success = (page) => page.getByText('Your data is copied', { exact: true });
const workspace = (page) => page.locator('.app-body');
const workspaceStart = (page) => page.getByRole('group', { name: 'Start of workspace', exact: true });
async function focused(page, locator, description, timeout = 2000) {
	// assert the actual active element, not a class or a document position
	let focus;
	try {
		const element = await locator.elementHandle({ timeout });
		await page.waitForFunction((el) => document.activeElement === el, element, { timeout });
	} catch {
		focus = await page.evaluate(() => `${document.activeElement.tagName}:${document.activeElement.getAttribute('aria-label') ?? ''}:${document.activeElement.className}`);
		assert.fail(`expected focus on ${description}; actual ${focus}`);
	}
}
async function openAtPhase(ctx, initialPhase) {
	const { page, log } = ctx;
	phases.set(page, initialPhase);
	await page.route('**/api/v1/network/status', async (route) => {
		if (route.request().method() !== 'GET') return route.continue();
		const phase = phases.get(page);
		log(`GET /api/v1/network/status -> 200 ${phase}`);
		await route.fulfill({ json: networkStatus(phase) });
	});
	await openNewChat(ctx);
	// Settings uses the same top bar; keep its implicit banner landmark exposed.
	await page.getByRole('banner').and(page.locator('header.topbar')).waitFor({ timeout: 5000 });
}

function makeCase(owner, viewports, throttle) {
	const standalone = owner === 'standalone-chip';
	return {
		name: owner,
		viewports,
		throttles: [throttle],
		async setup(ctx) {
			const { page } = ctx;
			await openAtPhase(ctx, standalone ? 'standalone-download' : 'syncing');
			if (standalone) await button(page, 'Minimize').waitFor();
			else await page.getByRole('status').filter({ hasText: /Copying your data from ServerWorker/ }).waitFor({ timeout: 15000 });
		},
		async act({ page, mark }) {
			const reload = button(page, 'Reload');
			const minimize = button(page, 'Minimize');
			let keptFocus;
			if (!standalone) {
				await mark('release ready + 312/1152 transcripts');
				phases.set(page, 'ready-download');
				await reload.waitFor({ timeout: 12000 });
				assert.equal(await success(page).count(), 0);
			}
			if (owner !== 'action') {
				await mark('minimize by keyboard');
				await minimize.press('Enter');
				await chip(page).waitFor({ timeout: 3000 });
				await focused(page, chip(page), 'transcript chip');
				if (owner === 'priority') {
					assert.equal(await success(page).count(), 0, 'success must not appear while minimized and transferring');
					assert.equal(await reload.count(), 0, 'Reload belongs to details only while transferring');
					await mark('release paused + 576/1152 transcripts');
					phases.set(page, 'paused');
					await page.getByRole('button', { name: /Paused.*50%.*Restore details/ }).waitFor({ timeout: 16000 });
					await mark('restore by keyboard');
					await chip(page).press('Space');
					await reload.waitFor({ timeout: 3000 });
					const message = page.getByText(/576 of 1,152 chats: HTTP 503/);
					await focused(page, message, 'restored transcript details');
					assert.equal(await chip(page).count(), 0);
				}
			}
			if (owner === 'composer') {
				await mark('type a draft, without sending');
				keptFocus = composer(page);
				await keptFocus.fill('Keep this draft through completion.');
			} else if (owner === 'modal') {
				await mark('open Keyboard shortcuts');
				await page.keyboard.press('?');
				keptFocus = page.getByRole('dialog', { name: 'Keyboard shortcuts' }).getByRole('button', { name: 'Close', exact: true });
				await keptFocus.focus();
			} else if (owner === 'action') {
				await mark('focus Reload without clicking');
				await reload.focus();
			}
			if (keptFocus) await focused(page, keptFocus, owner);
			await mark(`release complete, focus owner: ${owner}`);
			phases.set(page, standalone ? 'standalone-complete' : 'complete');
			if (standalone) await chip(page).waitFor({ state: 'detached', timeout: 16000 });
			else {
				await success(page).waitFor({ timeout: 16000 });
				assert.equal(await reload.isVisible(), true);
				assert.equal(await chip(page).count(), 0);
			}
			await focused(page, keptFocus || workspaceStart(page), keptFocus ? owner : 'Start of workspace');
			if (owner === 'composer') assert.equal(await keptFocus.innerText(), 'Keep this draft through completion.');
			await mark(`focus verified: ${keptFocus ? owner : 'Start of workspace'}`);
			if (standalone) {
				await mark('Tab from Start of workspace to the first chat control');
				await page.keyboard.press('Tab');
				await focused(page, button(page, 'Chats'), 'Chats after Tab from Start of workspace');
				await mark('Tab reached Chats');
			}
		}
	};
}
const historyAtCompletion = {
	name: 'history-empty-tap-after-transcript-completion',
	viewports: ['phone'],
	throttles: ['fast'],
	async setup(ctx) {
		const { page, log } = ctx;
		await openAtPhase(ctx, 'standalone-download');
		page.__transcriptNotice = `transcript-focus-${uniq()}`;
		await api(page, 'POST', '/notifications', {
			id: page.__transcriptNotice, severity: 'warning', surface: 'notification',
			category: 'journey', title: 'A notification to open during a download', message: 'Journey notice'
		});
		// A full phone history can cover the empty space this case must tap. Show
		// only this page's fixture, without clearing or dismissing stored history.
		await page.route((url) => url.pathname === '/api/v1/notifications', async (route) => {
			if (route.request().method() !== 'GET') return route.fallback();
			const response = await route.fetch();
			if (!response.ok()) return route.fulfill({ response });
			const history = await response.json();
			const notifications = history.notifications.filter(({ id }) => id === page.__transcriptNotice);
			log(`GET /api/v1/notifications: showing ${notifications.length} test-owned row of ${history.notifications.length}`);
			return route.fulfill({ response, json: { ...history, notifications, unread: notifications.filter((row) => !row.read).length } });
		});
		// Opening the bell would mark the whole account read, not just the fixture.
		await page.route((url) => url.pathname === '/api/v1/notifications/read-all', async (route) => {
			if (route.request().method() !== 'POST') return route.fallback();
			const path = `/api/v1/notifications/${encodeURIComponent(page.__transcriptNotice)}/read`;
			const url = new URL(path, route.request().url());
			const response = await route.fetch({ url: url.href });
			log(`POST /api/v1/notifications/read-all scoped to POST ${path}: HTTP ${response.status()}`);
			if (!response.ok()) return route.fulfill({ response });
			const { ok } = await response.json();
			return route.fulfill({ response, json: { updated: ok ? 1 : 0 } });
		});
		await openNewChat(ctx);
	},
	async act({ page, mark, log }) {
		const bell = page.getByRole('button', { name: /^Notifications(?: \(\d+ unread\))?$/ }).first();
		const panel = page.getByRole('dialog', { name: 'Notifications', exact: true });
		const restore = chip(page);
		try {
			await mark('minimize by keyboard and open notification history');
			await button(page, 'Minimize').press('Enter');
			await focused(page, restore, 'transcript chip');
			await bell.tap();
			await panel.waitFor();
			await mark('Tab from notification history to the transcript chip');
			for (let i = 0; i < 40 && !(await restore.evaluate((el) => document.activeElement === el)); i++) {
				await page.keyboard.press('Tab');
			}
			await focused(page, restore, 'transcript chip via Tab');
			await mark('complete the download while history stays open');
			phases.set(page, 'standalone-complete');
			await restore.waitFor({ state: 'detached', timeout: 16_000 });
			await focused(page, workspaceStart(page), 'Start of workspace');
			assert.equal(await workspace(page).getAttribute('tabindex'), null, 'Workspace itself is not focusable');
			assert.equal(await panel.isVisible(), true);

			const spot = await workspace(page).evaluate((body) => {
				const nonempty = '.notif-center, [data-ui-mount="ChatInput"], button, a, input, textarea, select, [contenteditable], [role="button"], [tabindex]';
				for (let y = 8; y < innerHeight - 8; y += 16) {
					for (let x = 12; x < innerWidth - 8; x += 24) {
						const el = document.elementFromPoint(x, y);
						if (!el || !body.contains(el) || !el.closest('[data-ui-state="chat"]') || el.closest(nonempty)) continue;
						// Stay clear of nearby controls that the browser might choose for a touch.
						const around = [-16, 0, 16];
						if (!around.every((dx) => around.every((dy) => document.elementFromPoint(x + dx, y + dy) === el))) continue;
						document.addEventListener('click', (event) => {
							window.__transcriptEmptyTap = {
								hit: event.target === el,
								target: `${event.target.localName}.${event.target.classList[0]}`,
								focus: document.activeElement === document.body ? 'body' : document.activeElement.getAttribute('aria-label')
							};
						}, { capture: true, once: true });
						return { x, y, target: `${el.localName}.${el.classList[0]}` };
					}
				}
				return null;
			});
			assert.ok(spot, 'hit-test must find empty chat space inside Workspace, outside history');
			await mark(`tap empty Workspace: ${spot.target} at ${spot.x},${spot.y}`);
			await page.touchscreen.tap(spot.x, spot.y);
			const click = await page.evaluate(() => window.__transcriptEmptyTap);
			log(`empty Workspace tap ${spot.target} at ${spot.x},${spot.y}; click target: ${click?.target}; active element before close: ${click?.focus}`);
			assert.equal(click?.hit, true, 'the actual click hit the empty element, not an overlay or control');
			await panel.waitFor({ state: 'hidden' });
			await focused(page, bell, 'bell after an empty Workspace tap');
			await mark('bell restored; reopen history and tap the composer');
			await bell.tap();
			await panel.waitFor();
			await composer(page).tap();
			await panel.waitFor({ state: 'hidden' });
			await focused(page, composer(page), 'composer after tapping a real control');
			await mark('composer kept focus');
		} finally {
			await api(page, 'POST', `/notifications/${page.__transcriptNotice}/dismiss`).catch(() => {});
		}
	}
};

await runJourney({
	name: 'transcript-copy-completion-focus',
	allow: DEV_INSTANCE_NOISE,
	browser: () => ({ context: { serviceWorkers: 'block' } }),
	recorder: { selectors: [
		'chip=button[aria-label*="Restore details"]', 'details=.copy-message', 'notice=.variant-page',
		'focusChip=button[aria-label*="Restore details"]:focus', 'focusDetails=.copy-message:focus',
		'focusAction=.banner-action:focus', 'focusWorkspaceStart=[aria-label="Start of workspace"]:focus',
		'focusComposer=[role="textbox"][aria-label="Message"]:focus', 'focusModal=[role="dialog"] :focus',
		'history=[role="dialog"][aria-label="Notifications"]', 'focusBell=.notif-btn:focus', 'focusChats=.tb-icon:focus'
	] },
	// The handoff crosses all three notice states at both widths. The remaining
	// cases cover distinct focus owners without replaying each poll at every width.
	cases: [
		makeCase('priority', ['desktop', 'phone'], 'slow'),
		makeCase('chip', ['desktop'], 'fast'),
		makeCase('action', ['phone'], 'slow'),
		makeCase('standalone-chip', ['phone'], 'fast'),
		makeCase('composer', ['phone'], 'fast'),
		makeCase('modal', ['desktop'], 'fast'),
		historyAtCompletion
	]
});
