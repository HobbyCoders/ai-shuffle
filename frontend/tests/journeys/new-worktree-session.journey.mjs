// Journey: start a new worktree session from the composer and send its first
// message. The chat surface must only move forward (new → active, with an
// optional loading step); it must never fall back to the centred new/cleared
// composer once the transcript has shown, and nothing may remount.
//
// Owner report this guards: "creating a new worktree session loads to full
// page, then shows the composer back in the middle as if it went back to new
// chat, then loads back to the proper full screen."
//
//   python -m scripts.devloop up            # in another terminal / a Monitor
//   node tests/journeys/new-worktree-session.journey.mjs [--only=auto@phone] [--list]
//
// Entry points covered (every way the composer binds a new worktree session):
//   auto            worktree toggle on, branch Auto (named from the message)
//                   → autoCreateWorktree (POST /sessions/with-worktree + prompt)
//   auto-claude     the same Auto flow through the delegated fake Claude CLI
//   auto-*-mirrored an observer whose new session row precedes its event log
//   new-branch      "New branch..." in the branch menu (creates the git branch),
//                   then send → createWorktree (POST without prompt, then query)
//   existing-branch an existing branch from the branch menu → createWorktree
//   main-checkout   control: the plain new-chat send in the main checkout
//                   → WebSocket query path
import { fakeAiTag, runJourney } from './runner.mjs';
import { expectComposerPositions } from './lib/timeline.mjs';

const PROJECT = 'Dev sandbox';
const SEEDED_BRANCH = 'feature/second';
const AGENT = 'Dev agent';
const CLAUDE_AGENT = 'Dev (Claude Code)';

let counter = 0;
const uniq = () => `${Date.now().toString(36)}${(counter++).toString(36)}`;

async function openNewChat({ page, status }, agent = AGENT) {
	await page.goto(status.urls.ui);
	await page.locator('[data-ui-state="chat"][data-ui-surface="new"]').waitFor({ timeout: 90_000 });
	const hero = page.getByRole('region', { name: 'New session' });
	await hero.waitFor();
	// Pick the engine explicitly; the default only exercises the native harness.
	await page.getByRole('button', { name: /^Model: / }).waitFor({ timeout: 60_000 });
	const agentMenu = page.getByRole('menu', { name: 'Agent selection' });
	await pick(
		hero.getByRole('button', { name: /^Agent:/ }),
		agentMenu,
		agentMenu.getByRole('button', { name: agent, exact: true }),
		hero.getByRole('button', { name: `Agent: ${agent}` })
	);
	return hero;
}

/**
 * Open `trigger`'s popup and click `item` in it until `done` shows. The app
 * may re-render the hero while it hydrates (stores loading in), which closes a
 * popup opened too early; retry instead of failing on that setup noise.
 */
async function pick(trigger, popup, item, done) {
	for (let attempt = 0; attempt < 5; attempt++) {
		if (await done.isVisible()) return;
		try {
			if (!(await popup.isVisible())) await trigger.click();
			await item.click({ timeout: 5000 });
			await done.waitFor({ timeout: 5000 });
			return;
		} catch {
			/* retry */
		}
	}
	await done.waitFor({ timeout: 1000 });
}

/** Click a trigger until its popup shows (an early click can land before hydration). */
async function openPopup(trigger, popup) {
	for (let attempt = 0; attempt < 3; attempt++) {
		if (await popup.isVisible()) return popup;
		await trigger.click();
		try {
			await popup.waitFor({ timeout: 10_000 });
			return popup;
		} catch {
			/* retry */
		}
	}
	throw new Error('popup never opened');
}

async function pickProject(page, hero) {
	const item = page.getByRole('button', { name: PROJECT, exact: true });
	await pick(hero.getByRole('button', { name: /^No project/ }), item, item, hero.getByRole('button', { name: `Workspace folders: ${PROJECT}` }));
}

async function worktreeOn(page, hero) {
	await hero.getByRole('button', { name: /Switch to a worktree$/ }).click();
	await hero.getByRole('button', { name: /^Worktree branch:/ }).waitFor();
}

async function openBranchMenu(page, hero) {
	return openPopup(hero.getByRole('button', { name: /^Worktree branch:/ }), page.getByRole('dialog', { name: 'Worktree branch' }));
}

/** Type the first message, send it and wait for the fake AI's reply to settle. */
async function sendFirstMessage({ page, variant, mark, log }, label) {
	const text = `${label} journey ${uniq()} ${fakeAiTag(variant)}`.trim();
	const box = page.getByRole('textbox', { name: 'Message' });
	await mark('type the first message');
	await box.click();
	await box.fill(text);
	await mark('send');
	const t0 = Date.now();
	await page.getByRole('button', { name: 'Send', exact: true }).click();
	// Settled: a bound session showing its transcript (the user's message and
	// the reply), no longer streaming, with the fake AI's text on screen.
	const chat = page.locator(
		'[data-ui-state="chat"][data-ui-surface="active"][data-ui-streaming="false"]:not([data-ui-session="none"])'
	);
	const reply = label.includes('claude') ? 'Fake Claude Code CLI here' : 'Fake AI here';
	await page.getByText(reply, { exact: false }).first().waitFor({ timeout: 60_000 });
	await chat.waitFor({ timeout: 30_000 });
	await page.waitForFunction(
		() => Number(document.querySelector('[data-ui-state="chat"]')?.getAttribute('data-ui-messages') || 0) >= 2,
		null,
		{ timeout: 30_000 }
	);
	log(`reply settled ${Date.now() - t0}ms after send`);
	// Hold a moment: a late fold or a feed restart is exactly the kind of thing
	// that sends the surface backwards after it first looked done.
	await page.waitForTimeout(1500);
}

/**
 * Replay a new mirror's startup order: its row arrives before its event log.
 * The real app still creates the worktree and runs the chosen fake engine. Only
 * the observing worker's metadata/sync status and replication lag stand in
 * for a second worker, as in mirrored-chat.journey.mjs. Real worker logs showed
 * an empty caught_up, then a from_seq=0 restart before the first reply. The
 * peer is chosen through Runs on, so ownership is remote from the first send
 * (not a local chat artificially moved to a peer when its row arrives).
 */
async function lagNewMirror(page) {
	await page.addInitScript(() => {
		const original = window.fetch.bind(window);
		let sessionId = null;
		let readyAt = 0;
		let polls = 0;
		window.__newMirror = { emptyReplays: 0, syncPolls: 0, targetedPeer: false };
		const json = (body, response) => new Response(JSON.stringify(body), {
			status: response?.status || 200,
			headers: response?.headers || { 'content-type': 'application/json' }
		});
		const markRemote = (value) => {
			if (Array.isArray(value)) return value.map(markRemote);
			if (!value || typeof value !== 'object') return value;
			const out = Object.fromEntries(Object.entries(value).map(([key, child]) => [key, markRemote(child)]));
			if (out.id === sessionId) Object.assign(out, { is_remote: true, device_name: 'Peer worker' });
			return out;
		};
		window.fetch = async (input, init) => {
			const method = (init?.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (url.searchParams.get('worker_id') === 'journey-peer') {
				url.searchParams.set('worker_id', 'local');
				input = url.href;
			}
			if (sessionId && url.pathname === `/api/v1/sessions/${sessionId}/sync-status`) {
				window.__newMirror.syncPolls = ++polls;
				return json({ state: polls === 1 ? 'syncing' : 'current', scope: 'session', pending_updates: 0 });
			}
			if (sessionId && url.pathname === `/api/v1/sessions/${sessionId}/events` && Date.now() < readyAt) {
				window.__newMirror.emptyReplays += 1;
				// The observer is caught up with its still-empty local log. Its
				// first replicated events arrive later, even across a feed restart.
				let timer;
				let reader;
				let cancelled = false;
				const body = new ReadableStream({
					start(controller) {
						controller.enqueue(new TextEncoder().encode('event: caught_up\ndata: {"seq":0,"epoch":"0"}\n\n'));
						timer = setTimeout(async () => {
							try {
								const response = await original(input, init);
								reader = response.body.getReader();
								while (!cancelled) {
									const chunk = await reader.read();
									if (chunk.done) { controller.close(); break; }
									controller.enqueue(chunk.value);
								}
							} catch (error) {
								if (!cancelled) controller.error(error);
							}
						}, readyAt - Date.now());
					},
					cancel() { cancelled = true; clearTimeout(timer); return reader?.cancel(); }
				});
				return new Response(body, { headers: { 'content-type': 'text/event-stream' } });
			}
			// Stand in for the target's transport, not for session creation or the
			// agent. The backend still provisions a real checkout and runs the engine.
			if (method === 'POST' && url.pathname === '/api/v1/sessions/with-worktree') {
				const body = JSON.parse(init.body);
				if (body.target_instance_id === 'journey-peer') {
					window.__newMirror.targetedPeer = true;
					delete body.target_instance_id;
					init = { ...init, body: JSON.stringify(body) };
				}
			}
			const response = await original(input, init);
			if (method === 'GET' && response.ok && ['/api/v1/cluster/installs', '/api/v1/cluster/git-state'].includes(url.pathname)) {
				const body = await response.json();
				const self = body.installs.find(row => row.is_self);
				body.installs.push({ ...self, id: 'journey-peer', device_id: 'journey-peer',
					legacy_instance_id: null, is_self: false, device_name: 'Peer worker', online: true, approved: true, eligible: true });
				return json(body, response);
			}
			if (method === 'POST' && url.pathname === '/api/v1/sessions/with-worktree' && response.ok) {
				sessionId = (await response.clone().json()).session_id;
				readyAt = Date.now() + 5000;
			}
			if (sessionId && method === 'GET' && response.ok && url.pathname.startsWith('/api/v1/sessions') &&
				(response.headers.get('content-type') || '').includes('application/json')) {
				return json(markRemote(await response.json()), response);
			}
			return response;
		};
	});
}

async function pickMirrorWorker(page, hero) {
	const menu = page.getByRole('menu', { name: 'Where this session runs' });
	await pick(hero.getByRole('button', { name: /^Runs on / }), menu,
		menu.getByRole('menuitemradio', { name: /Peer worker/ }),
		hero.getByRole('button', { name: 'Runs on Peer worker', exact: true }));
}

async function verifyNewMirror({ page, recording }) {
	expectComposerPositions(recording, ['centre', 'bottom']);
	const seen = await page.evaluate(() => window.__newMirror);
	if (!seen?.emptyReplays || seen.syncPolls < 2 || !seen.targetedPeer) throw new Error(`mirror startup not exercised: ${JSON.stringify(seen)}`);
}

/**
 * Let the staged composer settle before recording, the way a user's typing
 * would: the picks start their own loads (branches, git status of the staged
 * branch) and those belong to setup, not to the send the step judges.
 */
const settled = (setup) => async (ctx) => {
	await setup(ctx);
	await ctx.page.waitForTimeout(1000);
};

await runJourney({
	name: 'new-worktree-session',
	cases: [
		{
			name: 'auto',
			setup: settled(async (ctx) => {
				const hero = await openNewChat(ctx);
				await pickProject(ctx.page, hero);
				await worktreeOn(ctx.page, hero);
				await hero.getByRole('button', { name: /^Worktree branch: a new branch from/ }).waitFor();
			}),
			act: (ctx) => sendFirstMessage(ctx, 'auto')
		},
		{
			name: 'auto-claude',
			setup: settled(async (ctx) => {
				const hero = await openNewChat(ctx, CLAUDE_AGENT);
				await pickProject(ctx.page, hero);
				await worktreeOn(ctx.page, hero);
				await hero.getByRole('button', { name: /^Worktree branch: a new branch from/ }).waitFor();
			}),
			act: (ctx) => sendFirstMessage(ctx, 'auto-claude')
		},
		{
			name: 'auto-mirrored',
			setup: settled(async (ctx) => {
				await lagNewMirror(ctx.page);
				const hero = await openNewChat(ctx);
				await pickProject(ctx.page, hero);
				await worktreeOn(ctx.page, hero);
				await pickMirrorWorker(ctx.page, hero);
			}),
			act: (ctx) => sendFirstMessage(ctx, 'auto-mirrored'),
			verify: verifyNewMirror
		},
		{
			name: 'auto-claude-mirrored',
			setup: settled(async (ctx) => {
				await lagNewMirror(ctx.page);
				const hero = await openNewChat(ctx, CLAUDE_AGENT);
				await pickProject(ctx.page, hero);
				await worktreeOn(ctx.page, hero);
				await pickMirrorWorker(ctx.page, hero);
			}),
			act: (ctx) => sendFirstMessage(ctx, 'auto-claude-mirrored'),
			verify: verifyNewMirror
		},
		{
			name: 'new-branch',
			setup: settled(async (ctx) => {
				const { page } = ctx;
				const hero = await openNewChat(ctx);
				await pickProject(page, hero);
				await worktreeOn(page, hero);
				const menu = await openBranchMenu(page, hero);
				await menu.getByRole('button', { name: /New branch/ }).click();
				const branch = `journey/${uniq()}`;
				await page.getByPlaceholder('Enter a branch name').fill(branch);
				await page.getByRole('button', { name: 'Continue', exact: true }).click();
				await hero.getByRole('button', { name: `Worktree branch: ${branch}` }).waitFor({ timeout: 20_000 });
			}),
			act: (ctx) => sendFirstMessage(ctx, 'new-branch')
		},
		{
			name: 'existing-branch',
			setup: settled(async (ctx) => {
				const { page } = ctx;
				const hero = await openNewChat(ctx);
				await pickProject(page, hero);
				await worktreeOn(page, hero);
				const menu = await openBranchMenu(page, hero);
				await menu.getByRole('button', { name: SEEDED_BRANCH, exact: true }).click();
				await hero.getByRole('button', { name: `Worktree branch: ${SEEDED_BRANCH}` }).waitFor({ timeout: 20_000 });
			}),
			act: (ctx) => sendFirstMessage(ctx, 'existing-branch'),
			allow: [
				{
					kind: 'failed_requests',
					status: 409,
					url: /\/git\/(fetch|status)(\?|$)/,
					reason:
						'known bug #803 (existing-branch residual): an early background git fetch for the new worktree chat can still beat its checkout; seen intermittently on CI'
				},
				{
					kind: 'errors',
					message: /Failed to load git repository: No matching Git checkout exists/,
					reason: 'known bug #803 (existing-branch residual): the same early request, logged as a console error'
				}
			]
		},
		{
			name: 'main-checkout',
			setup: settled(async (ctx) => {
				const hero = await openNewChat(ctx);
				await pickProject(ctx.page, hero);
			}),
			act: (ctx) => sendFirstMessage(ctx, 'main-checkout')
		}
	],
	allow: [
		{
			kind: 'failed_requests',
			status: 403,
			url: /\/preferences(\/|$)/,
			reason: 'the preview cookie may not write preferences (devloop safety rule)'
		},
		{
			kind: 'failed_requests',
			status: 403,
			url: /\/model-providers\/refresh-capabilities(\?|$)/,
			reason: 'the preview cookie may not change providers (devloop safety rule)'
		},
		{
			kind: 'failed_requests',
			status: 502,
			url: /\/git\/fetch(\?|$)/,
			reason: 'background fetch: the seeded repository has no remote to fetch from'
		},
		{
			kind: 'failed_requests',
			status: 401,
			url: /\/github\/pulls(\?|$)/,
			reason: 'the dev instance has no GitHub sign-in'
		}
	]
});
