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
//   auto-attachment / new-branch-attachment  the same two flows with a file
//                   attached to the first message: it sends, the agent
//                   answers, and rewinding it brings the file back as a chip
//                   (#1084 review S1, S2)
//   main-checkout   control: the plain new-chat send in the main checkout
//                   → WebSocket query path
//   narrow-chips-*  Auto with long names in a 480/600px desktop chat column:
//                   the staged chip row stays usable and inside the composer,
//                   with the same geometry for Auto and the generated name
//   chip-sweep-*    the staged row at every width from a 480px composer to
//                   the widest desktop, held after Send: Auto and a short or
//                   long generated name give the same geometry at each width
//   chip-wide-ordinary  ordinary names at the widest and a ~640px composer,
//                   held after Send: one line, the same geometry for Auto and
//                   the generated name, and a ~25-character generated name is
//                   shown whole whenever the row has room for it
import { fakeAiTag, runJourney } from './runner.mjs';
import { pick, withChatSetup } from './lib/app.mjs';
import { readFile, access } from 'node:fs/promises';
import { join } from 'node:path';
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
	const hero = page.getByRole('region', { name: 'New chat' });
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
	await withChatSetup(page, async () => {
		// A sentence word on a wide screen, a labelled switch in Chat controls.
		await hero.getByRole('button', { name: /Switch to a worktree$/ }).or(hero.locator('.wt-chip')).click();
		await hero.getByRole('button', { name: /^Worktree branch:/ }).waitFor();
	});
}

/** The worktree's branch reads `name` on the hero (`name` may be a pattern). */
async function expectBranch(page, hero, name, options) {
	await withChatSetup(page, () => hero.getByRole('button', { name }).waitFor(options));
}

async function openBranchMenu(page, hero) {
	return openPopup(
		hero.getByRole('button', { name: /^Worktree branch:/ }),
		page.getByRole('dialog', { name: 'Worktree branch' }).or(hero.locator('.branch-dropdown'))
	);
}

/** Type the first message, send it and wait for the fake AI's reply to settle. */
async function sendFirstMessage(ctx, label) {
	const t0 = await typeAndSend(ctx, label);
	await awaitReply(ctx, label, t0);
}

/** Type the first message and press Send; returns when Send was pressed. */
async function typeAndSend({ page, variant, mark = async () => {} }, label) {
	const text = `${label} journey ${uniq()} ${fakeAiTag(variant)}`.trim();
	const box = page.getByRole('textbox', { name: 'Message' });
	await mark('type the first message');
	await box.click();
	await box.fill(text);
	await mark('send');
	const t0 = Date.now();
	await page.getByRole('button', { name: 'Send', exact: true }).click();
	return t0;
}

/** Wait for the bound session and the fake AI's settled reply. */
async function awaitReply({ page, log }, label, t0) {
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
	// With a project, Chat controls uses the runner chip's names for the same control.
	const menu = page.getByRole('menu', { name: /^(Where this session runs|Runner selection)$/ });
	await pick(hero.getByRole('button', { name: /^(Runs on |Runner: )/ }), menu,
		menu.getByRole('menuitemradio', { name: /Peer worker/ }),
		hero.getByRole('button', { name: /^(Runs on |Runner: )Peer worker$/ }));
}

async function verifyNewMirror({ page, recording }) {
	expectComposerPositions(recording, ['centre', 'bottom']);
	const seen = await page.evaluate(() => window.__newMirror);
	if (!seen?.emptyReplays || seen.syncPolls < 2 || !seen.targetedPeer) throw new Error(`mirror startup not exercised: ${JSON.stringify(seen)}`);
}

/** Names long enough that the staged chip row cannot fit a narrow composer. */
const LONG = {
	agent: 'Engineering agent for production',
	worker: 'Very long worker display name for production',
	repo: 'long-repository-name-council-scrim'
};

/** The names a typical setup shows: the dev agent and repository, a short worker. */
const ORDINARY = { agent: AGENT, worker: 'Unraid', repo: PROJECT };

/**
 * Agent, worker and repository names (`names`, long by default), a chosen
 * generated branch name, and a check of the staged chip row (Send until the
 * session binds).
 *
 * `window.__chipGeometry()` reads the row once: which branch label it shows
 * (`auto` before the name is generated, `generated` after), a signature of
 * every chip's box (x from the composer's left, width, line from the row's
 * top) and the row's height, and the rules each chip must meet: a usable
 * width, inside the composer, the element at its own centre (nothing covers
 * it), a few characters of its name showing, and no overlap with another chip.
 * It also reports how much of the branch label is cut off (`branchLabel`) and
 * the unused room at the end of the branch chip's line (`free`).
 * The branch label is the only thing Send changes in this row, so the two
 * signatures at one width must be the same.
 *
 * Once armed (`armed = true`, right before Send), every frame is checked and
 * its signature kept per label state. Generation waits until `autoFrames`
 * frames showed Auto, and the worktree POST until `generatedFrames` frames
 * showed the generated name, so neither state can be skipped. With
 * `manual: true` both wait for `release.generate` / `release.create` instead.
 */
async function chipProbe(page, branch, { names = LONG, autoFrames = 5, generatedFrames = 5, manual = false } = {}) {
	await page.addInitScript(([long, branch, opts]) => {
		const probe = { armed: false, sampling: true, counts: { auto: 0, generated: 0, other: 0 }, sigs: { auto: {}, generated: {}, other: {} }, problems: [], release: {} };
		window.__chipProbe = probe;
		const r = (v) => Math.round(v * 10) / 10;
		const geometry = () => {
			const bar = [...document.querySelectorAll('.context-pill-bar:not(.sheet-mode)')].find((el) => el.querySelector('.mode-bar-row'));
			const island = document.querySelector('.chat-island');
			const row = bar?.querySelector('.mode-bar-row');
			const trigger = bar?.querySelector('.branch-trigger');
			if (!bar || !island || !row || !trigger) return null;
			const text = trigger.textContent;
			const state = text.includes(branch) ? 'generated' : /Auto from|New branch from/.test(text) ? 'auto' : 'other';
			const clip = island.getBoundingClientRect();
			const rowBox = row.getBoundingClientRect();
			const chips = [
				['agent', '.selector-wrapper > button'],
				['runner', '.mode-trigger'],
				['repo', '.repo-chip'],
				['branch-slot', '.mode-bar-row .branch-selector'],
				['branch', '.branch-trigger'],
				['worktree', '.wt-chip']
			].map(([name, selector]) => ({ name, el: bar.querySelector(selector) })).filter((c) => c.el)
				.map((c) => ({ ...c, r: c.el.getBoundingClientRect() }));
			const sig = [`row h${r(rowBox.height)} w${r(rowBox.width)}`,
				...chips.map((c) => `${c.name} x${r(c.r.left - clip.left)} w${r(c.r.width)} y${r(c.r.top - rowBox.top)}`)].join(' | ');
			// The slot holds the branch button; the rules apply to the clickable chips.
			const clickable = chips.filter((c) => c.name !== 'branch-slot');
			const problems = [];
			for (const c of clickable) {
				const hit = document.elementFromPoint(c.r.left + c.r.width / 2, c.r.top + c.r.height / 2);
				const label = c.el.querySelector('.ctx-chip-label');
				if (c.r.width < 40) problems.push(`${c.name} is ${Math.round(c.r.width)}px wide`);
				if (c.r.left < clip.left - 0.5 || c.r.right > clip.right + 0.5) problems.push(`${c.name} leaves the composer`);
				if (!hit || !(hit === c.el || c.el.contains(hit))) problems.push(`${c.name} is covered at its centre`);
				if (label && label.clientWidth < Math.min(label.scrollWidth, 24) - 0.5) problems.push(`${c.name} shows ${label.clientWidth}px of its name`);
			}
			for (let i = 0; i < clickable.length; i++) for (let j = i + 1; j < clickable.length; j++) {
				const a = clickable[i].r, b = clickable[j].r;
				if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5)
					problems.push(`${clickable[i].name} overlaps ${clickable[j].name}`);
			}
			const inRow = chips.filter((c) => c.name !== 'agent' && c.name !== 'branch-slot');
			const lines = new Set(inRow.map((c) => Math.round(c.r.top - rowBox.top))).size;
			const branchBox = trigger.getBoundingClientRect();
			const onBranchLine = inRow.filter((c) => Math.abs(c.r.top - branchBox.top) < 1);
			const free = r(rowBox.right - Math.max(...onBranchLine.map((c) => c.r.right)));
			const labelEl = trigger.querySelector('.branch-name');
			const branchLabel = labelEl ? { scroll: labelEl.scrollWidth, client: labelEl.clientWidth, text: labelEl.textContent.trim() } : null;
			return { state, composer: r(clip.width), height: r(rowBox.height), lines, sig, problems, free, branchLabel };
		};
		window.__chipGeometry = geometry;
		const check = () => {
			const g = probe.armed && probe.sampling ? geometry() : null;
			if (g) {
				probe.counts[g.state]++;
				probe.sigs[g.state][g.sig] = (probe.sigs[g.state][g.sig] || 0) + 1;
				for (const p of g.problems) if (!probe.problems.includes(p)) probe.problems.push(p);
			}
			requestAnimationFrame(check);
		};
		requestAnimationFrame(check);

		const until = async (ready) => {
			const deadline = Date.now() + (opts.manual ? 180_000 : 20_000);
			while (!ready() && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 16));
		};
		const original = window.fetch.bind(window);
		const json = (body, response) => new Response(JSON.stringify(body), {
			status: response?.status || 200,
			headers: response?.headers || { 'content-type': 'application/json' }
		});
		window.fetch = async (input, init) => {
			const method = (init?.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (method === 'POST' && url.pathname.endsWith('/git/generate-branch-name')) {
				await until(() => probe.release.generate || (!opts.manual && probe.counts.auto >= opts.autoFrames));
				return json({ branch_name: branch });
			}
			if (method === 'POST' && url.pathname === '/api/v1/sessions/with-worktree') {
				await until(() => probe.release.create || (!opts.manual && probe.counts.generated >= opts.generatedFrames));
			}
			const response = await original(input, init);
			if (method !== 'GET' || !response.ok || !(response.headers.get('content-type') || '').includes('application/json')) return response;
			let text = await response.text();
			text = text.replaceAll('"Dev agent"', JSON.stringify(long.agent)).replaceAll('"Dev sandbox"', JSON.stringify(long.repo));
			let body = JSON.parse(text);
			for (const row of body?.installs || []) if (row.is_self) row.device_name = long.worker;
			return json(body, response);
		};
	}, [names, branch, { autoFrames, generatedFrames, manual }]);
}

/** Stage a new Auto worktree chat with `names`; the window is `width` wide. */
async function stageNames(ctx, width, names = LONG) {
	const { page } = ctx;
	await page.setViewportSize({ width, height: 900 });
	const hero = await openNewChat(ctx, names.agent);
	const item = page.getByRole('button', { name: names.repo, exact: true });
	await pick(hero.getByRole('button', { name: /^No project/ }), item, item, hero.getByRole('button', { name: `Workspace folders: ${names.repo}` }));
	await worktreeOn(page, hero);
}

/** A desktop window whose chat column (beside the open sidebar) is `chat` px wide. */
/**
 * Run a branch pick and fail if the app asks for the Git state of a checkout
 * that only the first send will create (#949). The recorder starts after a
 * case's setup, so it cannot see this request.
 */
async function withoutCheckoutRefusal(page, pick) {
	const refused = [];
	const listen = (response) => {
		if (response.status() === 409 && /\/git\/status(\?|$)/.test(response.url())) refused.push(response.url());
	};
	page.on('response', listen);
	try {
		await pick();
		// The header reloads its Git state just after the chip changes.
		await page.waitForTimeout(1500);
	} finally {
		page.off('response', listen);
	}
	if (refused.length) throw new Error(`a staged branch's Git status was refused ${refused.length}x: ${refused[0]}`);
	if (await page.getByText('Git unavailable').first().isVisible()) throw new Error('the header says Git unavailable for a staged branch');
}

function narrowChips(chat) {
	return {
		name: `narrow-chips-${chat}`,
		viewports: ['desktop'],
		throttles: ['fast'],
		// At 480 the window is narrow enough that the open sidebar is a large share
		// of it: the first send's new row measured 0.046-0.054 there. Only that one
		// sidebar move is excused; every other shift is still judged.
		...(chat === 480 ? { expectShift: [{
			step: 'send',
			moved: /^div\.scard-frame$/,
			maxValue: 0.06,
			reason: 'the first send adds this chat to the open sidebar, pushing the rows below it down'
		}] } : {}),
		setup: settled(async (ctx) => {
			await chipProbe(ctx.page, `fix/long-staged-branch-generated-from-message-${uniq()}`);
			await stageNames(ctx, chat + 320);
			const column = await ctx.page.evaluate(() => Math.round(document.querySelector('[data-ui-state="chat"]').getBoundingClientRect().width));
			if (Math.abs(column - chat) > 24) throw new Error(`chat column is ${column}px, wanted about ${chat}px (sidebar closed?)`);
		}),
		act: async (ctx) => {
			await ctx.page.evaluate(() => { window.__chipProbe.armed = true; });
			await sendFirstMessage(ctx, `narrow-chips-${chat}`);
		},
		verify: async ({ page }) => {
			const probe = await page.evaluate(() => window.__chipProbe);
			if (probe.counts.auto < 5) throw new Error(`the staged row showing Auto was checked in ${probe.counts.auto} frames`);
			if (probe.counts.generated < 5) throw new Error(`the staged row with the generated branch was checked in ${probe.counts.generated} frames`);
			if (probe.problems.length) throw new Error(`staged chip row: ${probe.problems.join('; ')}`);
			const auto = Object.keys(probe.sigs.auto);
			const generated = Object.keys(probe.sigs.generated);
			if (auto.length !== 1 || generated.length !== 1 || auto[0] !== generated[0]) {
				throw new Error(`staged chip row geometry changed across Send:\n  Auto: ${auto.join('\n        ')}\n  generated: ${generated.join('\n             ')}`);
			}
			// Once bound, the same long names sit in the session's own chip row (#1228).
			await page.locator('.context-pill-bar:not(.sheet-mode) .ctx-chip.runner').waitFor({ timeout: 30000 });
			await page.waitForTimeout(800);
			const bound = await page.evaluate(() => {
				const bar = document.querySelector('.context-pill-bar:not(.sheet-mode)');
				const clip = document.querySelector('.chat-island').getBoundingClientRect();
				const out = [];
				const chips = [...bar.querySelectorAll('.ctx-chip')].filter((el) => el.offsetParent && !el.closest('[role="menu"]'));
				for (const el of chips) {
					const r = el.getBoundingClientRect();
					const name = el.textContent.trim().slice(0, 20);
					const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
					if (r.width < 40) out.push(`${name} is ${Math.round(r.width)}px wide`);
					if (r.left < clip.left - 0.5 || r.right > clip.right + 0.5) out.push(`${name} leaves the composer`);
					if (!hit || !(hit === el || el.contains(hit))) out.push(`${name} is covered at its centre by ${hit?.tagName}.${hit?.className}`);
				}
				const tops = new Set(chips.map((el) => Math.round(el.getBoundingClientRect().top)));
				if (tops.size > 1) out.push(`the row wrapped onto ${tops.size} lines`);
				return { out, count: chips.length, widths: chips.map((el) => `${el.className.toString().replace(/svelte-\w+/g, '').trim()}:${Math.round(el.getBoundingClientRect().left)}+${Math.round(el.getBoundingClientRect().width)}`) };
			});
			if (bound.count < 3) throw new Error(`the bound row showed ${bound.count} chips`);
			if (bound.out.length) throw new Error(`bound chip row: ${bound.out.join('; ')} (widths ${bound.widths.join(', ')})`);
		}
	};
}

/** Window widths for the sweep: every 4px through the band where the composer
 *  changes width beside the open sidebar, then common wide desktops. */
const SWEEP_WINDOWS = [...Array.from({ length: 111 }, (_, i) => 760 + i * 4), 1280, 1366, 1440, 1680, 1920];

/** Resize the window and read the row once its layout holds still. */
async function chipGeometryAt(page, width) {
	await page.setViewportSize({ width, height: 900 });
	let last = null;
	for (let attempt = 0; attempt < 10; attempt++) {
		const g = await page.evaluate(() => new Promise((resolve) =>
			requestAnimationFrame(() => requestAnimationFrame(() => resolve(window.__chipGeometry())))));
		if (g && last && g.sig === last.sig) return g;
		last = g;
	}
	throw new Error(`the staged chip row at a ${width}px window never held still: ${JSON.stringify(last)}`);
}

/**
 * Boundary sweep: one staged row, held after Send, measured at every width in
 * SWEEP_WINDOWS showing Auto, then again showing the generated `branch`. At
 * each width both must give the same geometry for every chip and meet the
 * chip rules; the widest composer keeps one line. Setup sends and holds the
 * session (resizing is not a user journey and stays out of the recording);
 * `act` lets the worktree POST go and records the bind and the reply.
 */
function chipSweep(name, branch) {
	const label = `chip-sweep-${name}`;
	return {
		name: label,
		viewports: ['desktop'],
		throttles: ['fast'],
		setup: async (ctx) => {
			const { page } = ctx;
			await chipProbe(page, branch, { manual: true });
			await stageNames(ctx, 1440);
			await page.waitForTimeout(1000);
			await page.evaluate(() => { window.__chipProbe.armed = true; window.__chipProbe.sampling = false; });
			await typeAndSend(ctx, label);
			const sweep = async (state) => {
				await page.waitForFunction((s) => window.__chipGeometry()?.state === s, state, { timeout: 30_000 });
				const out = [];
				for (const width of SWEEP_WINDOWS) {
					const g = await chipGeometryAt(page, width);
					if (g.state !== state) throw new Error(`at a ${width}px window the branch label was ${g.state}, wanted ${state}`);
					out.push({ width, ...g });
				}
				return out;
			};
			const auto = await sweep('auto');
			await page.evaluate(() => { window.__chipProbe.release.generate = true; });
			const generated = await sweep('generated');
			const failures = [];
			for (let i = 0; i < SWEEP_WINDOWS.length; i++) {
				const a = auto[i], g = generated[i];
				const where = `${a.width}px window (${a.composer}px composer)`;
				if (a.sig !== g.sig) failures.push(`${where}: geometry changed across Send\n    Auto:      ${a.sig}\n    generated: ${g.sig}`);
				for (const [state, s] of [['Auto', a], ['generated', g]]) if (s.problems.length) failures.push(`${where}, ${state}: ${s.problems.join('; ')}`);
			}
			const composers = auto.map((a) => a.composer);
			const narrowest = Math.min(...composers), widest = Math.max(...composers);
			if (narrowest > 482) failures.push(`the sweep never reached a 480px composer (narrowest ${narrowest}px)`);
			// Dense coverage: no composer width from 480px to the widest is skipped by more than 8px.
			const covered = [...new Set(composers)].filter((w) => w >= 480).sort((x, y) => x - y);
			const gap = Math.max(0, ...covered.slice(1).map((w, i) => w - covered[i]));
			if (gap > 8) failures.push(`the sweep skipped a ${gap}px band of composer widths`);
			const wide = auto.filter((a) => a.composer === widest);
			for (const a of wide) if (a.lines !== 1) failures.push(`${a.width}px window (${a.composer}px composer, the widest): the row takes ${a.lines} lines: ${a.sig}`);
			const heights = [...new Set(auto.map((a) => a.height))].sort((x, y) => x - y);
			const summary = (`sweep ${label}: ${SWEEP_WINDOWS.length} widths, ${covered.length} composer widths ${narrowest}-${widest}px (largest gap ${gap}px), row heights ${heights.join('/')}px, ${failures.length} failures`);
			ctx.log(summary);
			console.log(summary);
			if (failures.length) throw new Error(`chip sweep ${label} (${failures.length} failures):\n  ${failures.slice(0, 12).join('\n  ')}`);
			await page.setViewportSize({ width: 1440, height: 900 });
			await page.waitForTimeout(500);
		},
		act: async (ctx) => {
			await ctx.page.evaluate(() => { window.__chipProbe.release.create = true; });
			await awaitReply(ctx, label, Date.now());
		}
	};
}

/**
 * Ordinary names at wide composers: the widest desktop and one about 640px
 * wide. The long-name sweeps squeeze the branch chip below its cap, so they
 * cannot tell whether the cap itself leaves an ordinary generated name room.
 * The row is held after Send (the staged row; before Send the composer shows
 * the sentence row instead) and read at each width showing Auto, then the
 * generated `branch`: one line, identical geometry in both, the chip rules
 * met, and the generated name shown whole whenever its line has unused room
 * (always, at the widest composer).
 */
function chipWideOrdinary(branch) {
	const label = 'chip-wide-ordinary';
	return {
		name: label,
		viewports: ['desktop'],
		throttles: ['fast'],
		setup: async (ctx) => {
			const { page } = ctx;
			await chipProbe(page, branch, { names: ORDINARY, manual: true });
			await stageNames(ctx, 1440, ORDINARY);
			await page.waitForTimeout(1000);
			await page.evaluate(() => { window.__chipProbe.armed = true; window.__chipProbe.sampling = false; });
			await typeAndSend(ctx, label);
			await page.waitForFunction(() => window.__chipGeometry()?.state === 'auto', null, { timeout: 30_000 });
			// The window whose composer is closest to 640px; the composer only
			// widens with the window, so halve the 760-1440px range.
			let lo = 760, hi = 1440;
			while (hi - lo > 4) {
				const mid = Math.round((lo + hi) / 8) * 4;
				if ((await chipGeometryAt(page, mid)).composer < 640) lo = mid; else hi = mid;
			}
			const near = [];
			for (const w of [lo, hi]) near.push({ w, composer: (await chipGeometryAt(page, w)).composer });
			near.sort((a, b) => Math.abs(a.composer - 640) - Math.abs(b.composer - 640));
			const windows = [1440, 1920, near[0].w];
			const read = async (state) => {
				await page.waitForFunction((s) => window.__chipGeometry()?.state === s, state, { timeout: 30_000 });
				const out = [];
				for (const width of windows) {
					const g = await chipGeometryAt(page, width);
					if (g.state !== state) throw new Error(`at a ${width}px window the branch label was ${g.state}, wanted ${state}`);
					out.push({ width, ...g });
				}
				return out;
			};
			const auto = await read('auto');
			await page.evaluate(() => { window.__chipProbe.release.generate = true; });
			const generated = await read('generated');
			const widest = Math.max(...auto.map((a) => a.composer));
			const failures = [];
			if (widest < 735) failures.push(`the widest composer measured was ${widest}px`);
			if (Math.abs(auto[2].composer - 640) > 8) failures.push(`the mid width reached a ${auto[2].composer}px composer, wanted about 640px`);
			for (let i = 0; i < windows.length; i++) {
				const a = auto[i], g = generated[i];
				const where = `${a.width}px window (${a.composer}px composer)`;
				if (a.sig !== g.sig) failures.push(`${where}: geometry changed across Send\n    Auto:      ${a.sig}\n    generated: ${g.sig}`);
				for (const [state, s] of [['Auto', a], ['generated', g]]) {
					if (s.problems.length) failures.push(`${where}, ${state}: ${s.problems.join('; ')}`);
					if (s.lines !== 1) failures.push(`${where}, ${state}: the row takes ${s.lines} lines: ${s.sig}`);
				}
				const lbl = g.branchLabel;
				if (!lbl || !lbl.text.includes(branch)) failures.push(`${where}: the branch label reads ${JSON.stringify(lbl?.text)}, wanted ${branch}`);
				else if (lbl.scroll > lbl.client) {
					const cut = `the branch label shows ${lbl.client}px of its ${lbl.scroll}px name`;
					if (g.composer === widest) failures.push(`${where}, the widest: ${cut}`);
					else if (g.free > 1) failures.push(`${where}: ${cut} with ${g.free}px unused on its line`);
				}
			}
			const summary = `${label}: ${windows.map((w, i) => `${w}px window → ${generated[i].composer}px composer, label ${generated[i].branchLabel?.client}/${generated[i].branchLabel?.scroll}px, ${generated[i].free}px free`).join('; ')}; ${failures.length} failures`;
			ctx.log(summary);
			console.log(summary);
			if (failures.length) throw new Error(`${label} (${failures.length} failures):\n  ${failures.join('\n  ')}`);
			await page.setViewportSize({ width: 1440, height: 900 });
			await page.waitForTimeout(500);
		},
		act: async (ctx) => {
			await ctx.page.evaluate(() => { window.__chipProbe.release.create = true; });
			await awaitReply(ctx, label, Date.now());
		}
	};
}

/**
 * Let the staged composer settle before recording, the way a user's typing
 * would: the picks start their own loads (branches, git status of the staged
 * branch) and those belong to setup, not to the send the step judges.
 */
/**
 * Attach a file to the first message, send it, and wait for the reply. Then
 * rewind it: the words and the file come back, the file as a chip, never as
 * its upload path (#1084).
 */
async function sendWithAttachmentThenRewind(ctx, label) {
	const { page, variant, mark } = ctx;
	const words = `${label}-${uniq()}`;
	const file = `note-${uniq()}.txt`;
	const box = page.getByRole('textbox', { name: 'Message' });
	// Words first: Auto names the branch from the message, so it starts with
	// this run's own words, not the upload path every attachment shares.
	await mark('type the first message');
	await box.click();
	await box.pressSequentially(`${words} ${fakeAiTag(variant)} `.trimStart());
	await mark('attach a file');
	await page.locator('input[type="file"]').first().setInputFiles({ name: file, mimeType: 'text/plain', buffer: Buffer.from('branch attachment\n') });
	await box.locator('[data-file-chip]').first().waitFor({ timeout: 20_000 });
	if ((await box.locator('[data-file-chip]').count()) !== 1) throw new Error('the chip left the composer before Send');
	await mark('send');
	const t0 = Date.now();
	await page.getByRole('button', { name: 'Send', exact: true }).click();
	await awaitReply(ctx, label, t0);
	if (await page.getByText(/Attachments need|outside (the|this) (chat|session)|not allowed/i).first().isVisible()) {
		throw new Error('the server refused the attachment');
	}
	await mark('rewind the first message');
	await page.getByTitle('Rewind to before this message', { exact: true }).last().focus();
	await page.keyboard.press('Enter');
	await page.getByRole('dialog').getByRole('button', { name: 'Rewind', exact: true }).click();
	await box.getByText(words).waitFor({ timeout: 30_000 });
	await box.locator('[data-file-chip]').first().waitFor({ timeout: 10_000 });
	const chips = await box.locator('[data-file-chip]').count();
	const text = await box.innerText();
	if (chips !== 1) throw new Error(`expected the file back as 1 chip, found ${chips}: ${JSON.stringify(text)}`);
	if (/@\/\S*uploads\//.test(text)) throw new Error(`the upload path came back as text: ${JSON.stringify(text)}`);
}

/** An attachment after the first reply belongs to the checkout, not main (#1329). */
async function attachInExistingWorktree(ctx) {
	const { page, mark, log, status } = ctx;
	const createdResponse = page.waitForResponse((r) => r.url().includes('/sessions/with-worktree') && r.request().method() === 'POST');
	await sendFirstMessage(ctx, 'worktree-upload');
	const created = await (await createdResponse).json();
	if (!created.worktree_path) throw new Error('new chat did not create a local worktree');
	const file = `after-reply-${uniq()}.txt`;
	await mark('attach after the first reply');
	const uploadedResponse = page.waitForResponse((r) => r.url().includes('/upload') && r.request().method() === 'POST');
	await page.locator('input[type="file"]').first().setInputFiles({ name: file, mimeType: 'text/plain', buffer: Buffer.from('worktree attachment\n') });
	const uploaded = await (await uploadedResponse).json();
	const expected = join(created.worktree_path, 'uploads', file);
	if (uploaded.full_path !== expected) throw new Error(`attachment landed outside the chat checkout: ${uploaded.full_path}; expected ${expected}`);
	if (await readFile(expected, 'utf8') !== 'worktree attachment\n') throw new Error('attachment bytes differ');
	const inMain = await access(join(status.seed.projects['dev-sandbox'], 'uploads', file)).then(() => true, () => false);
	if (inMain) throw new Error('attachment also polluted main');
	await page.getByRole('textbox', { name: 'Message' }).locator('[data-file-chip]').waitFor();
	await mark('send worktree attachment');
	await page.getByRole('button', { name: 'Send', exact: true }).click();
	await page.waitForFunction(() => Number(document.querySelector('[data-ui-state="chat"]')?.getAttribute('data-ui-messages')) >= 4 && document.querySelector('[data-ui-state="chat"]')?.getAttribute('data-ui-streaming') === 'false', null, { timeout: 60_000 });
	if (!(await page.locator('[data-ui-state="chat"]').innerText()).includes(expected)) throw new Error('sent prompt does not name the chat checkout');
	await mark('rewind the worktree attachment');
	await page.getByTitle('Rewind to before this message', { exact: true }).last().focus();
	await page.keyboard.press('Enter');
	await page.getByRole('dialog').getByRole('button', { name: 'Rewind', exact: true }).click();
	const restored = page.getByRole('textbox', { name: 'Message' }).locator('[data-file-chip]');
	await restored.waitFor({ timeout: 30_000 });
	if (!(await restored.innerText()).includes(file)) throw new Error('rewind lost the worktree attachment chip');
	await mark('resend the restored worktree attachment');
	await page.getByRole('button', { name: 'Send', exact: true }).click();
	await page.waitForFunction(() => Number(document.querySelector('[data-ui-state="chat"]')?.getAttribute('data-ui-messages')) >= 4 && document.querySelector('[data-ui-state="chat"]')?.getAttribute('data-ui-streaming') === 'false', null, { timeout: 60_000 });
	if (!(await page.locator('[data-ui-state="chat"]').innerText()).includes(expected)) throw new Error('restored send lost the worktree attachment path');
	log('upload bytes, sent prompt and restored resend use the worktree; main has no copy');
}

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
				await expectBranch(ctx.page, hero, /^Worktree branch: (a new branch from|Auto from)/);
			}),
			act: (ctx) => sendFirstMessage(ctx, 'auto')
		},
		{
			name: 'auto-claude',
			setup: settled(async (ctx) => {
				const hero = await openNewChat(ctx, CLAUDE_AGENT);
				await pickProject(ctx.page, hero);
				await worktreeOn(ctx.page, hero);
				await expectBranch(ctx.page, hero, /^Worktree branch: (a new branch from|Auto from)/);
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
				await withChatSetup(page, async () => {
					const menu = await openBranchMenu(page, hero);
					await menu.getByRole('button', { name: /New branch/ }).click();
					const branch = `journey/${uniq()}`;
					await page.getByPlaceholder('Enter a branch name').fill(branch);
					await withoutCheckoutRefusal(page, async () => {
						await page.getByRole('button', { name: 'Continue', exact: true }).click();
						await expectBranch(page, hero, `Worktree branch: ${branch}`, { timeout: 20_000 });
					});
				});
			}),
			act: (ctx) => sendFirstMessage(ctx, 'new-branch')
		},
		{
			name: 'existing-worktree-attachment',
			viewports: ['desktop', 'phone'],
			setup: settled(async (ctx) => {
				const hero = await openNewChat(ctx);
				await pickProject(ctx.page, hero);
				await worktreeOn(ctx.page, hero);
			}),
			act: attachInExistingWorktree
		},
		{
			name: 'auto-attachment',
			viewports: ['desktop', 'phone'],
			setup: settled(async (ctx) => {
				const hero = await openNewChat(ctx);
				await pickProject(ctx.page, hero);
				await worktreeOn(ctx.page, hero);
				await expectBranch(ctx.page, hero, /^Worktree branch: (a new branch from|Auto from)/);
			}),
			act: (ctx) => sendWithAttachmentThenRewind(ctx, 'auto-attachment')
		},
		{
			name: 'new-branch-attachment',
			viewports: ['desktop', 'phone'],
			setup: settled(async (ctx) => {
				const { page } = ctx;
				const hero = await openNewChat(ctx);
				await pickProject(page, hero);
				await worktreeOn(page, hero);
				await withChatSetup(page, async () => {
					const menu = await openBranchMenu(page, hero);
					await menu.getByRole('button', { name: /New branch/ }).click();
					const branch = `journey/${uniq()}`;
					await page.getByPlaceholder('Enter a branch name').fill(branch);
					await page.getByRole('button', { name: 'Continue', exact: true }).click();
					await expectBranch(page, hero, `Worktree branch: ${branch}`, { timeout: 20_000 });
				});
			}),
			act: (ctx) => sendWithAttachmentThenRewind(ctx, 'new-branch-attachment')
		},
		{
			name: 'existing-branch',
			setup: settled(async (ctx) => {
				const { page } = ctx;
				const hero = await openNewChat(ctx);
				await pickProject(page, hero);
				await worktreeOn(page, hero);
				await withChatSetup(page, async () => {
					const menu = await openBranchMenu(page, hero);
					await withoutCheckoutRefusal(page, async () => {
						await menu.getByRole('button', { name: SEEDED_BRANCH, exact: true }).click();
						await expectBranch(page, hero, `Worktree branch: ${SEEDED_BRANCH}`, { timeout: 20_000 });
					});
				});
			}),
			act: (ctx) => sendFirstMessage(ctx, 'existing-branch')
		},
		{
			name: 'main-checkout',
			setup: settled(async (ctx) => {
				const hero = await openNewChat(ctx);
				await pickProject(ctx.page, hero);
			}),
			act: (ctx) => sendFirstMessage(ctx, 'main-checkout')
		},
		// The staged chip row in a narrow desktop composer, with long names: it
		// may wrap, but no chip collapses, overlaps another or leaves the box,
		// and Auto and the generated name give the same geometry.
		narrowChips(480),
		narrowChips(600),
		// Every width from a 480px composer to the widest desktop, with a short
		// and a long generated name: the row's lines never follow the name.
		chipSweep('short', 'fix/a'),
		chipSweep('long', 'fix/long-staged-branch-generated-from-message-abcdef'),
		// Ordinary names with room to spare: a typical generated name must not
		// be cut off by the branch chip's cap.
		chipWideOrdinary('fix/composer-layout-shift')
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
