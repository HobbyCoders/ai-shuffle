// Real browser regression: a live cross-turn selection must survive window
// shifts. The selected span is pinned inside the SAME row/content budget; at
// that boundary browsing pauses until the selection is collapsed/cleared.
import { runJourney, VIEWPORTS } from './runner.mjs';
import { expect, openNewChat, typeAndSend, waitChatOpen, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

// Hosted journeys build only the shared desktop UI and exercise it at both
// widths. Opt in to the separately built production mobile shell for local QA:
// node frontend/tests/journeys/transcript-reader.journey.mjs --mobile-shell
const mobileShell = process.argv.includes('--mobile-shell');
const LOG = '[role="log"][aria-label="Conversation messages"]';
const twoFrames = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
// Review at the requested desktop size without changing the runner's defaults
// for any other journey (each journey runs in its own process).
if (process.argv.includes('--review-desktop')) VIEWPORTS.desktop = { viewport: { width: 1280, height: 800 } };
const privateScreenshot = async (page, variant, stage) => {
	if (process.env.JOURNEY_OUT) await page.screenshot({ path: `${process.env.JOURNEY_OUT}/${stage}@${variant.viewport}-${variant.throttle.cpu}x.png` });
};
const readViewport = page => page.locator(LOG).evaluate(log => {
	const viewport = log.getBoundingClientRect();
	const rows = [...log.querySelectorAll('[data-transcript-index]')].map(row => {
		const box = row.getBoundingClientRect();
		const visiblePixels = Math.max(0, Math.min(box.bottom, viewport.bottom) - Math.max(box.top, viewport.top));
		const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
		let node, textVisible = false;
		while (visiblePixels && !textVisible && (node = walker.nextNode())) {
			if (!node.textContent.trim()) continue;
			const range = document.createRange(); range.selectNodeContents(node);
			textVisible = [...range.getClientRects()].some(rect => rect.width > 0 && rect.height > 0 &&
				rect.bottom > viewport.top && rect.top < viewport.bottom && rect.right > viewport.left && rect.left < viewport.right);
		}
		return { index: Number(row.dataset.transcriptIndex), top: box.top - viewport.top, visiblePixels, textVisible };
	});
	return { rows, textRows: rows.filter(row => row.textVisible).map(row => row.index), height: log.clientHeight,
		scrollTop: log.scrollTop, maxScrollTop: log.scrollHeight - log.clientHeight, viewport: { width: innerWidth, height: innerHeight } };
});

const cases = [{
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
			// Scroll into the released edge. Scrolling an offscreen earlier button
			// into view can itself page and detach it before Playwright clicks it;
			// the contract is successful eviction after clear, not that click race.
			await page.locator(LOG).hover();
			await page.mouse.wheel(0, -1_000_000);
			await page.waitForFunction(start => Number(document.querySelector('[data-transcript-index]')?.dataset.transcriptIndex) < Number(start), oldStart);
			log('the accessible clear control released selected rows for eviction');
		}
	}, {
		name: 'same-count-streaming-growth',
		async setup({ page, status, variant, readingBoundary = false, replyScenario = 'reader-growth' }) {
			// Produce a fresh native prefix through the real composer/provider, then
			// replay it into the affected shell. No synthetic feed/state or SDK-only
			// fork endpoint: default native seed sessions have no SDK session id.
			await openNewChat({ page, status, variant });
			await typeAndSend(page, 'Prepare a rich reading prefix. [script:reader-growth tps=0 latency_ms=0]');
			await waitTurnSettled(page, { messagesAtLeast: 2, text: 'GROWING_READER_END', hold: 250 });
			const id = await page.evaluate(() => JSON.parse(sessionStorage.getItem('ai-shuffle-active-session') || 'null')?.sessionId);
			expect(typeof id === 'string', 'the real producer bound a fresh native session');
			const prefix = mobileShell && variant.viewport === 'phone' ? '/mobile/' : '/';
			await page.goto(new URL(`${prefix}?session=${encodeURIComponent(id)}`, status.urls.ui).toString());
			await waitChatOpen(page, { messagesAtLeast: 2, hold: 250 });
			await typeAndSend(page, `Grow the reply while I read it. [script:${replyScenario} latency_ms=0]`);
			await page.waitForFunction(() => {
				const row = [...document.querySelectorAll('[data-transcript-index]')].at(-1);
				return Number(row?.dataset.transcriptIndex) >= 3 && row.textContent.includes('GROWING_READER_START') && row.textContent.length > 4000;
			});
			// Release the controlled turn anchor with real wheel intent BEFORE
			// positioning the reading key; it must not fight our viewport setup.
			await page.locator(LOG).hover();
			await page.mouse.wheel(0, -160);
			await page.getByRole('button', { name: 'Jump to latest message', exact: true }).waitFor();
			await twoFrames(page);
			await page.locator(LOG).evaluate((log, boundary) => {
				const row = [...log.querySelectorAll('[data-transcript-index]')].filter(row => row.textContent.includes('GROWING_READER_START')).at(-1);
				const key = boundary ? log.querySelector(`[data-transcript-index="${Number(row.dataset.transcriptIndex) - 1}"]`) : row;
				log.scrollTop += key.getBoundingClientRect().top - log.getBoundingClientRect().top + (boundary ? 24 : 400);
			}, readingBoundary);
			await twoFrames(page);
			const visible = await page.locator(LOG).evaluate(log => {
				const top = log.getBoundingClientRect().top, bottom = log.getBoundingClientRect().bottom;
				return [...log.querySelectorAll('[data-transcript-index]')].filter(row => {
					const box = row.getBoundingClientRect(); return box.bottom > top && box.top < bottom;
				}).map(row => Number(row.dataset.transcriptIndex));
			});
			expect(visible[0] === (readingBoundary ? 2 : 3) && visible.includes(3), `read the growing reply at the intended viewport position (boundary=${readingBoundary}, visible=${visible.join(',')})`);
		},
		async act({ page, mark, log, variant, minimumChars = 17000 }) {
			await mark('read the live reply while same-count growth reprices the window');
			const before = await page.evaluate(selector => {
				const container = document.querySelector(selector);
				const row = [...container.querySelectorAll('[data-transcript-index]')].filter(row => row.textContent.includes('GROWING_READER_START')).at(-1);
				if (!row) throw new Error('The growing reply disappeared before follow mode could be released.');
				window.__growingReader = row;
				window.__growingSamples = [];
				window.__sampleGrowingReader = true;
				const sample = () => {
					if (!window.__sampleGrowingReader) return;
					window.__growingSamples.push({
						connected: row.isConnected, chars: row.textContent.length,
						top: row.getBoundingClientRect().top - container.getBoundingClientRect().top,
						indices: [...container.querySelectorAll('[data-transcript-index]')].map(row => Number(row.dataset.transcriptIndex))
					});
					requestAnimationFrame(sample);
				};
				requestAnimationFrame(sample);
				return { chars: row.textContent.length, index: Number(row.dataset.transcriptIndex),
					top: row.getBoundingClientRect().top - container.getBoundingClientRect().top,
					start: Number(container.querySelector('[data-transcript-index]').dataset.transcriptIndex) };
			}, LOG);
			try {
				await page.waitForFunction(minimum => !window.__growingReader.isConnected || window.__growingReader.textContent.length > minimum, minimumChars, { timeout: 45000 });
				await twoFrames(page);
				const after = await page.evaluate(selector => {
					window.__sampleGrowingReader = false;
					const samples = window.__growingSamples;
					return { connected: window.__growingReader.isConnected, chars: window.__growingReader.textContent.length,
						frames: samples.length, disconnected: samples.filter(sample => !sample.connected).length,
						maxDrift: Math.max(...samples.map(sample => Math.abs(sample.top - samples[0].top))),
						sameCount: samples.every(sample => sample.indices.at(-1) === Number(window.__growingReader.dataset.transcriptIndex)),
						start: Number(document.querySelector(`${selector} [data-transcript-index]`).dataset.transcriptIndex),
						rows: document.querySelectorAll('[data-transcript-index]').length,
						nodes: document.querySelector(selector).querySelectorAll('*').length };
				}, LOG);
				const viewport = await readViewport(page);
				log(`same-count growth ${JSON.stringify({ before, after, viewport })}`);
				expect(after.connected && after.disconnected === 0, 'content repricing must retain the currently-read streaming reply');
				expect(after.chars > minimumChars && after.chars > before.chars, 'the real native stream grew beyond the old window budget');
				expect(after.start > before.start && after.sameCount, 'same-count growth evicts offscreen neighbours, not the reading row');
				expect(viewport.textRows.includes(before.index), 'the retained reply has actual text intersecting the viewport');
				if (minimumChars > 48000) {
					const reply = viewport.rows.find(row => row.index === before.index);
					expect(after.rows === 1 && reply.visiblePixels > viewport.height / 2, 'the dominant oversize reply remains readable as the permitted singleton');
					expect(reply.top >= -1 && reply.top <= before.top + 1 && after.maxDrift <= Math.max(1, before.top), 'oversize fallback only clamps the removed space above the reply, never jumping past its start');
					await privateScreenshot(page, variant, 'oversize-reading');
				} else expect(after.maxDrift <= 1, 'the same reading key keeps its viewport offset during repricing');
				expect(after.rows <= 120 && after.nodes <= 12000, 'reading preservation keeps the row and nested DOM budgets');
			} finally {
				await page.evaluate(() => { window.__sampleGrowingReader = false; });
				const stop = page.getByRole('button', { name: /^Stop/ });
				if (await stop.isVisible()) await stop.click();
			}
		}
	}];
const growingReply = cases[1];
cases.push({
	...growingReply,
	name: 'same-count-visible-boundary-growth',
	setup: context => growingReply.setup({ ...context, readingBoundary: true })
});

cases.push({
	...growingReply,
	name: 'native-selection-budget-during-streaming',
	async act({ page, mark, log, variant }) {
		await mark('keep an offscreen native selection without enlarging its budget');
		const selected = await page.evaluate(() => {
			const row = document.querySelector('[data-transcript-index="0"]');
			if (!row) throw new Error('The native prefix must still be mounted before cost growth.');
			const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
			let node; while ((node = walker.nextNode()) && !node.textContent.trim()) {}
			if (!node) throw new Error('The prefix has no selectable text.');
			const range = document.createRange(); range.selectNodeContents(node);
			const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
			window.__pinnedBudgetNode = node;
			return range.toString();
		});
		try {
			const clear = page.getByRole('button', { name: 'Clear selection in transcript', exact: true });
			await clear.waitFor({ timeout: 25000 });
			const state = await page.locator(LOG).evaluate(log => ({
				connected: window.__pinnedBudgetNode.isConnected,
				selected: window.getSelection().getRangeAt(0).toString(),
				indices: [...log.querySelectorAll('[data-transcript-index]')].map(row => Number(row.dataset.transcriptIndex)),
				nodes: log.querySelectorAll('*').length
			}));
			log(`native selection priority ${JSON.stringify(state)}`);
			expect(state.connected && state.selected === selected, 'cost growth retains the native selected endpoint and text');
			expect(state.indices.at(-1) === 2, 'a separate reading key cannot retain an unselected reply over the selection budget');
			expect(state.indices.length <= 120 && state.nodes <= 12000, 'the native selection keeps the bounded DOM contract');
			const limitedViewport = await readViewport(page);
			log(`selection-limited viewport ${JSON.stringify(limitedViewport)}`);
			expect(limitedViewport.textRows.length > 0 && limitedViewport.scrollTop >= 0 && limitedViewport.scrollTop <= limitedViewport.maxScrollTop + 1, 'selection priority leaves actual transcript text in a valid, nonblank viewport');
			await privateScreenshot(page, variant, 'selection-limited');
			await clear.click();
			await twoFrames(page);
			expect(await page.evaluate(() => window.getSelection().isCollapsed), 'the accessible clear control releases selection priority');
			// This slice is already at its scroll boundary after pinning. Use
			// the explicit paging control rather than a wheel with no scroll event.
			await page.getByRole('button', { name: 'Show newer messages', exact: true }).click();
			await page.waitForFunction(() => document.querySelector('[data-transcript-index="3"]')?.textContent.includes('GROWING_READER_START'));
			const recoveredViewport = await readViewport(page);
			const reply = recoveredViewport.rows.find(row => row.index === 3);
			log(`selection-cleared viewport ${JSON.stringify(recoveredViewport)}`);
			expect(recoveredViewport.textRows.includes(3) && reply.visiblePixels > recoveredViewport.height / 2, 'clearing selection and paging makes real reply text readable, not merely mounted');
			expect(reply.top >= -1 && reply.top < recoveredViewport.height / 2 && recoveredViewport.scrollTop >= 0 && recoveredViewport.scrollTop <= recoveredViewport.maxScrollTop + 1, 'recovery shows the new reply start without a stale absolute scroll position');
			await privateScreenshot(page, variant, 'selection-cleared');
			log('clearing selection permits the still-streaming reply to remount visibly');
		} finally {
			const stop = page.getByRole('button', { name: /^Stop/ });
			if (await stop.isVisible()) await stop.click();
		}
	}
});

cases.push({
	...growingReply,
	name: 'same-count-visible-boundary-oversize',
	setup: context => growingReply.setup({ ...context, readingBoundary: true, replyScenario: 'reader-oversize' }),
	act: context => growingReply.act({ ...context, minimumChars: 52000 })
});

await runJourney({ name: 'transcript-reader', cases, allow: [...DEV_INSTANCE_NOISE] });
