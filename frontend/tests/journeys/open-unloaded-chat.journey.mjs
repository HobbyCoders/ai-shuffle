// Journey: open a chat whose history this tab has not loaded yet.
//
// The surface may only move forward: the screen you were on → loading → the
// opened chat. The composer moves at most once, when the outcome is known; it
// must never go centre → bottom → centre (or back) while the history arrives,
// and the transcript must not remount.
//
// Bug this guards (flight recorder on main, 2026-10-29): opening a chat that
// turns out empty (a cleared chat) from the new-session screen docked the
// composer at the bottom for the load and snapped it back to the centre 1-3
// frames later. Root cause: the 'loading' surface always used the message
// column layout. Fix: a load keeps the layout the view already had
// (createChatLayoutTracker in src/lib/components/layout/chat-empty-state.ts).
//
// Entry points (every way a chat is opened) × target (a long transcript, an
// empty cleared chat):
//   <target>-from-new      new-session screen → sidebar row
//   <target>-from-chat     another open chat  → sidebar row
//   long-from-cleared      an empty chat      → sidebar row (hero → column)
//   <target>-deep-link     a full load of /?session=<id>
//
// Each case's `verify` names the composer positions it is designed to take,
// read on every painted frame: one entry per position, so a second move, or
// a move there and back, fails even when it is too small or too quick to
// count as a layout shift.
import { runJourney } from './runner.mjs';
import { SEEDED, expect, gotoApp, openFromSidebar, seededChatId, waitChat, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { expectComposerPositions } from './lib/timeline.mjs';

/** The composer takes exactly these positions over the case. */
const composer = (...positions) => ({ recording }) => expectComposerPositions(recording, positions);

/** What each target looks like once open. */
const OPENED = {
	long: (page) => waitChatOpen(page, { messagesAtLeast: 50 }),
	cleared: async (page) => {
		await waitChat(page, { surface: 'cleared', loading: 'false', composer: 'centre', session: /^s:/ });
		await page.getByText('Conversation cleared', { exact: false }).waitFor();
		await page.waitForTimeout(1500);
	}
};

const fromNew = async ({ page, status }) => {
	await gotoApp(page, status);
	await waitChat(page, { surface: 'new' });
	await page.waitForTimeout(1000);
};

const fromChat = (key) => async ({ page, status }) => {
	await gotoApp(page, status, `/?session=${encodeURIComponent(seededChatId(status, key))}`);
	await waitChat(page, { loading: 'false', session: /^s:/ });
	await page.waitForTimeout(1000);
};

const viaSidebar = (target) => async ({ page, mark }) => {
	await mark(`open the ${target} chat from the sidebar`);
	await openFromSidebar(page, SEEDED[target]);
	await OPENED[target](page);
};

const viaDeepLink = (target) => async ({ page, status, mark }) => {
	await mark(`load a link to the ${target} chat`);
	await page.goto(new URL(`/?session=${encodeURIComponent(seededChatId(status, target))}`, status.urls.ui).toString());
	await OPENED[target](page);
};

// The switch-flash cases use the REAL mobile shell, unlike a narrow desktop
// viewport. Hold only delivery of an actual native SSE response: no reducer,
// content or loading flag is fabricated.
async function prepareHeldSwitch(ctx, delayMs) {
	const { page, status, variant } = ctx;
	const target = seededChatId(status, 'other_project');
	await page.addInitScript(({ target, delayMs }) => {
		const fetchOriginal = window.fetch.bind(window);
		const hold = window.__switchHydrationHold = { started: null, released: null };
		window.fetch = async (input, init) => {
			const response = await fetchOriginal(input, init);
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (url.pathname === `/api/v1/sessions/${target}/events` && hold.started === null) {
				hold.started = performance.now();
				await new Promise(resolve => setTimeout(resolve, delayMs));
				hold.released = performance.now();
			}
			return response;
		};
	}, { target, delayMs });
	const prefix = variant.viewport === 'phone' ? '/mobile/' : '/';
	await page.goto(new URL(`${prefix}?session=${encodeURIComponent(seededChatId(status, 'no_project'))}`, status.urls.ui).toString());
	await page.getByRole('textbox', { name: 'Message' }).waitFor({ timeout: 60_000 });
	await waitChatOpen(page, { messagesAtLeast: 2, hold: 500 });
	await page.getByRole('textbox', { name: 'Message' }).fill('Draft for the outgoing chat');
}

async function openSwitchChat(page, key) {
	if (new URL(page.url()).pathname.startsWith('/mobile/')) {
		await page.getByRole('button', { name: /^Open menu/ }).click();
		const group = page.getByRole('group', { name: `${SEEDED[key]} session`, exact: true });
		await group.getByRole('button').first().click();
	} else await openFromSidebar(page, SEEDED[key]);
}

async function sampleSwitchFrames(page) {
	await page.evaluate(() => {
		const composer = document.querySelector('[role="textbox"][aria-label="Message"]');
		const log = document.querySelector('[role="log"][aria-label="Conversation messages"]');
		const firstRow = log?.querySelector('.msg-virtualized');
		window.__switchHydrationFrames = [];
		window.__switchHydrationSampling = true;
		const sample = t => {
			if (!window.__switchHydrationSampling) return;
			const chat = document.querySelector('[data-ui-state="chat"]');
			const pending = !!document.querySelector('[data-transcript-pending="true"]');
			const loader = document.querySelector('.loading-transcript');
			const rows = [...document.querySelectorAll('[role="log"] .msg-virtualized')];
			window.__switchHydrationFrames.push({
				t, pending, heldAt: window.__switchHydrationHold.started,
				loader: !!loader && getComputedStyle(loader).visibility !== 'hidden',
				visibleRows: rows.filter(row => getComputedStyle(row).visibility !== 'hidden').length,
				outgoingConnected: !!firstRow?.isConnected,
				composerSame: composer === document.querySelector('[role="textbox"][aria-label="Message"]'),
				logSame: log === document.querySelector('[role="log"][aria-label="Conversation messages"]'),
				outgoingDraft: composer?.textContent?.includes('Draft for the outgoing chat'),
				surface: chat?.dataset.uiSurface,
				session: chat?.dataset.uiSession,
				target: chat?.dataset.uiTargetSession,
				loading: chat?.dataset.uiLoading
			});
			requestAnimationFrame(sample);
		};
		requestAnimationFrame(sample);
	});
}

async function finishSwitchFrames(page, log, { slow = false } = {}) {
	const frames = await page.evaluate(() => {
		window.__switchHydrationSampling = false;
		return window.__switchHydrationFrames;
	});
	const pending = frames.filter(frame => frame.pending && frame.heldAt !== null);
	// The first 100ms is deliberately inside the production 180ms grace, with
	// room for response-header/frame scheduling jitter on 4x CPU.
	const underGrace = pending.filter(frame => frame.t - frame.heldAt < 100);
	expect(underGrace.length > 0, 'observed the real incoming history while under grace');
	expect(underGrace.every(frame => !frame.loader && frame.visibleRows > 0 && frame.outgoingConnected), 'no empty/skeleton frame under grace');
	expect(frames.every(frame => frame.composerSame && frame.logSame), 'composer and MessageArea retain their identity');
	expect(pending.every(frame => !frame.outgoingDraft), 'the incoming composer never displays the outgoing draft');
	if (slow) {
		expect(pending.some(frame => frame.loader && frame.outgoingConnected), 'a slow load shows its overlay without discarding the old rows');
	}
	log(`switch frames=${frames.length}, pending=${pending.length}, under-grace=${underGrace.length}, loader=${pending.filter(frame => frame.loader).length}`);
}

const heldSwitch = (delayMs, slow = false) => ({
	setup: ctx => prepareHeldSwitch(ctx, delayMs),
	async act({ page, mark, log }) {
		await sampleSwitchFrames(page);
		await mark('open an unvisited short chat');
		await openSwitchChat(page, 'other_project');
		await waitChatOpen(page, { messagesAtLeast: 2, hold: 250 });
		await finishSwitchFrames(page, log, { slow });
	},
	verify: composer('bottom')
});

// Keep a real seeded chat outside this tab's sidebar page. Only the catalog
// response is filtered: /live identity, transcript and navigation stay real.
const offCatalogLong = async (ctx) => {
	await ctx.page.addInitScript((id) => {
		const original = window.fetch.bind(window);
		window.__breadcrumbReads = [];
		window.fetch = async (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			const method = (init?.method || input?.method || 'GET').toUpperCase();
			if (method === 'GET' && url.pathname === `/api/v1/sessions/${id}`) {
				window.__breadcrumbReads.push('transcript');
			}
			if (method === 'GET' && url.pathname === `/api/v1/sessions/${id}/live`
				&& url.searchParams.get('include_messages') === 'false'
				&& url.searchParams.get('owner_fields') === 'true') {
				window.__breadcrumbReads.push('metadata');
			}
			const response = await original(input, init);
			if (method !== 'GET' || url.pathname !== '/api/v1/sessions/cursor' || !response.ok) return response;
			const body = await response.json();
			body.items = body.items.filter(row => row.id !== id);
			return new Response(JSON.stringify(body), { status: response.status, headers: response.headers });
		};
	}, seededChatId(ctx.status, 'long'));
	await fromNew(ctx);
};

await runJourney({
	name: 'open-unloaded-chat',
	cases: [
		{ name: 'short-switch-under-grace', ...heldSwitch(80) },
		{ name: 'short-switch-slow-overlay', ...heldSwitch(800, true) },
		{ name: 'short-rapid-switch-cancels-overlay',
			setup: ctx => prepareHeldSwitch(ctx, 3000),
			async act({ page, mark, log }) {
				const outgoing = await page.locator('[data-ui-state="chat"]').getAttribute('data-ui-session');
				expect(typeof outgoing === 'string', 'outgoing session has a safe diagnostic tag');
				await sampleSwitchFrames(page);
				await mark('start B then return to A before its history settles');
				await openSwitchChat(page, 'other_project');
				await page.waitForFunction(() => window.__switchHydrationHold.started !== null);
				await openSwitchChat(page, 'no_project');
				await page.waitForFunction(tag => {
					const chat = document.querySelector('[data-ui-state="chat"]');
					return chat?.dataset.uiSession === tag && chat?.dataset.uiTargetSession === tag && chat?.dataset.uiLoading === 'false';
				}, outgoing);
				const readyAt = await page.evaluate(() => performance.now());
				await page.waitForFunction(() => window.__switchHydrationHold.released !== null, undefined, { timeout: 6000 });
				await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
				const frames = await page.evaluate(() => {
					window.__switchHydrationSampling = false;
					return window.__switchHydrationFrames;
				});
				const settled = frames.filter(frame => frame.t >= readyAt);
				expect(settled.length > 0, 'sampled the late stale response');
				expect(settled.every(frame => frame.session === outgoing && frame.target === outgoing && !frame.loader && frame.visibleRows > 0), 'no stale rows/overlay after returning to A');
				expect(frames.every(frame => frame.composerSame && frame.logSame), 'rapid navigation preserves the composer/transcript shell');
				log(`rapid switch: ${frames.length} frames, ${settled.length} after A settled; stale response ignored`);
			},
			verify: composer('bottom')
		},
		{ name: 'long-from-new', setup: fromNew, act: viaSidebar('long'), verify: composer('centre', 'bottom') },
		{ name: 'cleared-from-new', setup: fromNew, act: viaSidebar('cleared'), verify: composer('centre') },
		{ name: 'long-from-chat', setup: fromChat('no_project'), act: viaSidebar('long'), verify: composer('bottom') },
		{ name: 'cleared-from-chat', setup: fromChat('no_project'), act: viaSidebar('cleared'), verify: composer('bottom', 'centre') },
		{ name: 'long-from-cleared', setup: fromChat('cleared'), act: viaSidebar('long'), verify: composer('centre', 'bottom') },
		{ name: 'long-deep-link', setup: fromNew, act: viaDeepLink('long'), verify: composer('centre', 'bottom') },
		{
			name: 'long-deep-link-off-catalog-metadata',
			setup: offCatalogLong,
			act: viaDeepLink('long'),
			verify: async ({ page }) => {
				await page.getByRole('navigation', { name: 'Breadcrumb' }).getByRole('button', { name: SEEDED.long }).waitFor();
				const reads = await page.evaluate(() => window.__breadcrumbReads);
				expect(reads.includes('metadata'), 'an off-catalog breadcrumb must use transcript-free metadata');
				expect(!reads.includes('transcript'), 'breadcrumb identity must not fetch the full session transcript');
			}
		},
		{
			name: 'cleared-deep-link',
			setup: fromNew,
			act: viaDeepLink('cleared'),
			// The new page docks the composer for its load, then the cleared
			// chat centres it: one designed move after the reload, never back.
			verify: composer('centre', 'bottom', 'centre'),
			expectShift: [
				{
					step: 'load a link to the cleared chat',
					composer: ['bottom', 'centre'],
					maxValue: 0.15,
					reason:
						'a page opened by a link starts loading with the composer docked, the layout of the usual outcome (a transcript); a cleared chat then centres it once (createChatLayoutTracker), with no input on the page'
				}
			]
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
