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
import { readFileSync } from 'node:fs';
import { SEEDED, PROJECT, expect, gotoApp, openFromSidebar, showSidebar, seededChatId, waitChat, waitChatOpen, pickAgent, pickProject, pickAccess, typeAndSend, scriptTag, waitTurnSettled, scrollTranscriptTo, transcriptScroll, uniq } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { expectComposerPositions } from './lib/timeline.mjs';

// Hosted journeys build shared desktop UI only. Explicitly opt into the native
// phone shell locally, matching transcript-reader's --mobile-shell convention.
const mobileShell = process.argv.includes('--mobile-shell');

// Read the production deadline instead of giving this timing ratchet its own policy.
const boundarySource = readFileSync(new URL('../../src/lib/components/chat/TranscriptHydrationBoundary.svelte', import.meta.url), 'utf8');
const SWITCH_GRACE_MS = Number(boundarySource.match(/export const TRANSCRIPT_LOADING_GRACE_MS = (\d+);/)?.[1]);
expect(Number.isFinite(SWITCH_GRACE_MS), 'read the production switch-notice deadline');

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

const viaDeepLink = (target) => async ({ page, status, mark, variant }) => {
	await mark(`load a link to the ${target} chat`);
	const prefix = mobileShell && variant.viewport === 'phone' ? '/mobile/' : '/';
	await page.goto(new URL(`${prefix}?session=${encodeURIComponent(seededChatId(status, target))}`, status.urls.ui).toString());
	await OPENED[target](page);
};

// Default phone variants exercise the shared UI at a narrow viewport. With
// --mobile-shell they open the REAL /mobile/ app instead. Hold only delivery of
// an actual native SSE response: no reducer, content or loading flag is fabricated.
async function holdIncomingHistory(page, target, delayMs) {
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
}

async function prepareHeldSwitch(ctx, delayMs, source = 'no_project') {
	const { page, status, variant } = ctx;
	await holdIncomingHistory(page, seededChatId(status, 'other_project'), delayMs);
	const prefix = mobileShell && variant.viewport === 'phone' ? '/mobile/' : '/';
	await page.goto(new URL(`${prefix}?session=${encodeURIComponent(seededChatId(status, source))}`, status.urls.ui).toString());
	await page.getByRole('textbox', { name: 'Message' }).waitFor({ timeout: 60_000 });
	await waitChatOpen(page, { messagesAtLeast: 2, hold: 500 });
	await page.getByRole('textbox', { name: 'Message' }).fill('Draft for the outgoing chat');
}

async function openSwitchChat(page, key, beforeNavigate) {
	if (new URL(page.url()).pathname.startsWith('/mobile/')) {
		await page.getByRole('button', { name: /^Open menu/ }).click();
	} else await showSidebar(page);
	const group = page.getByRole('group', { name: `${SEEDED[key]} session`, exact: true });
	const button = group.getByRole('button').first();
	if (beforeNavigate) {
		// Both desktop scrolling/actionability and the phone drawer still belong
		// to the previous load. Mark only at the actionable navigation row.
		await button.click({ trial: true });
		await beforeNavigate();
	}
	await button.click();
}

async function sampleSwitchFrames(page) {
	await page.evaluate(() => {
		const composer = document.querySelector('[role="textbox"][aria-label="Message"]');
		const log = document.querySelector('[role="log"][aria-label="Conversation messages"]');
		const firstRow = log?.querySelector('.msg-virtualized');
		window.__switchHydrationFrames = [];
		window.__switchHydrationSampling = true;
		let loadingStarted = null;
		const sample = t => {
			if (!window.__switchHydrationSampling) return;
			const chat = document.querySelector('[data-ui-state="chat"]');
			if (chat?.dataset.uiLoading === 'true' && loadingStarted === null) loadingStarted = t;
			const pending = !!document.querySelector('[data-transcript-pending="true"]');
			const loader = document.querySelector('.loading-transcript');
			const rows = [...document.querySelectorAll('[role="log"] .msg-virtualized')];
			window.__switchHydrationFrames.push({
				t, pending, loadingStarted, heldAt: window.__switchHydrationHold.started,
				loader: !!loader && getComputedStyle(loader).visibility !== 'hidden',
				visibleRows: rows.filter(row => getComputedStyle(row).visibility !== 'hidden').length,
				outgoingConnected: !!firstRow?.isConnected,
				readerConnected: window.__switchReadingAnchor?.isConnected,
				readerTop: window.__switchReadingAnchor?.getBoundingClientRect().top,
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
	// Use the pre-existing live loading signature, not the new wrapper's pending
	// attribute, so this ratchet also measures (and fails on) the rollback build.
	const pending = frames.filter(frame => frame.loading === 'true' && frame.loadingStarted !== null);
	// Time starts at the first painted loading frame, not response headers: a
	// slow network can deliver headers after the production grace has elapsed.
	const underGrace = pending.filter(frame => frame.t - frame.loadingStarted < 100);
	const ready = frames.find(frame => frame.loadingStarted !== null && frame.loading === 'false');
	const duration = predicate => frames.reduce((sum, frame, index) => sum +
		(predicate(frame) ? Math.max(0, (frames[index + 1]?.t ?? frame.t) - frame.t) : 0), 0);
	log(`switch-metric ${JSON.stringify({
		frames: frames.length, pending: pending.length, underGrace: underGrace.length,
		loaderFrames: pending.filter(frame => frame.loader).length,
		loaderMs: duration(frame => frame.loading === 'true' && frame.loader),
		emptyMs: duration(frame => frame.loading === 'true' && !frame.loader && frame.visibleRows === 0),
		readyMs: ready ? ready.t - ready.loadingStarted : null
	})}`);
	expect(underGrace.length > 0, 'observed the real incoming history while under grace');
	expect(ready && ready.session === ready.target, 'the incoming target is painted ready before sampling stops');
	expect(underGrace.every(frame => !frame.loader && frame.visibleRows > 0 && frame.outgoingConnected), 'no empty/skeleton frame under grace');
	expect(frames.every(frame => frame.composerSame && frame.logSame), 'composer and MessageArea retain their identity');
	expect(pending.every(frame => !frame.outgoingDraft), 'the incoming composer never displays the outgoing draft');
	if (slow) {
		expect(pending.some(frame => frame.loader && frame.outgoingConnected), 'a slow load shows its overlay without discarding the old rows');
	}
}

const heldSwitch = (delayMs, slow = false) => ({
	setup: ctx => prepareHeldSwitch(ctx, delayMs),
	async act({ page, mark, log }) {
		await sampleSwitchFrames(page);
		await mark('open an unvisited short chat');
		await openSwitchChat(page, 'other_project');
		// Sidebar selection can run in the next rAF. Do not mistake the still-
		// painted outgoing ready state for the incoming chat's completion.
		await page.waitForFunction(() => window.__switchHydrationHold.started !== null);
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

// #916: an unknown owner shows the mirror pane on a cold deep link. Keep
// actual identity/sync/catalog responses pending across the column's reveal
// animation: its transform must not be the pane's only containing block.
const offCatalogLateIdentity = async (ctx) => {
	await offCatalogLong(ctx);
	await ctx.page.addInitScript((id) => {
		const original = window.fetch.bind(window);
		const released = new Promise(resolve => { window.__releaseOpeningIdentity = resolve; });
		const probe = window.__openingIdentity = { metadata: false, sync: false, frames: [], released: false };
		window.fetch = async (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			const response = await original(input, init);
			const metadata = url.pathname === `/api/v1/sessions/${id}/live` && url.searchParams.get('owner_fields') === 'true';
			const sync = url.pathname === `/api/v1/sessions/${id}/sync-status`;
			if (metadata || sync || url.pathname === '/api/v1/projects') {
				if (metadata) probe.metadata = true;
				if (sync) probe.sync = true;
				await released;
			}
			return response;
		};
		const sample = () => {
			if (probe.released) return;
			const pane = document.querySelector('.mirror-sync');
			const wrapper = document.querySelector('.message-area-wrapper');
			if (pane && wrapper) probe.frames.push({
				transitioning: document.querySelector('.chat-view').classList.contains('transitioning-from-empty'),
				wrapperParent: pane.offsetParent === wrapper
			});
			requestAnimationFrame(sample);
		};
		requestAnimationFrame(sample);
	}, seededChatId(ctx.status, 'long'));
};

const deepLinkLateIdentity = async ({ page, status, mark, variant, log }) => {
	await mark('load an off-catalog link with identity pending across the column reveal');
	const prefix = mobileShell && variant.viewport === 'phone' ? '/mobile/' : '/';
	await page.goto(new URL(`${prefix}?session=${encodeURIComponent(seededChatId(status, 'long'))}`, status.urls.ui).toString());
	await page.waitForFunction(() => {
		const probe = window.__openingIdentity;
		return probe.metadata && probe.sync && probe.frames.some(frame => frame.transitioning)
			&& probe.frames.slice(-2).length === 2 && probe.frames.slice(-2).every(frame => !frame.transitioning);
	});
	const frames = await page.evaluate(() => {
		const probe = window.__openingIdentity;
		probe.released = true;
		window.__releaseOpeningIdentity();
		return probe.frames;
	});
	await OPENED.long(page);
	log(`mirror pane: ${frames.length} painted frames across the reveal; ${frames.filter(frame => frame.transitioning).length} during its animation`);
	expect(frames.every(frame => frame.wrapperParent), 'the mirror pane must keep the message wrapper as its containing block before and after the reveal');
};

const offCatalogIdentity = async ({ page, variant }) => {
	if (mobileShell && variant.viewport === 'phone') {
		expect(new URL(page.url()).pathname === '/mobile/', 'the opt-in case must exercise the real mobile shell');
		await page.locator('header').getByText(SEEDED.long, { exact: true }).waitFor();
	} else {
		await page.getByRole('navigation', { name: 'Breadcrumb' }).getByRole('button', { name: SEEDED.long }).waitFor();
	}
	const reads = await page.evaluate(() => window.__breadcrumbReads);
	expect(reads.includes('metadata'), 'an off-catalog breadcrumb must use transcript-free metadata');
	expect(!reads.includes('transcript'), 'breadcrumb identity must not fetch the full session transcript');
};

// #804: on a full load the sidebar's session cards must paint in place. The
// project catalog used to gate every project chat (an empty catalog read as
// "every project deleted"), so with a long list — whose load let the catalog
// answer last — project chats popped in above the loose chats about a second
// later and pushed every card down (layout shift 0.13 from div.scard-frame).
// Holding the catalog until the cards have painted makes that ordering
// deterministic on the seeded list.
const cardTitles = (page) =>
	page.evaluate(() => [...document.querySelectorAll('.scard-frame')].map((el) => (el.textContent || '').replace(/\s+/g, ' ').trim()));

const deepLinkLateCatalog = async ({ page, status, mark }) => {
	// Wrap fetch rather than page.route: the app's service worker serves its
	// API calls, and page.route never sees requests a service worker handles.
	await page.addInitScript(() => {
		const original = window.fetch.bind(window);
		const painted = new Promise((resolve) => (window.__releaseCatalog = resolve));
		window.__catalogAnswered = false;
		window.fetch = async (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			const method = (init?.method || input?.method || 'GET').toUpperCase();
			if (method !== 'GET' || url.pathname !== '/api/v1/projects') return original(input, init);
			await painted;
			const response = await original(input, init);
			window.__catalogAnswered = true;
			return response;
		};
	});
	await mark('load a link with the project catalog answering after the sidebar paints');
	await page.goto(new URL(`/?session=${encodeURIComponent(seededChatId(status, 'no_project'))}`, status.urls.ui).toString());
	await page.locator('.scard-frame').first().waitFor();
	await page.waitForTimeout(300);
	const firstPaint = await cardTitles(page);
	await page.evaluate(() => window.__releaseCatalog());
	await page.waitForTimeout(2500);
	expect(await page.evaluate(() => window.__catalogAnswered), 'the held project catalog answered');
	const settled = await cardTitles(page);
	expect(
		JSON.stringify(settled) === JSON.stringify(firstPaint),
		`sidebar cards changed after the catalog answered:\n  first paint: ${firstPaint.slice(0, 6).join(' | ')}\n  settled:     ${settled.slice(0, 6).join(' | ')}`
	);
};

await runJourney({
	name: 'open-unloaded-chat',
	cases: [
		{ name: 'rewind-portal-closes-on-switch',
			async setup(ctx) {
				await prepareHeldSwitch(ctx, 1500);
				const { page } = ctx;
				// Keyboard navigation can reach the sidebar while this body portal is
				// open. On phones open the drawer first; do not force a pointer click
				// through the confirmation scrim.
				if (mobileShell && ctx.variant.viewport === 'phone') await page.getByRole('button', { name: /^Open menu/ }).click();
				else await showSidebar(page);
				await page.getByTitle('Rewind to before this message', { exact: true }).first().focus();
				await page.keyboard.press('Enter');
				await page.getByRole('heading', { name: 'Rewind Conversation?', exact: true }).waitFor();
				await page.evaluate(() => {
					window.__staleRewindRequests = 0;
					const original = window.fetch.bind(window);
					window.fetch = async (input, init) => {
						const url = new URL(typeof input === 'string' ? input : input.url, location.href);
						if ((init?.method || input?.method || 'GET').toUpperCase() === 'POST' && url.pathname.includes('/rewind')) {
							window.__staleRewindRequests++;
							return new Response(JSON.stringify({ success: false, error: 'Unexpected stale rewind' }), { status: 409 });
						}
						return original(input, init);
					};
				});
			},
			async act({ page, mark, log }) {
				await mark('keyboard-open B while A owns a rewind confirmation');
				const target = page.getByRole('group', { name: `${SEEDED.other_project} session`, exact: true }).getByRole('button').first();
				await target.focus();
				await page.keyboard.press('Enter');
				await page.waitForFunction(() => window.__switchHydrationHold.started !== null);
				expect(await page.getByRole('heading', { name: 'Rewind Conversation?', exact: true }).count() === 0,
					'outgoing body-portaled rewind confirmation closes before incoming history is released');
				expect(await page.evaluate(() => window.__staleRewindRequests) === 0, 'no outgoing rewind request during incoming hydration');
				await waitChatOpen(page, { messagesAtLeast: 2, hold: 250 });
				expect(await page.getByRole('heading', { name: 'Rewind Conversation?', exact: true }).count() === 0, 'the old confirmation never reopens over B');
				log('rewind portal closed on target divergence; no cross-session mutation');
			},
			verify: composer('bottom')
		},
		{ name: 'streaming-task-panel-retained',
			async setup(ctx) {
				const { page, status, variant } = ctx;
				await holdIncomingHistory(page, seededChatId(status, 'other_project'), 1500);
				await gotoApp(page, status, mobileShell && variant.viewport === 'phone' ? '/mobile/' : '/');
				await waitChat(page, { surface: 'new' });
				const hero = page.getByRole('region', { name: 'New session' });
				await hero.waitFor();
				await pickAgent(page, hero);
				await pickProject(page, hero, PROJECT);
				if (mobileShell && variant.viewport === 'phone') {
					// The production phone shell has a quick access picker, not the
					// desktop Permission mode menu used by pickAccess.
					await page.getByRole('button', { name: /^Tool access:/ }).click();
					await page.getByRole('radiogroup', { name: 'Tool access', exact: true })
						.getByRole('radio', { name: 'Bypass', exact: true }).click();
					await page.getByRole('button', { name: 'Tool access: Bypass', exact: true }).waitFor();
					await page.keyboard.press('Escape');
				} else await pickAccess(page, 'Bypass');
				// Produce a scrollable history through the real provider pipeline,
				// then a real standalone shell task; never seed activeTasks by hand.
				await typeAndSend(page, `task-retention-${uniq()} ${scriptTag(variant, 'long', { tps: 0, latency_ms: 0 })}`);
				await waitTurnSettled(page, { text: 'About the request path', hold: 250 });
				await typeAndSend(page, `Keep a real shell task visible ${scriptTag(variant, 'tool-background-stream', { tps: 2, latency_ms: 0 })}`);
				const toggle = page.locator('[data-ui-mount="MessageArea"]').getByTestId('tasks-toggle');
				await toggle.waitFor({ timeout: 60_000 });
				if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
				await page.waitForFunction(() => document.querySelector('[data-ui-mount="MessageArea"] [data-testid="tasks-toggle"]')
					?.closest('.inline')?.querySelector('.inline-body')?.textContent.includes('sleep 120'));
				await waitChat(page, { streaming: 'true' });
				await scrollTranscriptTo(page, 1);
				expect((await transcriptScroll(page)).max > 200, 'A has a genuine bottom reading position to protect');
				await page.evaluate(() => {
					const log = document.querySelector('[role="log"][aria-label="Conversation messages"]');
					const panel = log.querySelector('[data-testid="tasks-toggle"]').closest('.inline');
					window.__taskRetention = { panel, log, composer: document.querySelector('[role="textbox"][aria-label="Message"]'),
						height: panel.getBoundingClientRect().height, scrollTop: log.scrollTop };
				});
			},
			async act({ page, mark, log }) {
				await mark('switch away from streaming A with its real shell row visible');
				await openSwitchChat(page, 'other_project');
				await page.waitForFunction(() => window.__switchHydrationHold.started !== null
					&& document.querySelector('[data-transcript-pending="true"]'));
				const kept = await page.evaluate(() => {
					const old = window.__taskRetention;
					return { connected: old.panel.isConnected, height: old.panel.getBoundingClientRect().height,
						heightBefore: old.height, shell: old.panel.querySelector('.inline-body')?.textContent.includes('sleep 120'),
						scrollTop: old.log.scrollTop, scrollBefore: old.scrollTop,
						composerSame: old.composer === document.querySelector('[role="textbox"][aria-label="Message"]'),
						logSame: old.log === document.querySelector('[role="log"][aria-label="Conversation messages"]') };
				});
				expect(kept.connected && kept.shell, 'the real outgoing shell row remains connected during B’s held history');
				expect(Math.abs(kept.height - kept.heightBefore) <= 1, `task-panel height is frozen: ${JSON.stringify(kept)}`);
				expect(kept.scrollTop >= kept.scrollBefore - 1, `the bottom reading position is not clamped by missing task rows: ${JSON.stringify(kept)}`);
				expect(kept.composerSame && kept.logSame, 'composer and transcript nodes survive the running-task switch');
				await waitChatOpen(page, { messagesAtLeast: 2, hold: 250 });
				expect(await page.evaluate(() => !window.__taskRetention.panel.isConnected), 'A’s task panel leaves only when B is ready');
				log(`native Monitor task retained through the grace: ${JSON.stringify(kept)}`);
			},
			verify: composer('bottom')
		},
		{ name: 'short-switch-under-grace', ...heldSwitch(80) },
		{ name: 'short-switch-slow-overlay', ...heldSwitch(800, true) },
		{ name: 'reading-anchor-survives-hydration',
			async setup(ctx) {
				await prepareHeldSwitch(ctx, 800, 'long');
				await ctx.page.evaluate(() => {
					const log = document.querySelector('[role="log"][aria-label="Conversation messages"]');
					log.scrollTop = (log.scrollHeight - log.clientHeight) / 2;
				});
				await ctx.page.waitForTimeout(500);
				await ctx.page.evaluate(() => {
					const log = document.querySelector('[role="log"][aria-label="Conversation messages"]');
					window.__switchReadingAnchor = [...log.querySelectorAll('.msg-virtualized')]
						.find(row => row.getBoundingClientRect().bottom > log.getBoundingClientRect().top + 20);
					window.__switchReadingTop = window.__switchReadingAnchor?.getBoundingClientRect().top;
				});
			},
			async act({ page, mark, log }) {
				const top = await page.evaluate(() => window.__switchReadingTop);
				expect(Number.isFinite(top), 'captured a real outgoing reading anchor');
				await sampleSwitchFrames(page);
				await mark('open B while A is scrolled away from the bottom');
				await openSwitchChat(page, 'other_project');
				await waitChatOpen(page, { messagesAtLeast: 2, hold: 250 });
				await finishSwitchFrames(page, log, { slow: true });
				const pending = await page.evaluate(() => window.__switchHydrationFrames.filter(frame => frame.pending));
				expect(pending.length > 0, 'observed the retained reading viewport');
				expect(pending.every(frame => frame.readerConnected && Math.abs(frame.readerTop - top) <= 1),
					'the outgoing reading anchor keeps its exact geometry until the atomic swap');
				log(`reading anchor retained across ${pending.length} hydration frames; return-to-chat auto-scroll policy is unchanged (#805)`);
			},
			verify: composer('bottom')
		},
		{ name: 'short-rapid-switch-cancels-overlay',
			setup: ctx => prepareHeldSwitch(ctx, 3000),
			async act({ page, mark, log }) {
				const outgoing = await page.locator('[data-ui-state="chat"]').getAttribute('data-ui-session');
				expect(typeof outgoing === 'string', 'outgoing session has a safe diagnostic tag');
				if (new URL(page.url()).pathname.startsWith('/mobile/')) {
					await page.getByRole('button', { name: /^Open menu/ }).click();
				} else await showSidebar(page);
				const incomingRow = page.getByRole('group', { name: `${SEEDED.other_project} session`, exact: true }).getByRole('button').first();
				await incomingRow.click({ trial: true });
				const incomingButton = await incomingRow.elementHandle();
				await sampleSwitchFrames(page);
				await mark('start B then return to A before its history settles');
				// One browser-side sequence: driver round-trips must not turn this
				// rapid switch into a legitimate load past the notice deadline.
				const dispatched = await page.evaluate(async ({ incoming, outgoing, graceMs }) => {
					const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
					if (!incoming?.isConnected) throw new Error('missing rapid-switch row');
					const first = performance.now();
					incoming.click();
					await frame();
					// Reopen/paint the real drawer while B's native headers are in flight,
					// not afterwards: serializing network and drawer rendering can use
					// the whole grace interval on hosted runners. Both remain real.
					const menu = document.querySelector('button[aria-label^="Open menu"], button[aria-label="Show sidebar"]');
					if (menu) { menu.click(); await frame(); }
					while (window.__switchHydrationHold.started === null) {
						if (performance.now() - first >= graceMs) {
							throw new Error('invalid rapid-switch run: B did not start its held history before the notice deadline');
						}
						await frame();
					}
					// Responsive desktop unmounts its closed sidebar; find the freshly
					// opened card rather than dispatching on a detached old handle.
					const group = [...document.querySelectorAll('[role="group"]')]
						.find(node => node.getAttribute('aria-label') === `${outgoing} session`);
					const returnButton = group?.querySelector('[role="button"]');
					if (!returnButton) throw new Error('missing rapid-return session card');
					window.__flightRecorder.mark('return to A before B has hydrated');
					const returned = performance.now();
					returnButton.click();
					return { first, returned, elapsed: returned - first, graceMs };
				}, { incoming: incomingButton, outgoing: SEEDED.no_project, graceMs: SWITCH_GRACE_MS });
				log(`rapid-dispatch ${JSON.stringify(dispatched)}`);
				expect(dispatched.elapsed < dispatched.graceMs,
					`invalid rapid-switch run: return dispatched at ${dispatched.elapsed.toFixed(1)} ms, not before ${dispatched.graceMs} ms`);
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
				expect(frames.every(frame => frame.session === outgoing && !frame.loader && frame.visibleRows > 0), 'A never passes through a loading/empty frame during the rapid switch');
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
		// Desktop only: the phone shell keeps the sidebar closed on load.
		{ name: 'deep-link-late-catalog', viewports: ['desktop'], setup: fromNew, act: deepLinkLateCatalog },
		{
			name: 'long-deep-link-off-catalog-metadata',
			setup: offCatalogLong,
			act: viaDeepLink('long'),
			verify: offCatalogIdentity
		},
		{
			name: 'long-deep-link-off-catalog-late-identity',
			setup: offCatalogLateIdentity,
			act: deepLinkLateIdentity,
			verify: offCatalogIdentity
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
