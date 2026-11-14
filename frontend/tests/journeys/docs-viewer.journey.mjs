// Docs viewer (#1475, #1476, #1478, #1554, #1581): "On this page" is there on
// every open and reaches a repeated heading by its own anchor; a reload reopens
// the document; Docs adds no browser history entry of its own; every search
// result opened marks and shows its match, a whole word before letters inside
// another word; and a page holding the query's words together ranks first in
// the Docs panel and in Spotlight, whose rows read as text, not Markdown.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, settleServiceWorker } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const workspace = (page) => page.getByRole('navigation', { name: 'Workspace', exact: true });
const docsList = (page) => page.getByRole('navigation', { name: 'Documentation', exact: true });
const docTitle = (page) => page.locator('.docs-content h1').first();

async function openDestination(page, label) {
	if (!await workspace(page).isVisible()) await page.getByRole('button', { name: /^Chats(?: \(|$)|Show sidebar/ }).first().click();
	const entry = workspace(page).getByRole('button', { name: label, exact: true });
	if (await entry.count()) await entry.click();
	else {
		await workspace(page).getByRole('button', { name: 'More', exact: true }).click();
		await page.getByRole('menuitem', { name: label, exact: true }).click();
	}
}

async function waitTitle(page, title, timeout = 5000) {
	await page.waitForFunction((want) => document.querySelector('.docs-content h1')?.textContent?.trim() === want, title, { timeout });
}

/** The marked search hit as one string, and whether its first part is inside the visible pane. */
async function hit(page) {
	return page.evaluate(() => {
		const marks = [...document.querySelectorAll('mark.doc-find-hit')];
		const pane = marks[0]?.closest('.view-main-slot')?.getBoundingClientRect();
		const box = marks[0]?.getBoundingClientRect();
		return {
			text: marks.map((m) => m.textContent).join(''),
			visible: !!(pane && box && box.top >= pane.top && box.bottom <= pane.bottom)
		};
	});
}

async function openResult(page, query, title) {
	await page.getByRole('searchbox', { name: 'Search docs', exact: true }).fill(query);
	const results = page.locator('[aria-label="Doc search results"]');
	await results.getByRole('button').first().waitFor();
	const row = title ? results.getByRole('button').filter({ hasText: title }).first() : results.getByRole('button').first();
	await row.click();
}

await runJourney({
	name: 'docs-viewer',
	allow: DEV_INSTANCE_NOISE,
	cases: [{
		name: 'phone-on-this-page-on-reopen',
		viewports: ['phone'],
		async setup({ page, status }) {
			await gotoApp(page, status);
			await openDestination(page, 'Docs');
			await docsList(page).waitFor();
		},
		async act({ page, mark }) {
			const onThisPage = () => page.locator('.doc-inline-toggle');
			await mark('open the Changelog: "On this page" is there on the first paint');
			await docsList(page).getByText('Changelog', { exact: true }).click();
			await waitTitle(page, 'Changelog');
			expect(await onThisPage().isVisible(), '"On this page" shows on the first open');
			await mark('Back to Docs shows the list');
			await page.locator('.topbar').getByRole('button', { name: 'Back to Docs', exact: true }).click();
			await page.locator('[data-view-shell="Docs"][data-pane="panel"]').waitFor({ timeout: 5000 });
			await mark('reopen the Changelog: "On this page" is there again');
			await docsList(page).getByText('Changelog', { exact: true }).click();
			await waitTitle(page, 'Changelog');
			expect(await onThisPage().isVisible(), '"On this page" shows on a reopen');
		}
	}, {
		name: 'desktop-repeated-heading-anchor',
		viewports: ['desktop'],
		async setup({ page, status }) {
			await gotoApp(page, status);
			await openDestination(page, 'Docs');
			await waitTitle(page, 'AI Shuffle documentation');
		},
		async act({ page, mark }) {
			await docsList(page).getByText('Changelog', { exact: true }).click();
			await waitTitle(page, 'Changelog');
			await mark('"On this page" → the second "Removed" scrolls to that heading, not the first');
			await page.locator('.doc-inline-toggle').click();
			const toc = page.locator('.doc-inline-list nav[aria-label="On this page"]');
			await toc.getByRole('button', { name: 'Removed', exact: true }).nth(1).click();
			await page.waitForTimeout(800);
			const where = await page.evaluate(() => {
				const pane = document.querySelector('.docs-content')?.closest('.view-main-slot')?.getBoundingClientRect();
				const removed = [...document.querySelectorAll('.docs-content h2, .docs-content h3')].filter((h) => h.textContent?.trim() === 'Removed');
				const box = removed[1]?.getBoundingClientRect();
				return { ids: removed.map((h) => h.id), inView: !!(pane && box && box.top >= pane.top - 2 && box.top < pane.top + pane.height / 2) };
			});
			expect(where.ids.length >= 2 && new Set(where.ids).size === where.ids.length, `each "Removed" has its own anchor (${JSON.stringify(where.ids)})`);
			expect(where.inView, `the second "Removed" is at the top of the pane (${JSON.stringify(where)})`);
		}
	}, {
		name: 'desktop-no-history-and-reload',
		viewports: ['desktop'],
		async setup({ page, status }) {
			await gotoApp(page, status);
			// The case reloads on purpose: start from a settled service worker.
			await settleServiceWorker(page, status);
			await openDestination(page, 'Docs');
			await waitTitle(page, 'AI Shuffle documentation');
		},
		async act({ page, mark }) {
			const length = await page.evaluate(() => history.length);
			await mark('three documents add no browser history entry');
			for (const [name, title] of [['Updates', 'Updates'], ['FAQ', 'Frequently asked questions'], ['Changelog', 'Changelog']]) {
				await docsList(page).getByText(name, { exact: true }).click();
				await waitTitle(page, title);
			}
			const after = await page.evaluate(() => history.length);
			expect(after === length, `Docs adds no history entry (${length} before, ${after} after)`);
			await mark('a reload reopens the open document');
			// As settings-reload does: the new document's first paint ends this step,
			// so the app shell coming back once it hydrates is the next step's change.
			await page.reload({ waitUntil: 'domcontentloaded' });
			await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
			await mark('the reloaded page reopens the Changelog');
			await waitTitle(page, 'Changelog', 30_000);
		}
	}, {
		name: 'desktop-repeated-search',
		viewports: ['desktop'],
		async setup({ page, status }) {
			await gotoApp(page, status);
			await openDestination(page, 'Docs');
			await waitTitle(page, 'AI Shuffle documentation');
		},
		async act({ page, mark }) {
			await mark('a search result marks its phrase');
			await openResult(page, 'remote access');
			await page.locator('mark.doc-find-hit').first().waitFor();
			const first = await hit(page);
			expect(first.text.toLowerCase() === 'remote access' && first.visible, `the first search marks and shows its phrase (${JSON.stringify(first)})`);

			await mark('after a document chosen from the list, the next search marks its phrase across inline markup');
			await docsList(page).getByText('FAQ', { exact: true }).click();
			await waitTitle(page, 'Frequently asked questions');
			await openResult(page, 'Away for all chats (the moon', 'Changelog');
			await waitTitle(page, 'Changelog');
			await page.locator('mark.doc-find-hit').first().waitFor();
			await page.waitForTimeout(300);
			const second = await hit(page);
			expect(second.text === 'Away for all chats (the moon' && second.visible, `the second search marks and shows the whole phrase (${JSON.stringify(second)})`);

			await mark('a one-word search marks the whole word, not the same letters inside another word');
			await openResult(page, '<script>', 'Overview');
			await waitTitle(page, 'AI Shuffle');
			await page.locator('mark.doc-find-hit').first().waitFor();
			await page.waitForTimeout(300);
			const word = await hit(page);
			const around = await page.evaluate(() => document.querySelector('mark.doc-find-hit')?.parentElement?.textContent ?? '');
			expect(word.text === 'script' && word.visible && around.includes('your own script'), `the mark is the word "script" (${JSON.stringify({ ...word, around })})`);
		}
	}, {
		// #1581: a page holding the query's words together ranks first in the
		// Docs panel, whatever the query's punctuation, and opening it marks them.
		name: 'search-phrase-ranking',
		viewports: ['desktop', 'phone'],
		async setup({ page, status }) {
			await gotoApp(page, status);
			await openDestination(page, 'Docs');
			await docsList(page).waitFor();
		},
		async act({ page, mark }) {
			const sentence = 'write its prompt (its instructions), and choose its tools';
			const docsResults = page.locator('[aria-label="Doc search results"]');
			const firstDocsRow = async (query) => {
				await page.getByRole('searchbox', { name: 'Search docs', exact: true }).fill(query);
				await docsResults.getByRole('button').first().waitFor();
				return (await docsResults.getByRole('button').first().innerText()).replace(/\s+/g, ' ').trim();
			};
			await mark('Docs panel: "remote, access!" ranks Remote Access first');
			const remote = await firstDocsRow('remote, access!');
			expect(remote.startsWith('Remote Access'), `the first Docs result is Remote Access (${remote.slice(0, 80)})`);
			await mark('Docs panel: a pasted sentence ranks its page first, and opening it marks the sentence');
			const pasted = await firstDocsRow(sentence);
			expect(pasted.startsWith('Quick Start') && pasted.includes('write its prompt'), `the first Docs result is Quick Start (${pasted.slice(0, 80)})`);
			await docsResults.getByRole('button').first().click();
			await page.locator('mark.doc-find-hit').first().waitFor();
			await page.waitForTimeout(300);
			const marked = await hit(page);
			expect(marked.text === sentence && marked.visible, `the opened page marks and shows the sentence (${JSON.stringify(marked)})`);
		}
	}, {
		// #1581, #1554: Spotlight ranks from the backend index the same way, and its
		// rows read as text, not Markdown. Desktop only: on a phone the Spotlight
		// sheet jumps whenever its results change, whatever the query (#1600).
		name: 'spotlight-phrase-ranking',
		viewports: ['desktop'],
		async setup({ page, status }) {
			await gotoApp(page, status);
		},
		async act({ page, mark }) {
			const sentence = 'write its prompt (its instructions), and choose its tools';
			const spotlight = page.getByRole('textbox', { name: 'Search everything', exact: true });
			const docsGroup = page.getByRole('dialog', { name: 'Search everything' }).getByRole('region', { name: 'Documentation', exact: true });
			const firstSpotlightRow = async (query, title) => {
				await spotlight.fill(query);
				await docsGroup.getByRole('button').filter({ hasText: title }).first().waitFor({ timeout: 8000 });
				return (await docsGroup.getByRole('button').first().innerText()).replace(/\s+/g, ' ').trim();
			};
			await mark('Spotlight: "remote, access!" ranks Remote Access first');
			// The sidebar's Search button, not Ctrl+K: right after load the hotkey
			// may not be bound yet.
			await page.getByRole('complementary', { name: 'Chats', exact: true }).getByRole('button', { name: /^Search\b/ }).click();
			await spotlight.waitFor();
			const spotRemote = await firstSpotlightRow('remote, access!', 'Remote Access');
			expect(spotRemote.startsWith('Remote Access'), `Spotlight's first Documentation row is Remote Access (${spotRemote.slice(0, 80)})`);
			await mark('Spotlight: the pasted sentence ranks Quick Start first, as readable text');
			const spotPasted = await firstSpotlightRow(sentence, 'Quick Start');
			expect(spotPasted.startsWith('Quick Start') && spotPasted.includes(sentence), `Spotlight's first Documentation row is Quick Start with the sentence (${spotPasted.slice(0, 120)})`);
			expect(!/[|*`]|\]\(/.test(spotPasted), `Spotlight's row has no Markdown (${spotPasted.slice(0, 120)})`);
		}
	}]
});
