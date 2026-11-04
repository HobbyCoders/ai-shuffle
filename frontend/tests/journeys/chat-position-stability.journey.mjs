// Regression journeys for the mobile scroll/composer fix chain. Geometry is
// sampled after layout on every painted frame: transforms and a down/up bounce
// do not necessarily create a LayoutShift entry. All replies/queues/errors are
// produced by real UI sends through the devloop's fake providers; the long
// error/Stop case specifically requires the seeded Claude Code CLI project.
import { writeFileSync } from 'node:fs';
import { runJourney } from './runner.mjs';
import { api, composer, expect, gotoApp, openPopup, pick, pickAgent, pickProject, scriptTag, seededChatId, sendButton, sidebarRow, typeAndSend, uniq, waitChat, waitChatOpen, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const mobile = process.argv.includes('--mobile-shell');
const LOG = '[role="log"][aria-label="Conversation messages"]';
const prefix = (variant) => mobile && variant.viewport === 'phone' ? '/mobile/' : '/';

/** IO entries are the browser's post-layout geometry, not an rAF read before
 * the app's own scroll callback. Keep chronological order at 4x CPU too. */
async function watchFrames(page, { deferAssertions = false } = {}) {
	await page.evaluate((deferAssertions) => {
		const viewport = document.querySelector('[role="log"][aria-label="Conversation messages"]');
		const anchor = [...document.querySelectorAll('[data-transcript-key]')].find(el => el.getBoundingClientRect().bottom > (viewport?.getBoundingClientRect().top ?? 0) + 1);
		const watch = window.__chatFrames = { running: true, next: 0, frames: [], shifts: [], phase: 'before-stop', assertions: !deferAssertions };
		const nodes = new WeakMap();
		let nextNode = 0;
		const nodeId = el => {
			if (!nodes.has(el)) nodes.set(el, nextNode++);
			return nodes.get(el);
		};
		watch.shiftObserver = new PerformanceObserver(list => {
			for (const entry of list.getEntries()) watch.shifts.push({ at: entry.startTime, value: entry.value, hadRecentInput: entry.hadRecentInput, lastInputTime: entry.lastInputTime,
				sources: entry.sources.map(source => ({ html: source.node?.outerHTML?.slice(0, 500), previous: source.previousRect, current: source.currentRect })) });
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
				bubble: el.querySelector('.bubble'), actions: el.querySelector('.pending-actions')
			})).filter((row, i, all) => row.pending || i >= all.length - 4);
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
				frame.rows = rows.map(({ el, bubble, actions, ...identity }) => ({ ...identity, rect: geometry(el), bubble: geometry(bubble), actions: geometry(actions) }));
				frame.paintedAt = entries[0]?.time;
				frame.deliveredAt = performance.now();
				frame.phase = watch.phase;
				watch.frames.push(frame);
			});
			for (const el of [...Object.values(elements), ...rows.flatMap(row => [row.el, row.bubble, row.actions])].filter(Boolean)) observer.observe(el);
			requestAnimationFrame(sample);
		};
		requestAnimationFrame(sample);
	}, deferAssertions);
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
		const hero = page.getByRole('region', { name: 'New session' });
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

async function ensureFast(page) {
	// A mobile portal can already be open before its dialog semantics mount.
	// Close it through the real control path before choosing the model.
	await page.keyboard.press('Escape');
	const models = page.getByRole('dialog', { name: mobile ? 'Model' : 'Choose model', exact: true });
	await openPopup(page.getByRole('button', { name: /^Model:/ }).first(), models);
	await models.getByRole('textbox', { name: 'Search models' }).fill('Opus');
	await models.getByRole('option', { name: /^Opus/ }).first().click();
	if (mobile) {
		await models.waitFor({ state: 'hidden' });
		await page.getByRole('button', { name: /^All chat controls/ }).click();
		const fast = page.getByRole('switch', { name: 'Fast mode', exact: true });
		await fast.waitFor();
		if (await fast.getAttribute('aria-checked') !== 'true') await fast.click();
		await page.keyboard.press('Escape');
		return;
	}
	await page.keyboard.press('Escape');
	const fast = page.getByRole('button', { name: 'Fast mode', exact: true });
	await openPopup(page.getByRole('button', { name: /^Effort:/ }).first(), fast);
	if (await fast.getAttribute('aria-pressed') !== 'true') await fast.click();
	await page.keyboard.press('Escape');
	await page.waitForTimeout(400);
}

async function openHistory(ctx) {
	await gotoApp(ctx.page, ctx.status, `${prefix(ctx.variant)}?session=${seededChatId(ctx.status, 'no_project')}`);
	await waitChatOpen(ctx.page, { messagesAtLeast: 2 });
}

const cases = [{
	name: 'fast-completion',
	async setup(ctx) { await openClaude(ctx); await ensureFast(ctx.page); },
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
		await sendButton(page).click();
		await waitTurnSettled(page, { hold: 3000 });
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
		await pickAgent(ctx.page, ctx.page); // Dev native: CLI sends can become steers.
		// Keep the seed's explicit Fake AI provider model. A brand-new tab's
		// remembered Claude override can otherwise turn this into a CLI steer.
		await api(ctx.page, 'POST', `/sessions/${seededChatId(ctx.status, 'no_project')}/clear`, {});
		await waitChat(ctx.page, { surface: 'cleared', messagesAtMost: 0 });
	},
	async act({ page, mark, variant, log }) {
		for (let i = 0; i < 3; i++) {
			await mark(`send long turn ${i + 1}`);
			await typeAndSend(page, `handoff ${uniq()} ${scriptTag(variant, 'long', { tps: 300 })}`);
			await waitChat(page, { streaming: 'true' });
			await mark('queue next turn');
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
		await pickAgent(ctx.page, ctx.page); // Exercise native auto-delivery, not CLI steer.
	},
	async act({ page, mark, variant, log }) {
		await typeAndSend(page, `reader handoff ${uniq()} ${scriptTag(variant, 'long', { tps: 120 })}`);
		await waitChat(page, { streaming: 'true' });
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
		await page.evaluate(() => { window.__chatFrames.assertions = true; });
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
		else expect(travel(banners, 'banner') <= 2, `the error stays still through Stop (${travel(banners, 'banner').toFixed(1)}px)`);
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
		await pickAgent(ctx.page, ctx.page.getByRole('region', { name: 'New session' }));
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

cases.push({
	name: 'long-history-short-send',
	async setup(ctx) { await openClaude(ctx, { history: true }); },
	act: cases.find(test => test.name === 'short-send').act
});

await runJourney({ name: 'chat-position-stability', cases, allow: [...DEV_INSTANCE_NOISE] });
