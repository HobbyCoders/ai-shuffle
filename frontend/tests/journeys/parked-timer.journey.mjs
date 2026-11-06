// Journey: a session that parks on its own background work keeps counting its
// total working time in the sidebar. The card shows "Parked" while the job
// runs and its timer keeps going up; it never drops back below what it showed
// while the turn was live, and it is still there after the wake turn settles.
import { runJourney } from './runner.mjs';
import { PROJECT, expect, openNewChat, pickAccess, scriptTag, showSidebar, typeAndSend, uniq } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const ACTIVE_CARD = '[data-session-card] .scard[aria-current="true"]';

/** The exact total in the hover title, "42s", "4m 12s", "1h 04m 09s" → seconds. */
function seconds(text) {
	const m = (text || '').trim().match(/^(?:(\d+)h )?(?:(\d+)m )?(\d+)s$/);
	return m ? Number(m[1] || 0) * 3600 + Number(m[2] || 0) * 60 + Number(m[3]) : NaN;
}

function readCard(page) {
	return page.evaluate((selector) => {
		const card = document.querySelector(selector);
		return {
			// No row prints a status word; its screen-reader details always start with it.
			status: card?.querySelector('.details')?.textContent?.split(' · ')[0].trim() || null,
			shown: card?.querySelector('.timer')?.textContent?.trim() || null,
			// Only a working row ticks seconds; every row keeps the exact total on hover.
			timer: card?.querySelector('.timer')?.getAttribute('title')?.match(/^Total working time ([^·]+)/)?.[1].trim() || null
		};
	}, ACTIVE_CARD);
}

await runJourney({
	name: 'parked-timer',
	recorder: { selectors: [`timer=${ACTIVE_CARD} .timer`, `status=${ACTIVE_CARD} .details`] },
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
					(selector) => document.querySelector(selector)?.querySelector('.details')?.textContent?.split(' · ')[0].trim() === 'Parked',
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
				expect(/^(<1m|\d+m|\d+h \d\dm)$/.test(second.shown), `a parked row shows whole minutes (${second.shown})`);

				await mark('woken');
				await page.waitForFunction(
					(selector) => {
						const status = document.querySelector(selector)?.querySelector('.details')?.textContent?.split(' · ')[0].trim();
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
	allow: [...DEV_INSTANCE_NOISE]
});
