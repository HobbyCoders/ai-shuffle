// Journey: a session that parks on its own background work keeps counting its
// total working time in the sidebar. The card shows "Parked" while the job
// runs and its timer keeps going up; it never drops back below what it showed
// while the turn was live, and it is still there after the wake turn settles.
import { runJourney } from './runner.mjs';
import { PROJECT, expect, openNewChat, pickAccess, scriptTag, showSidebar, typeAndSend, uniq } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const ACTIVE_CARD = '[data-session-card] .scard[aria-current="true"]';

/** "42s", "4m 12s", "1h 04m" → seconds. */
function seconds(text) {
	const t = (text || '').trim();
	let m = t.match(/^(\d+)s$/);
	if (m) return Number(m[1]);
	m = t.match(/^(\d+)m (\d+)s$/);
	if (m) return Number(m[1]) * 60 + Number(m[2]);
	m = t.match(/^(\d+)h (\d+)m$/);
	if (m) return Number(m[1]) * 3600 + Number(m[2]) * 60;
	return NaN;
}

function readCard(page) {
	return page.evaluate((selector) => {
		const card = document.querySelector(selector);
		return {
			status: card?.querySelector('.st')?.textContent?.trim() || null,
			timer: card?.querySelector('.timer')?.textContent?.trim() || null
		};
	}, ACTIVE_CARD);
}

/**
 * Known bug #835: on a phone a wake turn's reply moves the transcript and the
 * composer's send buttons when it ends (no input). Only that shift: under 0.2
 * in total, biggest frame those three elements.
 */
const WAKE_REPLY_SHIFT = {
	kind: 'layout_shift',
	message: /^layout shifted 0\.[01]\d+ in total; biggest 0\.0\d+ at \d+ms \(moved: div\.w-full in MessageArea, div\.send-cluster in ChatInput, span\.aname-text in MessageArea\)$/,
	reason: "known bug #835: on a phone a wake turn's reply moves the transcript and send buttons when it ends"
};

await runJourney({
	name: 'parked-timer',
	recorder: { selectors: [`timer=${ACTIVE_CARD} .timer`, `status=${ACTIVE_CARD} .st`] },
	cases: [
		{
			name: 'park',
			async setup(ctx) {
				// The background-job tool is offered in a project chat.
				await openNewChat(ctx, { project: PROJECT });
				// Not about approvals: the background job starts without a prompt.
				await pickAccess(ctx.page, 'Bypass');
				await ctx.page.waitForTimeout(800);
			},
			async act({ page, mark, variant, log }) {
				await mark('send');
				await typeAndSend(page, `park tok-${uniq()} ${scriptTag(variant, 'park-wait')}`);

				// The sidebar is a closed drawer on a phone; open it to watch the card.
				await showSidebar(page);

				await mark('parked');
				await page.waitForFunction(
					(selector) => document.querySelector(selector)?.querySelector('.st')?.textContent?.trim() === 'Parked',
					ACTIVE_CARD,
					{ timeout: 60_000 }
				);
				const first = await readCard(page);
				await page.waitForTimeout(4000);
				const second = await readCard(page);
				log(`parked timer ${first.timer} → ${second.timer} (status ${second.status})`);
				expect(second.status === 'Parked', `still parked 4s later (status ${second.status})`);
				const grew = seconds(second.timer) - seconds(first.timer);
				expect(grew >= 3, `the parked timer kept counting (${first.timer} → ${second.timer})`);

				await mark('woken');
				await page.waitForFunction(
					(selector) => {
						const status = document.querySelector(selector)?.querySelector('.st')?.textContent?.trim();
						return status && status !== 'Parked' && status !== 'Working';
					},
					ACTIVE_CARD,
					{ timeout: 90_000 }
				);
				await page.waitForTimeout(1500);
				const done = await readCard(page);
				log(`after the wake: ${done.timer} (status ${done.status})`);
				// The park ran ~20s; the banked total keeps it.
				expect(seconds(done.timer) >= 15, `the park's time stayed in the total (${done.timer})`);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE, WAKE_REPLY_SHIFT]
});
