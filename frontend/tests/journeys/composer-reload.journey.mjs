// Journey: a cold reload leaves the composer where it was (#1049).
//
// The composer mounts before boot says which chat the tab is on. It must still
// paint where the chat will settle: docked under a transcript, centred in an
// empty or new chat. Two judges: the recorder's layout-shift check, and the
// composer's rendered position each frame, because an animated transform (the
// slide a reload used to play) moves it on screen without any layout shift.
//
// Very long saved drafts are part of the default matrix, not opt-in: boot must
// seed their editor before the first paint (#1049).
//
// --mobile-shell runs the phone variants in the built /mobile/ app (CI's hosted
// journeys build only the shared UI).
import { runJourney } from './runner.mjs';
import { SEEDED, composer, expect, gotoApp, openFromSidebar, openNewChat, settleServiceWorker, sidebarRow, waitChat, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { expectComposerPositions } from './lib/timeline.mjs';

const mobileShell = process.argv.includes('--mobile-shell');
// Long persisted drafts are a default regression, not an opt-in escape hatch.
const withLongDrafts = true;
const LONG_DRAFT_LINES = 180;
const longDraft = (token) => Array.from({ length: LONG_DRAFT_LINES }, (_, i) => `${token} line ${i + 1}`).join('\n');

/** Plain text of the composer, without the newlines the editor adds between blocks. */
const composerText = (page) => composer(page).innerText();

/** Records the composer's rendered top and the empty chat hero's width each frame on the next page load. */
const sampleComposerTop = () => {
	window.__composerTops = [];
	window.__composerPaints = [];
	window.__heroWidths = [];
	const sample = () => {
		const slot = document.querySelector('.input-slot');
		if (slot) {
			window.__composerTops.push(slot.getBoundingClientRect().top);
			const island = document.querySelector('.chat-island');
			const editor = document.querySelector('.tiptap');
			window.__composerPaints.push({ top: slot.getBoundingClientRect().top, height: slot.getBoundingClientRect().height, island: island?.getBoundingClientRect().height, editor: editor?.getBoundingClientRect().height, wrapped: island?.classList.contains('wrapped'), chars: editor?.textContent.length, surface: document.querySelector('[data-ui-state=chat]')?.dataset.uiSurface, fonts: document.fonts.status });
		}
		const hero = document.querySelector('.cleared-hero');
		if (hero) window.__heroWidths.push(hero.getBoundingClientRect().width);
		requestAnimationFrame(sample);
	};
	requestAnimationFrame(sample);
};

const openChat = (title) => async (ctx) => {
	const { page, status, variant } = ctx;
	const inMobileShell = mobileShell && variant.viewport === 'phone';
	if (inMobileShell) {
		await page.goto(new URL('/mobile/', status.urls.ui).toString());
		await composer(page).waitFor({ timeout: 90_000 });
	} else {
		await openNewChat(ctx);
	}
	await settleServiceWorker(page, status);
	if (!title) return;
	if (inMobileShell) {
		// The phone shell has a drawer rather than the sidebar's show button.
		await page.getByRole('button', { name: /^Open menu/ }).click();
		await sidebarRow(page, title).click();
	} else {
		await openFromSidebar(page, title);
	}
};

const typeDraft = async (page, text) => {
	await composer(page).click();
	await page.keyboard.insertText(text);
	// Past the composer's draft debounce, so the reload finds it saved.
	await page.waitForTimeout(800);
};

const reloadCase = ({ name, title, settled, position, draft }) => ({
	name,
	async setup(ctx) {
		await openChat(title)(ctx);
		await settled(ctx.page);
		if (draft) await typeDraft(ctx.page, longDraft(name));
		// The tab has now seen this chat settled, as anybody reloading it has.
		await ctx.page.waitForTimeout(500);
		await ctx.page.addInitScript(sampleComposerTop);
	},
	async act({ page, mark, log }) {
		await mark('reload');
		await page.reload({ waitUntil: 'domcontentloaded' });
		await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
		await mark('page reloaded');
		await settled(page);
		if (draft) {
			const lastLine = `${name} line ${LONG_DRAFT_LINES}`;
			await page.waitForFunction(
				(line) => document.querySelector('[role="textbox"][aria-label="Message"]')?.innerText.includes(line),
				lastLine,
				{ timeout: 30_000 }
			);
		}
		// A late fill or a late load is what would move the composer now.
		await page.waitForTimeout(1500);
		{
			const tops = await page.evaluate(() => window.__composerTops);
			const travel = Math.max(...tops) - Math.min(...tops);
			const paints = await page.evaluate(() => window.__composerPaints);
			log(`composer paint changes ${JSON.stringify([...new Set(paints.map(p => JSON.stringify(p)))].map(p => JSON.parse(p)))}`);
			expect(tops.length > 0 && travel <= 2, `the composer stayed put on screen over ${tops.length} frames (moved ${Math.round(travel)}px)`);
			const widths = await page.evaluate(() => window.__heroWidths);
			expect(widths.length === 0 || Math.max(...widths) - Math.min(...widths) <= 1, `the hero kept its width while the chat opened (${Math.round(Math.min(...widths))} to ${Math.round(Math.max(...widths))}px)`);
		}
		if (draft) {
			expect((await composerText(page)).split(`${name} line 1\n`).length === 2, 'the saved draft came back once');
			// These are shared seeded chats: leave the composer as it was found.
			await mark('clear the composer');
			await composer(page).click();
			await page.keyboard.press('ControlOrMeta+A');
			await page.keyboard.press('Backspace');
			await page.waitForTimeout(800);
		}
	},
	verify({ recording, variant }) {
		// The phone shell docks the composer under its hero too.
		expectComposerPositions(recording, [mobileShell && variant.viewport === 'phone' ? 'bottom' : position]);
	}
});

const settledHistory = (page) => waitChatOpen(page, { messagesAtLeast: 2, hold: 0 });
const settledEmpty = (page) => waitChat(page, { surface: 'cleared', loading: 'false' });
const settledNew = (page) => waitChat(page, { surface: 'new', loading: 'false' });

await runJourney({
	name: 'composer-reload',
	cases: [
		reloadCase({ name: 'history', title: SEEDED.no_project, settled: settledHistory, position: 'bottom' }),
		reloadCase({ name: 'empty', title: SEEDED.cleared, settled: settledEmpty, position: 'centre' }),
		reloadCase({ name: 'new', title: null, settled: settledNew, position: 'centre' }),
		...(withLongDrafts
			? [
					reloadCase({ name: 'history-long-draft', title: SEEDED.no_project, settled: settledHistory, position: 'bottom', draft: true }),
					reloadCase({ name: 'empty-long-draft', title: SEEDED.cleared, settled: settledEmpty, position: 'centre', draft: true }),
					reloadCase({ name: 'new-long-draft', title: null, settled: settledNew, position: 'centre', draft: true })
				]
			: [])
	],
	allow: [...DEV_INSTANCE_NOISE]
});
