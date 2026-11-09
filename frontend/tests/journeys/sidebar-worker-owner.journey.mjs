// Journey: which worker a chat is on, once its local record and its owner
// disagree (#1288).
//
// `owner_device_id` is synced and decides. `instance_id` is per worker and is
// not synced, so after a chat goes A to B to C, worker A still holds
// instance_id=B and owner_device_id=C. Bugs this guards: a row only showed the
// server icon when it had nothing else to print, so rows on the same worker
// looked different; and the header's Move menu read instance_id, so it called
// the former worker "current" and refused to move the chat back there.
//
// One dev instance has no second worker, so the page sees seeded chats as
// other workers' (the fields the server derives, and two installs). A real
// move between two workers is checked by hand and in the PR's notes; this
// keeps the rendering of the disagreement in the suite.
import { runJourney } from './runner.mjs';
import { SEEDED, expect, gotoApp, isPhone, openFromSidebar, seededChatId, showSidebar, waitChat, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { expectNeverDecreases, markTime } from './lib/timeline.mjs';

const OWNER = 'Laptop';
const FORMER = 'Office PC';
const LOOK = 'read the rows';

/** In the page, not page.route: the built app's service worker makes these requests. */
async function simulateWorkers(page, status) {
	await page.addInitScript(
		({ passedOn, ownerless, owner, former }) => {
			const original = window.fetch.bind(window);
			const pathOf = (input) => {
				try {
					return new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
				} catch {
					return '';
				}
			};
			const remote = { is_remote: true, device_name: owner, device_online: true, controllable: true };
			const patch = (value) => {
				if (Array.isArray(value)) return value.map(patch);
				if (!value || typeof value !== 'object') return value;
				const out = {};
				for (const [k, v] of Object.entries(value)) out[k] = patch(v);
				if (out.id === passedOn) Object.assign(out, remote, { instance_id: 'worker-former', owner_device_id: 'worker-owner' });
				if (out.id === ownerless) Object.assign(out, remote, { instance_id: 'worker-owner', owner_device_id: null });
				return out;
			};
			const install = (id, device_name) => ({ id, device_name, is_self: false, online: true });
			const json = (body) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
			window.fetch = async (input, init) => {
				const method = (init?.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
				const path = pathOf(input);
				if (path === `/api/v1/sessions/${passedOn}/sync-status`) {
					return json({ state: 'current', scope: 'session', initial_caught_up: true, pending_updates: 0 });
				}
				const response = await original(input, init);
				const faked = path === '/api/v1/cluster/installs' || (path.startsWith('/api/v1/sessions') && !/\/(events|stream)$/.test(path));
				if (method !== 'GET' || !response.ok || !faked) return response;
				if (!(response.headers.get('content-type') || '').includes('application/json')) return response;
				const body = await response.clone().json().catch(() => undefined);
				if (body === undefined) return response;
				if (path === '/api/v1/cluster/installs') {
					return new Response(
						JSON.stringify({ ...body, installs: [...(body.installs ?? []), install('worker-former', former), install('worker-owner', owner)] }),
						{ status: response.status, headers: response.headers }
					);
				}
				return new Response(JSON.stringify(patch(body)), { status: response.status, headers: response.headers });
			};
		},
		{ passedOn: seededChatId(status, 'long'), ownerless: seededChatId(status, 'cleared'), owner: OWNER, former: FORMER }
	);
}

const rowOf = (sessions, title) => sessions.getByRole('group', { name: `${title} session`, exact: true });

/** The icon is the row's only mention of another worker; its name is on the icon. */
async function expectMarked(sessions, title) {
	const row = rowOf(sessions, title);
	await row.waitFor();
	const icon = row.locator('.worker');
	expect((await icon.count()) === 1, `"${title}" carries one worker icon`);
	expect((await icon.getAttribute('title')) === `On ${OWNER}`, `"${title}" names its owner on hover`);
	expect((await icon.getAttribute('aria-label')) === `On ${OWNER}`, `"${title}" names its owner to screen readers`);
	const text = await row.locator('.l2').innerText();
	expect(!text.includes(OWNER) && !text.includes(FORMER), `"${title}" prints no worker name, got ${JSON.stringify(text)}`);
}

async function expectUnmarked(sessions, title) {
	const row = rowOf(sessions, title);
	await row.waitFor();
	expect((await row.locator('.worker').count()) === 0, `"${title}" is on this worker, so it has no icon`);
	const text = await row.innerText();
	expect(!text.includes(OWNER) && !text.includes(FORMER), `"${title}" prints no worker name, got ${JSON.stringify(text)}`);
}

/** The filter goes by the owner: it offers the owner and never the former worker. */
async function expectFilterFollowsOwner(page, sessions) {
	await sessions.getByRole('button', { name: 'Filter', exact: true }).click();
	const sheet = page.getByRole('dialog', { name: 'Filter chats' });
	await sheet.waitFor();
	const owner = sheet.getByRole('button', { name: OWNER, exact: true });
	await owner.waitFor();
	expect((await sheet.getByRole('button', { name: FORMER, exact: true }).count()) === 0, `the filter does not offer ${FORMER}, which owns nothing`);
	await owner.click();
	await rowOf(sessions, SEEDED.no_project).waitFor({ state: 'detached', timeout: 5_000 });
	await expectMarked(sessions, SEEDED.long);
	await expectMarked(sessions, SEEDED.cleared);
	await sheet.getByRole('button', { name: 'All devices', exact: true }).click();
	await rowOf(sessions, SEEDED.no_project).waitFor({ timeout: 5_000 });
	await page.keyboard.press('Escape');
	await sheet.waitFor({ state: 'hidden', timeout: 5_000 });
}

/** The Move menu calls the owner current and offers the former worker as a destination. */
async function expectMoveMenuFollowsOwner(page) {
	await page.locator('.ctx-chip.runner:visible').first().click();
	const rows = page.getByRole('menuitemradio');
	await page.getByRole('menuitemradio', { name: 'Continue here' }).waitFor();
	const seen = await rows.evaluateAll((els) => els.map((el) => ({
		name: el.querySelector('.worker-row-name')?.textContent?.trim() ?? '',
		checked: el.getAttribute('aria-checked')
	})));
	const owner = seen.find((row) => row.name === OWNER);
	const former = seen.find((row) => row.name === FORMER);
	expect(owner?.checked === 'true', `the owner is the current worker, got ${JSON.stringify(seen)}`);
	expect(former?.checked === 'false', `the former worker is a destination, got ${JSON.stringify(seen)}`);
	await page.keyboard.press('Escape');
}

await runJourney({
	name: 'sidebar-worker-owner',
	recorder: { selectors: ['marks=.scard .worker'] },
	cases: [
		{
			name: 'owner-beats-out-of-date-instance',
			async setup({ page, status }) {
				await simulateWorkers(page, status);
				await gotoApp(page, status);
				await waitChat(page, { surface: 'new' });
				await page.waitForTimeout(1000);
			},
			async act({ page, variant, mark }) {
				await mark(LOOK);
				const sessions = await showSidebar(page);
				await expectMarked(sessions, SEEDED.long);
				await expectMarked(sessions, SEEDED.cleared);
				await expectUnmarked(sessions, SEEDED.no_project);
				await expectFilterFollowsOwner(page, sessions);
				// Phone: opening a chat closes the drawer, and the menu is covered by unit tests in sheet mode.
				if (isPhone(variant)) return;
				await openFromSidebar(page, SEEDED.long);
				await waitChatOpen(page, { messagesAtLeast: 50 });
				await expectMarked(sessions, SEEDED.long);
				await expectMoveMenuFollowsOwner(page);
			},
			verify({ recording }) {
				// Once listed, no row loses its marker to a later reload.
				expectNeverDecreases(recording, 'marks', { from: markTime(recording, LOOK) });
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
