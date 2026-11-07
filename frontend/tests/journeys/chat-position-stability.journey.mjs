// Regression journeys for the mobile scroll/composer fix chain. Geometry is
// sampled after layout on every painted frame: transforms and a down/up bounce
// do not necessarily create a LayoutShift entry. All replies/queues/errors are
// produced by real UI sends through the devloop's fake providers; the long
// error/Stop case specifically requires the seeded Claude Code CLI project.
import { writeFileSync } from 'node:fs';
import { runJourney } from './runner.mjs';
import { SEEDED, api, composer, expect, gotoApp, openFromSidebar, openPopup, pick, pickAgent, pickProject, scriptTag, seededChatId, sendButton, sidebarRow, typeAndSend, uniq, waitChat, waitChatOpen, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const mobile = process.argv.includes('--mobile-shell');
const LOG = '[role="log"][aria-label="Conversation messages"]';
const prefix = (variant) => mobile && variant.viewport === 'phone' ? '/mobile/' : '/';

/** IO entries are the browser's post-layout geometry, not an rAF read before
 * the app's own scroll callback. Keep chronological order at 4x CPU too. */
async function watchFrames(page, { deferAssertions = false } = {}) {
	await page.evaluate(({ deferAssertions, articleDiagnostics }) => {
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
				banner: document.querySelector('.chat-error-banner'), pending: document.querySelector('.pending-messages .userrow:last-child'),
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
	}, { deferAssertions, articleDiagnostics: process.env.JOURNEY_TRACE_ARTICLE === '1' });
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
	prose: travel(frames, 'prose'), banner: travel(frames, 'banner'), pending: travel(frames, 'pending'),
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
		await page.locator('.chat-error-banner').filter({ hasText: 'claude reported success: API Error: 500' }).waitFor({ timeout: 60_000 });
		await mark('Stop the failed CLI turn with queued messages');
		await page.evaluate(() => { window.__chatFrames.phase = 'stop-requested'; });
		await page.getByRole('button', { name: 'Stop generating', exact: true }).click();
		await waitTurnSettled(page, { hold: 4500 });
		const frames = await stop();
		log(`error/Stop geometry ${JSON.stringify(summary(frames))}`);
		const banners = frames.filter(f => f.banner);
		expect(banners.length > 4, 'the real CLI error banner appeared and was sampled');
		if (scrolledUp) expect(travel(frames, 'anchor') <= 2, 'error and Stop leave the older row being read alone');
		else {
			expect(travel(banners, 'banner') <= 2, `the error stays still through Stop (${travel(banners, 'banner').toFixed(1)}px)`);
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
			await page.evaluate(token => {
				const pane = document.querySelector('.msg-scroll');
				const view = document.querySelector('[data-ui-state="chat"]');
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
					if (tracked && document.querySelector('.chat-error-banner') && view.dataset.uiStreaming === 'false' && p.errorAt === null) p.errorAt = t;
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
			}, token);
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
		await page.locator('.chat-error-banner').waitFor({ timeout: 60_000 });
		await page.waitForFunction(token => document.querySelector('[data-ui-state="chat"]')?.dataset.uiStreaming === 'true'
			&& [...document.querySelectorAll('[data-user-turn-id]')].some(row => row.textContent.includes(token)), ctx.firstQueueToken);
		const held = await page.locator('.tail-controls').evaluate(el => parseFloat(el.style.minHeight));
		expect(held >= 150, `two confirmed queued bubbles produced a real held slot (${held}px)`);
	},
	async act({ page, mark, log, freshQueueToken }) {
		const readState = () => page.evaluate(token => ({
			streaming: document.querySelector('[data-ui-state="chat"]')?.dataset.uiStreaming,
			session: document.querySelector('[data-ui-state="chat"]')?.dataset.uiSession,
			users: document.querySelectorAll('[data-user-turn-id]').length,
			freshInTranscript: [...document.querySelectorAll('[data-user-turn-id]')].some(row => row.textContent.includes(token)),
			freshPending: document.querySelector('.pending-messages')?.textContent.includes(token) ?? false,
			slot: parseFloat(document.querySelector('.tail-controls')?.style.minHeight),
			error: !!document.querySelector('.chat-error-banner')
		}), freshQueueToken);
		await mark('fresh local send during the automatically delivered queued stream');
		const before = await readState();
		expect(before.streaming === 'true' && before.slot >= 150 && before.error,
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

await runJourney({ name: 'chat-position-stability', cases, allow: [...DEV_INSTANCE_NOISE] });
