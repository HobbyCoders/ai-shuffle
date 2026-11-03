// Real producing path: Claude CLI -> Fluid transcript -> UI rewind -> CLI reseed/resume.
// Preview-cookie no-project chats intentionally use the restricted chat-uploads lane.
import { readFileSync } from 'node:fs';
import { runJourney } from './runner.mjs';
import { chatState, composer, expect, gotoApp, pick, scriptTag, typeAndSend, uniq, waitChat, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

function cliLog(status) {
	return readFileSync(status.claude_cli.log, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
}

await runJourney({
	name: 'claude-rewind-cwd',
	allow: DEV_INSTANCE_NOISE,
	cases: ['reply', 'edit-and-resend', 'fork'].map(action => ({
		name: `no-project-${action}`,
		async setup(ctx) {
			const { page, status, variant } = ctx;
			expect(status.seed?.claude_cli?.agent_id, 'fake Claude Code is seeded');
			await gotoApp(page, status);
			await waitChat(page, { surface: 'new' });
			const hero = page.getByRole('region', { name: 'New session' });
			const menu = page.getByRole('menu', { name: 'Agent selection' });
			await pick(hero.getByRole('button', { name: /^Agent:/ }), menu,
				menu.getByRole('button', { name: 'Dev (Claude Code)', exact: true }),
				hero.getByRole('button', { name: 'Agent: Dev (Claude Code)' }));
			expect(await hero.getByRole('button', { name: /^No project/ }).isVisible(), 'fresh chat has no project');
			const retained = `retained-${uniq()}`;
			const removed = `removed-${uniq()}`;
			await typeAndSend(page, `${retained} ${scriptTag(variant, 'default')}`);
			await waitTurnSettled(page, { text: `You said: “${retained}` });
			await typeAndSend(page, `${removed} ${scriptTag(variant, 'default')}`);
			await waitTurnSettled(page, { messagesAtLeast: 4, text: `You said: “${removed}` });
			ctx.state = { retained, removed };
			if (action === 'fork') {
				// Exercise the same endpoint/creation shape as the title menu,
				// not a fixture that preselects the child's cwd or mode.
				const parentTag = (await chatState(page)).session;
				// gotoApp opens the desktop shell, also at a phone viewport.
				await page.locator('button.title-trigger[aria-current="page"]').click();
				const response = page.waitForResponse(r => r.url().endsWith('/fork') && r.request().method() === 'POST');
				await page.getByRole('menuitem', { name: 'Fork session', exact: true }).click();
				const result = await (await response).json();
				expect(result.status === 'success', `fork succeeded: ${JSON.stringify(result)}`);
				await page.waitForFunction(parent => document.querySelector('[data-ui-state="chat"]')?.getAttribute('data-ui-session') !== parent, parentTag);
				await waitChat(page, { surface: 'active', loading: 'false', messagesAtLeast: 4 });
				return;
			}
			// This journey judges the post-rewind CLI resume, not history reload
			// rendering. Stage the real UI rewind here: its pre-existing
			// active -> cleared -> active flicker is tracked separately, not
			// allowed or called clean by this backend regression.

			// Keyboard activation avoids the hover-only action row disappearing as
			// the pointer crosses the adjacent user bubble (not this backend test's subject).
			await page.getByTitle('Rewind to before this message', { exact: true }).last().focus();
			await page.keyboard.press('Enter');
			const response = page.waitForResponse(r => r.url().includes('/rewind/execute/') && r.request().method() === 'POST');
			await page.getByRole('dialog').getByRole('button', { name: 'Rewind', exact: true }).click();
			const result = await (await response).json();
			expect(result.success && result.chat_rewound, `rewind succeeded: ${JSON.stringify(result)}`);
			await page.getByRole('dialog').waitFor({ state: 'hidden' });
			await waitChat(page, { streaming: 'false', messagesAtMost: 2 });
			expect((await composer(page).innerText()).includes(removed), 'rewound words restored to composer');
			await waitChat(page, { loading: 'false', messagesAtLeast: 2 });
			await page.waitForTimeout(500);
		},
		async act({ page, status, variant, mark, log, state }) {
			const { retained, removed } = state;
			const offset = cliLog(status).length;
			await mark(action === 'fork' ? 'reply in the fork' : action === 'reply' ? 'reply after rewind' : 'edit and resend restored words');
			const next = `${action === 'reply' ? 'reply' : removed + '-edited'}-${uniq()}`;
			await typeAndSend(page, `${next} ${scriptTag(variant, 'default')}`);
			await waitTurnSettled(page, { messagesAtLeast: action === 'fork' ? 6 : 4, text: `You said: “${next}` });
			const entries = cliLog(status).slice(offset);
			const init = entries.filter(e => e.kind === 'init');
			const starts = entries.filter(e => e.kind === 'start' && e.argv.includes('-p'));
			log(`CLI after rewind: ${JSON.stringify(entries.filter(e => ['start', 'resume_refused', 'init'].includes(e.kind)).map(({ kind, pid, cwd, session_id, resumed, rows, history, argv }) => ({ kind, pid, cwd, session_id, resumed, rows, history, resume: argv?.includes('--resume') ? argv[argv.indexOf('--resume') + 1] : undefined })))}`);
			expect(!entries.some(e => e.kind === 'resume_refused'), 'CLI must find reseeded transcript in its actual cwd');
			expect(init.length === 1 && init[0].resumed && init[0].rows === (action === 'fork' ? 4 : 2),
				`CLI resumes retained user + assistant, not fresh rows=0: ${JSON.stringify(init)}`);
			expect(starts.length === 1 && starts[0].cwd === `${status.workspace_dir}${action === 'fork' ? '' : '/chat-uploads'}`, 'CLI uses this row’s actual runtime cwd');
			expect(init[0].history?.some(row => row.text.includes(retained)), 'CLI actually loaded retained prompt');
			expect(init[0].history?.some(row => row.text.includes(removed)) === (action === 'fork'), 'fork retains the second prompt; rewind removes it');
			expect((await chatState(page)).streaming === 'false', 'reply settled');
		}
	}))
});
