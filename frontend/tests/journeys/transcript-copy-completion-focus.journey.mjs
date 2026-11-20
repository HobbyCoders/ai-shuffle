// Named running-app check with controlled network status and scoped notification requests in the combined case.
import assert from 'node:assert/strict';
import { runJourney } from './runner.mjs';
import { composer, gotoApp, waitChat } from './lib/app.mjs';
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
async function openAtPhase({ page, status, log }, initialPhase) {
	phases.set(page, initialPhase);
	await page.route('**/api/v1/network/status', async (route) => {
		if (route.request().method() !== 'GET') return route.continue();
		const phase = phases.get(page);
		log(`GET /api/v1/network/status -> 200 ${phase}`);
		await route.fulfill({ json: networkStatus(phase) });
	});
	// These cases need the new-chat surface, not an agent selection or its phone sheet.
	await gotoApp(page, status);
	await waitChat(page, { surface: 'new' });
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
		async act({ page, mark, log }) {
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
				assert.equal(await page.locator('.app-body').getAttribute('tabindex'), null, 'Workspace itself is not focusable');
				await mark('Tab from Start of workspace to the first chat control');
				await page.keyboard.press('Tab');
				// The phone sidebar toggle's name includes the unread count when nonzero.
				const chats = page.locator('header.topbar button.tb-icon.phone');
				await focused(page, chats, 'Chats after Tab from Start of workspace');
				log(`Tab focused the sidebar toggle: ${await chats.getAttribute('aria-label')}`);
				await mark('Tab reached Chats');
			}
		}
	};
}
await runJourney({
	name: 'transcript-copy-completion-focus',
	allow: DEV_INSTANCE_NOISE,
	browser: () => ({ context: { serviceWorkers: 'block' } }),
	recorder: { selectors: [
		'chip=button[aria-label*="Restore details"]', 'details=.copy-message', 'notice=.variant-page',
		'focusChip=button[aria-label*="Restore details"]:focus', 'focusDetails=.copy-message:focus',
		'focusAction=.banner-action:focus', 'focusWorkspaceStart=[aria-label="Start of workspace"]:focus',
		'focusComposer=[role="textbox"][aria-label="Message"]:focus', 'focusModal=[role="dialog"] :focus',
		'focusChats=.tb-icon:focus'
	] },
	// The handoff crosses all three notice states at both widths. The remaining
	// cases cover distinct focus owners without replaying each poll at every width.
	cases: [
		makeCase('priority', ['desktop', 'phone'], 'slow'),
		makeCase('chip', ['desktop'], 'fast'),
		makeCase('action', ['phone'], 'slow'),
		makeCase('standalone-chip', ['phone'], 'fast'),
		makeCase('composer', ['phone'], 'fast'),
		makeCase('modal', ['desktop'], 'fast')
	]
});
