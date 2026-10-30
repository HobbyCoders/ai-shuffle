/** One production virtual-window shift; no fixture renderer or mock scroll handler. */
import { arm, snapshot } from './chat-journeys.mjs';

export async function scrollJourney(p, { direction = 'older' } = {}) {
	const before = await p.page.locator('.msg-virtualized').first().getAttribute('data-transcript-index');
	if (before == null || (direction === 'older' && Number(before) === 0)) {
		throw new Error('No older virtual window; use the long fixture');
	}
	await arm(p, 'scroll');
	const t0 = await p.page.evaluate((direction) => {
		const log = document.querySelector('[role="log"][aria-label="Conversation messages"]');
		const t0 = performance.now(), older = direction === 'older';
		log.dispatchEvent(new WheelEvent('wheel', { deltaY: older ? -100000 : 100000, bubbles: true }));
		log.scrollTop = older ? 0 : log.scrollHeight;
		log.dispatchEvent(new Event('scroll'));
		return t0;
	}, direction);
	await p.page.waitForFunction(({ before, direction }) => {
		const row = document.querySelector('.msg-virtualized');
		const index = Number(row?.getAttribute('data-transcript-index'));
		return row && (direction === 'older' ? index < Number(before) : index > Number(before));
	}, { before, direction }, { timeout: 30000 });
	const done = await p.page.evaluate(() => new Promise((resolve) =>
		requestAnimationFrame(() => requestAnimationFrame(() => resolve(performance.now())))));
	return snapshot(p, { t0, firstMs: Math.round(done - t0), usableMs: Math.round(done - t0),
		beforeIndex: Number(before), direction });
}
