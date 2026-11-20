// Owner-only Settings > Memory > Learned guidance, against the real worker
// API and the dev loop's scripted provider. No learner or real inference.
import { createHash } from 'node:crypto';
import { lstat, mkdir, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { basename, join } from 'node:path';
import { fakeAiTag, runJourney } from './runner.mjs';
import {
	expect, gotoApp, pickAgent, settleServiceWorker, showSidebar,
	typeAndSend, uniq, waitChat, waitTurnSettled
} from './lib/app.mjs';

const ROUTE = '/api/v1/learned-guidance';
// Matches the dev fake-ai model without using its complete ID as a storage key.
const STEM = 'ake-ai';
const button = (scope, name) => scope.getByRole('button', { name, exact: true }).filter({ visible: true });
const workspace = (page) => page.getByRole('navigation', { name: 'Workspace', exact: true });
const textbox = (page, model) => page.getByRole('textbox', { name: `Guidance for ${model}`, exact: true }).filter({ visible: true });

async function fixtureDirectory(status) {
	const scratch = await realpath(status.scratch_dir);
	const data = join(scratch, 'data');
	const home = join(scratch, 'home');
	expect(basename(scratch).startsWith('ai-shuffle-preview-') && status.data_dir === data
		&& (await readFile(join(scratch, '.branch-preview'), 'utf8')).trim() === data,
	'do not seed anything except launcher-marked disposable data');
	expect(await realpath(home) === home, 'the disposable home must not be a link');
	const directory = join(home, '.fluid', 'guidance');
	await mkdir(directory, { recursive: true });
	expect(await realpath(directory) === directory, 'the disposable store must not be a link');
	return directory;
}

async function openGuidance(page) {
	await showSidebar(page);
	await button(workspace(page), 'Settings').click();
	const settings = page.getByRole('navigation', { name: 'Settings', exact: true });
	await button(settings, 'Memory').first().click();
	await page.getByRole('button', { name: /^Learned guidance\b/ }).filter({ visible: true }).click();
}

function responseTo(page, method) {
	return page.waitForResponse((response) => {
		const { pathname } = new URL(response.url());
		return response.request().method() === method && (pathname === ROUTE || pathname.startsWith(`${ROUTE}/`));
	});
}

async function chatPrompt(page, status, variant, token) {
	if (variant.viewport === 'phone') {
		const bar = page.locator('.topbar');
		await button(bar, 'Back to Memory').click();
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
	const instructions = request.body.messages.filter(({ role }) => ['system', 'developer'].includes(role));
	expect(instructions.length > 0, 'the captured request must contain prompt instructions');
	return JSON.stringify(instructions);
}

await runJourney({
	name: 'settings-learned-guidance',
	allow: [
		{ kind: 'failed_requests', status: 403, url: /\/api\/v1\/terminals\/targets(\?|$)/,
			reason: 'terminal discovery requires more than the passwordless loopback owner used by this dev instance' },
		{ kind: 'failed_requests', status: 401, url: /\/api\/v1\/projects\/[^/]+\/github\/pulls(\?|$)/,
			reason: 'the dev instance has no GitHub sign-in' }
	],
	cases: [{
		name: 'edit-reload-cancel-delete-and-chat',
		async setup({ page, status, log }) {
			const port = status.app?.local_port;
			const model = status.seed?.model;
			expect(Number.isInteger(port) && port > 1024 && ![8000, 8001, 8002].includes(port)
				&& model?.includes(STEM) && status.fake_ai?.request_log,
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
			const directory = await fixtureDirectory(status);
			const token = uniq();
			const original = `Start each answer with copper lantern ${token}.`;
			const edited = `Start each answer with silver lantern ${token}.`;
			const file = join(directory, `${STEM}.md`);
			const marker = join(directory, '.manual', `m-${createHash('sha256').update(STEM).digest('hex')}`);
			// Set up in act so a recorder startup failure cannot leave fixtures behind.
			const markerExists = await lstat(marker).then(() => true, error => {
				if (error.code !== 'ENOENT') throw error;
				return false;
			});
			expect(!markerExists, 'fixture must not reuse a pre-existing manual marker');
			await writeFile(file, original, { flag: 'wx' });
			try {
				await mark('open Settings > Memory > Learned guidance');
				await openGuidance(page);
				const block = page.getByRole('region', { name: STEM, exact: true }).filter({ visible: true });
				const draft = textbox(page, STEM);
				await draft.waitFor();
				expect(await draft.inputValue() === original, 'the editor must read the synthetic saved block');
				const builtIn = page.getByText('Built-in model guidance', { exact: true }).filter({ visible: true });
				await builtIn.waitFor();
				await page.getByText(`Model IDs containing “${STEM}” (case-insensitive)`, { exact: true }).filter({ visible: true }).waitFor();

				await mark('edit and save to the worker');
				await draft.fill(edited);
				const saveButton = button(block, 'Save guidance');
				const [save] = await Promise.all([responseTo(page, 'PUT'), saveButton.click()]);
				expect(save.status() === 200, `the real PUT must save the edited block (HTTP ${save.status()})`);
				const stored = (await save.json()).content;
				expect(stored.trim() === edited, 'the worker must return the edited instruction (apart from file whitespace)');
				await saveButton.and(page.locator(':disabled')).waitFor();

				await mark('reload the guidance detail');
				const [reloaded] = await Promise.all([responseTo(page, 'GET'), (async () => {
					await page.reload({ waitUntil: 'domcontentloaded' });
					// Separate the first paint (blank shell) from rehydration, as in settings-reload.
					await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
					await mark('the reloaded detail reads the saved edit');
				})()]);
				expect(reloaded.ok(), 'reloading must fetch real saved guidance again');
				await draft.waitFor();
				expect(await draft.inputValue() === stored, 'the saved edit must survive a full reload');

				await mark('cancel delete and keep the saved block');
				let deletes = 0;
				page.on('request', req => {
					if (req.method() === 'DELETE' && new URL(req.url()).pathname.startsWith(`${ROUTE}/`)) deletes++;
				});
				await button(block, 'Delete guidance').click();
				await button(block, 'Confirm delete').waitFor();
				await button(block, 'Cancel').click();
				await button(block, 'Confirm delete').waitFor({ state: 'hidden' });
				expect(deletes === 0 && await draft.inputValue() === stored, 'Cancel must not write or remove the saved block');

				await mark('send a fresh chat after saving and cancelling delete');
				const before = await chatPrompt(page, status, variant, `${token}-before-delete`);
				expect(before.includes(edited) && !before.includes(original), 'the real outgoing prompt must use the edit, not the original');

				await mark('return to Learned guidance and confirm delete');
				await openGuidance(page);
				await draft.waitFor();
				expect(await draft.inputValue() === stored, 'reopening settings must still read the edit');
				await button(block, 'Delete guidance').click();
				const [removed] = await Promise.all([responseTo(page, 'DELETE'), button(block, 'Confirm delete').click()]);
				expect(removed.status() === 204 && deletes === 1, 'one real DELETE must succeed after confirmation');
				await draft.waitFor({ state: 'hidden' });
				await builtIn.waitFor();

				await mark('send a fresh chat after deleting');
				const after = await chatPrompt(page, status, variant, `${token}-after-delete`);
				expect(!after.includes(edited) && !after.includes(original), 'neither saved instruction may appear after deletion');
				log('Real worker PUT/reload/cancel/DELETE verified. Fake provider captured the edited instruction in a fresh chat and neither instruction after deletion; this is prompt evidence, not inference.');
			} finally {
				await rm(file, { force: true });
				await rm(marker, { force: true });
			}
		}
	}]
});
