// Owner-only Settings > Learned behaviour against the real worker API and the
// dev loop's scripted provider: add a rule, see it in a fresh chat's prompt,
// switch the learner off and on, delete the rule. No real inference.
import { readFile } from 'node:fs/promises';
import { fakeAiTag, runJourney } from './runner.mjs';
import {
	expect, gotoApp, pickAgent, settleServiceWorker, showSidebar,
	typeAndSend, uniq, waitChat, waitTurnSettled
} from './lib/app.mjs';

const ROUTE = '/api/v1/learner';
const POLICY = '/api/v1/settings/owner-policy';
const button = (scope, name) => scope.getByRole('button', { name, exact: true }).filter({ visible: true });
const workspace = (page) => page.getByRole('navigation', { name: 'Workspace', exact: true });

async function openPage(page) {
	await showSidebar(page);
	await button(workspace(page), 'Settings').click();
	const settings = page.getByRole('navigation', { name: 'Settings', exact: true });
	await button(settings, 'Learned behaviour').first().click();
}

function responseTo(page, method, path) {
	return page.waitForResponse((response) => {
		const { pathname } = new URL(response.url());
		return response.request().method() === method && (pathname === path || pathname.startsWith(`${path}/`));
	});
}

async function chatPrompt(page, status, variant, token) {
	if (variant.viewport === 'phone') {
		// From a rule's page, Back twice; deleting a rule already returned to the list.
		const bar = page.locator('.topbar');
		const toList = button(bar, 'Back to Learned behaviour');
		if (await toList.count()) await toList.click();
		await button(bar, 'Back to Settings').click();
	}
	await showSidebar(page);
	await button(workspace(page), 'New chat').click();
	await waitChat(page, { surface: 'new' });
	await pickAgent(page, page.getByRole('region', { name: 'New chat' }));
	await typeAndSend(page, `Reply to ${token}. ${fakeAiTag(variant)}`.trim());
	await waitTurnSettled(page, { messagesAtLeast: 2 });
	const requests = (await readFile(status.fake_ai.request_log, 'utf8')).trim().split('\n').filter(Boolean).map(JSON.parse);
	const request = requests.find(({ method, body }) => method === 'POST' && body?.model === status.seed.model
		&& body.messages?.some(({ role, content }) => role === 'user' && JSON.stringify(content).includes(token)));
	expect(request, 'the seeded fake model must receive this chat turn');
	// Rules render in the # Memory section, which the native leg sends as a
	// reminder block on the first user turn, not as a system message: search
	// the whole request. The typed text never contains the rule's wording.
	const memory = request.body.messages.find(({ content }) => JSON.stringify(content).includes('# Memory'));
	expect(memory, 'the captured request must carry the memory section');
	return JSON.stringify(request.body.messages);
}

await runJourney({
	name: 'settings-learned-behaviour',
	allow: [
		{ kind: 'failed_requests', status: 403, url: /\/api\/v1\/terminals\/targets(\?|$)/,
			reason: 'terminal discovery requires more than the passwordless loopback owner used by this dev instance' },
		{ kind: 'failed_requests', status: 401, url: /\/api\/v1\/projects\/[^/]+\/github\/pulls(\?|$)/,
			reason: 'the dev instance has no GitHub sign-in' }
	],
	cases: [{
		name: 'add-rule-chat-toggle-delete',
		async setup({ page, status, log }) {
			const port = status.app?.local_port;
			expect(Number.isInteger(port) && port > 1024 && ![8000, 8001, 8002].includes(port)
				&& status.seed?.model && status.fake_ai?.request_log,
			'start a seeded, disposable dev instance with the fake provider');
			// The normal preview grant must not be used for owner-only reads or writes.
			const ui = `http://localhost:${port}/`;
			const auth = await page.request.get(new URL('/api/v1/auth/status', ui).href, { maxRedirects: 0 });
			expect(auth.ok(), `disposable loopback owner auth check returned ${auth.status()}`);
			const { authenticated, is_admin, preview_access } = await auth.json();
			expect(authenticated === true && is_admin === true && preview_access === false,
			'loopback must be an authenticated owner, not a preview grant');
			page.setDefaultTimeout(15_000);
			await gotoApp(page, { urls: { ui } });
			await settleServiceWorker(page, { urls: { ui } });
			const { width, height } = page.viewportSize();
			log(`Rendered owner session: ${width}×${height}; preview_access=false.`);
		},
		async act({ page, status, variant, mark, log }) {
			const token = uniq();
			const rule = `When you answer: start with copper lantern ${token}.`;
			let created = null;
			try {
				await mark('open Settings > Learned behaviour');
				await openPage(page);
				await page.getByRole('button', { name: /^Setup\b/ }).filter({ visible: true }).waitFor();

				await mark('add a rule');
				await button(page, 'Add rule').first().click();
				await page.getByRole('textbox', { name: 'Rule text', exact: true }).filter({ visible: true }).fill(rule);
				const [made] = await Promise.all([responseTo(page, 'POST', `${ROUTE}/rules`), button(page, 'Add rule').last().click()]);
				expect(made.status() === 201, `the real POST must add the rule (HTTP ${made.status()})`);
				created = await made.json();
				expect(created.text === rule && created.kept === true, 'an added rule is the owner’s own');
				// The new rule opens in its editor: beside the list on desktop, as
				// its own page on a phone (the list is then hidden).
				await page.waitForFunction((text) => [...document.querySelectorAll('textarea[aria-label="Rule text"]')]
					.some((box) => box.value === text && box.offsetParent !== null), rule);

				await mark('a fresh chat carries the rule');
				const withRule = await chatPrompt(page, status, variant, `${token}-rule`);
				expect(withRule.includes(rule), 'the outgoing prompt must carry the saved rule');

				await mark('switch the learner off and back on');
				await openPage(page);
				await page.getByRole('button', { name: /^Setup\b/ }).filter({ visible: true }).click();
				const toggle = page.getByRole('switch', { name: 'Learn from chats' }).filter({ visible: true });
				expect(await toggle.getAttribute('aria-checked') === 'true', 'the learner starts on');
				const [off] = await Promise.all([responseTo(page, 'PUT', POLICY), toggle.click()]);
				expect(off.ok() && (await off.json()).learner_enabled === false, 'the switch must save learner_enabled=false');
				await page.getByText('Off: no learning calls', { exact: false }).filter({ visible: true }).first().waitFor();
				const [on] = await Promise.all([responseTo(page, 'PUT', POLICY), toggle.click()]);
				expect(on.ok() && (await on.json()).learner_enabled === true, 'the switch must save learner_enabled=true');

				await mark('delete the rule');
				// A phone shows Setup as its own page: back to the list first.
				if (variant.viewport === 'phone') await button(page.locator('.topbar'), 'Back to Learned behaviour').click();
				await page.getByRole('button', { name: new RegExp(`copper lantern ${token}`) }).filter({ visible: true }).first().click();
				await button(page, 'Delete').click();
				const [removed] = await Promise.all([responseTo(page, 'DELETE', `${ROUTE}/rules`), button(page, 'Delete').last().click()]);
				expect(removed.status() === 204, `the real DELETE must remove the rule (HTTP ${removed.status()})`);
				created = null;
				// A phone returns to the list; let the rule's page finish leaving.
				if (variant.viewport === 'phone') {
					await button(page.locator('.topbar'), 'Back to Learned behaviour').waitFor({ state: 'hidden' });
				}

				await mark('a fresh chat after deleting');
				const after = await chatPrompt(page, status, variant, `${token}-gone`);
				expect(!after.includes(rule), 'a deleted rule must not reach the prompt');
				log('Real worker POST/PUT/DELETE verified; the fake provider captured the rule in a fresh chat and not after deletion. Prompt evidence, not inference.');
			} finally {
				if (created) {
					const ui = `http://localhost:${status.app.local_port}`;
					await page.request.delete(`${ui}${ROUTE}/rules/${created.id}?expected_revision=${created.revision}&expected_metadata_version=${encodeURIComponent(created.metadata_version)}`).catch(() => {});
				}
				await page.request.put(`http://localhost:${status.app.local_port}${POLICY}`, { data: { learner_enabled: true } }).catch(() => {});
			}
		}
	}]
});
