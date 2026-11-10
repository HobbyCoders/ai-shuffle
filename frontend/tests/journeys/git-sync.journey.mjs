// Journey: every Git panel action says what happened, in the panel.
//
// Owner report this guards: "Sync for out-of-sync repos just doesn't work,
// clicking it does nothing". The pull did run: the server stopped it on
// conflicts and answered, but the answer went only to the notification bell,
// so the panel showed nothing at all.
//
// Each case diverges the Dev other project's `main` from its local bare
// `origin` with plain git, opens a fresh chat in that project, opens the Git
// panel and presses the action. The repository goes back to the seed's state
// after every case (tag `journey/git-sync-base`).
//
// Cases:
//   conflict       both sides changed README.md: Sync stops, the panel names
//                  the file, says nothing was changed and offers "Resolve with
//                  agent", which sends this chat a merge request
//   clean          both sides changed different files: Sync integrates them,
//                  the panel says so and keeps saying so (no collapse that
//                  jumps the phone sheet) until the panel is closed
//   dirty          diverged with an uncommitted edit: Sync is refused, the
//                  panel offers "Stash & pull", which then succeeds
//   push-rejected  the remote refuses the push: the panel shows why, the bell
//                  keeps a history entry for it, and "Pull first" then brings
//                  in the upstream commit
//
//   node tests/journeys/git-sync.journey.mjs [--case=conflict@phone-slow] [--list]
import { writeFileSync, chmodSync, readFileSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
import { runJourney } from './runner.mjs';
import { api, expect, openNewChat, scriptTag, typeAndSend, uniq, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { PROJECT_NAME, commitLocal, commitUpstream, git, repoPaths, restoreRepo, stageConflict } from './lib/git-repo.mjs';

const STAGES = {
	conflict: stageConflict,
	clean(status) {
		commitUpstream(status, 'UPSTREAM.md', 'from upstream\n', 'Upstream adds a file');
		commitLocal(status, 'LOCAL.md', 'from this branch\n', 'Local adds a file');
		git(repoPaths(status).repo, 'fetch', '-q', 'origin');
	},
	dirty(status) {
		STAGES.clean(status);
		const { repo } = repoPaths(status);
		writeFileSync(join(repo, 'README.md'), readFileSync(join(repo, 'README.md'), 'utf8') + '\nAn uncommitted note.\n');
	},
	'push-rejected'(status, ctx) {
		// Ahead only: the app fetches on its own and disables Push once it knows
		// the branch is behind, so the upstream commit lands later (in the act).
		commitLocal(status, 'LOCAL.md', 'from this branch\n', 'Local adds a file');
		// A token in the refusal finds this run's entry in the bell's history.
		ctx.rejectToken = uniq();
		const hook = join(repoPaths(status).remote, 'hooks', 'pre-receive');
		writeFileSync(hook, `#!/bin/sh\necho "! [rejected] main -> main (fetch first) ${ctx.rejectToken}" >&2\nexit 1\n`);
		chmodSync(hook, 0o755);
	}
};

/** Fresh chat in Dev other with the repository staged; leaves the Git panel open. */
function setupFor(stage) {
	return async (ctx) => {
		const { page, status, variant } = ctx;
		restoreRepo(status);
		STAGES[stage](status, ctx);
		ctx.headBefore = git(repoPaths(status).repo, 'rev-parse', 'HEAD');
		await openNewChat(ctx, { project: PROJECT_NAME });
		await typeAndSend(page, `${scriptTag(variant, 'default')} git sync ${uniq()}`);
		await waitTurnSettled(page, { messagesAtLeast: 2, hold: 800 });
		await page.getByRole('button', { name: /^Git actions/ }).click();
		const panel = page.getByRole('dialog', { name: 'Git actions' });
		await panel.waitFor({ timeout: 15_000 });
		ctx.panel = panel;
		const row = stage === 'push-rejected'
			? panel.getByRole('button', { name: 'Push', exact: true })
			: panel.getByRole('button', { name: 'Sync', exact: true });
		// The checkout's state arrives with the git poll; wait for the row to be live.
		await row.and(page.locator(':not([disabled])')).waitFor({ timeout: 30_000 });
		await page.waitForTimeout(800);
		ctx.row = row;
	};
}

/** Wrap a case's act so the repository is restored even when it fails. */
function restoring(act) {
	return async (ctx) => {
		try {
			await act(ctx);
		} finally {
			try { restoreRepo(ctx.status); } catch (error) { ctx.log(`restore failed: ${error.message}`); }
		}
	};
}

/** The outcome surface inside the Git panel (role=status, see GitProgressPopover). */
const outcome = (panel) => panel.getByRole('status');

/** A success stays put while the panel is open, and closing the panel clears it. */
async function successHoldsUntilClose({ page, mark, panel }) {
	await page.waitForTimeout(3500); // past the old 2.5s collapse that jumped the phone sheet
	expect(await outcome(panel).getByText(/Pulled from origin\/main/i).isVisible(), 'the success is still shown while the panel is open');
	await mark('close the Git panel');
	await page.keyboard.press('Escape');
	await panel.waitFor({ state: 'hidden', timeout: 5000 });
	await page.getByRole('button', { name: /^Git actions/ }).click();
	await panel.waitFor({ timeout: 5000 });
	expect(await outcome(panel).count() === 0, 'reopening the panel starts clean');
}

await runJourney({
	name: 'git-sync',
	cases: [
		{
			name: 'conflict',
			setup: setupFor('conflict'),
			act: restoring(async ({ page, mark, panel, row, status, headBefore, log }) => {
				await mark('press Sync');
				await row.click();
				const surface = outcome(panel);
				try {
					await surface.getByText(/Nothing was changed/i).waitFor({ timeout: 20_000 });
				} catch {
					throw new Error('pressing Sync showed nothing in the Git panel: the conflict result never appeared (owner report: "clicking it does nothing")');
				}
				await surface.getByText('README.md', { exact: true }).waitFor({ timeout: 5000 });
				const resolve = surface.getByRole('button', { name: 'Resolve with agent' });
				await resolve.waitFor({ timeout: 5000 });
				expect(await resolve.isEnabled(), '"Resolve with agent" is enabled in an idle chat');
				expect(git(repoPaths(status).repo, 'rev-parse', 'HEAD') === headBefore, 'the stopped pull left the branch where it was');
				await page.waitForTimeout(800);

				await mark('press Resolve with agent');
				await resolve.click();
				await page.getByText(/resolve the merge conflicts/i).first().waitFor({ timeout: 15_000 });
				await waitTurnSettled(page, { messagesAtLeast: 4, hold: 1200 });
				const sent = await page.getByText(/origin\/main/).count();
				log(`messages naming origin/main on screen: ${sent}`);
				expect(sent > 0, 'the request names the upstream it should merge');
			})
		},
		{
			name: 'clean',
			setup: setupFor('clean'),
			act: restoring(async (ctx) => {
				const { mark, panel, row, status, headBefore } = ctx;
				await mark('press Sync');
				await row.click();
				try {
					await outcome(panel).getByText(/Pulled from origin\/main/i).waitFor({ timeout: 20_000 });
				} catch {
					throw new Error('a clean sync showed no result in the Git panel');
				}
				const { repo } = repoPaths(status);
				await successHoldsUntilClose(ctx);
				expect(git(repo, 'rev-parse', 'HEAD') !== headBefore, 'the branch moved');
				expect(git(repo, 'rev-list', '--count', 'HEAD..origin/main') === '0', 'the branch now contains origin/main');
			})
		},
		{
			name: 'dirty',
			setup: setupFor('dirty'),
			allow: [{ kind: 'failed_requests', status: 409, url: /\/git\/pull(\?|$)/, reason: 'the case leaves an uncommitted edit so the pull is refused on purpose' }],
			act: restoring(async ({ page, mark, panel, row, status }) => {
				await mark('press Sync');
				await row.click();
				const surface = outcome(panel);
				const stash = surface.getByRole('button', { name: 'Stash & pull' });
				try {
					await stash.waitFor({ timeout: 20_000 });
				} catch {
					throw new Error('a pull refused for uncommitted changes showed no "Stash & pull" in the Git panel');
				}
				await surface.getByText(/uncommitted changes/i).waitFor({ timeout: 5000 });
				await page.waitForTimeout(800);

				await mark('press Stash & pull');
				await stash.click();
				await outcome(panel).getByText(/Pulled from origin\/main/i).waitFor({ timeout: 20_000 });
				await successHoldsUntilClose({ page, mark, panel });
				const { repo } = repoPaths(status);
				expect(git(repo, 'rev-list', '--count', 'HEAD..origin/main') === '0', 'the branch now contains origin/main');
				expect(readFileSync(join(repo, 'README.md'), 'utf8').includes('An uncommitted note.'), 'the uncommitted edit came back after the pull');
			})
		},
		{
			name: 'push-rejected',
			setup: setupFor('push-rejected'),
			allow: [{ kind: 'failed_requests', status: 400, url: /\/git\/push(\?|$)/, reason: 'the case installs a remote hook that refuses the push on purpose' }],
			act: restoring(async ({ page, mark, panel, row, status, rejectToken }) => {
				await mark('press Push');
				await row.click();
				const surface = outcome(panel);
				try {
					await surface.getByRole('button', { name: 'Pull first' }).waitFor({ timeout: 20_000 });
				} catch {
					throw new Error('a rejected push showed no reason and no "Pull first" in the Git panel');
				}
				await surface.getByText(/rejected/i).first().waitFor({ timeout: 5000 });
				await page.waitForTimeout(1500);
				expect(await panel.getByRole('button', { name: 'Pull first' }).isVisible(), 'the rejection stays on screen until dismissed');
				// The panel shows the outcome; the bell still keeps it in history
				// (the next Git action replaces the panel's outcome).
				const history = await api(page, 'GET', '/notifications?limit=50');
				expect(history.notifications.some((n) => String(n.message ?? '').includes(rejectToken)), 'the bell keeps a history entry for the rejected push');

				// Someone else's push is what the remote was guarding; let it in so
				// "Pull first" has something to bring down.
				unlinkSync(join(repoPaths(status).remote, 'hooks', 'pre-receive'));
				commitUpstream(status, 'UPSTREAM.md', 'from upstream\n', 'Upstream adds a file');

				await mark('press Pull first');
				await surface.getByRole('button', { name: 'Pull first' }).click();
				try {
					await outcome(panel).getByText(/Pulled from origin\/main/i).waitFor({ timeout: 20_000 });
				} catch {
					throw new Error('"Pull first" did not report a pull in the Git panel');
				}
				expect(git(repoPaths(status).repo, 'rev-list', '--count', 'HEAD..origin/main') === '0', '"Pull first" brought in origin/main');
			})
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
