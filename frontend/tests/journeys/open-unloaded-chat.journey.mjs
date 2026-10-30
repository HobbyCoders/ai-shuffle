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
	let release;
	const painted = new Promise((resolve) => (release = resolve));
	let answered = false;
	await page.route(
		(url) => url.pathname === '/api/v1/projects',
		async (route) => {
			await painted;
			await route.continue();
			answered = true;
		}
	);
	await mark('load a link with the project catalog answering after the sidebar paints');
	await page.goto(new URL(`/?session=${encodeURIComponent(seededChatId(status, 'no_project'))}`, status.urls.ui).toString());
	await page.locator('.scard-frame').first().waitFor();
	await page.waitForTimeout(300);
	const firstPaint = await cardTitles(page);
	release();
	await page.waitForTimeout(2500);
	expect(answered, 'the held project catalog answered');
	const settled = await cardTitles(page);
	expect(
		JSON.stringify(settled) === JSON.stringify(firstPaint),
		`sidebar cards changed after the catalog answered:\n  first paint: ${firstPaint.slice(0, 6).join(' | ')}\n  settled:     ${settled.slice(0, 6).join(' | ')}`
	);
};

await runJourney({
	name: 'open-unloaded-chat',
	cases: [
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
