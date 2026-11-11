// The Dev other project's repository, staged with plain git for the journeys
// that drive its Git panel (git-sync, send-order). `restoreRepo` puts `main`
// (local and origin) back where the seed left it (tag
// `journey/git-sync-base`).
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, unlinkSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const PROJECT_ID = 'dev-other';
export const PROJECT_NAME = 'Dev other';
const BASE_TAG = 'journey/git-sync-base';

const GIT_ENV = {
	...process.env,
	GIT_AUTHOR_NAME: 'Journey', GIT_AUTHOR_EMAIL: 'journey@localhost',
	GIT_COMMITTER_NAME: 'Journey', GIT_COMMITTER_EMAIL: 'journey@localhost',
	GIT_CONFIG_NOSYSTEM: '1', GIT_TERMINAL_PROMPT: '0'
};

export function git(cwd, ...args) {
	return execFileSync('git', ['-c', 'commit.gpgsign=false', ...args], { cwd, env: GIT_ENV, encoding: 'utf8', timeout: 30_000 }).trim();
}

export function repoPaths(status) {
	const repo = status.seed?.projects?.[PROJECT_ID];
	const remoteUrl = status.seed?.remotes?.[PROJECT_ID];
	if (!repo || !remoteUrl) throw new Error(`the dev instance seeded no ${PROJECT_ID} repository with a remote (seed: ${JSON.stringify(status.seed)})`);
	return { repo, remote: fileURLToPath(remoteUrl) };
}

/** Put `main` (local and origin) back where the seed left it. */
export function restoreRepo(status) {
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
export function commitUpstream(status, file, content, message) {
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

export function commitLocal(status, file, content, message) {
	const { repo } = repoPaths(status);
	writeFileSync(join(repo, file), content);
	git(repo, 'add', file);
	git(repo, 'commit', '-q', '-m', message);
}

/** Both sides changed README.md: a pull stops on the conflict. */
export function stageConflict(status) {
	commitUpstream(status, 'README.md', '# Dev other\n\nUpstream rewrote this line.\n', 'Upstream README change');
	commitLocal(status, 'README.md', '# Dev other\n\nThis branch rewrote this line.\n', 'Local README change');
	git(repoPaths(status).repo, 'fetch', '-q', 'origin');
}
