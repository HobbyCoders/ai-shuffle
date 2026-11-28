// Regression journeys for the mobile scroll/composer fix chain. Geometry is
// sampled after layout on every painted frame: transforms and a down/up bounce
// do not necessarily create a LayoutShift entry. All replies/queues/errors are
// produced by real UI sends through the devloop's fake providers; the long
// error/Stop case specifically requires the seeded Claude Code CLI project.
import { writeFileSync } from 'node:fs';
import { ERROR_ROW_PROBE, PROJECT, SEEDED, api, chatIdByToken, composer, errorRow, expect, gotoApp, openFromSidebar, openNewChat, openPopup, pick, pickAccess, pickAgent, pickProject, scriptTag, seededChatId, sendButton, sidebarRow, typeAndSend, uniq, waitChat, waitChatOpen, waitSeated, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const mobile = process.argv.includes('--mobile-shell');
const LOG = '[role="log"][aria-label="Conversation messages"]';
const prefix = (variant) => mobile && variant.viewport === 'phone' ? '/mobile/' : '/';

/** IO entries are the browser's post-layout geometry, not an rAF read before
 * the app's own scroll callback. Keep chronological order at 4x CPU too. */
async function watchFrames(page, { deferAssertions = false } = {}) {
	await page.evaluate(({ deferAssertions, articleDiagnostics, errorRowProbe }) => {
		const viewport = document.querySelector('[role="log"][aria-label="Conversation messages"]');
		const pickAnchor = () => [...document.querySelectorAll('[data-transcript-key]')].find(el => el.getBoundingClientRect().bottom > (viewport?.getBoundingClientRect().top ?? 0) + 1);
		let anchor = pickAnchor();
		const watch = window.__chatFrames = { running: true, next: 0, frames: [], shifts: [], phase: 'before-stop', assertions: !deferAssertions };
		// Early diagnostics must not change which visible row the original
		// post-scroll assertion window protects.
		watch.beginAssertions = () => { anchor = pickAnchor(); watch.assertions = true; };
		const nodes = new WeakMap();
		let nextNode = 0;
		const textProbes = [];
		const nodeId = el => {
			if (!nodes.has(el)) nodes.set(el, nextNode++);
			return nodes.get(el);
		};
		watch.shiftObserver = new PerformanceObserver(list => {
			for (const entry of list.getEntries()) watch.shifts.push({ at: entry.startTime, value: entry.value, hadRecentInput: entry.hadRecentInput, lastInputTime: entry.lastInputTime,
				sources: entry.sources.map(source => ({ node: source.node ? nodeId(source.node) : null, key: (source.node?.nodeType === Node.ELEMENT_NODE ? source.node : source.node?.parentElement)?.closest('[data-transcript-key]')?.dataset.transcriptKey, html: source.node?.outerHTML?.slice(0, 500), previous: source.previousRect, current: source.currentRect })) });
		});
		watch.shiftObserver.observe({ type: 'layout-shift' });
		const sample = () => {
			if (!watch.running) return;
			const order = watch.next++;
			const log = document.querySelector('[role="log"][aria-label="Conversation messages"]');
			const prose = [...document.querySelectorAll('.chat-column .prose')].at(-1);
			const elements = {
				composer: document.querySelector('.input-slot'), rail: document.querySelector('nav.turn-nav'),
				send: document.querySelector('.send-cluster'), prose, anchor,
				banner: document.querySelector('.chat-error-banner'), errorRow: [...document.querySelectorAll(errorRowProbe.rows)].filter(d => [...d.querySelectorAll(':scope > summary > span')].some(s => s.textContent.trim() === errorRowProbe.title)).at(-1), pending: document.querySelector('.pending-messages .userrow:last-child'),
				tailControls: document.querySelector('.tail-controls'), pendingList: document.querySelector('.pending-messages'),
				tailError: document.querySelector('.tail-error'), tailSpacer: document.querySelector('.transcript-tail-spacer'),
			};
			// Content identifies a prompt across queue -> transcript delivery; nodeId
			// distinguishes a retained keyed row from a remount with the same text.
			const rows = [...document.querySelectorAll('.pending-messages .userrow, .chat-column > .msg-virtualized')].map(el => ({
				el, node: nodeId(el), key: el.dataset.transcriptKey ?? null,
				pending: el.matches('.pending-messages .userrow'), status: el.dataset.queueStatus ?? null,
				text: (el.querySelector('.bubble-text') ?? el).textContent.slice(0, 160),
				bubble: el.querySelector('.bubble'), actions: el.querySelector('.pending-actions'),
				article: articleDiagnostics ? el.querySelector('.message-container') : null,
				articleNode: articleDiagnostics && el.querySelector('.message-container') ? nodeId(el.querySelector('.message-container')) : null
			})).filter((row, i, all) => row.pending || i >= all.length - 4);
			if (articleDiagnostics && prose && log) {
				const last = textProbes.at(-1);
				const rect = last?.range.getBoundingClientRect();
				const box = log.getBoundingClientRect();
				if (!last?.textNode.isConnected || !rect?.height || rect.top < box.top || rect.bottom > box.bottom) {
					const caret = document.caretRangeFromPoint(prose.getBoundingClientRect().left + 40, box.top + box.height / 2);
					if (caret?.startContainer.nodeType === Node.TEXT_NODE && prose.contains(caret.startContainer)) {
						const textNode = caret.startContainer;
						const range = caret.cloneRange();
						range.setEnd(textNode, Math.min(textNode.length, range.startOffset + 24));
						textProbes.push({ range, textNode, node: nodeId(textNode), text: range.toString() });
						if (textProbes.length > 8) textProbes.shift();
					}
				}
			}
			const frame = { order, at: performance.now(), assertion: watch.assertions, streaming: document.querySelector('[data-ui-state="chat"]')?.dataset.uiStreaming,
				proseKey: prose?.closest('[data-transcript-key]')?.dataset.transcriptKey,
				chars: prose?.textContent.length ?? 0, scroll: log?.scrollTop, height: log?.scrollHeight, viewport: log?.clientHeight,
				tail: document.querySelector('.transcript-tail-spacer')?.getBoundingClientRect().height,
				activity: !!document.querySelector('.turn-activity') };
			const observer = new IntersectionObserver(entries => {
				observer.disconnect();
				const geometry = el => {
					const rect = entries.find(entry => entry.target === el)?.boundingClientRect;
					return rect?.height > 0 ? { top: rect.top, bottom: rect.bottom, height: rect.height, left: rect.left, width: rect.width } : null;
				};
				for (const [key, el] of Object.entries(elements)) frame[key] = geometry(el);
				frame.rows = rows.map(({ el, bubble, actions, article, ...identity }) => ({ ...identity, rect: geometry(el), bubble: geometry(bubble), actions: geometry(actions), article: geometry(article) }));
				if (articleDiagnostics) frame.textRanges = textProbes.map(({ range, textNode, node, text }) => {
					const rect = range.getBoundingClientRect();
					return { node, text, connected: textNode.isConnected, at: performance.now(), rect: { top: rect.top, bottom: rect.bottom, left: rect.left, width: rect.width, height: rect.height } };
				});
				frame.paintedAt = entries[0]?.time;
				frame.deliveredAt = performance.now();
				frame.phase = watch.phase;
				watch.frames.push(frame);
			});
			for (const el of [...Object.values(elements), ...rows.flatMap(row => [row.el, row.bubble, row.actions, row.article])].filter(Boolean)) observer.observe(el);
			requestAnimationFrame(sample);
		};
		requestAnimationFrame(sample);
	}, { deferAssertions, articleDiagnostics: process.env.JOURNEY_TRACE_ARTICLE === '1', errorRowProbe: ERROR_ROW_PROBE });
	// Include the resting frame before the action, not just its aftermath.
	await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
	return async () => {
		const frames = await page.evaluate(() => {
			window.__chatFrames.running = false;
			window.__chatFrames.shiftObserver.disconnect();
			return window.__chatFrames.frames.sort((a, b) => a.order - b.order);
		});
		if (process.env.JOURNEY_OUT) {
			writeFileSync(`${process.env.JOURNEY_OUT}/frames-${Date.now()}.json`, JSON.stringify(frames));
			writeFileSync(`${process.env.JOURNEY_OUT}/shifts-${Date.now()}.json`, JSON.stringify(await page.evaluate(() => window.__chatFrames.shifts)));
		}
		return frames.filter(frame => frame.assertion);
	};
}
const travel = (frames, key, edge = 'top') => {
	const positions = frames.filter(f => f[key]).map(f => f[key][edge]);
	return positions.length ? Math.max(...positions) - Math.min(...positions) : Infinity;
};
const summary = (frames) => ({ frames: frames.length, composer: travel(frames, 'composer'), rail: travel(frames, 'rail'),
	prose: travel(frames, 'prose'), banner: travel(frames, 'banner'), errorRow: travel(frames, 'errorRow'), pending: travel(frames, 'pending'),
	heights: [...new Set(frames.map(f => f.height))], tail: [...new Set(frames.map(f => f.tail))] });

async function openClaude(ctx, { history = false } = {}) {
	const { page, status, variant } = ctx;
	const id = status.seed?.claude_cli?.chat;
	expect(id, 'devloop must seed the fake CLI project chat');
	if (history) {
		await gotoApp(page, status, `${prefix(variant)}?session=${id}`);
		await waitChat(page, { loading: 'false' });
		// Only disposable, journey-produced data in this seeded CLI chat. Keep
		// the reproduction at eight long turns rather than accumulating across runs.
		await api(page, 'POST', `/sessions/${id}/clear`, {});
		await waitChat(page, { surface: 'cleared', messagesAtMost: 0 });
	} else {
		await gotoApp(page, status, prefix(variant));
		await waitChat(page, { surface: 'new', loading: 'false' });
		const hero = page.getByRole('region', { name: 'New chat' });
		const menu = page.getByRole('menu', { name: 'Agent selection' });
		await pick(hero.getByRole('button', { name: /^Agent:/ }), menu,
			menu.getByRole('button', { name: 'Dev (Claude Code)', exact: true }),
			hero.getByRole('button', { name: 'Agent: Dev (Claude Code)' }));
		await pickProject(page, hero);
	}
	await composer(page).waitFor();
	// The baseline seed starts empty. Build long history by actually sending to
	// the fake CLI, not by stuffing the reducer/DB with consumer-side fixtures.
	if (history) {
		for (let turn = 0; turn < 8; turn++) {
			const count = Number(await page.locator('[data-ui-state="chat"]').getAttribute('data-ui-messages'));
			await typeAndSend(page, `history ${uniq()} [script:long tps=2000]`);
			await waitChat(page, { streaming: 'true' });
			await waitTurnSettled(page, { messagesAtLeast: count + 2, hold: 200 });
		}
		await page.waitForTimeout(1600);
		const geometry = await page.locator(LOG).evaluate(el => ({ height: el.scrollHeight, messages: document.querySelector('[data-ui-state="chat"]').dataset.uiMessages, userTurns: document.querySelectorAll('[data-user-turn-id]').length, proseAnswers: document.querySelectorAll('.msg-virtualized .prose').length }));
		ctx.log(`produced fake CLI history: ${JSON.stringify(geometry)}`);
		expect(geometry.userTurns === 8 && geometry.proseAnswers >= 8, 'eight real user/answer turns were produced through the CLI (thinking events are additional rows)');
			expect(geometry.height >= (variant.viewport === 'phone' ? 23000 : 10000), 'the required long CLI history really scrolls, rather than a native fake-AI substitute');
	}
	await page.waitForTimeout(700);
}

async function ensureFast(page, variant) {
	const mobileShell = mobile && variant.viewport === 'phone';
	// A mobile portal can already be open before its dialog semantics mount.
	// Close it through the real control path before choosing the model.
	await page.keyboard.press('Escape');
	const models = page.getByRole('dialog', { name: mobileShell ? 'Model' : 'Choose model', exact: true });
	await openPopup(page.getByRole('button', { name: /^Model:/ }).first(), models);
	await models.getByRole('textbox', { name: 'Search models' }).fill('Opus');
	await models.getByRole('option', { name: /^Opus/ }).first().click();
	if (mobileShell) {
		await models.waitFor({ state: 'hidden' });
		await page.getByRole('button', { name: /^All chat controls/ }).click();
		const fast = page.getByRole('switch', { name: 'Fast mode', exact: true });
		await fast.waitFor();
		if (await fast.getAttribute('aria-checked') !== 'true') await fast.click();
		await page.keyboard.press('Escape');
		return;
	}
	await page.keyboard.press('Escape');
	// Fast lives in the effort popover at desktop width and in Chat controls
	// (as a switch) at phone width.
	const fast = page.getByRole('button', { name: 'Fast mode', exact: true })
		.or(page.getByRole('switch', { name: 'Fast mode', exact: true }));
	await openPopup(page.getByRole('button', { name: /^(Effort:|All chat controls)/ }).first(), fast);
	const on = (await fast.getAttribute('aria-pressed')) ?? (await fast.getAttribute('aria-checked'));
	if (on !== 'true') await fast.click();
	await page.keyboard.press('Escape');
	await page.waitForTimeout(400);
}

async function openHistory(ctx) {
	await gotoApp(ctx.page, ctx.status, `${prefix(ctx.variant)}?session=${seededChatId(ctx.status, 'no_project')}`);
	await waitChatOpen(ctx.page, { messagesAtLeast: 2 });
}

async function pickNativeAgent({ page, variant }) {
	const inMobileShell = mobile && variant.viewport === 'phone';
	if (inMobileShell) await page.getByRole('button', { name: /^All chat controls/ }).click();
	await pickAgent(page, page);
	if (inMobileShell) {
		await page.keyboard.press('Escape');
		await page.getByRole('dialog', { name: 'Chat controls', exact: true }).waitFor({ state: 'hidden' });
	}
}

// These cases measure established queue/handoff geometry, not rapid-start
// admission (known product bug https://github.com/HobbyCoders/ai-shuffle/issues/1052).
// Capture the transcript frontier before Send and require this unique prompt in
// NEW reply prose: old history and optimistic streaming=true are not readiness.
async function replyFrontier(page) {
	return page.locator('.chat-column [data-transcript-key]').evaluateAll(rows => rows.map(row => row.dataset.transcriptKey));
}
async function waitForCurrentReplyProse(page, frontier, token) {
	await page.waitForFunction(({ frontier, token }) => [...document.querySelectorAll('.chat-column [data-transcript-key]')]
		.some(row => !frontier.includes(row.dataset.transcriptKey) && row.querySelector('.prose')?.textContent.includes(token)), { frontier, token });
}

const cases = [{
	name: 'fast-completion',
	async setup(ctx) { await openClaude(ctx); await ensureFast(ctx.page, ctx.variant); },
	async act({ page, mark, variant, log }) {
		await mark('send a Fast reply and follow completion');
		const stop = await watchFrames(page);
		await typeAndSend(page, `fast ${uniq()} ${scriptTag(variant, 'long', { tps: 300 })}`);
		await waitChat(page, { streaming: 'true' });
		await waitTurnSettled(page, { hold: 4500 });
		const frames = await stop();
		const lastChars = frames.at(-1).chars;
		const end = frames.find(f => f.chars === lastChars && f.streaming === 'false');
		// The explicit prompt repin after the 1.2s hold is covered separately.
		const completed = frames.filter(f => f.proseKey === frames.at(-1).proseKey && f.chars === lastChars && f.prose && f.at < end.at + 1000);
		log(`completion frame geometry ${JSON.stringify(summary(completed))}`);
		expect(completed.length > 4, 'sampled final prose before/through settlement');
		expect(travel(completed, 'prose') <= 2, `newest completed prose never bounces (${travel(completed, 'prose').toFixed(1)}px)`);
	}
}, ...[false, true].map(scrolledUp => ({
	name: scrolledUp ? 'long-settle-reader' : 'long-settle-follower',
	setup: openHistory,
	async act({ page, mark, variant, log }) {
		await mark('send a long reply');
		await typeAndSend(page, `long ${uniq()} ${scriptTag(variant, 'long', { tps: 300 })}`);
		await waitChat(page, { streaming: 'true' });
		await page.getByText('About the composer', { exact: false }).last().waitFor();
		if (scrolledUp) {
			await mark('scroll up to read while the answer grows');
			await page.locator(LOG).hover();
			await page.mouse.wheel(0, -500);
			await page.waitForTimeout(300);
		}
		const stop = await watchFrames(page);
		await waitTurnSettled(page, { hold: 4500 });
		const frames = await stop();
		const completed = frames.filter(f => f.proseKey === frames.at(-1).proseKey && f.chars === frames.at(-1).chars);
		log(`long answer ${JSON.stringify(summary(completed))}`);
		const drift = scrolledUp ? travel(frames, 'anchor') : travel(completed, 'prose');
		expect(drift <= 2, `completion keeps the reader's visible prose (${drift.toFixed(1)}px drift)`);
		if (!scrolledUp) {
			const last = frames.at(-1);
			expect(last.height - last.viewport - last.scroll <= 2, 'the follower stays at the bottom, not returned to their prompt');
		}
	}
})), {
	name: 'short-send', setup: openHistory,
	async act({ page, mark, variant, log }) {
		await composer(page).fill('hi [script:default]');
		await page.waitForTimeout(500);
		await mark('send a short draft');
		const stop = await watchFrames(page);
		const messagesBefore = Number(await page.locator('[data-ui-state="chat"]').getAttribute('data-ui-messages'));
		await sendButton(page).click();
		// Old history's idle state must not finish this case while the new turn
		// is still starting, leaving the next seeded-chat clear to fail with 409.
		await waitTurnSettled(page, { messagesAtLeast: messagesBefore + 2, hold: 3000 });
		const frames = await stop();
		log(`short send ${JSON.stringify(summary(frames))}`);
		expect(travel(frames, 'rail') <= 2, `short send keeps the turn rail still (${travel(frames, 'rail').toFixed(1)}px)`);
		// Centred within the rail's 40px hold-still band (TurnNavigator).
		const railOffCentre = await page.evaluate(() => {
			const nav = document.querySelector('nav.turn-nav').getBoundingClientRect();
			const shell = document.querySelector('.transcript-shell').getBoundingClientRect();
			return Math.abs((nav.top + nav.bottom) / 2 - (shell.top + shell.bottom) / 2);
		});
		expect(railOffCentre <= 41, `the turn rail is vertically centred beside the transcript (${railOffCentre.toFixed(1)}px off)`);
		const tops = frames.map(f => f.composer?.top).filter(Number.isFinite);
		const directionChanges = tops.slice(1).reduce((state, top, i) => {
			const direction = Math.sign(Math.round(top - tops[i]));
			if (direction && state.direction && direction !== state.direction) state.reversals++;
			if (direction) state.direction = direction;
			return state;
		}, { direction: 0, reversals: 0 });
		expect(directionChanges.reversals === 0, `clearing a short draft moves the composer at most once (${directionChanges.reversals} reversals)`);
	}
}, {
	name: 'queue-handoff', async setup(ctx) {
		await openHistory(ctx);
		await pickNativeAgent(ctx); // Dev native: CLI sends can become steers.
		// Keep the seed's explicit Fake AI provider model. A brand-new tab's
		// remembered Claude override can otherwise turn this into a CLI steer.
		await api(ctx.page, 'POST', `/sessions/${seededChatId(ctx.status, 'no_project')}/clear`, {});
		await waitChat(ctx.page, { surface: 'cleared', messagesAtMost: 0 });
	},
	async act({ page, mark, variant, log }) {
		for (let i = 0; i < 3; i++) {
			await mark(`send long turn ${i + 1}`);
			const frontier = await replyFrontier(page), token = uniq();
			await typeAndSend(page, `handoff ${token} ${scriptTag(variant, 'long', { tps: 300 })}`);
			await waitChat(page, { streaming: 'true' });
			await waitForCurrentReplyProse(page, frontier, token);
			await mark('queue next turn after the current reply is really running');
			await typeAndSend(page, `queued ${uniq()} [script:default tps=300]`);
			await page.locator('.pending-messages [data-queue-confirmed="true"]').waitFor();
			const stop = await watchFrames(page);
			await page.locator('.pending-messages').waitFor({ state: 'hidden', timeout: 60_000 });
			await waitTurnSettled(page, { hold: 3000 });
			const frames = await stop();
			log(`handoff ${i + 1}: ${JSON.stringify(summary(frames))}`);
			expect(travel(frames, 'prose', 'left') <= 1, 'handoff does not change the rail lane or reflow already-read prose');
			expect(travel(frames, 'composer') <= 2, `handoff keeps the composer still (${travel(frames, 'composer').toFixed(1)}px)`);
			const pendingFrames = frames.filter(f => f.pending);
			expect(travel(pendingFrames, 'pending') <= 2, `handoff keeps the pending bubble docked (${travel(pendingFrames, 'pending').toFixed(1)}px)`);
		}
	}
}, {
	name: 'queue-handoff-reader', async setup(ctx) {
		await openHistory(ctx);
		await pickNativeAgent(ctx); // Exercise native auto-delivery, not CLI steer.
	},
	async act({ page, mark, variant, log }) {
		const frontier = await replyFrontier(page), token = uniq();
		await typeAndSend(page, `reader handoff ${token} ${scriptTag(variant, 'long', { tps: 120 })}`);
		await waitChat(page, { streaming: 'true' });
		await waitForCurrentReplyProse(page, frontier, token);
		await typeAndSend(page, `queued reader ${uniq()} [script:default tps=300]`);
		await page.locator('.pending-messages [data-queue-confirmed="true"]').waitFor();
		await page.waitForFunction(selector => document.querySelector(selector).scrollTop > 650, LOG);
		await page.locator(LOG).hover();
		await page.mouse.wheel(0, -500);
		await page.waitForTimeout(150);
		await mark('leave an up-scrolled reader alone through auto handoff');
		const stop = await watchFrames(page);
		await page.locator('.pending-messages').waitFor({ state: 'hidden', timeout: 60_000 });
		await waitTurnSettled(page, { hold: 3000 });
		const frames = await stop();
		log(`reader handoff ${JSON.stringify(summary(frames))}`);
		expect(travel(frames, 'anchor') <= 2, `queued delivery leaves the reader's visible row alone (${travel(frames, 'anchor').toFixed(1)}px)`);
	}
}, ...[false, true].map(scrolledUp => ({
	name: scrolledUp ? 'long-cli-error-stop-reader' : 'long-cli-error-stop',
	async setup(ctx) { await openClaude(ctx, { history: true }); },
	async act({ page, mark, variant, log }) {
		// Retain diagnostic paint history from before input too: CLS can name a
		// wrapper's old rect even when the last pending child stays docked.
		const stop = await watchFrames(page, { deferAssertions: true });
		await mark('send a failing CLI turn');
		await typeAndSend(page, `error ${uniq()} ${scriptTag(variant, 'error-500', { latency_ms: 4500 })}`);
		await waitChat(page, { streaming: 'true' });
		await mark('queue messages during the failing reply');
		for (let i = 0; i < 2; i++) await typeAndSend(page, `waiting-${i} ${uniq()} [script:slow tps=2]`);
		await page.locator('.pending-messages [data-queue-confirmed="true"]').first().waitFor();
		if (scrolledUp) {
			await mark('read older prose before the queued CLI failure');
			await page.locator(LOG).hover();
			await page.mouse.wheel(0, -500);
			await page.waitForTimeout(300);
		}
		// Preserve the original strict assertion window (confirmed queue and,
		// for readers, after their deliberate scroll), plus its resting frame.
		await page.evaluate(() => window.__chatFrames.beginAssertions());
		await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
		// The failed CLI turn's error shows in its transcript Error row; the banner
		// steps aside because the row says the same words (#1341).
		await errorRow(page, 'claude reported success: API Error: 500').first().waitFor({ timeout: 60_000 });
		// The failed turn ends as its Error row shows and the first queued
		// message starts: Stop leaves its seat and comes back. On a slow page the
		// button in between holds the seat long enough for Stop to count as
		// arriving, and a press made at once is ignored (#1632). Press a Stop
		// that has kept its seat, as a person would.
		await waitSeated(page, 'Stop generating');
		await mark('Stop the failed CLI turn with queued messages');
		await page.evaluate(() => { window.__chatFrames.phase = 'stop-requested'; });
		await page.getByRole('button', { name: 'Stop generating', exact: true }).click();
		await waitTurnSettled(page, { hold: 4500 });
		const frames = await stop();
		log(`error/Stop geometry ${JSON.stringify(summary(frames))}`);
		// Anchored on the Error row's arrival (#1341: no banner repeats it).
		const banners = frames.filter(f => f.errorRow);
		expect(banners.length > 4, 'the real CLI error row appeared and was sampled');
		// The banner was chrome, so it stayed put while the queue handed off. The
		// row is transcript: once the first queued message leaves the pending list
		// a follower rightly scrolls to that new turn. The row holds still until
		// then; the held slot checks below still run through the handoff (#1341).
		// Read the painted frame: row membership is sampled in rAF, before the
		// app's own rAF commit in that frame, so a bubble that left in this paint
		// is still listed but has no painted rect.
		const handoff = frames.findIndex(f => f.errorRow && !f.rows.some(row => row.pending && row.rect && row.text.startsWith('waiting-0 ')));
		const resting = (handoff < 0 ? frames : frames.slice(0, handoff)).filter(f => f.errorRow);
		if (scrolledUp) expect(travel(frames, 'anchor') <= 2, 'error and Stop leave the older row being read alone');
		else {
			log(`error row resting frames before handoff: ${resting.length}`);
			expect(!resting.length || travel(resting, 'errorRow') <= 2, `the error row stays still until the queue hands off (${travel(resting, 'errorRow').toFixed(1)}px over ${resting.length} frames)`);
			// Both flex boxes must retain the already-presented queue slot.
			// Shrinking either during follow-scroll was the pre-Stop 0.082
			// CLS; separate paints could hide it below 0.05.
			expect(travel(banners, 'tailControls') <= 2, `the error's queue slot stays still through handoff (${travel(banners, 'tailControls').toFixed(1)}px)`);
			expect(travel(banners, 'pendingList') <= 2, `the pending list shares the held slot (${travel(banners, 'pendingList').toFixed(1)}px)`);
			const retained = banners.map(frame => ({ pending: frame.rows.find(row => row.pending && row.text.startsWith('waiting-1 '))?.bubble })).filter(frame => frame.pending);
			expect(retained.length > 4, 'the retained queued bubble was sampled');
			expect(travel(retained, 'pending') <= 2 && travel(retained, 'pending', 'bottom') <= 2, 'retaining the slot must not move its remaining queued bubble');
		}
	}
})), {
	name: 'placeholder', setup: openHistory,
	async act({ page, mark, log }) {
		await mark('read the empty placeholder');
		const geometry = await composer(page).evaluate(el => {
			const p = el.querySelector('p');
			const css = getComputedStyle(p, '::before');
			const copy = document.createElement('span');
			copy.textContent = p.dataset.placeholder;
			Object.assign(copy.style, { font: getComputedStyle(el).font, display: 'block', width: `${p.clientWidth}px`, whiteSpace: css.whiteSpace });
			document.body.appendChild(copy);
			const height = copy.getBoundingClientRect().height;
			copy.remove();
			const top = p.getBoundingClientRect().top;
			return { neededBottom: top + height, editorBottom: el.closest('.tiptap-editor').getBoundingClientRect().bottom };
		});
		log(`placeholder ${JSON.stringify(geometry)}`);
		expect(geometry.neededBottom <= geometry.editorBottom + 1, 'every line of the placeholder fits inside the editor, above the controls');
	}
}, ...(mobile ? ['first-send', 'switch-from-empty'].map(action => ({
	name: `mobile-${action}`, viewports: ['phone'],
	async setup(ctx) {
		await gotoApp(ctx.page, ctx.status, '/mobile/');
		await waitChat(ctx.page, { surface: 'new', loading: 'false' });
		await pickAgent(ctx.page, ctx.page.getByRole('region', { name: 'New chat' }));
		if (action === 'first-send') await composer(ctx.page).fill('hi');
		await ctx.page.waitForTimeout(1000);
	},
	async act({ page, mark, log }) {
		await mark(action);
		const stop = await watchFrames(page);
		if (action === 'first-send') {
			await sendButton(page).click();
			await waitTurnSettled(page);
		} else {
			await page.getByRole('button', { name: /^Open menu/ }).click();
			await sidebarRow(page, 'Quick question (seeded)').click();
			await waitChatOpen(page, { messagesAtLeast: 2 });
		}
		const frames = await stop();
		log(`already-docked composer ${JSON.stringify(summary(frames))}`);
		expect(travel(frames, 'composer') <= 2, `the mobile composer never slides from an invented position (${travel(frames, 'composer').toFixed(1)}px)`);
	}
})) : [])];


// Coherent geometry in one callback: a retained Range/row identity is evidence
// of visible continuity; native source rectangles alone can be clipped or empty.
async function watchUpwardPagingAcrossRetainedGap(ctx) {
	const { page } = ctx, pane = page.locator(LOG);
	const initial = await pane.evaluate(n => ({ top: n.scrollTop,
		first: Number(n.querySelector('[data-transcript-index]').dataset.transcriptIndex),
		gap: parseFloat(n.querySelector('.earlier-row').style.marginTop) || 0 }));
	expect(initial.gap > 2000, 'reader regression follows a real multi-thousand-pixel eviction');
	await ctx.mark('read upward near the retained mounted origin, without Show earlier');
	await pane.hover();
	await page.mouse.wheel(0, initial.gap + 1000 - initial.top);
	await page.waitForFunction(target => Math.abs(document.querySelector('.msg-scroll').scrollTop - target) <= 1, initial.gap + 1000);
	const before = await page.evaluate(() => {
		const pane = document.querySelector('.msg-scroll'), view = pane.getBoundingClientRect();
		let tracked;
		for (const y of [72, 120, 168]) {
			const caret = document.caretRangeFromPoint(view.left + 60, view.top + y), node = caret?.startContainer;
			if (node?.nodeType !== Node.TEXT_NODE || !node.textContent.trim()) continue;
			const row = node.parentElement.closest('[data-transcript-index]');
			if (!row) continue;
			const range = document.createRange(), start = Math.min(caret.startOffset, Math.max(0, node.length - 24));
			range.setStart(node, start); range.setEnd(node, Math.min(node.length, start + 24));
			const r = range.getBoundingClientRect();
			if (!range.toString().trim() || r.top <= view.top || r.bottom + 240 >= view.bottom) continue;
			tracked = { node, row, range }; break;
		}
		if (!tracked) throw new Error('No visible nonempty retained prose Range before upward paging');
		const p = window.__upwardReader = { ...tracked, running: true, frames: [],
			first: Number(pane.querySelector('[data-transcript-index]').dataset.transcriptIndex) };
		const ids = new WeakMap([[p.row, 1], [p.node, 2]]);
		const sample = () => p.frames.push({ first: Number(pane.querySelector('[data-transcript-index]').dataset.transcriptIndex),
			top: pane.scrollTop, height: pane.scrollHeight, client: pane.clientHeight,
			gap: parseFloat(pane.querySelector('.earlier-row').style.marginTop) || 0,
			rowId: ids.get(p.row), textId: ids.get(p.node), connected: p.row.isConnected && p.node.isConnected,
			text: p.range.toString(), rect: p.range.getBoundingClientRect().toJSON(),
			viewportBottom: innerHeight, composer: document.querySelector('.input-slot').getBoundingClientRect().toJSON() });
		const frame = () => {
			if (!p.running) return;
			const afterLayout = new IntersectionObserver(() => {
				afterLayout.disconnect(); if (p.running) sample();
			});
			afterLayout.observe(pane); p.raf = requestAnimationFrame(frame);
		};
		p.raf = requestAnimationFrame(frame);
		return { first: p.first, text: p.range.toString(), rect: p.range.getBoundingClientRect().toJSON() };
	});
	await ctx.mark('cross the older paging edge with a real upward wheel');
	await page.mouse.wheel(0, -240);
	const paged = await page.waitForFunction(() => {
		const p = window.__upwardReader;
		return p.frames.filter(f => f.first < p.first && f.gap === 0).length >= 30;
	}, null, { timeout: 5000 }).then(() => true, () => false);
	const observed = await page.evaluate(() => {
		const p = window.__upwardReader; p.running = false; cancelAnimationFrame(p.raf);
		return { frames: p.frames };
	});
	if (process.env.JOURNEY_OUT) writeFileSync(`${process.env.JOURNEY_OUT}/upward-reader-${prefix(ctx.variant) === '/mobile/' ? 'mobile' : 'root'}-${ctx.variant.viewport}.json`, JSON.stringify({ initial, before, ...observed }));
	expect(paged, 'ordinary upward scrolling must page before the retained gap can leave a blank viewport');
	const after = observed.frames.filter(f => f.first < before.first && f.gap === 0);
	for (const f of after) {
		expect(f.connected && f.rowId === 1 && f.textId === 2 && f.text === before.text, 'upward paging retains the same connected visible prose Range');
		expect(Math.abs(f.rect.top - before.rect.top - 240) <= 1, 'anchored gap release adds no movement beyond the intended 240px reader scroll');
		expect(Math.abs(f.composer.bottom - f.viewportBottom) <= 1, 'reader paging keeps the composer at the viewport bottom');
	}
	ctx.log(`automatic upward paging ${JSON.stringify({ initial, before, first: after[0], samples: after.length })}`);
	await page.getByRole('button', { name: 'Jump to latest message', exact: true }).click();
	await page.waitForFunction(() => {
		const pane = document.querySelector('.msg-scroll'), view = pane.getBoundingClientRect();
		const last = [...pane.querySelectorAll('[data-user-turn-id]')].at(-1)?.getBoundingClientRect();
		return last && last.top > view.top && last.bottom < view.bottom;
	});
}

async function watchLongCompletionThenError(ctx) {
	const { page } = ctx;
	await page.evaluate(() => {
		const pane = document.querySelector('.msg-scroll');
		const p = window.__completionProbe = { started: performance.now(), phase: 'long', phaseAt: 0, frames: [], shifts: [], running: true };
		const ids = new WeakMap(); let next = 1;
		const id = n => { if (!n) return null; if (!ids.has(n)) ids.set(n, next++); return ids.get(n); };
		const probes = new Map();
		const sample = reason => {
			const box = pane.getBoundingClientRect();
			for (const y of [box.top + 32, box.top + box.height / 2, box.bottom - 48]) {
				const caret = document.caretRangeFromPoint(box.left + 60, y);
				const node = caret?.startContainer;
				if (node?.nodeType !== Node.TEXT_NODE || !node.textContent.trim() || !pane.contains(node) || probes.has(node)) continue;
				const range = caret.cloneRange();
				range.setEnd(node, Math.min(node.length, range.startOffset + 24));
				if (range.toString().trim()) probes.set(node, { range, row: node.parentElement.closest('[data-transcript-key]') });
			}
			const userRow = [...pane.querySelectorAll('[data-user-turn-id]')].at(-1);
			const bubble = userRow?.querySelector('.bubble-text');
			if (bubble) {
				const walker = document.createTreeWalker(bubble, NodeFilter.SHOW_TEXT);
				const node = walker.nextNode();
				if (node?.textContent.trim() && !probes.has(node)) {
					const range = document.createRange(); range.selectNodeContents(node);
					probes.set(node, { range, row: userRow });
				}
			}
			// Keep the current continuity window, not every old prose node from all
			// turns. Large transfers at stop would stall Chromium/CI needlessly.
			while (probes.size > 16) probes.delete(probes.keys().next().value);
			const allRows = [...pane.querySelectorAll('[data-transcript-index]')];
			const rows = [...new Set([...allRows.slice(0, 2), ...allRows.slice(-6)])];
			const f = { t: performance.now() - p.started, phase: p.phase, reason,
				userRow: id(userRow), top: pane.scrollTop, height: pane.scrollHeight, client: pane.clientHeight, viewport: box.toJSON(),
				streaming: document.querySelector('[data-ui-state="chat"]').dataset.uiStreaming,
				firstIndex: Number(rows[0]?.dataset.transcriptIndex ?? -1),
				leadingGap: parseFloat(getComputedStyle(pane.querySelector('.chat-column > .earlier-row')).marginTop) || 0,
				rows: rows.map(row => ({ id: id(row), key: row.dataset.transcriptKey, index: Number(row.dataset.transcriptIndex), rect: row.getBoundingClientRect().toJSON() })),
				texts: [...probes].map(([node, { range, row }]) => ({ id: id(node), rowId: id(row), key: row?.dataset.transcriptKey, connected: node.isConnected && row?.isConnected,
					text: range.toString(), rect: range.getBoundingClientRect().toJSON() })),
				controls: Object.fromEntries(['.input-slot', '.tail-controls', '.tail-error', '.transcript-tail-spacer', '.turn-activity'].map(sel => [sel, document.querySelector(sel)?.getBoundingClientRect().toJSON()])) };
			p.frames.push(f);
			return f;
		};
		p.observer = new PerformanceObserver(list => {
			for (const e of list.getEntries()) p.shifts.push({ t: e.startTime - p.started, value: e.value, recent: e.hadRecentInput,
				sources: e.sources.map(s => ({ id: id(s.node), connected: s.node?.isConnected, html: s.node?.outerHTML?.slice(0, 600),
					key: s.node?.closest?.('[data-transcript-key]')?.dataset.transcriptKey, previous: s.previousRect.toJSON(), current: s.currentRect.toJSON() })), frame: sample('native') });
		});
		p.observer.observe({ type: 'layout-shift' });
		p.mutations = new MutationObserver(() => sample('mutation'));
		p.mutations.observe(pane, { subtree: true, childList: true, characterData: true, attributes: true });
		const frame = () => {
			if (!p.running) return;
			sample('raf');
			// Observe after layout too: rAF can precede the app's follow callback.
			const painted = new IntersectionObserver(() => {
				painted.disconnect();
				if (p.running) sample('post-layout');
			});
			painted.observe(pane);
			p.raf = requestAnimationFrame(frame);
		};
		frame();
	});
	const sendLong = async () => {
		await page.evaluate(() => { const p = window.__completionProbe; p.phase = 'long'; p.phaseAt = performance.now() - p.started; });
		await ctx.mark('preceding long completion');
		await typeAndSend(page, 'parent preceding long answer [script:long tps=2000]');
		await waitChat(page, { streaming: 'true' });
		await waitTurnSettled(page, { hold: 1800 });
	};
	await sendLong();
	await page.evaluate(() => { const p = window.__completionProbe; p.phase = 'error-initial'; p.phaseAt = performance.now() - p.started; });
	await ctx.mark('error after preceding long completion');
	await typeAndSend(page, 'parent error after long [script:error-500 latency_ms=4500]');
	await waitChat(page, { streaming: 'true' });
	await waitTurnSettled(page, { hold: 900 });
	// One more real long turn places the large WORKLOG at the next budget edge.
	// Continue real errors until completion, not input admission, evicts it.
	await sendLong();
	let largeTerminalEviction = false;
	for (let turn = 0; turn < 12; turn++) {
		await page.evaluate(turn => { const p = window.__completionProbe; p.phase = `error-${turn}`; p.phaseAt = performance.now() - p.started; }, turn);
		await ctx.mark(`error after long ${turn}`);
		await typeAndSend(page, `parent error after long ${turn} [script:error-500 latency_ms=4500]`);
		await waitChat(page, { streaming: 'true' });
		await waitTurnSettled(page, { hold: 900 });
		const removed = await page.evaluate(minHeight => {
			const p = window.__completionProbe, fs = p.frames.filter(f => f.phase === p.phase && f.t > p.phaseAt + 2000);
			return fs.some((f,i) => i && f.firstIndex > fs[i-1].firstIndex && fs[i-1].rows.some(row =>
				row.index < f.firstIndex && row.key.startsWith('wl-') && row.rect.height > minHeight));
		}, ctx.variant.viewport === 'phone' ? 2000 : 1000);
		ctx.log(`terminal worklog eviction ${turn}: ${removed}`);
		if (removed) { largeTerminalEviction = true; break; }
	}
	const observed = await page.evaluate(() => {
		const p = window.__completionProbe;
		p.running = false; p.observer.disconnect(); p.mutations.disconnect(); cancelAnimationFrame(p.raf);
		return { frames: p.frames, shifts: p.shifts };
	});
	if (process.env.JOURNEY_OUT) writeFileSync(`${process.env.JOURNEY_OUT}/long-completion-error-${prefix(ctx.variant) === '/mobile/' ? 'mobile' : 'root'}-${ctx.variant.viewport}.json`, JSON.stringify(observed));
	const phases = [...new Set(observed.frames.map(f => f.phase))].filter(phase => phase.startsWith('error'));
	for (const phase of phases) {
		const fs = observed.frames.filter(f => f.phase === phase);
		const before = fs.filter(f => f.streaming === 'true').at(-1);
		const text = before?.texts.find(t => t.rowId === before.userRow && t.connected);
		expect(text && text.rect.top > before.viewport.top && text.rect.bottom < before.viewport.bottom, 'the error prompt is visibly tracked before completion');
		for (const f of fs.filter(f => f.t >= before.t && (f.reason === 'raf' || f.reason === 'post-layout'))) {
			const current = f.texts.find(t => t.id === text.id);
			expect(current?.connected && current.rowId === text.rowId && current.text === text.text, 'completion retains the same connected prompt Range and row');
			expect(Math.abs(current.rect.top - text.rect.top) <= 1, `completion/rebase must not move the prompt (${current.rect.top} vs ${text.rect.top})`);
		}
	}
	expect(largeTerminalEviction, 'a real error completion must evict a large leading WORKLOG (phone >2000px; desktop >1000px)');
	ctx.log(`long completion then error: ${JSON.stringify({ frames: observed.frames.length, phases, largeTerminalEviction,
		shifts: observed.shifts.map(({ t, value, recent, sources }) => ({ t, value, recent, keys: sources.map(s => s.key) })) })}`);
	await watchUpwardPagingAcrossRetainedGap(ctx);
}

// A fresh send can be queued without a new transcript row. Keep this phone-only
// producing regression separate from the original error/handoff/Stop actions.
// Exercise the leading-row boundary with real CLI history and retain the same
// connected current-user text range across the error's mounted-window rebase.
cases.push({
	name: 'error-rebase-keeps-visible-prompt', viewports: ['desktop', 'phone'], throttles: ['slow'],
	async setup(ctx) {
		await openClaude(ctx, { history: true });
		const count = Number(await ctx.page.locator('[data-ui-state="chat"]').getAttribute('data-ui-messages'));
		await typeAndSend(ctx.page, `ninth history ${uniq()} [script:long tps=2000]`);
		await waitTurnSettled(ctx.page, { messagesAtLeast: count + 2, hold: 200 });
		if (ctx.variant.viewport === 'desktop') {
			// Scope this NEW desktop transcript-origin check with the real UI.
			// Intended Needs-you/Active sidebar reclassification is tested separately;
			// this extended flow sidebar-visible remains strict RED (0.120), not a
			// global desktop pass. Existing desktop neighbors keep their sidebar.
			// Hide sidebar lives in the Chats panel header; the title bar shows its
			// toggle only as Show sidebar while the panel is collapsed.
			await ctx.page.getByRole('button', { name: 'Hide sidebar', exact: true }).filter({ visible: true }).first().click();
			const show = ctx.page.getByRole('button', { name: 'Show sidebar', exact: true }).filter({ visible: true }).first();
			await show.waitFor({ state: 'visible' });
			expect(await show.isVisible(), 'the desktop transcript check really collapsed the sidebar');
		}
	},
	async act(ctx) {
		const { page } = ctx;
		let evicted = false;
		for (let turn = 0; turn < 3; turn++) {
			const token = `rebase-${uniq()}`;
			await page.evaluate(({ token, probe }) => {
				const pane = document.querySelector('.msg-scroll');
				const view = document.querySelector('[data-ui-state="chat"]');
				const errorRowProbe = probe;
				const p = window.__errorRebaseProbe = { started: performance.now(), frames: [], shifts: [], tracked: null, errorAt: null, running: true };
				const ids = new WeakMap(); let nextId = 1;
				const id = node => { if (!node) return null; if (!ids.has(node)) ids.set(node, nextId++); return ids.get(node); };
				const sample = reason => {
					if (!p.tracked) {
						const row = [...pane.querySelectorAll('[data-user-turn-id]')].find(row => row.textContent.includes(token));
						if (row) {
							const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
							let node;
							while ((node = walker.nextNode())) if (node.textContent.includes(token)) {
								const range = document.createRange(); range.selectNodeContents(node);
								p.tracked = { row, node, range }; break;
							}
						}
					}
					const t = performance.now() - p.started, tracked = p.tracked;
					const f = { t, reason, top: pane.scrollTop, height: pane.scrollHeight, viewport: pane.getBoundingClientRect().toJSON(),
						firstIndex: Number(pane.querySelector('[data-transcript-index]')?.dataset.transcriptIndex ?? -1),
						user: tracked ? { rowId: id(tracked.row), textId: id(tracked.node), connected: tracked.node.isConnected && tracked.row.isConnected,
							key: tracked.row.dataset.transcriptKey ?? tracked.row.closest('[data-transcript-key]')?.dataset.transcriptKey,
							text: tracked.range.toString(), rect: tracked.range.getBoundingClientRect().toJSON(), rowRect: tracked.row.getBoundingClientRect().toJSON() } : null };
					p.frames.push(f);
					if (tracked && [...document.querySelectorAll(errorRowProbe.rows)].filter(d => [...d.querySelectorAll(':scope > summary > span')].some(s => s.textContent.trim() === errorRowProbe.title)).some(d => tracked.row.compareDocumentPosition(d) & Node.DOCUMENT_POSITION_FOLLOWING) && view.dataset.uiStreaming === 'false' && p.errorAt === null) p.errorAt = t;
					return f;
				};
				p.observer = new PerformanceObserver(list => {
					for (const entry of list.getEntries()) p.shifts.push({ t: entry.startTime - p.started, value: entry.value, recent: entry.hadRecentInput,
						sources: (entry.sources ?? []).map(s => ({ nodeId: id(s.node), key: s.node?.closest?.('[data-transcript-key]')?.dataset.transcriptKey,
							previous: s.previousRect.toJSON(), current: s.currentRect.toJSON() })), frame: sample('native-layout-shift') });
				});
				p.observer.observe({ type: 'layout-shift' });
				const frame = () => { if (!p.running) return; sample('raf'); p.raf = requestAnimationFrame(frame); };
				p.raf = requestAnimationFrame(frame);
			}, { token, probe: ERROR_ROW_PROBE });
			await typeAndSend(page, `${token} [script:error-500 latency_ms=4500]`);
			await waitChat(page, { streaming: 'true' });
			await page.waitForFunction(() => {
				const p = window.__errorRebaseProbe;
				return p.errorAt !== null && performance.now() - p.started >= p.errorAt + 3300;
			});
			const observed = await page.evaluate(() => {
				const p = window.__errorRebaseProbe; p.running = false; p.observer.disconnect(); cancelAnimationFrame(p.raf);
				return { frames: p.frames, shifts: p.shifts, errorAt: p.errorAt };
			});
			if (process.env.JOURNEY_OUT) writeFileSync(`${process.env.JOURNEY_OUT}/error-rebase-${prefix(ctx.variant) === '/mobile/' ? 'mobile' : 'root'}-${ctx.variant.viewport}-${turn}.json`, JSON.stringify(observed));
			const before = observed.frames.filter(f => f.user?.connected && f.t < observed.errorAt).at(-1);
			expect(before?.user.rect.top > before?.viewport.top && before?.user.rect.bottom < before?.viewport.bottom, 'the current user text was actually visible before the error');
			const after = observed.frames.filter(f => f.t >= observed.errorAt);
			expect(after.length >= 2, 'observe the error through multiple painted frames');
			for (const f of after) {
				expect(f.user?.connected && f.user.rowId === before.user.rowId && f.user.textId === before.user.textId
					&& f.user.key === before.user.key && f.user.text === before.user.text, 'retain the same connected current-user text range');
				expect(Math.abs(f.user.rect.top - before.user.rect.top) <= 1, `visible current-user text must not bounce at rebase (${f.user.rect.top} vs ${before.user.rect.top})`);
			}
			evicted ||= after.some(f => f.firstIndex > before.firstIndex);
			ctx.log(`error rebase ${turn}: ${JSON.stringify({ evicted, before, last: after.at(-1), shifts: observed.shifts })}`);
		}
		expect(evicted, 'this production path must actually evict a leading transcript row');
		// Record the whole historical producer, including the preceding long reply's
		// completion. Installing diagnostics only before the error misses that commit.
		await watchLongCompletionThenError(ctx);
		// The runner owns the unchanged strict recorder/anomaly verdict.
	}
});

cases.push({
	name: 'fresh-queued-send-releases-error-slot', viewports: ['phone'], throttles: ['slow'],
	async setup(ctx) {
		const { page, variant } = ctx;
		await openClaude(ctx); // New short project chat; never clears the long CLI seed.
		// Establish this CLI through a real completed reply before testing queued
		// geometry. Adoption/streaming optimism is not CLI readiness: a send in
		// the spawn -> hook-registration window can take the stdin steer fallback,
		// so neither bubble remains queued (hosted run 42107295628). No timer,
		// provider substitution or injected queue state stands in for this turn.
		const warmup = `queue-ready-${uniq()}`, frontier = await replyFrontier(page);
		await typeAndSend(page, `${warmup} [script:default tps=300]`);
		await waitForCurrentReplyProse(page, frontier, warmup);
		await waitTurnSettled(page, { hold: 200 });
		ctx.log('queue setup: real CLI warm-up reply completed before the held error send');
		ctx.freshQueueToken = `fresh-${uniq()}`;
		ctx.firstQueueToken = `waiting-0-${uniq()}`;
		await typeAndSend(page, `held error ${uniq()} ${scriptTag(variant, 'error-500', { latency_ms: 10000 })}`);
		await waitChat(page, { streaming: 'true' });
		// Streaming optimism precedes adoption on a brand-new chat. Do not turn
		// the two queued sends into two further session=none creation requests.
		await page.waitForFunction(() => {
			const state = document.querySelector('[data-ui-state="chat"]')?.dataset;
			return state?.uiSession && state.uiSession !== 'none' && state.uiStreaming === 'true';
		});
		// Two real two-line bubbles establish the >=150px prerequisite without
		// depending on whether a random token wraps onto a second line. A single
		// line less made the original setup 142.828px instead of 164.938px.
		await typeAndSend(page, `${ctx.firstQueueToken}\n[script:slow tps=1]`);
		await typeAndSend(page, `waiting-1-${uniq()}\n[script:slow tps=1]`);
		await page.waitForFunction(() => document.querySelectorAll('.pending-messages [data-queue-confirmed="true"]').length === 2);
		ctx.log(`confirmed queued producer geometry ${JSON.stringify(await page.locator('.pending-messages').evaluate(el => ({
			rows: [...el.querySelectorAll('[data-queue-confirmed="true"]')].map(row => ({ text: row.textContent, height: row.getBoundingClientRect().height,
				lineHeight: getComputedStyle(row.querySelector('.bubble-text')).lineHeight, bubbleHeight: row.querySelector('.bubble').getBoundingClientRect().height })),
			height: el.getBoundingClientRect().height
		})))}`);
		// The failure's own Error row is the signal now; whether a banner (and
		// with it a held error slot) still shows is what the checks below read (#1341).
		await errorRow(page, 'claude reported success: API Error: 500').first().waitFor({ timeout: 60_000 });
		await page.waitForFunction(token => document.querySelector('[data-ui-state="chat"]')?.dataset.uiStreaming === 'true'
			&& [...document.querySelectorAll('[data-user-turn-id]')].some(row => row.textContent.includes(token)), ctx.firstQueueToken);
		const held = await page.locator('.tail-controls').evaluate(el => parseFloat(el.style.minHeight));
		expect(held >= 150, `two confirmed queued bubbles produced a real held slot (${held}px)`);
	},
	async act({ page, mark, log, freshQueueToken }) {
		const readState = () => page.evaluate(({ token, probe }) => ({
			streaming: document.querySelector('[data-ui-state="chat"]')?.dataset.uiStreaming,
			session: document.querySelector('[data-ui-state="chat"]')?.dataset.uiSession,
			users: document.querySelectorAll('[data-user-turn-id]').length,
			freshInTranscript: [...document.querySelectorAll('[data-user-turn-id]')].some(row => row.textContent.includes(token)),
			freshPending: document.querySelector('.pending-messages')?.textContent.includes(token) ?? false,
			slot: parseFloat(document.querySelector('.tail-controls')?.style.minHeight),
			error: !!document.querySelector('.chat-error-banner'),
			// The failure is presented by its own Error row once the banner steps aside (#1341).
			errorRow: [...document.querySelectorAll(probe.rows)].some(d => [...d.querySelectorAll(':scope > summary > span')].some(s => s.textContent.trim() === probe.title))
		}), { token: freshQueueToken, probe: ERROR_ROW_PROBE });
		await mark('fresh local send during the automatically delivered queued stream');
		const before = await readState();
		expect(before.streaming === 'true' && before.slot >= 150 && (before.error || before.errorRow),
			'fresh send starts during real queued streaming with the presented error slot, not after completion');
		await typeAndSend(page, `${freshQueueToken} [script:slow tps=1]`);
		await page.locator('.pending-messages .bubble-text').filter({ hasText: freshQueueToken }).waitFor();
		const after = await readState();
		log(`fresh queued send ${JSON.stringify({ before, after })}`);
		expect(after.streaming === 'true', 'the fresh send took the live-turn queue branch');
		expect(after.session === before.session && after.users === before.users && !after.freshInTranscript && after.freshPending,
			'the fresh local prompt is queued, not appended to the transcript');
		expect(after.slot === 0 && !after.error, 'the admitted queued send releases the old error and measured slot');
		await page.evaluate(() => {
			const watch = window.__freshQueueSlot = { running: true, values: [] };
			const sample = () => {
				if (!watch.running) return;
				watch.values.push(parseFloat(document.querySelector('.tail-controls')?.style.minHeight));
				requestAnimationFrame(sample);
			};
			sample();
		});
		await mark('Stop the live turn and drain its queued recovery rows');
		await page.getByRole('button', { name: 'Stop generating', exact: true }).click();
		await waitTurnSettled(page, { hold: 4500 });
		const samples = await page.evaluate(() => { window.__freshQueueSlot.running = false; return window.__freshQueueSlot.values; });
		const settled = await readState();
		log(`fresh slot after Stop ${JSON.stringify({ settled, samples: samples.length, heights: [...new Set(samples)] })}`);
		expect(samples.length > 4 && samples.every(height => height === 0), 'the released slot never reappears through Stop and queue recovery');
		expect(settled.slot === 0 && settled.streaming === 'false' && !settled.error && !settled.freshPending,
			'the idle drained view has no stale held error geometry');
	}
});

cases.push({
	name: 'long-history-short-send',
	async setup(ctx) { await openClaude(ctx, { history: true }); },
	act: cases.find(test => test.name === 'short-send').act
});

cases.push({
	name: 'long-to-short-navigation',
	async setup(ctx) {
		const { page, status, variant } = ctx;
		await gotoApp(page, status, `${prefix(variant)}?session=${seededChatId(status, 'other_project')}`);
		await waitChatOpen(page, { messagesAtLeast: 2 });
		await openSwitchTarget(ctx, SEEDED.no_project);
		for (let turn = 0; turn < 4; turn++) {
			const count = Number(await page.locator('[data-ui-state="chat"]').getAttribute('data-ui-messages'));
			await typeAndSend(page, `navigation history ${uniq()} ${scriptTag(variant, 'long', { tps: 2000 })}`);
			await waitTurnSettled(page, { messagesAtLeast: count + 2, hold: 200 });
		}
		await page.waitForTimeout(1600);
		const top = await page.locator(LOG).evaluate(el => el.scrollTop);
		expect(top > 5000, `the outgoing followed chat really has long coordinates (${top}px)`);
	},
	async act(ctx) {
		const { page, mark, log } = ctx;
		await mark('switch from the followed long chat to the cached short chat');
		await openSwitchTarget(ctx, SEEDED.other_project);
		await page.waitForTimeout(2000);
		const geometry = await page.locator(LOG).evaluate(el => {
			const box = el.getBoundingClientRect();
			const column = el.querySelector('.chat-column');
			const last = [...column.querySelectorAll(':scope > .msg-virtualized')].at(-1);
			const row = last.getBoundingClientRect();
			return { top: el.scrollTop, height: el.scrollHeight, viewport: el.clientHeight,
				padding: parseFloat(getComputedStyle(column).paddingBottom), rowTop: row.top, rowBottom: row.bottom,
				viewportTop: box.top, viewportBottom: box.bottom };
		});
		log(`long-to-short navigation ${JSON.stringify(geometry)}`);
		expect(geometry.padding <= geometry.viewport + 1, 'short chat reserve belongs to its viewport, not the outgoing scroll offset');
		expect(geometry.top <= geometry.viewport, 'the incoming short chat does not inherit the outgoing long scroll offset');
		expect(geometry.rowBottom > geometry.viewportTop && geometry.rowTop < geometry.viewportBottom, 'the short completed answer remains on screen, not above blank padding');
	}
});

// A follower's live work run folds into one label when new prose starts, so the
// prose above the reader shrinks by more than a phone viewport mid-stream. The
// followed floor held that shrink as padding: scrollTop stayed on the old end and
// the whole transcript sat above a blank view, Stop live and the scrollbar near
// the bottom. Every painted frame must keep transcript content on screen.
async function openFoldChat(ctx, path = prefix(ctx.variant)) {
	const { page, status, variant } = ctx;
	// Bash needs a project chat; Bypass is picked in the shared UI (the phone
	// sheet's picker stays open), then the chat reopens in the real /mobile/.
	await openNewChat(ctx, { project: PROJECT });
	await pickAccess(page, 'Bypass');
	const token = `fold-history-${uniq()}`;
	await typeAndSend(page, `${token} ${scriptTag(variant, 'long', { tps: 400 })}`);
	await waitTurnSettled(page, { hold: 500, timeout: 120_000 });
	const id = await chatIdByToken(page, token);
	if (path !== '/') {
		await gotoApp(page, status, `${path}?session=${encodeURIComponent(id)}`);
		await waitChatOpen(page, { messagesAtLeast: 2, hold: 800 });
	}
	return id;
}

/** Per painted frame: blank-viewport runs while streaming, the largest prose-end
 * drop (the fold), and once `track(key)` is called, that row's geometry for
 * `foldClamp`. */
async function watchFold(page) {
	await page.evaluate(() => {
		const watch = window.__foldFrames = { running: true, frames: 0, streaming: 0, blank: [], run: 0, longest: 0, proseDrop: 0, lastProseEnd: null,
			earlyFold: false, track: null, tracked: 0, trace: [] };
		const visible = (el, box) => { const r = el.getBoundingClientRect(); return r.height > 0 && r.bottom > box.top + 8 && r.top < box.bottom - 8; };
		const sample = () => {
			if (!watch.running) return;
			const log = document.querySelector('[role="log"][aria-label="Conversation messages"]');
			const spacer = log?.querySelector('.transcript-tail-spacer');
			const box = log?.getBoundingClientRect();
			if (log && spacer && document.querySelector('[data-ui-state="chat"]')?.dataset.uiStreaming === 'true') {
				const rows = [...log.querySelectorAll('.chat-column > [data-transcript-key]')];
				const controls = [...log.querySelectorAll('.tail-controls, .tail-error, .turn-activity')];
				watch.streaming++;
				// Finished commands fold as each completes, before the new prose starts.
				if (!watch.earlyFold && log.querySelector('.chat-column [data-run-fold]')
					&& ![...log.querySelectorAll('.chat-column .prose')].some(e => e.textContent.includes('All sixteen checks ran'))) watch.earlyFold = true;
				// The prose end in scroll coordinates: proves the fold really shrank it.
				const proseEnd = log.scrollTop + spacer.getBoundingClientRect().top - box.top;
				if (watch.lastProseEnd !== null) watch.proseDrop = Math.max(watch.proseDrop, watch.lastProseEnd - proseEnd);
				watch.lastProseEnd = proseEnd;
				if (rows.length && !rows.some(el => visible(el, box)) && !controls.some(el => visible(el, box))) {
					watch.run++;
					watch.longest = Math.max(watch.longest, watch.run);
					if (watch.blank.length < 5) watch.blank.push({ scrollTop: Math.round(log.scrollTop), scrollHeight: log.scrollHeight, viewport: log.clientHeight,
						padding: getComputedStyle(log.querySelector('.chat-column')).paddingBottom, lastRowBottom: Math.round(rows.at(-1).getBoundingClientRect().bottom - box.top) });
				} else watch.run = 0;
			}
			const row = watch.track && log?.querySelector(`.chat-column > [data-transcript-key="${CSS.escape(watch.track)}"]`);
			if (row) {
				watch.tracked++;
				// [rowTop, scrollTop, scrollHeight, proseEnd on screen, column padding, queued bubbles, user rows]
				watch.trace.push([row.getBoundingClientRect().top - box.top, log.scrollTop, log.scrollHeight, spacer.getBoundingClientRect().top - box.top,
					parseFloat(getComputedStyle(log.querySelector('.chat-column')).paddingBottom), log.querySelectorAll('.pending-messages .userrow').length,
					log.querySelectorAll('[data-user-turn-id]').length]);
			}
			watch.frames++;
			requestAnimationFrame(sample);
		};
		requestAnimationFrame(sample);
	});
	return {
		track: key => page.evaluate(key => { window.__foldFrames.track = key; }, key),
		stop: () => page.evaluate(() => { window.__foldFrames.running = false; return window.__foldFrames; })
	};
}

/** A follower at the live edge gives prose that collapsed above it back as
 * scroll, keeping the edge still. Scroll lost beyond that shrink is a clamp: the
 * reserve fell (a dequeued bubble's room released before its row landed) and
 * the browser pulled the follower up into content it already passed. */
function foldClamp(trace) {
	let worst = { by: 0, at: -1 };
	for (let i = 1; i < trace.length; i++) {
		const [, top0, , edge0] = trace[i - 1], [, top1, , edge1] = trace[i];
		const shrink = Math.max(0, (top0 + edge0) - (top1 + edge1));
		const by = (top0 - top1) - shrink;
		if (by > worst.by) worst = { by, at: i };
	}
	// The order under test: the bubble left before its transcript row landed.
	const left = trace.findLastIndex(f => f[5] > 0), users = left < 0 ? 0 : trace[left][6];
	const landed = left < 0 ? -1 : trace.findIndex((f, i) => i > left && f[6] > users);
	const round = rows => rows.map(f => f.map(v => Math.round(v)));
	return { by: worst.by, gapFrames: left < 0 || landed < 0 ? 0 : landed - left - 1,
		around: worst.at < 0 ? [] : round(trace.slice(Math.max(0, worst.at - 3), worst.at + 4)),
		handoff: left < 0 ? [] : round(trace.slice(Math.max(0, left - 2), left + 6)) };
}

async function checkFold(page, log, watch, label) {
	const viewport = await page.locator(LOG).evaluate(el => el.clientHeight);
	log(`${label} ${JSON.stringify({ frames: watch.frames, streaming: watch.streaming, proseDrop: Math.round(watch.proseDrop), viewport, blankFrames: watch.blank.length,
		longest: watch.longest, blank: watch.blank, tracked: watch.tracked, clamp: foldClamp(watch.trace) })}`);
	expect(watch.streaming > 20, `the stream was sampled while live (${watch.streaming} frames)`);
	expect(watch.earlyFold, 'the finished commands folded one by one, before the new prose started');
	// The long opening line folds with the commands once newer prose takes over.
	expect(watch.proseDrop > viewport, `the folding run shrank the prose by more than the viewport (${Math.round(watch.proseDrop)}px > ${viewport}px)`);
	expect(watch.longest === 0, `no streaming frame left the follower on a blank viewport (longest blank run ${watch.longest} frames)`);
	const settled = await page.locator(LOG).evaluate(el => {
		const box = el.getBoundingClientRect();
		const last = [...el.querySelectorAll('.chat-column > [data-transcript-key]')].at(-1).getBoundingClientRect();
		return { rowTop: last.top - box.top, rowBottom: last.bottom - box.top, viewport: el.clientHeight };
	});
	expect(settled.rowBottom > 8 && settled.rowTop < settled.viewport - 8, `the settled reply stays on screen (${JSON.stringify(settled)})`);
}

cases.push({
	name: 'live-run-fold-keeps-the-stream-on-screen', viewports: ['phone'], throttles: ['slow'],
	setup: openFoldChat,
	async act({ page, variant, mark, log }) {
		const { stop } = await watchFold(page);
		await mark('a long flat tool run folds above the follower as new prose streams');
		await typeAndSend(page, `fold ${uniq()} ${scriptTag(variant, 'tool-run-fold', { tps: 8 })}`);
		await waitChat(page, { streaming: 'true' });
		await waitTurnSettled(page, { hold: 800, timeout: 120_000 });
		await checkFold(page, log, await stop(), 'live run fold');
	}
});

// Round-1 review order: the fold first, then a fresh queued bubble docks under
// the follower, then it leaves the queue. Native delivery retires the bubble in
// the same commit its row lands (#1052); the fake Claude CLI's queue snapshot
// (WebSocket) and delivered row (events feed) can arrive apart, so the bubble
// leaves before its transcript row. The floor captured over the bubble must owe
// its whole room (not only the room from before the fold), or the follower is
// clamped up by the bubble and comes back when the row lands. Local phone runs
// on both paths delivered atomically, so the split order is pinned by
// MessageArea.follow-floor.test.ts and this case guards the combined flow.
async function foldThenQueue({ page, variant, mark, log }, label) {
	const { track, stop } = await watchFold(page);
	await mark('fold a long tool run above the follower');
	await typeAndSend(page, `fold ${uniq()} ${scriptTag(variant, 'tool-run-fold', { tps: 8 })}`);
	await waitChat(page, { streaming: 'true' });
	const key = await page.waitForFunction(() => [...document.querySelectorAll('.chat-column > [data-transcript-key]')]
		.findLast(row => row.querySelector('.prose')?.textContent.includes('All sixteen checks ran'))?.dataset.transcriptKey, null, { timeout: 120_000 })
		.then(handle => handle.jsonValue());
	await mark('queue a prompt after the fold, while the new prose streams');
	await typeAndSend(page, `queued after fold ${uniq()} [script:default tps=300]`);
	await page.locator('.pending-messages [data-queue-confirmed="true"]').waitFor();
	await track(key);
	await page.locator('.pending-messages').waitFor({ state: 'hidden', timeout: 120_000 });
	await waitTurnSettled(page, { hold: 3000, timeout: 120_000 });
	const watch = await stop();
	await checkFold(page, log, watch, label);
	expect(watch.tracked > 20, `the folded reply was tracked through the handoff (${watch.tracked} frames)`);
	const clamp = foldClamp(watch.trace);
	expect(clamp.by <= 2, `the follower is never clamped back over the dequeued bubble's room (${clamp.by.toFixed(1)}px beyond collapsed prose)`);
}

cases.push({
	name: 'live-run-fold-then-queue-handoff', viewports: ['phone'], throttles: ['slow'],
	setup: openFoldChat,
	act: ctx => foldThenQueue(ctx, 'fold then queue handoff')
});

// Closing a foldable on a live chat (owner report, 2026-11-18): the SAME page
// goes from the inner screen to the cover screen. ChatActivity handles the
// size change itself (configChanges), so the document is kept; onStop pauses
// the WebView, which holds frames (rAF, scroll, ResizeObserver) until onResume
// while the reply keeps streaming. Emulated by holding those callbacks. This
// order did not leave a blank viewport on main d2fe0a710 either: a guard for
// the transition, not a reproduction of the owner's report.
//
// A stopped page also paints nothing, but headless Chromium keeps painting a
// page that only reports itself hidden, and scores layout shifts in those
// frames. The held ResizeObserver defers the follower's correction for the
// reflow until the next DOM change, past the browser's 500ms excuse for the
// resize, so a frame no closed device shows scored the docked queue scrolling
// into view (#1824 CI). While paused the page is drawn invisible (layout,
// scroll and hit-boxes unchanged); resume shows it in the same frame as the
// held callbacks, as onResume does, and every frame from there is scored.
function holdFramesWhilePaused() {
	const r = (window.__render = { paused: false, rafQ: [], ros: new Set(), scrolls: new Set() });
	const unseen = new CSSStyleSheet();
	unseen.replaceSync('*, *::before, *::after { visibility: hidden !important; }');
	const painted = on => { document.adoptedStyleSheets = on ? document.adoptedStyleSheets.filter(s => s !== unseen) : [...document.adoptedStyleSheets, unseen]; };
	const raf = window.requestAnimationFrame.bind(window), caf = window.cancelAnimationFrame.bind(window);
	window.requestAnimationFrame = cb => { if (!r.paused) return raf(cb); r.rafQ.push(cb); return -r.rafQ.length; };
	window.cancelAnimationFrame = id => { if (id < 0) r.rafQ[-id - 1] = null; else caf(id); };
	const RO = window.ResizeObserver;
	window.ResizeObserver = class extends RO {
		constructor(cb) {
			let self = null;
			super((entries, obs) => { if (r.paused) r.ros.add(self); else cb(entries, obs); });
			self = this; this.cb = cb; this.targets = new Set();
		}
		observe(t, o) { this.targets.add(t); return super.observe(t, o); }
		unobserve(t) { this.targets.delete(t); return super.unobserve(t); }
		disconnect() { this.targets.clear(); r.ros.delete(this); return super.disconnect(); }
	};
	document.addEventListener('scroll', e => { if (r.paused) { e.stopImmediatePropagation(); r.scrolls.add(e.target); } }, true);
	const visibility = hidden => {
		Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => hidden ? 'hidden' : 'visible' });
		Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
		document.dispatchEvent(new Event('visibilitychange'));
	};
	r.stop = () => { visibility(true); r.paused = true; painted(false); };
	r.resume = () => {
		painted(true);
		visibility(false);
		r.paused = false;
		const queued = r.rafQ.splice(0);
		raf(t => {
			for (const target of r.scrolls) target.dispatchEvent(new Event('scroll'));
			r.scrolls.clear();
			for (const ro of [...r.ros]) ro.cb([...ro.targets].map(target => ({ target, contentRect: { width: target.clientWidth, height: target.clientHeight } })), ro);
			r.ros.clear();
			for (const cb of queued) cb?.(t);
		});
	};
}

/** A long two-turn chat on the inner screen, reopened in the shell that runs on the device. */
async function openCloseChat(ctx) {
	const { page, status, variant } = ctx;
	await page.addInitScript(holdFramesWhilePaused);
	await openNewChat(ctx);
	const token = `foldclose-${uniq()}`;
	await typeAndSend(page, `${token} ${scriptTag(variant, 'long', { tps: 4000 })}`);
	await waitTurnSettled(page, { messagesAtLeast: 2, hold: 300, timeout: 120_000 });
	await typeAndSend(page, `more ${uniq()} ${scriptTag(variant, 'long', { tps: 4000 })}`);
	await waitTurnSettled(page, { messagesAtLeast: 4, hold: 300, timeout: 120_000 });
	const id = await chatIdByToken(page, token);
	await gotoApp(page, status, `${mobile ? '/mobile/' : '/'}?session=${encodeURIComponent(id)}`);
	await waitChatOpen(page, { messagesAtLeast: 4, hold: 800 });
}

cases.push({
	name: 'fold-close-resume-keeps-the-live-reply-on-screen', viewports: ['fold-inner'], throttles: ['slow'],
	setup: openCloseChat,
	async act({ page, variant, mark, log }) {
		await mark('a plain reply streams on the inner screen, followed');
		await typeAndSend(page, `live ${uniq()} ${scriptTag(variant, 'long', { tps: 25 })}`);
		await waitChat(page, { streaming: 'true' });
		await page.waitForTimeout(2500);
		await page.evaluate(LOG => {
			const w = window.__foldFrames = { frames: [], running: true };
			const tick = () => {
				if (!w.running) return;
				const log = document.querySelector(LOG);
				const box = log.getBoundingClientRect();
				const seen = el => { const r = el.getBoundingClientRect(); return r.height > 0 && r.bottom > box.top + 8 && r.top < box.bottom - 8; };
				const rows = [...log.querySelectorAll('.chat-column > [data-transcript-key]')];
				const last = rows.at(-1)?.getBoundingClientRect();
				w.frames.push({ w: innerWidth, top: log.scrollTop, height: log.scrollHeight, viewport: log.clientHeight,
					blank: rows.length > 0 && !rows.some(seen) && ![...log.querySelectorAll('.tail-controls, .turn-activity')].some(seen),
					lastTop: last ? last.top - box.top : null, lastBottom: last ? last.bottom - box.top : null,
					streaming: document.querySelector('[data-ui-state="chat"]')?.dataset.uiStreaming });
				requestAnimationFrame(tick);
			};
			requestAnimationFrame(tick);
		}, LOG);
		await mark('close the device: the page is hidden and paused, then sized to the cover screen');
		await page.evaluate(() => window.__render.stop());
		await page.waitForTimeout(300);
		await page.setViewportSize({ width: 360, height: 882 });
		await page.waitForTimeout(1500);
		await mark('resume the same chat on the cover screen');
		await page.evaluate(() => window.__render.resume());
		await page.waitForTimeout(3000);
		const frames = (await page.evaluate(() => { window.__foldFrames.running = false; return window.__foldFrames.frames; })).filter(f => f.w === 360);
		let run = 0, longest = 0;
		for (const f of frames) { run = f.blank ? run + 1 : 0; longest = Math.max(longest, run); }
		const end = frames.at(-1);
		log(`fold close ${JSON.stringify({ frames: frames.length, longest, first: frames[0], end })}`);
		expect(frames.filter(f => f.streaming === 'true').length > 20, `the cover screen was sampled while the reply streamed (${frames.length} frames)`);
		expect(longest === 0, `no cover-screen frame shows a blank transcript (longest blank run ${longest} frames)`);
		expect(end.lastBottom > 8 && end.lastTop < end.viewport - 8, `the followed live reply is on screen after resume (${JSON.stringify(end)})`);
	}
});

/** Per painted frame on the cover screen: scroll, the followed content end and
 * blank runs, the row carrying `token`, the queue and, when `bubble` names the
 * words of a queued prompt, whether that bubble is drawn in this frame. No
 * frames run while paused. */
function sampleCover({ LOG, token, bubble: words }) {
	const w = window.__cover = { running: true, frames: [] };
	// Inside the scroller, opaque, with nothing over it: three hit-tests down its
	// left edge must land in this bubble, in the same frame its box is read.
	const drawn = (el, log, box) => {
		if (!el) return null;
		const r = el.getBoundingClientRect();
		let opacity = 1, shown = getComputedStyle(el).visibility === 'visible';
		for (let at = el; at && at !== log; at = at.parentElement) opacity *= +getComputedStyle(at).opacity;
		const x = r.left + Math.min(24, r.width / 2);
		const hits = [r.top + 8, (r.top + r.bottom) / 2, r.bottom - 8].filter(y => el.contains(document.elementFromPoint(x, y))).length;
		return { top: Math.round(r.top - box.top), bottom: Math.round(r.bottom - box.top), hits, opacity: Math.round(opacity * 100) / 100,
			ok: r.height > 16 && r.top >= box.top && r.bottom <= box.bottom && shown && opacity > 0.99 && hits === 3,
			text: words.every(s => el.textContent.includes(s)) };
	};
	const tick = () => {
		const log = document.querySelector(LOG);
		if (!w.running || !log) return;
		const box = log.getBoundingClientRect();
		const seen = el => { const r = el.getBoundingClientRect(); return r.height > 0 && r.bottom > box.top + 8 && r.top < box.bottom - 8; };
		const rows = [...log.querySelectorAll('.chat-column > [data-transcript-key]')];
		const controls = [...log.querySelectorAll('.tail-controls, .tail-error, .turn-activity')].filter(el => el.getBoundingClientRect().height > 0);
		const prompt = rows.findLast(el => el.textContent.includes(token));
		const queuedBubble = words && [...log.querySelectorAll('.pending-messages [data-queue-confirmed="true"]')].find(el => el.textContent.includes(words[0]));
		w.frames.push({ w: innerWidth, at: Math.round(performance.now()), top: log.scrollTop, viewport: log.clientHeight,
			// The followed edge: the last row or a docked control, in scroll coordinates.
			end: log.scrollTop + Math.max(...[rows.at(-1), ...controls].filter(Boolean).map(el => el.getBoundingClientRect().bottom - box.top)),
			blank: rows.length > 0 && !rows.some(seen) && !controls.some(seen),
			promptTop: prompt ? Math.round(prompt.getBoundingClientRect().top - box.top) : null,
			promptBottom: prompt ? Math.round(prompt.getBoundingClientRect().bottom - box.top) : null,
			// The collapse under test: tool-run-fold's long opener folded away (see runFolded).
			folded: (p => p.some(e => e.textContent.includes('All sixteen checks ran')) && !p.some(e => e.textContent.includes('Checking the project step by step')))(
				[...log.querySelectorAll('.chat-column .prose')]),
			...(words ? { bubble: drawn(queuedBubble, log, box) } : {}),
			users: log.querySelectorAll('[data-user-turn-id]').length, queued: log.querySelectorAll('.pending-messages [data-queue-confirmed="true"]').length,
			streaming: document.querySelector('[data-ui-state="chat"]')?.dataset.uiStreaming });
		requestAnimationFrame(tick);
	};
	requestAnimationFrame(tick);
}

/** Resumed frames until the transcript or queue changes: a follower's scroll may
 * only reveal content past the viewport (MIN_TRANSCRIPT_TAIL_RESERVE below it),
 * never move into the blank reserve owed to the reply. */
function coverResume(frames, from) {
	let longest = 0, run = 0, worst = { by: 0, at: -1 }, i = from;
	for (; i < frames.length; i++) {
		const f = frames[i], prev = frames[i - 1];
		run = f.blank ? run + 1 : 0;
		longest = Math.max(longest, run);
		if (i === from || f.users !== prev.users || f.queued !== prev.queued) { if (i > from) break; continue; }
		const by = f.top - Math.max(prev.top, f.end - f.viewport + 24);
		if (by > worst.by) worst = { by, at: i };
	}
	return { longest, frames: i - from, by: Math.round(worst.by), around: worst.at < 0 ? [] : frames.slice(Math.max(from, worst.at - 2), worst.at + 2) };
}

// Past the minimum tail, room for the reply's next row (its activity line) as
// the catch-up lands; the regression parked the reader 245-261px into the reserve.
const RESUME_SLACK = 48;
const pauseClose = async (page) => { await page.evaluate(() => window.__render.stop()); await page.setViewportSize({ width: 360, height: 882 }); };
const pausedUntil = (page, fn, arg) => page.waitForFunction(fn, arg, { polling: 50, timeout: 120_000 });

// Round-2 review order: the device closes while a confirmed queued bubble is
// docked under the follower, and the tool run above it folds. An ordinary
// queued prompt is handed into the running turn at its next tool boundary
// (steering), so its bubble leaves before the fold. A note reference needs turn
// setup and is never steered (session_runtime._steer_refusal): this bubble stays
// docked until the turn ends, then runs as its own turn (a missing note is sent
// as typed). A hidden page paints no fold: the feed defers streamed text and the
// transcript window mounts no new rows until visible. So the server finishes the
// tools while the device is closed, and the collapse paints in the catch-up at
// resume, on the cover screen, with the bubble still docked. The commands fold
// one by one as they finish, a row at a time; the collapse is the long opening
// line, which folds with them only once the final prose takes over from it.
const runFolded = () => {
	const prose = [...document.querySelectorAll('.chat-column .prose')];
	return prose.some(e => e.textContent.includes('All sixteen checks ran'))
		&& !prose.some(e => e.textContent.includes('Checking the project step by step'));
};
cases.push({
	name: 'fold-close-across-a-tool-fold-keeps-reply-and-queue-on-screen', viewports: ['fold-inner'], throttles: ['slow'],
	async setup(ctx) {
		await ctx.page.addInitScript(holdFramesWhilePaused);
		ctx.foldChatId = await openFoldChat(ctx, mobile ? '/mobile/' : '/');
	},
	async act({ page, variant, mark, log, foldChatId: session }) {
		const queued = `queued-note-${uniq()}`;
		expect(!!session, 'setup named the chat');
		await mark('a long tool run streams on the inner screen, followed');
		await typeAndSend(page, `fold ${uniq()} ${scriptTag(variant, 'tool-run-fold', { tps: 8 })}`);
		await page.waitForFunction(() => [...document.querySelectorAll('.chat-column .prose')].some(e => e.textContent.includes('Checking the project')), null, { timeout: 60_000 });
		await mark('queue a wrapping prompt the running turn cannot take: it names a note');
		await typeAndSend(page, `${queued} runs as its own turn, it names @Notes/note-0f0f0f0f\nThis second line wraps on the cover display ${'word '.repeat(14)}[script:default tps=300]`);
		await page.locator('.pending-messages [data-queue-confirmed="true"]').waitFor();
		expect(!(await page.evaluate(runFolded)), 'the bubble was confirmed before the opening line folded');
		await page.evaluate(sampleCover, { LOG, token: queued, bubble: [queued, '@Notes/note-0f0f0f0f'] });
		await mark('close the device: hidden and paused, then sized to the cover screen');
		await pauseClose(page);
		// The server, not the hidden page, says when the run is done: all sixteen
		// commands completed, so the final prose (and the fold) is under way.
		// Polled from Node: the hidden page's own feed is what is under test.
		const url = `${new URL(page.url()).origin}/api/v1/sessions/${encodeURIComponent(session)}`;
		let ran = 0;
		for (const until = Date.now() + 120_000; ran < 16 && Date.now() < until; await page.waitForTimeout(250)) {
			const response = await page.request.get(url);
			const { messages = [] } = response.ok() ? await response.json() : {};
			ran = messages.filter(m => m.type === 'tool_use' && m.toolName === 'Bash' && m.toolStatus && m.toolStatus !== 'running').length;
		}
		expect(ran >= 16, `the server finished all sixteen commands while the device was closed (${ran})`);
		await page.waitForTimeout(1200);
		const paused = { folded: await page.evaluate(runFolded), queued: await page.locator('.pending-messages [data-queue-confirmed="true"]').count() };
		expect(!paused.folded && paused.queued === 1, `while closed, the opening line had not folded on the page and the bubble is docked (${JSON.stringify(paused)})`);
		await mark('resume the same chat on the cover screen: the run folds in the catch-up');
		const from = await page.evaluate(() => { window.__render.resume(); return window.__cover.frames.length; });
		await page.waitForFunction(runFolded, null, { polling: 'raf', timeout: 15_000 }).catch(() => {});
		await page.waitForTimeout(1200);
		// What the reader sees: hit-test the live prose and the bubble, not only their boxes.
		const shown = await page.locator(LOG).evaluate((log, token) => {
			const box = log.getBoundingClientRect();
			const hit = el => {
				if (!el) return { mounted: false };
				const r = el.getBoundingClientRect(), top = Math.max(r.top, box.top), bottom = Math.min(r.bottom, box.bottom);
				if (bottom - top < 8) return { mounted: true, painted: false, top: r.top - box.top, bottom: r.bottom - box.top };
				const at = document.elementFromPoint(r.left + Math.min(24, r.width / 2), (top + bottom) / 2);
				return { mounted: true, painted: !!at && el.contains(at), top: Math.round(r.top - box.top), bottom: Math.round(r.bottom - box.top) };
			};
			const prose = [...log.querySelectorAll('.chat-column .prose')].findLast(e => e.textContent.includes('All sixteen checks ran'));
			const bubble = log.querySelector('.pending-messages [data-queue-confirmed="true"]');
			return { viewport: log.clientHeight, prose: hit(prose), bubble: { ...hit(bubble), text: !!bubble?.textContent.includes(token) && bubble.textContent.includes('@Notes/note-0f0f0f0f') },
				streaming: document.querySelector('[data-ui-state="chat"]')?.dataset.uiStreaming };
		}, queued);
		const resumed = await page.evaluate(() => window.__cover.frames.slice());
		const check = coverResume(resumed, from);
		const folded = await page.evaluate(runFolded);
		// Resumed frames before the collapse painted, and the first frame it did.
		const foldAt = resumed.findIndex((f, i) => i >= from && f.folded) - from;
		const atFold = resumed[from + foldAt];
		log(`fold close with queue ${JSON.stringify({ paused, folded, foldAt, atFold, shown, check, first: resumed[from] })}`);
		expect(folded, 'the tool run folded once the page was visible again');
		expect(foldAt >= 0 && foldAt < check.frames && atFold.queued === 1, `the run folded on the cover screen with the bubble still docked (frame ${foldAt} of ${check.frames})`);
		// The collapse frame itself, sampled in one tick: this prompt's bubble is
		// inside the scroller, opaque and on top, showing its own words. A bubble
		// covered, clipped or pushed off screen there and only drawn later fails.
		expect(atFold.bubble?.ok && atFold.bubble.text, `the queued prompt is drawn on screen with its own words in the frame the run folds (${JSON.stringify(atFold.bubble)})`);
		expect(shown.streaming === 'true' && shown.bubble.mounted, `the reply still streams over the mounted bubble after resume (${JSON.stringify(shown)})`);
		expect(check.frames > 20 && check.longest === 0, `no resumed cover frame is blank (${check.longest} of ${check.frames})`);
		expect(check.by <= RESUME_SLACK, `the follower is not scrolled into the blank reserve on resume (${check.by}px past the content end: ${JSON.stringify(check.around)})`);
		expect(shown.prose.painted, `the live prose is painted on screen after the fold (${JSON.stringify(shown.prose)})`);
		expect(shown.bubble.painted && shown.bubble.text, `the queued prompt is still painted on screen with its own words (${JSON.stringify(shown.bubble)})`);
		await mark('the turn ends: the queued prompt runs as its own turn');
		await page.locator('.pending-messages').waitFor({ state: 'hidden', timeout: 120_000 });
		await waitTurnSettled(page, { hold: 800, timeout: 120_000 });
		const settled = await page.evaluate(() => { window.__cover.running = false; return window.__cover.frames.at(-1); });
		expect(settled.promptTop !== null && settled.promptTop < settled.viewport - 8, `the delivered prompt is on screen or scrolled past by its reply (${JSON.stringify(settled)})`);
	}
});

// Round-2 review order: the device closes on a live reply with a prompt queued;
// the reply ends and the prompt is delivered while the page is paused, then it
// resumes on the cover screen. The hidden page mounts no row for the delivered
// prompt, so its anchor was dropped as missing; resume then pinned the follower
// to the bottom of the reserve its reply had not filled yet: everything jumped
// up, the prompt off the top, over a blank band (main d2fe0a710). The same flow
// on a cover screen that is never hidden only reveals the reply as it grows.
cases.push({
	name: 'fold-close-dequeue-while-paused-keeps-the-prompt-in-place', viewports: ['fold-inner'], throttles: ['slow'],
	setup: openCloseChat,
	async act({ page, variant, mark, log }) {
		const queued = `queued-close-${uniq()}`;
		await mark('a plain reply streams on the inner screen; a wrapping prompt waits in the queue');
		await typeAndSend(page, `live ${uniq()} ${scriptTag(variant, 'long', { tps: 300 })}`);
		await waitChat(page, { streaming: 'true' });
		await typeAndSend(page, `${queued} two lines to check the clearance\nThis line wraps on the cover display ${'word '.repeat(14)}[script:slow tps=6 latency_ms=0]`);
		await page.locator('.pending-messages [data-queue-confirmed="true"]').waitFor();
		await page.evaluate(sampleCover, { LOG, token: queued });
		await mark('close the device; the reply ends and the prompt is delivered while paused');
		await pauseClose(page);
		// The hidden page mounts no new row, so the bubble leaving is the signal.
		await pausedUntil(page, () => !document.querySelector('.pending-messages [data-queue-confirmed="true"]'));
		await page.waitForTimeout(600);
		await mark('resume the same chat on the cover screen');
		const from = await page.evaluate(() => { window.__render.resume(); return window.__cover.frames.length; });
		await page.waitForTimeout(1500);
		const resumed = await page.evaluate(() => { window.__cover.running = false; return window.__cover.frames; });
		const check = coverResume(resumed, from);
		const end = resumed.at(-1);
		log(`fold close dequeue ${JSON.stringify({ check, first: resumed[from], end })}`);
		expect(check.frames > 20 && check.longest === 0, `no resumed cover frame is blank (${check.longest} of ${check.frames})`);
		expect(check.by <= RESUME_SLACK, `the follower is not scrolled into the blank reserve on resume (${check.by}px past the content end: ${JSON.stringify(check.around)})`);
		expect(end.promptTop !== null && end.promptBottom > 24 && end.promptTop < end.viewport - 8, `the delivered prompt stays on screen while its reply fits below it (${JSON.stringify(end)})`);
	}
});

async function openSwitchTarget(ctx, title) {
	const { page, variant } = ctx;
	if (mobile && variant.viewport === 'phone') {
		await page.getByRole('button', { name: /^Open menu/ }).click();
		await sidebarRow(page, title).click();
	} else {
		await openFromSidebar(page, title);
	}
	await waitChatOpen(page, { messagesAtLeast: 2, hold: 300 });
}

// Run in parts (see lib/split.mjs) so one file does not fill a CI shard.
export const journey = { name: 'chat-position-stability', cases, allow: [...DEV_INSTANCE_NOISE] };

// The first two parts were balanced on the per-case seconds of main run 42719765869.
export const FIRST_PART = [
	'error-rebase-keeps-visible-prompt',
	'long-history-short-send',
	'fast-completion',
	'long-to-short-navigation',
	'short-send',
	'fresh-queued-send-releases-error-slot'
];

// Part 2 takes every case not named by parts 1 and 3.
export const THIRD_PART = [
	'live-run-fold-keeps-the-stream-on-screen',
	'live-run-fold-then-queue-handoff'
];
