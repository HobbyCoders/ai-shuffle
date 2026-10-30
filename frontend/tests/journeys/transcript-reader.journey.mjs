// Real browser regression: a live cross-turn selection must survive window
// shifts. The selected span is pinned inside the SAME row/content budget; at
// that boundary browsing pauses until the selection is collapsed/cleared.
import { runJourney } from './runner.mjs';
import { expect, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

// Hosted journeys build only the shared desktop UI and exercise it at both
// widths. Opt in to the separately built production mobile shell for local QA:
// node frontend/tests/journeys/transcript-reader.journey.mjs --mobile-shell
const mobileShell = process.argv.includes('--mobile-shell');
const LOG = '[role="log"][aria-label="Conversation messages"]';
const twoFrames = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));

await runJourney({
	name: 'transcript-reader',
	cases: [{
		name: 'selection-survives-eviction',
		async setup({ page, status, variant }) {
			const id = status.seed?.chats?.long ?? status.seed?.sessions?.large;
			if (!id) throw new Error('This reader journey needs the standard long or performance seed.');
			const prefix = mobileShell && variant.viewport === 'phone' ? '/mobile/' : '/';
			await page.goto(new URL(`${prefix}?session=${encodeURIComponent(id)}`, status.urls.ui).toString());
			await waitChatOpen(page, { messagesAtLeast: 50, timeout: 90_000 });
			await page.locator(LOG).hover();
			await page.mouse.wheel(0, -200);
			await twoFrames(page);
		},
		async act({ page, mark, log }) {
			await mark('select across two transcript turns');
			const selected = await page.evaluate(() => {
				const rows = [...document.querySelectorAll('[data-transcript-index]')];
				// Pin the tail pair: moving to older history MUST eventually evict
				// these rows without the fix. A middle pair in the short CI seed
				// can fit at BOTH history edges and never hit a selection boundary.
				const start = Math.max(0, rows.length - 2);
				const first = document.createTreeWalker(rows[start], NodeFilter.SHOW_TEXT);
				const last = document.createTreeWalker(rows[start + 1], NodeFilter.SHOW_TEXT);
				const anchor = first.nextNode();
				let focus, node; while ((node = last.nextNode())) focus = node;
				if (!anchor || !focus) throw new Error('Adjacent mounted rows have no selectable text.');
				const range = document.createRange();
				range.setStart(anchor, 0); range.setEnd(focus, focus.textContent.length);
				const selection = window.getSelection();
				selection.removeAllRanges(); selection.addRange(range);
				window.__readerSelectionAnchor = anchor;
				window.__readerSelectionFocus = focus;
				window.__readerSelectionOffsets = [selection.anchorOffset, selection.focusOffset];
				window.__readerSelectionText = range.toString();
				return selection.toString();
			});
			expect(selected.length > 0, 'created a real noncollapsed cross-turn selection');
			await twoFrames(page);
			for (let shift = 0; shift < 20; shift++) {
				await mark(`browse ${shift < 10 ? 'older' : 'newer'} with selection, shift ${shift + 1}`);
				await page.mouse.wheel(0, shift < 10 ? -1_000_000 : 1_000_000);
				await twoFrames(page);
				const state = await page.evaluate(selector => {
					const selection = window.getSelection();
					return {
						selection: selection?.toString(),
						connected: window.__readerSelectionAnchor?.isConnected && window.__readerSelectionFocus?.isConnected,
						sameBounds: selection?.anchorNode === window.__readerSelectionAnchor && selection?.focusNode === window.__readerSelectionFocus &&
							selection?.anchorOffset === window.__readerSelectionOffsets[0] && selection?.focusOffset === window.__readerSelectionOffsets[1],
						sameText: !!selection?.rangeCount && selection.getRangeAt(0).toString() === window.__readerSelectionText,
						outer: document.querySelectorAll('[data-transcript-index]').length,
						nested: document.querySelector(selector).querySelectorAll('*').length
					};
				}, LOG);
				log(`shift ${shift + 1}: chars=${state.selection?.length}, connected=${state.connected}, bounds=${state.sameBounds}, text=${state.sameText}, rows=${state.outer}, nested=${state.nested}`);
				// Selection.toString() changes a terminal layout newline when an
				// unselected neighbouring block is evicted. Native endpoint identity,
				// offsets and the exact DOM Range text must all remain unchanged.
				expect(state.sameText && state.sameBounds && state.connected, 'window eviction must not destroy the active selection');
				expect(state.outer <= 120 && state.nested <= 12_000, 'selection pinning must not grow the mounted DOM budget');
			}
			await mark('clear the selection through its accessible control and continue browsing');
			const clear = page.getByRole('button', { name: 'Clear selection in transcript', exact: true });
			// The selected rows can begin anywhere in the mounted slice. Reach
			// its protected edge without assuming a particular row cost/density.
			await page.locator(LOG).hover();
			for (let attempt = 0; attempt < 6 && !(await clear.count()); attempt++) {
				// A touch click elsewhere can legitimately collapse a native
				// selection. Wheel input isolates pinning from that default.
				await page.mouse.wheel(0, -1_000_000);
				await twoFrames(page);
			}
			expect(await clear.count() === 1, 'the selection budget exposes a named clear control');
			const oldStart = await page.locator('[data-transcript-index]').first().getAttribute('data-transcript-index');
			await clear.click();
			await twoFrames(page);
			expect(await page.evaluate(() => window.getSelection().isCollapsed), 'the visible clear control collapses the native selection');
			await page.getByRole('button', { name: 'Show earlier messages', exact: true }).click();
			await page.waitForFunction(start => Number(document.querySelector('[data-transcript-index]')?.dataset.transcriptIndex) < Number(start), oldStart);
			log('the accessible clear control released selected rows for eviction');
		}
	}],
	allow: [...DEV_INSTANCE_NOISE]
});
