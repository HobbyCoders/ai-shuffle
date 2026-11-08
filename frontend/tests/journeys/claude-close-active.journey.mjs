// Real delegated close through the UI rewind, with the free scripted CLI.
import { readFileSync } from 'node:fs';
import { runJourney } from './runner.mjs';
import { composer, expect, gotoApp, pick, typeAndSend, uniq, waitChat, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

function cliLog(status) {
	return readFileSync(status.claude_cli.log, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
}

await runJourney({
	name: 'claude-close-active',
	allow: DEV_INSTANCE_NOISE,
	cases: [{
		name: 'rewind-mid-reply',
		expectShift: [{ step: 'rewind running reply', messages: [4, 2], maxValue: 0.12,
			reason: 'the requested user and partial assistant rows leave once the rewind answers' }],
		async setup(ctx) {
			const { page, status } = ctx;
			await gotoApp(page, status);
			await waitChat(page, { surface: 'new' });
			const hero = page.getByRole('region', { name: 'New chat' });
			const menu = page.getByRole('menu', { name: 'Agent selection' });
			await pick(hero.getByRole('button', { name: /^Agent:/ }), menu,
				menu.getByRole('button', { name: 'Dev (Claude Code)', exact: true }),
				hero.getByRole('button', { name: 'Agent: Dev (Claude Code)' }));
			await typeAndSend(page, `retain-${uniq()} [script:default]`);
			await waitTurnSettled(page, { messagesAtLeast: 2 });
			const offset = cliLog(status).length;
			await typeAndSend(page, `remove-${uniq()} [script:long tps=2]`);
			await waitChat(page, { streaming: 'true', messagesAtLeast: 4 });
			const entries = cliLog(status).slice(offset);
			ctx.state = { pid: entries.find(e => e.kind === 'start' && e.argv.includes('-p'))?.pid
				?? cliLog(status).filter(e => e.kind === 'start' && e.argv.includes('-p')).at(-1)?.pid };
			expect(ctx.state.pid, 'real fake-CLI process started');
		},
		async act({ page, status, state, mark, log }) {
			await mark('rewind running reply');
			await page.getByTitle('Rewind to before this message', { exact: true }).last().focus();
			await page.keyboard.press('Enter');
			const answered = page.waitForResponse(r => r.url().includes('/rewind/execute/') && r.request().method() === 'POST');
			const start = performance.now();
			await page.getByRole('dialog').getByRole('button', { name: 'Rewind', exact: true }).click();
			const result = await (await answered).json();
			const elapsed = performance.now() - start;
			log(`active CLI rewind took ${elapsed.toFixed(0)}ms; pid=${state.pid}`);
			expect(result.success && result.chat_rewound, `rewind succeeded: ${JSON.stringify(result)}`);
			expect(elapsed < 4000, `rewind must not wait out the 5-second engine-close limit (${elapsed.toFixed(0)}ms)`);
			let alive = false;
			try { process.kill(state.pid, 0); alive = true; } catch (e) { if (e.code !== 'ESRCH') throw e; }
			expect(!alive, 'closed CLI process was reaped');
			await page.getByRole('dialog').waitFor({ state: 'hidden' });
			await waitChat(page, { streaming: 'false', messages: '2' });
			await mark('send after rewind');
			await composer(page).fill('');
			await typeAndSend(page, `after-${uniq()} [script:default]`);
			await waitTurnSettled(page, { messagesAtLeast: 4 });
		}
	}]
});
