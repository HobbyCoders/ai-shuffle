// Journey: a long Claude Code turn compacts twice while it works. The dev
// instance's fake `claude` CLI plays scripts/devloop/fake_claude/scenarios/
// compact-twice.json: work, an automatic compaction, work, another, work, the
// last tool held for 20 s so the turn is still running when it is checked.
//
// What the chat must show: the turn's work as ONE fold while it runs (the
// "Context compacted" cards never split it into three) and once it settles,
// the fold naming both compactions, and both cards inside it when opened.
// The rest of compaction lives in compaction.journey.mjs; this case has its own
// file so neither fills a hosted shard alone.
//
// Cases:
//   twice-live  the live turn, then the settled turn
import { runJourney } from './runner.mjs';
import {
	PROJECT,
	expect,
	gotoApp,
	pick,
	pickAccess,
	pickProject,
	typeAndSend,
	uniq,
	waitChat,
	waitTurnSettled
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const CLAUDE_AGENT = 'Dev (Claude Code)';

const cards = (page) => page.locator('[data-testid="compaction-card"]');

/** Fresh Claude Code chat on the new-session screen, Dev sandbox, Bypass. */
async function openClaudeChat(ctx) {
	const { page, status } = ctx;
	if (!status.seed?.claude_cli?.agent_id) {
		throw new Error(`the dev instance has no Claude Code agent (seed: ${JSON.stringify(status.seed?.claude_cli)})`);
	}
	await gotoApp(page, status);
	await waitChat(page, { surface: 'new' }, { timeout: 90_000 });
	const hero = page.getByRole('region', { name: 'New chat' });
	await hero.waitFor();
	const agentMenu = page.getByRole('menu', { name: 'Agent selection' });
	await pick(
		hero.getByRole('button', { name: /^Agent:/ }),
		agentMenu,
		agentMenu.getByRole('button', { name: CLAUDE_AGENT, exact: true }),
		hero.getByRole('button', { name: `Agent: ${CLAUDE_AGENT}` })
	);
	await pickProject(page, hero, PROJECT);
	await pickAccess(page, 'Bypass');
	await page.waitForTimeout(800);
}

/**
 * One work fold for the whole turn, naming `times` compactions, with every
 * card inside it and none outside. `when` labels the failure messages.
 */
async function expectOneFoldAcross(page, times, when) {
	const folds = page.getByRole('button', { name: /^(Show|Hide) work details/ });
	const hide = page.getByRole('button', { name: /^Hide work details/ });
	if (await hide.count()) await hide.first().click();
	const n = await folds.count();
	const names = await folds.evaluateAll((els) => els.map((el) => el.getAttribute('aria-label') || el.textContent.trim()));
	expect(n === 1, `${when}: one work fold for the turn (${n}: ${JSON.stringify(names)})`);
	const outside = await cards(page).count();
	expect(outside === 0, `${when}: no compaction card outside the closed fold (${outside})`);
	const named = page.getByRole('button', { name: new RegExp(`^Show work details: .*compacted the context ${times} times`, 'i') });
	expect((await named.count()) === 1, `${when}: the fold names ${times} compactions (${JSON.stringify(names)})`);
	await named.click();
	await cards(page).first().waitFor({ timeout: 10_000 });
	const inside = await cards(page).count();
	expect(inside === times, `${when}: the opened fold holds ${times} compaction cards (${inside})`);
	await hide.first().click();
	await cards(page).first().waitFor({ state: 'detached', timeout: 10_000 }).catch(() => {});
}

await runJourney({
	name: 'compaction-live',
	cases: [
		{
			name: 'twice-live',
			setup: openClaudeChat,
			async act({ page, mark }) {
				const token = uniq();
				await mark('send');
				await typeAndSend(page, `keep going ${token} [script:compact-twice]`);
				// The scenario holds its last tool for 20 s after this line.
				await page.getByText('Tests pass. Checking the baseline next.').first().waitFor({ timeout: 60_000 });
				await mark('live fold');
				const stop = page.getByRole('button', { name: 'Stop generating' });
				expect(await stop.isVisible(), 'the turn is still running when the live fold is checked');
				await expectOneFoldAcross(page, 2, 'live');
				expect(await stop.isVisible(), 'the turn was still running through the live check');

				await mark('settled fold');
				await waitTurnSettled(page, { text: 'Done after two compactions.', messagesAtLeast: 2 });
				await expectOneFoldAcross(page, 2, 'settled');
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
