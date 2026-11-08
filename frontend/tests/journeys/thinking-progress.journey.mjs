// A long silent think keeps moving on the working line and never reaches the transcript (#1271).
import { runJourney } from './runner.mjs';
import { expect, openNewChat, pick, typeAndSend, uniq, waitChat } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const CLAUDE = 'Dev (Claude Code)';
const tail = (page) => page.getByTestId('turn-activity').filter({ hasText: /Thinking/ });
const tokens = (text) => Number(/([\d.]+)(k?) tokens/.exec(text)?.slice(1, 2)[0] ?? NaN) * (/k tokens/.test(text) ? 1000 : 1);

await runJourney({
	name: 'thinking-progress',
	allow: DEV_INSTANCE_NOISE,
	cases: [{
		name: 'silent-think-keeps-moving',
		viewports: ['desktop', 'phone'],
		async setup(ctx) {
			const hero = await openNewChat(ctx);
			const menu = ctx.page.getByRole('menu', { name: 'Agent selection' });
			await pick(
				hero.getByRole('button', { name: /^Agent:/ }),
				menu,
				menu.getByRole('button', { name: CLAUDE, exact: true }),
				hero.getByRole('button', { name: `Agent: ${CLAUDE}` })
			);
		},
		async act({ page, mark, log }) {
			await mark('send');
			await typeAndSend(page, `think ${uniq()} [script:long-think]`);
			await tail(page).waitFor({ timeout: 30000 });
			const first = await tail(page).innerText();
			await page.waitForTimeout(3500);
			const later = await tail(page).innerText();
			log(`working line: "${first}" then "${later}"`);
			expect(tokens(later) > tokens(first), `the estimate keeps climbing: "${first}" then "${later}"`);
			await page.getByText('Done thinking.', { exact: false }).first().waitFor({ timeout: 30000 });
			await waitChat(page, { streaming: 'false' }, { timeout: 30000 });
			expect(await tail(page).count() === 0, 'the thinking line ends with the turn');
			expect(await page.getByText('Thinking Tokens', { exact: false }).count() === 0, 'no transcript row for the estimate');
			await mark('reload');
			await page.reload({ waitUntil: 'domcontentloaded' });
			// A new step once the recorder has seen the old page go: a reload is not the app going backwards.
			await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
			await mark('page reloaded');
			await page.getByText('Done thinking.', { exact: false }).first().waitFor({ timeout: 30000 });
			expect(await page.getByText('Thinking Tokens', { exact: false }).count() === 0, 'reload keeps the estimate hidden');
		}
	}]
});
