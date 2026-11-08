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
//   push-rejected  the remote refuses the push: the panel shows why and offers
//                  "Pull first"
//
//   node tests/journeys/git-sync.journey.mjs [--case=conflict@phone-slow] [--list]
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, unlinkSync, writeFileSync, chmodSync, existsSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { runJourney } from './runner.mjs';
import { expect, openNewChat, scriptTag, typeAndSend, uniq, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const PROJECT_ID = 'dev-other';
const PROJECT_NAME = 'Dev other';
const BASE_TAG = 'journey/git-sync-base';

const GIT_ENV = {
	...process.env,
	GIT_AUTHOR_NAME: 'Journey', GIT_AUTHOR_EMAIL: 'journey@localhost',
	GIT_COMMITTER_NAME: 'Journey', GIT_COMMITTER_EMAIL: 'journey@localhost',
	GIT_CONFIG_NOSYSTEM: '1', GIT_TERMINAL_PROMPT: '0'
};

function git(cwd, ...args) {
	return execFileSync('git', ['-c', 'commit.gpgsign=false', ...args], { cwd, env: GIT_ENV, encoding: 'utf8', timeout: 30_000 }).trim();
}

function repoPaths(status) {
	const repo = status.seed?.projects?.[PROJECT_ID];
	const remoteUrl = status.seed?.remotes?.[PROJECT_ID];
	if (!repo || !remoteUrl) throw new Error(`the dev instance seeded no ${PROJECT_ID} repository with a remote (seed: ${JSON.stringify(status.seed)})`);
	return { repo, remote: fileURLToPath(remoteUrl) };
}

/** Put `main` (local and origin) back where the seed left it. */
function restoreRepo(status) {
	const { repo, remote } = repoPaths(status);
	const hook = join(remote, 'hooks', 'pre-receive');
	if (existsSync(hook)) unlinkSync(hook);
	let base;
	try { base = git(repo, 'rev-parse', '--verify', `refs/tags/${BASE_TAG}`); } catch {
		// First run on this instance: the seed's main is the baseline.
		git(repo, 'tag', BASE_TAG, 'origin/main');
		base = git(repo, 'rev-parse', `refs/tags/${BASE_TAG}`);
	}
	for (const marker of ['rebase-merge', 'rebase-apply']) {
		if (existsSync(join(repo, '.git', marker))) git(repo, 'rebase', '--abort');
	}
	if (existsSync(join(repo, '.git', 'MERGE_HEAD'))) git(repo, 'merge', '--abort');
	// The app's own pull rebases/merges as the server process, which has no git
	// identity on a dev instance; a real checkout has one.
	git(repo, 'config', 'user.name', 'Journey');
	git(repo, 'config', 'user.email', 'journey@localhost');
	git(repo, 'checkout', '-q', '-f', 'main');
	git(repo, 'reset', '-q', '--hard', base);
	git(repo, 'clean', '-qfd');
	git(repo, 'stash', 'clear');
	git(repo, 'push', '-q', '-f', 'origin', `${base}:refs/heads/main`);
	git(repo, 'fetch', '-q', 'origin');
	return base;
}

/** A commit on origin/main made from another clone (the "someone else pushed" side). */
function commitUpstream(status, file, content, message) {
	const { remote } = repoPaths(status);
	const dir = mkdtempSync(join(tmpdir(), 'journey-git-sync-'));
	try {
		git(dir, 'clone', '-q', remote, 'clone');
		const clone = join(dir, 'clone');
		writeFileSync(join(clone, file), content);
		git(clone, 'add', file);
		git(clone, 'commit', '-q', '-m', message);
		git(clone, 'push', '-q', 'origin', 'HEAD:main');
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
}

function commitLocal(status, file, content, message) {
	const { repo } = repoPaths(status);
	writeFileSync(join(repo, file), content);
	git(repo, 'add', file);
	git(repo, 'commit', '-q', '-m', message);
}

const STAGES = {
	conflict(status) {
		commitUpstream(status, 'README.md', '# Dev other\n\nUpstream rewrote this line.\n', 'Upstream README change');
		commitLocal(status, 'README.md', '# Dev other\n\nThis branch rewrote this line.\n', 'Local README change');
		git(repoPaths(status).repo, 'fetch', '-q', 'origin');
	},
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
	'push-rejected'(status) {
		commitLocal(status, 'LOCAL.md', 'from this branch\n', 'Local adds a file');
		const hook = join(repoPaths(status).remote, 'hooks', 'pre-receive');
		writeFileSync(hook, '#!/bin/sh\necho "! [rejected] main -> main (fetch first)" >&2\nexit 1\n');
		chmodSync(hook, 0o755);
	}
};

/** Fresh chat in Dev other with the repository staged; leaves the Git panel open. */
function setupFor(stage) {
	return async (ctx) => {
		const { page, status, variant } = ctx;
		restoreRepo(status);
		STAGES[stage](status);
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
			act: restoring(async ({ page, mark, panel, row }) => {
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
			})
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
