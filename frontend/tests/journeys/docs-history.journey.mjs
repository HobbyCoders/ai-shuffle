// Docs history and search (#1477, #1478): each document is its own Back step,
// leaving Docs takes all of them off the stack, and every search result opened
// marks and shows its phrase, also across inline markup.
import { runJourney } from './runner.mjs';
import { expect, gotoApp } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const workspace = (page) => page.getByRole('navigation', { name: 'Workspace', exact: true });
const docsList = (page) => page.getByRole('navigation', { name: 'Documentation', exact: true });
const docTitle = (page) => page.locator('.docs-content h1').first();
const detailMarker = (page) => page.evaluate(() => history.state?.['sveltekit:states'] ?? {});

async function openDestination(page, label) {
	if (!await workspace(page).isVisible()) await page.getByRole('button', { name: /^Chats(?: \(|$)|Show sidebar/ }).first().click();
	const entry = workspace(page).getByRole('button', { name: label, exact: true });
	if (await entry.count()) await entry.click();
	else {
		await workspace(page).getByRole('button', { name: 'More', exact: true }).click();
		await page.getByRole('menuitem', { name: label, exact: true }).click();
	}
}

async function waitTitle(page, title) {
	await page.waitForFunction((want) => document.querySelector('.docs-content h1')?.textContent?.trim() === want, title, { timeout: 5000 });
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
	name: 'docs-history',
	allow: DEV_INSTANCE_NOISE,
	cases: [{
		name: 'phone-document-steps-retire-for-files',
		viewports: ['phone'],
		async setup({ page, status }) {
			await gotoApp(page, status);
			await openDestination(page, 'Docs');
			await docsList(page).waitFor();
		},
		async act({ page, mark }) {
			await mark('open Start Here, then a document it links to');
			await docsList(page).getByText('Start Here', { exact: true }).click();
			await waitTitle(page, 'AI Shuffle documentation');
			await page.locator('.docs-content').getByRole('link', { name: 'Install with Docker', exact: true }).click();
			await waitTitle(page, 'Install with Docker');
			await mark('Back returns to the linking document, Forward to the linked one');
			await page.locator('.topbar').getByRole('button', { name: 'Back to Docs', exact: true }).click();
			await waitTitle(page, 'AI Shuffle documentation');
			await page.goForward();
			await waitTitle(page, 'Install with Docker');
			await mark('switch to Files: its detail gets its own Back step');
			await openDestination(page, 'Files');
			await page.getByRole('button', { name: 'Workspace', exact: true }).click();
			await page.locator('[data-view-shell="Files"][data-pane="detail"]').waitFor();
			await page.waitForTimeout(300);
			const inFiles = await detailMarker(page);
			expect(inFiles.viewPanelDetail && !inFiles.docsDoc, `the Files detail is the current Back step, not a Docs one (${JSON.stringify(inFiles)})`);
			await mark('Back closes the Files detail and leaves no hidden Docs step');
			await page.locator('.topbar').getByRole('button', { name: 'Back to Files', exact: true }).click();
			await page.locator('[data-view-shell="Files"][data-pane="panel"]').waitFor();
			const after = await detailMarker(page);
			expect(!after.viewPanelDetail && !after.docsDoc, `no hidden detail step remains after Files Back (${JSON.stringify(after)})`);
		}
	}, {
		name: 'desktop-reselect-and-repeated-search',
		viewports: ['desktop'],
		async setup({ page, status }) {
			await gotoApp(page, status);
			await openDestination(page, 'Docs');
			await waitTitle(page, 'AI Shuffle documentation');
		},
		async act({ page, mark }) {
			await mark('choosing the open document again keeps Back to the first');
			await docsList(page).getByText('FAQ', { exact: true }).click();
			await waitTitle(page, 'Frequently asked questions');
			await docsList(page).getByText('FAQ', { exact: true }).click();
			await page.goBack();
			await waitTitle(page, 'AI Shuffle documentation');
			await page.goForward();
			await waitTitle(page, 'Frequently asked questions');

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
		}
	}]
});
