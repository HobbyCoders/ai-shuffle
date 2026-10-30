// Real browser regression: a live cross-turn selection must survive window
// shifts. The selected span is pinned inside the SAME row/content budget; at
// that boundary browsing pauses until the selection is collapsed/cleared.
import { runJourney, VIEWPORTS } from './runner.mjs';
import { SLOW_REPLY, expect, openNewChat, scriptTag, typeAndSend, uniq, waitChat, waitChatOpen, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { permitsOversizeOriginSettle } from './lib/transcript-settle.mjs';

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

// The row under the top edge of the transcript, and where the reader is in it:
// its top offset when the row starts inside the view, else the fraction of the
// row already scrolled past (a long reply reflows to a new height).
const readingAnchor = (page, key) => page.evaluate(({ selector, key }) => {
	const top = document.querySelector(selector).getBoundingClientRect().top;
	const rows = [...document.querySelectorAll('[data-transcript-key]')];
	const row = key ? rows.find(node => node.dataset.transcriptKey === key) : rows.find(node => node.getBoundingClientRect().bottom > top + 1);
	if (!row) return null;
	const box = row.getBoundingClientRect();
	return { key: row.dataset.transcriptKey, offset: box.top - top, height: box.height, past: Math.max(0, top - box.top) / box.height };
}, { selector: LOG, key });
/** How far (px, in the row's new geometry) the reading point moved within its row. */
const anchorDrift = (before, after) => before.offset >= 0
	? Math.abs(after.offset - before.offset)
	: Math.abs(after.past - before.past) * after.height + Math.max(0, after.offset);

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
		async setup({ page, status, variant, readingBoundary = false, replyScenario = 'reader-growth', project = null }) {
			// Produce a fresh native prefix through the real composer/provider, then
			// replay it into the affected shell. No synthetic feed/state or SDK-only
			// fork endpoint: default native seed sessions have no SDK session id.
			await openNewChat({ page, status, variant }, { project });
			await typeAndSend(page, 'Prepare a rich reading prefix. [script:reader-growth tps=0 latency_ms=0]');
			await waitTurnSettled(page, { messagesAtLeast: 2, text: 'GROWING_READER_END', hold: 250 });
			// First-send binding persists after the rendered turn settles. Wait for
			// that real producer identity; never retry the send or invent a feed.
			await page.waitForFunction(() => typeof JSON.parse(sessionStorage.getItem('ai-shuffle-active-session') || 'null')?.sessionId === 'string', null, { timeout: 15000 });
			const id = await page.evaluate(() => JSON.parse(sessionStorage.getItem('ai-shuffle-active-session') || 'null')?.sessionId);
			expect(typeof id === 'string', 'the real producer bound a fresh native session');
			const prefix = mobileShell && variant.viewport === 'phone' ? '/mobile/' : '/';
			await page.goto(new URL(`${prefix}?session=${encodeURIComponent(id)}`, status.urls.ui).toString());
			await waitChatOpen(page, { messagesAtLeast: 2, hold: 250 });
			if (project) await page.getByRole('button', { name: /^Git actions:/ }).waitFor();
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
				const rows = [...log.querySelectorAll('[data-transcript-index]')];
				return { indices: rows.filter(row => {
					const box = row.getBoundingClientRect(); return box.bottom > top && box.top < bottom;
				}).map(row => Number(row.dataset.transcriptIndex)),
					reply: Number(rows.filter(row => row.textContent.includes('GROWING_READER_START')).at(-1).dataset.transcriptIndex) };
			});
			expect(visible.indices[0] === visible.reply - (readingBoundary ? 1 : 0) && visible.indices.includes(visible.reply), `read the growing reply at the intended viewport position (boundary=${readingBoundary}, visible=${visible.indices.join(',')})`);
		},
		async act({ page, mark, log, variant, minimumChars = 17000 }) {
			await mark('read the live reply while same-count growth reprices the window');
			let oversizeSettle, after;
			if (minimumChars > 48000) await page.evaluate(selector => {
				const container = document.querySelector(selector);
				window.__oversizeShifts = [];
				window.__oversizeTransitions = [];
				let previousRows = container.querySelectorAll('[data-transcript-index]').length;
				window.__oversizeMutation = new MutationObserver(() => {
					const rows = [...container.querySelectorAll('[data-transcript-index]')];
					if (previousRows > 1 && rows.length === 1 && rows[0] === window.__growingReader) {
						const transition = { id: window.__oversizeTransitions.length, fromRows: previousRows,
							toRows: rows.length, reader: true, began: performance.now(), scrollTop: null, frameEnd: null };
						window.__oversizeTransitions.push(transition);
						requestAnimationFrame(() => {
							transition.scrollTop = container.scrollTop;
							requestAnimationFrame(() => { transition.frameEnd = performance.now(); });
						});
					}
					previousRows = rows.length;
				});
				window.__oversizeMutation.observe(container.querySelector('[data-transcript-index]').parentElement, { childList: true });
				window.__captureOversizeShifts = entries => {
					for (const entry of entries) {
						if (entry.hadRecentInput || entry.value === 0) continue;
						const rows = [...container.querySelectorAll('[data-transcript-index]')];
						window.__oversizeShifts.push({ value: entry.value, at: entry.startTime, scrollTop: container.scrollTop,
							singleton: rows.length === 1 && rows[0] === window.__growingReader,
							virtualizedOnly: entry.sources.length > 0 && entry.sources.every(source => source.node?.classList?.contains('msg-virtualized')) });
					}
				};
				window.__oversizeObserver = new PerformanceObserver(list => window.__captureOversizeShifts(list.getEntries()));
				window.__oversizeObserver.observe({ type: 'layout-shift' });
			}, LOG);
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
						at: performance.now(), scrollTop: container.scrollTop, connected: row.isConnected, chars: row.textContent.length,
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
				after = await page.evaluate(selector => {
					window.__sampleGrowingReader = false;
					const samples = window.__growingSamples;
					return { connected: window.__growingReader.isConnected, chars: window.__growingReader.textContent.length,
						frames: samples.length, disconnected: samples.filter(sample => !sample.connected).length,
						maxDrift: Math.max(...samples.map(sample => Math.abs(sample.top - samples[0].top))),
						driftSteps: samples.filter((sample, index) => index === 0 || sample.top !== samples[index - 1].top),
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
					expect(reply.top >= -1 && reply.top <= before.top + 1, 'the oversize fallback never jumps past the reply start');
					await privateScreenshot(page, variant, 'oversize-reading');
				} else expect(after.maxDrift <= 1, 'the same reading key keeps its viewport offset during repricing');
				expect(after.rows <= 120 && after.nodes <= 12000, 'reading preservation keeps the row and nested DOM budgets');
			} finally {
				await page.evaluate(() => { window.__sampleGrowingReader = false; });
				const stop = page.getByRole('button', { name: /^Stop/ });
				if (await stop.isVisible()) await stop.click();
				if (minimumChars > 48000) {
					await twoFrames(page);
					oversizeSettle = await page.evaluate(() => {
						window.__captureOversizeShifts(window.__oversizeObserver.takeRecords());
						window.__oversizeObserver.disconnect();
						window.__oversizeMutation.disconnect();
						return { total: window.__oversizeShifts.reduce((sum, entry) => sum + entry.value, 0),
							entries: window.__oversizeShifts.map(entry => ({ ...entry,
								transition: window.__oversizeTransitions.find(event => event.frameEnd !== null &&
									entry.at >= event.began - 1 && entry.at <= event.frameEnd + 1)?.id ?? null })),
							transitions: window.__oversizeTransitions };
					});
				}
			}
			if (minimumChars > 48000) {
				log(`oversize singleton settle ${JSON.stringify(oversizeSettle)}`);
				const originSettle = permitsOversizeOriginSettle({ phone: variant.viewport === 'phone', before, after, settle: oversizeSettle });
				expect(after.maxDrift <= 1 || originSettle, 'the <=1px reading limit only permits the single phone singleton transition at scroll origin');
				expect(oversizeSettle.total < 0.05 || originSettle, 'strict CLS applies except to that same single phone-origin transition');
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
		// #869: on the phone composer every transcript pointerdown blurred the
		// editor through TipTap's blur command, which cleared the whole document
		// selection a frame later, so a Shift+click extension vanished.
		name: 'shift-click-extends-selection',
		async setup({ page, status, variant }) {
			const id = status.seed?.chats?.long ?? status.seed?.sessions?.large;
			if (!id) throw new Error('This reader journey needs the standard long or performance seed.');
			const prefix = mobileShell && variant.viewport === 'phone' ? '/mobile/' : '/';
			await page.goto(new URL(`${prefix}?session=${encodeURIComponent(id)}`, status.urls.ui).toString());
			await waitChatOpen(page, { messagesAtLeast: 10, timeout: 90_000 });
		},
		async act({ page, mark, log }) {
			await mark('select three characters, then Shift+click further along');
			const target = await page.evaluate(() => {
				const viewport = document.querySelector('[role="log"][aria-label="Conversation messages"]').getBoundingClientRect();
				const paragraph = [...document.querySelectorAll('[data-transcript-index] .abody p')].reverse().find(p => {
					const box = p.getBoundingClientRect();
					return box.top > viewport.top + 8 && box.bottom < viewport.bottom - 8 && (p.firstChild?.textContent?.length ?? 0) > 30;
				});
				if (!paragraph) throw new Error('No fully visible assistant paragraph to select in.');
				const text = paragraph.firstChild;
				const range = document.createRange();
				range.setStart(text, 0); range.setEnd(text, 3);
				const selection = window.getSelection();
				selection.removeAllRanges(); selection.addRange(range);
				const point = document.createRange();
				point.setStart(text, 20); point.setEnd(text, 21);
				const box = point.getBoundingClientRect();
				return { x: box.left + box.width / 2, y: box.top + box.height / 2 };
			});
			await page.keyboard.down('Shift');
			await page.mouse.click(target.x, target.y);
			await page.keyboard.up('Shift');
			await twoFrames(page);
			await page.waitForTimeout(300);
			const state = await page.evaluate(() => ({ ranges: window.getSelection().rangeCount, chars: window.getSelection().toString().length }));
			log(`after Shift+click: ranges=${state.ranges}, chars=${state.chars}`);
			expect(state.ranges === 1 && state.chars > 3, 'Shift+click extends the transcript selection and it survives the click');
		}
	}, {
		// #870: each streamed delta replaced the reply's {@html} blocks, which
		// detached the selected text nodes and dropped the reader's selection.
		name: 'selection-survives-streaming',
		async setup(ctx) {
			await openNewChat(ctx);
			await ctx.page.waitForTimeout(800);
		},
		async act({ page, mark, variant, log }) {
			await mark('send a slowly streamed reply');
			await typeAndSend(page, `stream slowly tok-${uniq()} ${scriptTag(variant, 'slow', { tps: 3 })}`);
			await page.getByText(SLOW_REPLY.start, { exact: false }).first().waitFor({ timeout: 60_000 });
			await waitChat(page, { streaming: 'true' });
			await mark('select part of the streaming reply');
			const selected = await page.evaluate(start => {
				const bodies = document.querySelectorAll('[data-transcript-index] .abody');
				const body = bodies[bodies.length - 1];
				const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
				let node; while ((node = walker.nextNode()) && !node.textContent.includes(start));
				if (!node) throw new Error('The streaming reply has no selectable text yet.');
				const range = document.createRange();
				range.setStart(node, 5); range.setEnd(node, 18);
				const selection = window.getSelection();
				selection.removeAllRanges(); selection.addRange(range);
				window.__streamSelectionNode = node;
				return selection.toString();
			}, SLOW_REPLY.start);
			await page.waitForTimeout(2500);
			const state = await page.evaluate(() => ({
				streaming: document.querySelector('[data-ui-state="chat"]').getAttribute('data-ui-streaming'),
				connected: window.__streamSelectionNode.isConnected,
				text: window.getSelection().toString()
			}));
			log(`after 2.5s of deltas: streaming=${state.streaming}, connected=${state.connected}, text=${JSON.stringify(state.text)}`);
			expect(state.streaming === 'true', 'the reply was still streaming while the selection was held');
			expect(state.connected && state.text === selected, 'the selection inside the streaming reply survives appended text');
			await mark('clear the selection; the rest of the reply appears');
			await page.evaluate(() => window.getSelection().removeAllRanges());
			await waitTurnSettled(page, { text: SLOW_REPLY.end, messagesAtLeast: 2 });
		}
});

cases.push({
	// #865: rotating the phone (or resizing the window) reflowed every row
	// above the reader at the same scrollTop, so the turn being read moved
	// far out of view. The row at the top of the view must stay put.
	name: 'rotation-keeps-reading-row',
	async setup({ page, status, variant }) {
		const id = status.seed?.chats?.long ?? status.seed?.sessions?.large;
		if (!id) throw new Error('This reader journey needs the standard long or performance seed.');
		const prefix = mobileShell && variant.viewport === 'phone' ? '/mobile/' : '/';
		await page.goto(new URL(`${prefix}?session=${encodeURIComponent(id)}`, status.urls.ui).toString());
		await waitChatOpen(page, { messagesAtLeast: 50, timeout: 90_000 });
		await page.locator(LOG).hover();
		// Partway up the mounted transcript: well clear of both edges.
		const target = await page.evaluate(selector => {
			const log = document.querySelector(selector);
			return Math.round((log.scrollHeight - log.clientHeight) * 0.45);
		}, LOG);
		for (let i = 0; i < 40; i++) {
			const left = await page.evaluate(selector => document.querySelector(selector).scrollTop, LOG);
			if (left <= target + 50) break;
			await page.mouse.wheel(0, -Math.min(600, left - target));
			await twoFrames(page);
		}
		await page.waitForTimeout(400);
	},
	async act({ page, mark, variant, log }) {
		const start = page.viewportSize();
		const turned = variant.viewport === 'desktop'
			? { width: 960, height: start.height }
			: { width: start.height, height: start.width };
		const settle = async () => { await twoFrames(page); await page.waitForTimeout(500); await twoFrames(page); };
		for (const [label, size] of [['rotate', turned], ['rotate back', start]]) {
			const before = await readingAnchor(page);
			expect(before, 'a transcript row sits at the top of the view');
			await mark(`${label} to ${size.width}x${size.height}`);
			await page.setViewportSize(size);
			await settle();
			const after = await readingAnchor(page, before.key);
			const drift = after ? anchorDrift(before, after) : Infinity;
			log(`${label}: row ${before.key} offset ${before.offset.toFixed(1)} (past ${(before.past * 100).toFixed(1)}%) -> ${after ? `${after.offset.toFixed(1)} (past ${(after.past * 100).toFixed(1)}%)` : 'unmounted'}, drift ${drift.toFixed(1)}px`);
			expect(drift <= 4, `${label}: the row being read stays where it was (drift ${drift.toFixed(1)}px)`);
		}
	}
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

const oversizeCase = {
	...growingReply,
	name: 'same-count-visible-boundary-oversize',
	setup: context => growingReply.setup({ ...context, readingBoundary: true, replyScenario: 'reader-oversize' }),
	act: context => growingReply.act({ ...context, minimumChars: 52000 })
};
// Desktop retains the original recorder and <=1px reading limits. Only the
// phone definition has a recorder allowance; the measured transition predicate
// above must independently accept its exact event before that rule can apply.
cases.push({ ...oversizeCase, viewports: ['desktop'] });
cases.push({ ...oversizeCase, viewports: ['phone'], allow: [{ kind: 'layout_shift',
	message: /^layout shifted 0\.0\d{2} in total; biggest 0\.0\d{2} at \d+ms \(moved: div\.msg-virtualized in MessageArea\)$/,
	reason: 'Only the measured phone singleton TRANSITION at scrollTop=0: exactly one associated non-input shift, exact CLS<0.1, <=20px position and a readable reply. Desktop and every other event retain strict limits.' }] });

// Fixed mobile sheets do not resize the transcript. Read the real native
// prefix above the sheet while a later reply grows past the same budget.
if (mobileShell) for (const { sheet, opening = false } of [
	...['usage', 'controls', 'workers', 'navigation', 'actions', 'attach', 'git'].map(sheet => ({ sheet })),
	{ sheet: 'usage', opening: true }
]) cases.push({
	name: `overlay-${sheet}${opening ? '-opening' : ''}-reading-growth`,
	viewports: ['phone'],
	setup: context => growingReply.setup({ ...context, replyScenario: 'reader-growth tps=3', project: sheet === 'git' ? 'Dev sandbox' : null }),
	async act({ page, mark, log, variant }) {
		await mark(`preserve the exposed reading key under the real ${sheet} sheet`);
		const fullCover = sheet === 'controls' || sheet === 'navigation' || sheet === 'attach';
		await page.locator(LOG).evaluate((container, fullCover) => {
			const replies = [...container.querySelectorAll('[data-transcript-index]')].filter(row => row.textContent.includes('GROWING_READER_START'));
			const prefix = replies[0], reply = replies.at(-1);
			if (!prefix || !reply) throw new Error('Both native replies must still be mounted before opening the sheet.');
			const gap = reply.getBoundingClientRect().top - prefix.getBoundingClientRect().bottom;
			const height = container.clientHeight;
			const coverage = fullCover ? height * 0.7 : Math.floor((height - gap) / 2) - 8;
			container.scrollTop += prefix.getBoundingClientRect().bottom - container.getBoundingClientRect().top - coverage;
			window.__overlayPrefix = prefix;
			window.__overlayReply = reply;
			window.__overlayPrefixTop = prefix.getBoundingClientRect().top;
			window.__overlayMaxDrift = 0;
			window.__overlaySampling = true;
			const sample = () => {
				if (!window.__overlaySampling) return;
				window.__overlayMaxDrift = Math.max(window.__overlayMaxDrift, Math.abs(prefix.getBoundingClientRect().top - window.__overlayPrefixTop));
				requestAnimationFrame(sample);
			}; requestAnimationFrame(sample);
			window.__overlayShifts = [];
			window.__overlayShiftObserver = new PerformanceObserver(list => {
				for (const entry of list.getEntries()) if (!entry.hadRecentInput) window.__overlayShifts.push({
					value: entry.value, at: entry.startTime, sources: entry.sources.map(source => ({
						node: `${source.node?.tagName}.${source.node?.className}`, index: source.node?.closest?.('[data-transcript-index]')?.dataset.transcriptIndex,
						previous: source.previousRect.toJSON(), current: source.currentRect.toJSON() })) });
			});
			window.__overlayShiftObserver.observe({ type: 'layout-shift' });
		}, fullCover);
		const opener = sheet === 'usage' ? page.getByRole('button', { name: /^Context and usage/ })
			: sheet === 'controls' ? page.getByRole('button', { name: /^All chat controls/ })
			: sheet === 'workers' ? page.getByRole('button', { name: /^Connected to .*Switch worker/ })
			: sheet === 'actions' ? page.getByRole('button', { name: 'Chat actions', exact: true })
			: sheet === 'attach' ? page.getByRole('button', { name: /^No project\. Attach a project/ })
			: sheet === 'git' ? page.getByRole('button', { name: /^Git actions:/ })
			: page.getByRole('button', { name: 'Open menu', exact: true });
		if (opening) await page.evaluate(() => {
			// Hold the REAL CSS opening animation at origin through the initial
			// ResizeObserver delivery, then advance it without a resize/end event.
			// The real provider keeps streaming across the budget while it is held.
			const observer = new MutationObserver(() => {
				const node = document.querySelector('[role="dialog"][aria-label="Context and usage"]');
				const animation = node?.getAnimations().find(animation => animation.animationName?.includes('mcc-sheet'));
				if (!animation) return;
				animation.pause(); animation.currentTime = 0;
				window.__overlayOpening = animation;
				observer.disconnect();
			});
			observer.observe(document.body, { childList: true, subtree: true });
		});
		await opener.click();
		const overlay = sheet === 'navigation' ? page.locator('#mobile-nav-drawer')
			: page.getByRole('dialog', { name: { usage: 'Context and usage', controls: 'Chat controls', workers: 'Connected to', actions: 'Chat actions', attach: 'Attach a project', git: 'Git actions' }[sheet], exact: true });
		try {
			await overlay.waitFor();
			if (opening) {
				await twoFrames(page);
				expect(await overlay.evaluate(node => window.__overlayOpening?.effect.target === node), 'the real usage-sheet opening animation was captured before it finished');
				await page.evaluate(() => { window.__overlayOpening.currentTime = 100; });
				await twoFrames(page);
			} else await overlay.evaluate(async node => { await Promise.all(node.getAnimations().map(animation => animation.finished)); });
			const measure = (sheet, { selector, layer }) => {
				const container = document.querySelector(selector), prefix = window.__overlayPrefix;
				const viewport = container.getBoundingClientRect(), overlay = (layer ? sheet.closest('.attach-layer') : sheet).getBoundingClientRect();
				const exposedBottom = Math.max(viewport.top, Math.min(viewport.bottom, overlay.top));
				const pixels = row => { const box = row.getBoundingClientRect(); return Math.max(0, Math.min(box.bottom, exposedBottom) - Math.max(box.top, viewport.top)); };
				const others = [...container.querySelectorAll('[data-transcript-index]')].filter(row => row !== prefix);
				return { retained: prefix.isConnected, replyRetained: window.__overlayReply.isConnected,
					drift: Math.abs(prefix.getBoundingClientRect().top - window.__overlayPrefixTop), maxDrift: window.__overlayMaxDrift, shifts: window.__overlayShifts, exposedHeight: exposedBottom - viewport.top,
					visiblePixels: pixels(prefix), otherVisiblePixels: Math.max(0, ...others.map(pixels)),
					rows: container.querySelectorAll('[data-transcript-index]').length, nodes: container.querySelectorAll('*').length };
			};
			const bounds = { selector: LOG, layer: sheet === 'attach' };
			const before = await overlay.evaluate(measure, bounds);
			// Dominant means more exposed pixels than EACH other row, not more
			// than half the viewport: a visible prompt can occupy the remainder.
			expect(fullCover ? before.exposedHeight === 0 : before.visiblePixels > before.otherVisiblePixels,
				'the prefix is the dominant exposed row before eviction (or was captured before full cover)');
			await page.waitForFunction(() => !window.__overlayPrefix.isConnected || !window.__overlayReply.isConnected, null, { timeout: 45000 });
			await twoFrames(page);
			const state = await overlay.evaluate(measure, bounds);
			if (opening) {
				const phase = await page.evaluate(() => ({ playState: window.__overlayOpening.playState,
					at: window.__overlayOpening.currentTime, duration: window.__overlayOpening.effect.getTiming().duration }));
				log(`budget crossed during the real opening animation ${JSON.stringify(phase)}`);
				expect(phase.playState === 'paused' && phase.at === 100 && phase.at < phase.duration && state.rows < before.rows,
					'the same-count budget crossing happened DURING opening, with no animation-end event');
			}
			log(`real ${sheet}${opening ? ' opening' : ''} overlay ${JSON.stringify(state)}`);
			expect(state.retained && !state.replyRetained, 'repricing preserves the exposed/previous reading prefix, not the covered growing reply');
			expect(state.drift <= 1 && state.maxDrift <= 1, 'opening a sheet must not lose the reading key during repricing, even for a frame');
			expect(fullCover ? state.exposedHeight === 0 : state.visiblePixels > state.otherVisiblePixels, 'the key stays the dominant exposed row or is preserved while fully obscured');
			expect(state.rows <= 120 && state.nodes <= 12000, 'overlay preservation keeps the same mounted-content budget');
			await privateScreenshot(page, variant, `overlay-${sheet}`);
		} finally {
			await page.evaluate(() => { window.__overlaySampling = false; window.__overlayShiftObserver?.disconnect(); });
			await page.keyboard.press('Escape');
			await twoFrames(page);
			const stop = page.getByRole('button', { name: /^Stop/ });
			if (await stop.isVisible()) await stop.click();
		}
	}
});

await runJourney({ name: 'transcript-reader', cases, allow: [...DEV_INSTANCE_NOISE] });
