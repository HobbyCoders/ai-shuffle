// Real MessageArea regression: heterogeneous bodies must not erase every
// keyed anchor when a smaller overlap fits; disjoint shifts must align their
// navigation boundary, never reuse the evicted DOM's absolute scrollTop.
import { runJourney } from './runner.mjs';
import { expect, openNewChat, typeAndSend, waitChatOpen, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const LOG = '[role="log"][aria-label="Conversation messages"]';
const mobileShell = process.argv.includes('--mobile-shell');
const twoFrames = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
async function navigate(page, direction) {
	// A trusted key activates the actual control without Playwright scrolling
	// an offscreen button into auto-paging. Unlike synthetic .click(), this
	// correctly classifies the intentional navigation as recent user input.
	await page.getByRole('button', { name: `Show ${direction} messages`, exact: true }).evaluate(button => button.focus({ preventScroll: true }));
	await page.keyboard.press('Enter');
	await twoFrames(page);
}
async function middle(page) {
	// A real positive offset in the old DOM reproduces the stale-scrollTop bug.
	// Stay away from the edge so automatic paging does not race the button.
	await page.locator(LOG).evaluate(log => { log.scrollTop = 5168; });
	await twoFrames(page);
}
async function state(page, boundary) {
	return page.evaluate(({ selector, boundary }) => {
		const log = document.querySelector(selector);
		const viewport = log.getBoundingClientRect();
		const row = log.querySelector(`[data-transcript-index="${boundary}"]`);
		const rect = row?.getBoundingClientRect();
		return {
			indices: [...log.querySelectorAll('[data-transcript-index]')].map(row => Number(row.dataset.transcriptIndex)),
			top: rect?.top - viewport.top - log.clientTop,
			bottom: rect?.bottom - viewport.top - log.clientTop - log.clientHeight,
			height: log.clientHeight, total: log.scrollHeight, rowHeight: rect?.height,
			overlapConnected: window.__boundaryAnchor?.isConnected,
			nodes: log.querySelectorAll('*').length
		};
	}, { selector: LOG, boundary });
}

await runJourney({
	name: 'transcript-boundary',
	cases: [
		{ name: 'affordable-overlap', fixture: 'boundary_overlap', previous: [0, 1, 2], next: [2, 3] },
		{ name: 'unavoidable-disjoint', fixture: 'boundary_disjoint', previous: [0, 1], next: [2, 3] }
	].map(spec => ({
		name: spec.name,
		async setup({ page, status, variant }) {
			const id = status.seed?.chats?.[spec.fixture];
			if (!id) throw new Error(`This boundary journey needs the default native fixture: ${spec.fixture}`);
			const prefix = mobileShell && variant.viewport === 'phone' ? '/mobile/' : '/';
			await page.goto(new URL(`${prefix}?session=${encodeURIComponent(id)}`, status.urls.ui).toString());
			await waitChatOpen(page, { messagesAtLeast: 4 });
			// Reuse the direct navigation helper: Playwright's scroll-into-view
			// can itself trigger automatic older paging and detach this button.
			await navigate(page, 'earlier');
			await page.waitForFunction(expected => JSON.stringify([...document.querySelectorAll('[data-transcript-index]')].map(row => Number(row.dataset.transcriptIndex))) === JSON.stringify(expected), spec.previous, { timeout: 5000 });
			const before = await state(page, 0);
			expect(JSON.stringify(before.indices) === JSON.stringify(spec.previous), `setup range: ${JSON.stringify(before)}`);
			await middle(page);
		},
		async act({ page, mark, log }) {
			await page.evaluate(() => { window.__boundaryAnchor = document.querySelector('[data-transcript-index="2"]'); });
			await mark('newer across heterogeneous bodies');
			await navigate(page, 'newer');
			const newer = await state(page, spec.name === 'affordable-overlap' ? 3 : 2);
			log(`newer: ${JSON.stringify(newer)}`);
			expect(JSON.stringify(newer.indices) === JSON.stringify(spec.next), 'newer shift retains the largest affordable suffix');
			if (spec.name === 'affordable-overlap') {
				expect(newer.overlapConnected, 'the affordable overlap retains the same keyed DOM node');
				expect(newer.top >= -1 && newer.top < newer.height, 'the new reply starts in the viewport, not thousands of pixels above it');
			} else {
				// A folded user body fits wholly in the viewport. The navigation row
				// and tail spacer can leave insufficient scroll room to align its top
				// exactly; the complete incoming body must still be visible.
				if (newer.rowHeight > newer.height) expect(Math.abs(newer.top) <= 1, 'without overlap, align the first new row at the viewport top');
				else expect(newer.top >= 0 && newer.bottom <= 0, 'a short boundary body is fully visible');
				await middle(page);
				await mark('older across an unavoidable disjoint boundary');
				await navigate(page, 'earlier');
				const older = await state(page, 1);
				log(`older: ${JSON.stringify(older)}`);
				expect(JSON.stringify(older.indices) === JSON.stringify(spec.previous), 'older shift reaches the preceding range');
				expect(Math.abs(older.bottom) <= 1, 'without overlap, align the last older row at the viewport bottom');
			}
			expect(newer.indices.length <= 120 && newer.nodes < 12000, 'boundary recovery does not expand the mounted DOM budget');
		},
		allow: DEV_INSTANCE_NOISE
	})).concat([{
		// Scrolling up to the top of the mounted slice while a later reply streams.
		// Two rich replies cannot share the content budget, so automatic paging at
		// the top cannot admit the older one beside the reply being read. It used
		// to drop only the newer rows; the next streamed token put them back, and
		// the slice with its Show newer / Jump to latest controls flipped per frame.
		name: 'streaming-top-edge-holds',
		async setup({ page, status, variant }) {
			await openNewChat({ page, status, variant });
			for (const messagesAtLeast of [2, 4]) {
				await typeAndSend(page, 'Write a rich reply. [script:reader-growth tps=0 latency_ms=0]');
				await waitTurnSettled(page, { messagesAtLeast, text: 'GROWING_READER_END', hold: 250 });
			}
			await typeAndSend(page, 'Keep streaming while I scroll up. [script:long tps=12 latency_ms=0]');
			await page.getByRole('button', { name: /^Stop/ }).waitFor();
			await page.waitForFunction(selector => {
				const last = [...document.querySelectorAll(`${selector} [data-transcript-index]`)].at(-1);
				return Number(last?.dataset.transcriptIndex) >= 4 && last.textContent.trim().length > 200;
			}, LOG, { timeout: 30_000 });
		},
		async act({ page, mark, log }) {
			await mark('scroll up through the earlier rich reply while the later one streams');
			try {
				await page.evaluate(selector => {
					const log = document.querySelector(selector);
					const frames = window.__topEdgeFrames = [];
					window.__topEdgeSampling = true;
					const sample = () => {
						if (!window.__topEdgeSampling) return;
						frames.push([...log.querySelectorAll('[data-transcript-index]')].map(row => row.dataset.transcriptIndex).join(','));
						requestAnimationFrame(sample);
					};
					requestAnimationFrame(sample);
				}, LOG);
				// Wheel up like a reader, in small steps, until the first mounted row
				// is on screen, then keep pushing against that edge for a while.
				await page.locator(LOG).hover();
				const firstRowTop = () => page.locator(LOG).evaluate(log =>
					log.querySelector('[data-transcript-index]').getBoundingClientRect().top - log.getBoundingClientRect().top);
				let steps = 0;
				while (steps < 300 && await firstRowTop() < 0) {
					await page.mouse.wheel(0, -200);
					await page.waitForTimeout(30);
					steps++;
				}
				for (let i = 0; i < 30; i++) {
					await page.mouse.wheel(0, -40);
					await page.waitForTimeout(50);
				}
				const run = await page.evaluate(() => {
					window.__topEdgeSampling = false;
					const ranges = window.__topEdgeFrames.filter((range, i, all) => !i || range !== all[i - 1]);
					return {
						frames: window.__topEdgeFrames.length, ranges,
						// Paging toward older rows moves one way; returning to a range
						// already left is the frame-by-frame flip.
						revisits: ranges.filter((range, i) => ranges.slice(0, i).includes(range)).length,
						streaming: [...document.querySelectorAll('button')].some(button => /^Stop/.test(button.getAttribute('aria-label') || button.textContent.trim()))
					};
				});
				log(`scroll up ${steps} steps: ${JSON.stringify({ ...run, ranges: run.ranges.slice(0, 12) })}`);
				const firstStart = Number(run.ranges[0].split(',')[0]), lastStart = Number(run.ranges.at(-1).split(',')[0]);
				expect(run.streaming, 'the later reply was still streaming at the end');
				expect(firstStart > 0 && lastStart < firstStart, `scrolling up admitted older rows (start ${firstStart} -> ${lastStart})`);
				expect(run.revisits === 0, `the mounted slice never flips back while the reader scrolls up (${run.ranges.length} distinct steps, ${run.revisits} revisits)`);
			} finally {
				const stop = page.getByRole('button', { name: /^Stop/ });
				if (await stop.isVisible()) await stop.click();
			}
		},
		allow: DEV_INSTANCE_NOISE
	}])
});
