/**
 * Behavior tests for GitHeaderActions — the chat header's one git button and
 * the panel it opens.
 *
 * These mount the real header with its git/github stores mocked (but the real
 * header-dropdowns and gitActionPreferences singletons) so we can drive the
 * panel and its option popovers end-to-end. Covered:
 *  - the header button's label follows the priority ladder (approvals waiting,
 *    conflicts, behind, uncommitted changes, to push, open PR, up to date)
 *  - the row-visibility matrix inside the panel (Commit / Create PR / Ship It /
 *    Push / Pull enable for the right repo state)
 *  - the panel opens from the button; Escape and outside-click close it; a
 *    row's chevron opens its popover; opening one popover closes another
 *    (shared single-open store) and closing a popover returns to the panel,
 *    with focus back on its chevron (or the first overview control, then Close)
 *  - persisted options (include-unstaged, next-step, pull strategy) write
 *    through gitActionPreferences; a per-invocation option (force push) does NOT persist
 *  - a row fires its handler with defaults (no option bag) and closes the
 *    panel; the popover primary fires with an explicit bag
 *  - everything disables under actionsBusy (isStreaming)
 *  - the private-access row reads chatAccessSummary and opens the dialog
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, within } from '@testing-library/svelte';
import { flushSync, tick } from 'svelte';

// jsdom lacks ResizeObserver; some lucide/layout paths probe for it.
if (!('ResizeObserver' in globalThis)) {
	(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
		observe() {}
		unobserve() {}
		disconnect() {}
	};
}

// A tiny Svelte-store-compatible container so `$store` auto-subscription works
// without pulling in the real git store graph (which polls / hits the network).
const mocks = vi.hoisted(() => {
	function store<T>(initial: T) {
		let val = initial;
		const subs = new Set<(v: T) => void>();
		return {
			subscribe(fn: (v: T) => void) {
				fn(val);
				subs.add(fn);
				return () => subs.delete(fn);
			},
			set(v: T) {
				val = v;
				subs.forEach((f) => f(val));
			},
			get value() {
				return val;
			}
		};
	}
	return {
		gitSnapshot: store<Record<string, unknown>>({
			contextKey: null,
			status: null,
			branches: [],
			diffStats: null,
			prState: null,
			changedFiles: null,
			currentBranch: null,
			defaultBranch: null,
			openPr: null,
			prStateStale: false,
			stale: false,
			hydrated: true,
			githubAvailable: true
		}),
		gitMode: store<string>('project'),
		mobileShell: false,
		githubRepoInfo: store<Record<string, unknown> | null>({ default_branch: 'main' }),
		loadBranches: vi.fn(),
		refreshDiffStats: vi.fn(),
		refreshChangedFiles: vi.fn(),
		retryLoad: vi.fn(),
		startBackgroundRefresh: vi.fn(),
		stopBackgroundRefresh: vi.fn(),
		gitOperationState: store({ active: false, status: 'idle', title: null as string | null, steps: [] as Record<string, unknown>[] })
	};
});

vi.mock('$lib/utils/platform', () => ({ buildShell: () => mocks.mobileShell ? 'mobile' : 'desktop' }));

vi.mock('$lib/stores/git.svelte', () => ({
	git: {
		loadBranches: mocks.loadBranches,
		refreshDiffStats: mocks.refreshDiffStats,
		refreshChangedFiles: mocks.refreshChangedFiles,
		retryLoad: mocks.retryLoad,
		startBackgroundRefresh: mocks.startBackgroundRefresh,
		stopBackgroundRefresh: mocks.stopBackgroundRefresh
	},
	gitSnapshot: mocks.gitSnapshot,
	gitMode: mocks.gitMode
}));
vi.mock('$lib/stores/gitOperations.svelte', () => ({
 gitOperationState: mocks.gitOperationState,
 gitOperations: { dismiss: () => mocks.gitOperationState.set({ active: false, status: 'idle', title: null, steps: [] }) }
}));
vi.mock('$lib/stores/github.svelte', () => ({
	githubRepoInfo: mocks.githubRepoInfo
}));

/**
 * Publish ONE coherent snapshot. The header reads every action input from this
 * single object — that is the whole point of finding G4's fix — so tests drive
 * it the same way the store does.
 */
function setSnapshot(patch: Record<string, unknown>) {
	const status = (patch.status ?? null) as Record<string, unknown> | null;
	mocks.gitSnapshot.set({
		contextKey: 'p1#br:x',
		branches: [],
		diffStats: null,
		prState: null,
		changedFiles: null,
		prStateStale: false,
		stale: false,
		hydrated: true,
		githubAvailable: true,
		openPr: null,
		...patch,
		status,
		currentBranch: (status?.current_branch as string | null) ?? null,
		defaultBranch: (status?.default_branch as string | null) ?? null
	});
}

// Imported AFTER the mocks are registered.
import GitHeaderActions from './GitHeaderActions.svelte';
import { gitActionPreferences } from '$lib/stores/gitActionPreferences.svelte';
import { openHeaderDropdown } from '$lib/stores/header-dropdowns';
import { chatAccessSummary } from '$lib/stores/chat-access';

// ── Scenario setup ──────────────────────────────────────────────────────────

/** Dirty tree on a feature branch → Commit + Ship It enable. */
function dirtyFeatureBranch() {
	setSnapshot({
		status: {
			current_branch: 'feature/x',
			default_branch: 'main',
			ahead: 0,
			differs_from_base: true,
			is_git_repo: true
		},
		diffStats: { additions: 5, deletions: 2, files_changed: 1 },
		changedFiles: [{ file: 'a.ts', status: 'M', category: 'modified', additions: 5, deletions: 2, is_binary: false }],
		branches: [
			{ name: 'feature/x', current: true, remote: false, upstream: 'origin/feature/x', ahead: 0, behind: 0 },
			{ name: 'main', current: false, remote: false }
		]
	});
}

/** Clean tree, commits ahead of upstream, not behind → Push + Create PR + Ship It. */
function cleanAheadBranch() {
	setSnapshot({
		status: {
			current_branch: 'feature/x',
			default_branch: 'main',
			ahead: 2,
			differs_from_base: true,
			is_git_repo: true
		},
		diffStats: { additions: 0, deletions: 0, files_changed: 0 },
		changedFiles: [],
		branches: [
			{ name: 'feature/x', current: true, remote: false, upstream: 'origin/feature/x', ahead: 2, behind: 0 },
			{ name: 'main', current: false, remote: false }
		]
	});
}

/** Behind upstream → Pull enables. */
function behindBranch() {
	setSnapshot({
		status: {
			current_branch: 'feature/x',
			default_branch: 'main',
			ahead: 0,
			differs_from_base: true,
			is_git_repo: true
		},
		diffStats: { additions: 0, deletions: 0, files_changed: 0 },
		branches: [
			{ name: 'feature/x', current: true, remote: false, upstream: 'origin/feature/x', ahead: 0, behind: 3 }
		]
	});
}

function baseHandlers() {
	return {
		onClickCommit: vi.fn(),
		onClickCreatePR: vi.fn(),
		onClickPush: vi.fn(),
		onClickPull: vi.fn(),
		onClickShipIt: vi.fn(),
		onClickInitRepo: vi.fn(),
		onOpenDiffReview: vi.fn()
	};
}

function renderHeader(props: Record<string, unknown> = {}, handlers = baseHandlers()) {
	const result = render(GitHeaderActions, {
		props: {
			isGitRepo: true,
			...handlers,
			...props
		}
	});
	return { ...result, handlers };
}

/** The one header button. Its accessible name starts with "Git actions:". */
const trigger = () => screen.getByRole('button', { name: /^Git actions:/ }) as HTMLButtonElement;
const panel = () => screen.getByRole('dialog', { name: 'Git actions' });
const queryPanel = () => screen.queryByRole('dialog', { name: 'Git actions' });
/** An action's option popover (GitActionOptionsDropdown labels itself "<action> options"). */
const popover = (action: string) => screen.getByRole('region', { name: `${action} options` });
const queryPopover = (action: string) => screen.queryByRole('region', { name: `${action} options` });
const row = (name: string) => within(panel()).getByRole('button', { name }) as HTMLButtonElement;
const queryRow = (name: string) => within(panel()).queryByRole('button', { name }) as HTMLButtonElement | null;

async function openPanel() {
	await fireEvent.click(trigger());
	return panel();
}

/** Wait one animation frame — the popover registers its outside-click listener
 *  on rAF so the opening click can't immediately close it. */
const nextFrame = () => new Promise((r) => requestAnimationFrame(() => r(null)));

beforeEach(() => {
	localStorage.clear();
	gitActionPreferences.reset();
	mocks.gitOperationState.set({ active: false, status: 'idle', title: null, steps: [] });
	openHeaderDropdown.set(null);
	chatAccessSummary.set(null);
	mocks.gitMode.set('project');
	mocks.mobileShell = false;
	vi.clearAllMocks();
});

afterEach(() => {
	cleanup();
	openHeaderDropdown.set(null);
	chatAccessSummary.set(null);
});

// ── Header button label (priority ladder) ───────────────────────────────────

describe('GitHeaderActions header label', () => {
	it('shows the diff numbers for uncommitted changes', () => {
		dirtyFeatureBranch();
		renderHeader();
		const btn = trigger();
		expect(btn.textContent).toContain('+5');
		expect(btn.textContent).toContain('−2');
		expect(btn.textContent).toContain('1 file');
		expect(btn.title).toBe('1 file changed');
	});

	it('shows commits to push for a clean branch ahead of upstream', () => {
		cleanAheadBranch();
		renderHeader();
		expect(trigger().textContent).toContain('2 to push');
	});

	it('shows "behind" over uncommitted changes', () => {
		setSnapshot({
			status: { current_branch: 'feature/x', default_branch: 'main', ahead: 0, differs_from_base: true, is_git_repo: true },
			diffStats: { additions: 5, deletions: 2, files_changed: 1 },
			branches: [{ name: 'feature/x', current: true, remote: false, upstream: 'origin/feature/x', ahead: 0, behind: 3 }]
		});
		renderHeader();
		expect(trigger().textContent).toContain('3 behind');
		expect(trigger().textContent).not.toContain('+5');
	});

	it('shows the PR and "conflicts" when the open PR is not mergeable', () => {
		cleanAheadBranch();
		renderHeader({ openPR: { number: 42, title: 'x', url: 'http://x', mergeable: false } });
		expect(trigger().textContent).toContain('PR #42');
		expect(trigger().textContent).toContain('conflicts');
		expect(trigger().title).toContain('merge conflicts');
	});

	it('shows the open PR when there is nothing else to do', () => {
		setSnapshot({
			status: { is_git_repo: true, current_branch: 'feature/x', default_branch: 'main', ahead: 0, unpushed: 0, differs_from_base: false },
			diffStats: { additions: 0, deletions: 0, files_changed: 0 },
			openPr: { number: 42, title: 'x', url: 'http://x' }
		});
		renderHeader();
		expect(trigger().textContent).toContain('PR #42');
		expect(trigger().textContent).toContain('open');
	});

	it('says "Up to date" on a clean default branch', () => {
		setSnapshot({
			status: { current_branch: 'main', default_branch: 'main', ahead: 0, differs_from_base: false, is_git_repo: true },
			diffStats: { additions: 0, deletions: 0, files_changed: 0 },
			branches: [{ name: 'main', current: true, remote: false, upstream: 'origin/main', ahead: 0, behind: 0 }]
		});
		renderHeader();
		expect(trigger().textContent).toContain('Up to date');
		expect(trigger().title).toBe('Up to date with main');
	});

	it('puts approvals waiting above every git state', () => {
		cleanAheadBranch();
		chatAccessSummary.set({ sessionId: 's1', pending: 2, active: 1, saved: 0, error: false, open: vi.fn() });
		renderHeader({ openPR: { number: 42, title: 'x', url: 'http://x', mergeable: false } });
		expect(trigger().textContent).toContain('2 to approve');
		expect(trigger().textContent).not.toContain('conflicts');
	});
});

// ── Row visibility matrix inside the panel ──────────────────────────────────

describe('GitHeaderActions panel rows', () => {
	it.each(['branch', 'mode'])('does not offer Init Repo for a removed worktree (%s)', (source) => {
		setSnapshot({ status: { is_git_repo: false } });
		if (source === 'mode') mocks.gitMode.set('worktree');
		renderHeader({ isGitRepo: false, worktreeBranch: source === 'branch' ? 'feature/gone' : null });
		expect(screen.queryByText('Init Repo')).toBeNull();
		expect(screen.queryByRole('button', { name: /^Git actions:/ })).toBeNull();
		expect(screen.queryByText('Git unavailable · Retry')).toBeNull();
	});

	it('still offers Init Repo for an ordinary non-repository project', () => {
		setSnapshot({ status: { is_git_repo: false } });
		renderHeader({ isGitRepo: false });
		expect(screen.getByText('Init Repo')).toBeTruthy();
	});

	it('offers a retry when git is unavailable', async () => {
		renderHeader({ error: 'boom' });
		await fireEvent.click(screen.getByText('Git unavailable · Retry'));
		expect(mocks.retryLoad).toHaveBeenCalledOnce();
	});

	it('shows only the merge step for an up-to-date open PR, even with no branch diff', async () => {
		setSnapshot({
			status: { is_git_repo: true, current_branch: 'feature/x', default_branch: 'main',
				ahead: 0, unpushed: 0, differs_from_base: false },
			diffStats: { additions: 0, deletions: 0, files_changed: 0 },
			openPr: { number: 42, title: 'x', url: 'http://x' }
		});
		gitActionPreferences.setCleanupAfterMerge(false);
		const { handlers } = renderHeader();
		await openPanel();
		const button = row('Ship It');
		expect(button.title).toBe('Merge PR #42');
		expect(row('Commit').disabled).toBe(true);
		expect(queryRow('Create PR')).toBeNull();
		await fireEvent.click(button);
		expect(handlers.onClickShipIt).toHaveBeenCalledOnce();
		// Operation feedback stays in this workspace: the panel is still open, so
		// the options chevron is reachable without reopening it.
		expect(panel()).toBeTruthy();
		await fireEvent.click(row('Ship It options'));
		expect(within(popover('shipit')).getAllByRole('listitem').map((e) => e.textContent)).toEqual(['Merge PR #42']);
	});

	it('prevents the Ship It popover from bypassing merge conflicts', async () => {
		cleanAheadBranch();
		const { handlers } = renderHeader({ openPR: { number: 42, title: 'x', url: 'u', mergeable: false } });
		await openPanel();
		await fireEvent.click(row('Ship It options'));
		const submit = popover('shipit').querySelector('button.opt-submit') as HTMLButtonElement;
		expect(submit.disabled).toBe(true);
		await fireEvent.click(submit);
		expect(handlers.onClickShipIt).not.toHaveBeenCalled();
	});

	it('hides merged work and restores actions as the same branch gets new work', async () => {
		const sha = 'a'.repeat(40);
		const snapshot = {
			status: { current_branch: 'feature/x', default_branch: 'main', ahead: 1,
				differs_from_base: true, is_git_repo: true, head_commit: sha.slice(0, 12) },
			prState: { current_branch: 'feature/x', open_pr: null, merged_head_sha: sha },
			diffStats: { additions: 0, deletions: 0, files_changed: 0 }
		};
		setSnapshot(snapshot);
		renderHeader();
		await openPanel();
		expect(row('Create PR').disabled).toBe(true);
		expect(queryRow('Ship It')).toBeNull();

		setSnapshot({ ...snapshot, diffStats: { additions: 1, deletions: 0, files_changed: 1 } });
		await tick();
		expect(row('Commit').disabled).toBe(false);
		expect(row('Ship It')).toBeTruthy();

		setSnapshot({ ...snapshot, status: { ...snapshot.status, head_commit: 'b'.repeat(12) } });
		await tick();
		expect(row('Create PR').disabled).toBe(false);
		expect(row('Ship It')).toBeTruthy();
	});

	it('enables Commit (with chevron) and Ship It for a dirty feature branch', async () => {
		dirtyFeatureBranch();
		renderHeader();
		await openPanel();
		expect(row('Commit').disabled).toBe(false);
		expect(row('Commit options')).toBeTruthy();
		expect(row('Ship It').disabled).toBe(false);
		expect(row('Ship It options')).toBeTruthy();
		expect(row('Push').disabled).toBe(true);
	});

	it('lists the changed files and opens the review from them', async () => {
		dirtyFeatureBranch();
		const { handlers } = renderHeader();
		await openPanel();
		await fireEvent.click(within(panel()).getByTitle('a.ts'));
		expect(handlers.onOpenDiffReview).toHaveBeenCalledOnce();
		expect(queryPanel()).toBeNull();
	});

	describe('githubAvailable gate (G10c)', () => {
		// The header itself no longer requires GitHub auth — local git has to keep
		// working when `gh` is down. That means the gh-BACKED actions now need
		// their own gate, or they would render as rows guaranteed to fail.
		it('disables Create PR and hides Ship It when GitHub is unavailable', async () => {
			cleanAheadBranch();
			renderHeader({ githubAvailable: false });
			await openPanel();
			expect(row('Create PR').disabled).toBe(true);
			expect(queryRow('Ship It')).toBeNull();
			expect(within(panel()).getByText('Needs GitHub')).toBeTruthy();
		});

		it('still offers the LOCAL actions when GitHub is unavailable', async () => {
			cleanAheadBranch();
			renderHeader({ githubAvailable: false });
			await openPanel();
			// Push is plain git — losing gh must not take it away.
			expect(row('Push').disabled).toBe(false);
		});

		it('hides Ship It (PR merge) when GitHub is unavailable', async () => {
			cleanAheadBranch();
			renderHeader({
				githubAvailable: false,
				openPR: { number: 12, title: 'A PR', url: 'u', mergeable: true }
			});
			await openPanel();
			expect(queryRow('Ship It')).toBeNull();
		});

		it('defaults to available so existing call sites are unchanged', async () => {
			cleanAheadBranch();
			renderHeader();
			await openPanel();
			expect(row('Create PR').disabled).toBe(false);
		});
	});

	it('enables Push and Create PR for a clean branch ahead of upstream', async () => {
		cleanAheadBranch();
		renderHeader();
		await openPanel();
		expect(row('Push').disabled).toBe(false);
		expect(row('Push options')).toBeTruthy();
		expect(row('Create PR').disabled).toBe(false);
		expect(row('Pull request options')).toBeTruthy();
	});

	it('enables Ship It (merge) when an open PR is passed', async () => {
		cleanAheadBranch();
		renderHeader({ openPR: { number: 42, title: 'x', url: 'http://x' } });
		await openPanel();
		expect(row('Ship It').disabled).toBe(false);
		expect(row('Ship It options')).toBeTruthy();
	});

	it('enables Pull when the branch is behind upstream', async () => {
		behindBranch();
		renderHeader();
		await openPanel();
		expect(row('Pull').disabled).toBe(false);
		expect(row('Pull options')).toBeTruthy();
	});

	it('disables Ship It when the open PR is explicitly not mergeable', async () => {
		cleanAheadBranch();
		renderHeader({
			openPR: { number: 42, title: 'x', url: 'http://x', mergeable: false }
		});
		await openPanel();
		const shipBtn = row('Ship It');
		expect(shipBtn.disabled).toBe(true);
		expect(shipBtn.title).toContain('merge conflicts');
		expect(shipBtn.textContent).toContain('Fix conflicts first');
	});

	it('keeps Ship It enabled when mergeability is unknown (null)', async () => {
		cleanAheadBranch();
		renderHeader({
			openPR: { number: 42, title: 'x', url: 'http://x', mergeable: null }
		});
		await openPanel();
		expect(row('Ship It').disabled).toBe(false);
	});

	it('links the open PR from the panel', async () => {
		cleanAheadBranch();
		renderHeader({ openPR: { number: 42, title: 'My PR', url: 'http://x/pr/42' } });
		await openPanel();
		const link = within(panel()).getByRole('link', { name: 'Open PR #42 on GitHub' }) as HTMLAnchorElement;
		expect(link.href).toBe('http://x/pr/42');
	});

	it('enables Create PR when differsFromBase even with zero commits ahead', async () => {
		// The matrix gap the helper now closes: content differs from base but
		// commit count ahead (vs default branch) is 0.
		setSnapshot({
			status: {
				current_branch: 'feature/x',
				default_branch: 'main',
				ahead: 0,
				differs_from_base: true,
				is_git_repo: true
			},
			diffStats: { additions: 0, deletions: 0, files_changed: 0 },
			changedFiles: [],
			branches: [
				{ name: 'feature/x', current: true, remote: false, upstream: 'origin/feature/x', ahead: 0, behind: 0 }
			]
		});
		renderHeader();
		await openPanel();
		expect(row('Create PR').disabled).toBe(false);
	});
});

// ── Default branch comes from /git/status (finding G3) ──────────────────────

describe('GitHeaderActions default branch', () => {
	it('uses status.default_branch, not a hardcoded "main"', async () => {
		// A `master`-default repo sitting ON its default branch: nothing to ship
		// and nothing to PR. The old code fell back to 'main' (the github store
		// that would have supplied the real value was never populated), so
		// `master !== 'main'` and Ship It / Create PR appeared on the default branch.
		setSnapshot({
			status: {
				current_branch: 'master',
				default_branch: 'master',
				ahead: 3,
				differs_from_base: true,
				is_git_repo: true
			},
			diffStats: { additions: 0, deletions: 0, files_changed: 0 },
			branches: [
				{ name: 'master', current: true, remote: false, upstream: 'origin/master', ahead: 0, behind: 0 }
			]
		});
		renderHeader();
		expect(trigger().title).toBe('Up to date with master');
		await openPanel();
		expect(row('Create PR').disabled).toBe(true);
		expect(queryRow('Ship It')).toBeNull();
	});

	it('still offers Create PR on a non-default branch of a master-default repo', async () => {
		setSnapshot({
			status: {
				current_branch: 'feature/x',
				default_branch: 'master',
				ahead: 3,
				differs_from_base: true,
				is_git_repo: true
			},
			diffStats: { additions: 0, deletions: 0, files_changed: 0 },
			branches: [
				{ name: 'feature/x', current: true, remote: false, upstream: 'origin/feature/x', ahead: 0, behind: 0 }
			]
		});
		renderHeader();
		await openPanel();
		expect(row('Create PR').disabled).toBe(false);
	});
});

// ── Snapshot coherence on a session switch (finding G4) ─────────────────────

describe('GitHeaderActions snapshot coherence', () => {
	it('holds the previous action while a switch revalidates — never an intermediate value', async () => {
		// Coherent state: an open PR ⇒ Ship It.
		cleanAheadBranch();
		renderHeader({ openPR: null });
		setSnapshot({
			status: {
				current_branch: 'feature/x',
				default_branch: 'main',
				ahead: 2,
				differs_from_base: true,
				is_git_repo: true
			},
			diffStats: { additions: 0, deletions: 0, files_changed: 0 },
			branches: [
				{ name: 'feature/x', current: true, remote: false, upstream: 'origin/feature/x', ahead: 2, behind: 0 }
			],
			openPr: { number: 12, title: 'x', url: 'http://x', mergeable: null }
		});
		await tick();
		await openPanel();
		expect(row('Ship It')).toBeTruthy();

		// Session switch: the store keeps the last-known values as a placeholder
		// (hydrated: false) while it revalidates. Half of that placeholder — no
		// PR yet — would otherwise resolve to "Create PR" for a beat.
		setSnapshot({
			status: {
				current_branch: 'feature/x',
				default_branch: 'main',
				ahead: 2,
				differs_from_base: true,
				is_git_repo: true
			},
			diffStats: { additions: 0, deletions: 0, files_changed: 0 },
			branches: [
				{ name: 'feature/x', current: true, remote: false, upstream: 'origin/feature/x', ahead: 2, behind: 0 }
			],
			openPr: null,
			stale: true,
			hydrated: false
		});
		await tick();

		// Still Ship It — held from the last coherent snapshot — and disabled so it
		// can't be fired against unconfirmed state.
		expect(row('Ship It').disabled).toBe(true);
		expect(queryRow('Create PR')).toBeNull();
		expect(within(panel()).getByRole('link', { name: 'Open PR #12 on GitHub' })).toBeTruthy();
		// Commits to push outrank the open PR on the header label.
		expect(trigger().textContent).toContain('2 to push');
	});

	it('never renders Ship It against a carried-over placeholder', async () => {
		dirtyFeatureBranch();
		renderHeader();
		await openPanel();
		expect(row('Ship It')).toBeTruthy();

		// Switch to an unknown checkout: values carry over, hydration does not.
		setSnapshot({
			status: {
				current_branch: 'feature/x',
				default_branch: 'main',
				ahead: 0,
				differs_from_base: true,
				is_git_repo: true
			},
			diffStats: { additions: 5, deletions: 2, files_changed: 1 },
			changedFiles: [{ file: 'a.ts', status: 'M', category: 'modified', additions: 5, deletions: 2, is_binary: false }],
			branches: [
				{ name: 'feature/x', current: true, remote: false, upstream: 'origin/feature/x', ahead: 0, behind: 0 }
			],
			stale: true,
			hydrated: false
		});
		await tick();
		// The HELD decision still shows Ship It (it was coherent a moment ago),
		// but the button is disabled until the new context confirms.
		expect(row('Ship It').disabled).toBe(true);
	});
});

// ── Failed PR lookup keeps the PR (findings G2 / G3) ───────────────────────

describe('GitHeaderActions failed PR lookup', () => {
	it('keeps "Ship It" and marks it last-known when the lookup failed', async () => {
		cleanAheadBranch();
		renderHeader({ openPR: null });
		setSnapshot({
			status: {
				current_branch: 'feature/x',
				default_branch: 'main',
				ahead: 2,
				differs_from_base: true,
				is_git_repo: true
			},
			diffStats: { additions: 0, deletions: 0, files_changed: 0 },
			branches: [
				{ name: 'feature/x', current: true, remote: false, upstream: 'origin/feature/x', ahead: 2, behind: 0 }
			],
			// The backend re-served its last known-good PR with pr_lookup_failed +
			// stale; the store surfaces that as prStateStale.
			openPr: { number: 12, title: 'x', url: 'http://x', mergeable: null },
			prStateStale: true
		});
		await tick();

		// …and the user is told the value is unconfirmed, on the header button.
		expect(screen.getByLabelText('Last known PR state — GitHub unreachable')).toBeTruthy();
		expect(trigger().title).toContain('last known state');
		await openPanel();
		// NEVER "Create PR" because a lookup blipped.
		expect(queryRow('Create PR')).toBeNull();
		expect(within(panel()).getByRole('link', { name: 'Open PR #12 on GitHub' })).toBeTruthy();
		expect(row('Ship It')).toBeTruthy();
		expect(row('Ship It').title).toContain('last known state');
	});

	it('shows no last-known marker once the lookup succeeds again', async () => {
		cleanAheadBranch();
		renderHeader({ openPR: { number: 12, title: 'x', url: 'http://x', mergeable: null } });
		await tick();
		expect(screen.queryByLabelText('Last known PR state — GitHub unreachable')).toBeNull();
	});
});

// ── Open / close coordination ───────────────────────────────────────────────

describe('GitHeaderActions panel and popover open/close', () => {
	it('opens the panel from the header button and closes it on Escape', async () => {
		dirtyFeatureBranch();
		renderHeader();
		expect(queryPanel()).toBeNull();
		await openPanel();
		expect(trigger().getAttribute('aria-expanded')).toBe('true');
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(queryPanel()).toBeNull();
	});

	it('closes the panel on an outside click, and the button toggles it', async () => {
		dirtyFeatureBranch();
		renderHeader();
		await openPanel();
		await fireEvent.click(document.body);
		expect(queryPanel()).toBeNull();
		await openPanel();
		await fireEvent.click(trigger());
		expect(queryPanel()).toBeNull();
	});

	it('opens the commit popover on chevron click; Escape returns to the panel', async () => {
		dirtyFeatureBranch();
		renderHeader();
		await openPanel();
		await fireEvent.click(row('Commit options'));
		expect(popover('commit')).toBeTruthy();
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(queryPopover('commit')).toBeNull();
		expect(panel()).toBeTruthy();
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(queryPanel()).toBeNull();
	});

	it('keeps the panel open when a chevron or Back click detaches its own button', async () => {
		// A real click gives Svelte a microtask checkpoint between listeners, so the
		// overview (or editor) is already swapped out when the window listener runs.
		const flush = () => flushSync();
		document.addEventListener('click', flush);
		try {
			dirtyFeatureBranch();
			renderHeader();
			await openPanel();
			await fireEvent.click(row('Commit options'));
			expect(popover('commit')).toBeTruthy();
			await fireEvent.click(within(panel()).getByRole('button', { name: 'Back to Git actions' }));
			expect(queryPopover('commit')).toBeNull();
			expect(panel()).toBeTruthy();
		} finally {
			document.removeEventListener('click', flush);
		}
	});

	it('closes the popover AND the panel on a click outside both', async () => {
		dirtyFeatureBranch();
		renderHeader();
		await openPanel();
		await fireEvent.click(row('Commit options'));
		expect(popover('commit')).toBeTruthy();
		await nextFrame();
		await fireEvent.click(document.body);
		expect(queryPopover('commit')).toBeNull();
		expect(queryPanel()).toBeNull();
	});

	it('keeps the editor open on a click inside the panel; Back returns to overview', async () => {
		dirtyFeatureBranch();
		renderHeader();
		await openPanel();
		await fireEvent.click(row('Commit options'));
		await nextFrame();
		await fireEvent.click(within(panel()).getByText('1 file changed'));
		expect(popover('commit')).toBeTruthy();
		expect(screen.getAllByRole('dialog')).toHaveLength(1);
		await fireEvent.click(screen.getByRole('button', { name: 'Back to Git actions' }));
		expect(queryPopover('commit')).toBeNull();
		expect(panel()).toBeTruthy();
	});

	it('opening the Ship It popover closes the Commit popover (single-open)', async () => {
		dirtyFeatureBranch();
		renderHeader();
		await openPanel();
		await fireEvent.click(row('Commit options'));
		expect(within(popover('commit')).getByText('Commit changes')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Back to Git actions' }));
		await fireEvent.click(row('Ship It options'));
		expect(queryPopover('commit')).toBeNull();
		// The Ship It popover's step preview row "Push to remote" is unique to it.
		expect(within(popover('shipit')).getByText('Push to remote')).toBeTruthy();
	});

	describe('focus on leaving an editor', () => {
		const back = () => screen.getByRole('button', { name: 'Back to Git actions' });

		it.each([
			['Back', 'Commit options'],
			['Back', 'Ship It options'],
			['Escape', 'Commit options'],
			['Escape', 'Ship It options']
		] as const)('%s from the %s editor returns focus to its chevron', async (how, chevron) => {
			dirtyFeatureBranch();
			renderHeader();
			await openPanel();
			await fireEvent.click(row(chevron));
			// Opening still focuses the text field (Commit) or Back (Ship It).
			expect(document.activeElement).toBe(chevron === 'Commit options' ? within(panel()).getByRole('textbox') : back());
			if (how === 'Back') await fireEvent.click(back());
			else await fireEvent.keyDown(window, { key: 'Escape' });
			await vi.waitFor(() => expect(document.activeElement).toBe(row(chevron)));
			expect(panel()).toBeTruthy();
		});

		it('falls back to the first overview control when the chevron is gone', async () => {
			dirtyFeatureBranch();
			renderHeader();
			await openPanel();
			await fireEvent.click(row('Commit options'));
			// The tree goes clean while the editor is open: the Commit row loses its chevron.
			cleanAheadBranch();
			await tick();
			await fireEvent.click(back());
			expect(queryRow('Commit options')).toBeNull();
			await vi.waitFor(() => expect(document.activeElement).toBe(row('Ship It')));
		});

		it('skips a disabled chevron and lands on the first enabled overview control', async () => {
			dirtyFeatureBranch();
			const { rerender } = renderHeader();
			await openPanel();
			await fireEvent.click(row('Commit options'));
			await rerender({ isStreaming: true });
			await fireEvent.keyDown(window, { key: 'Escape' });
			expect(row('Commit options').disabled).toBe(true);
			await vi.waitFor(() => expect(document.activeElement?.closest('.git-panel')).toBe(panel()));
			// The chevron is disabled while streaming; Review all is the first enabled overview control.
			expect(document.activeElement).toBe(within(panel()).getByRole('button', { name: 'Review all' }));
		});

		it('falls back to the Close button when no overview control is enabled', async () => {
			dirtyFeatureBranch();
			const { rerender } = renderHeader();
			await openPanel();
			await fireEvent.click(row('Commit options'));
			// Streaming disables every action, and without a review handler the
			// file list is inert, so the overview has nothing focusable left.
			await rerender({ isStreaming: true, onOpenDiffReview: undefined });
			await fireEvent.click(back());
			await vi.waitFor(() => expect(document.activeElement).toBe(within(panel()).getByRole('button', { name: 'Close Git panel' })));
		});
	});
});

	describe('single workspace', () => {
		it.each([
			['commit', dirtyFeatureBranch, 'Commit options'],
			['push', cleanAheadBranch, 'Push options'],
			['pull', behindBranch, 'Pull options'],
			['pr', cleanAheadBranch, 'Pull request options'],
			['shipit', dirtyFeatureBranch, 'Ship It options']
		] as const)('renders %s as one in-panel region, never another dialog', async (action, setup, name) => {
			setup();
			renderHeader();
			await openPanel();
			await fireEvent.click(row(name));
			expect(screen.getAllByRole('dialog')).toHaveLength(1);
			expect(popover(action).closest('.git-panel')).toBe(panel());
			expect(panel().querySelector('.gp-rows')).toBeNull();
			await fireEvent.click(screen.getByRole('button', { name: 'Back to Git actions' }));
			expect(panel().querySelector('.gp-rows')).toBeTruthy();
		});
		it('keeps operation progress/errors in-panel and permits closing while work continues', async () => {
			dirtyFeatureBranch();
			mocks.gitOperationState.set({ active: true, status: 'running', title: 'Commit', steps: [{ id: 'commit', label: 'Commit changes', status: 'running' }] });
			renderHeader({ operationInProgress: true });
			await tick();
			expect(screen.getAllByRole('dialog')).toHaveLength(1);
			expect(panel().querySelector('.git-progress')).toBeTruthy();
			await fireEvent.click(screen.getByRole('button', { name: 'Close Git panel' }));
			expect(queryPanel()).toBeNull();
			mocks.gitOperationState.set({ active: true, status: 'error', title: 'Commit', steps: [{ id: 'commit', label: 'Commit changes', status: 'error', error: 'Fixture failure' }] });
			await tick();
			expect(queryPanel()).toBeNull();
			await openPanel();
			expect(within(panel()).getByText('Fixture failure')).toBeTruthy();
			await fireEvent.click(within(panel()).getByRole('button', { name: 'Dismiss' }));
			expect(panel().querySelector('.git-progress')).toBeNull();
		});
	});

describe('GitHeaderActions mobile sheet', () => {
	beforeEach(() => {
		vi.stubGlobal('PointerEvent', class extends MouseEvent {
			pointerId: number;
			isPrimary: boolean;
			constructor(type: string, init: PointerEventInit = {}) {
				super(type, init);
				this.pointerId = init.pointerId ?? 1;
				this.isPrimary = init.isPrimary ?? true;
			}
		});
	});
	afterEach(() => vi.unstubAllGlobals());

	async function sheet() {
		dirtyFeatureBranch();
		mocks.mobileShell = true;
		renderHeader();
		await openPanel();
		Object.defineProperty(panel(), 'offsetHeight', { value: 600 });
		const handle = screen.getByRole('button', { name: 'Close Git panel (drag down or tap)' });
		handle.setPointerCapture = vi.fn();
		return handle;
	}

	it('always uses a bottom sheet in the mobile build, even at unfolded widths', () => {
		mocks.mobileShell = true;
		const { container } = renderHeader();
		expect(container.querySelector('.git-actions.sheet-layout')).toBeTruthy();
	});

	it('uses a sheet for coarse-pointer web/tablet screens and responds to folding', async () => {
		let change: () => void = () => {};
		const media = { matches: true, addEventListener: vi.fn((_, fn) => { change = fn; }), removeEventListener: vi.fn() };
		vi.stubGlobal('matchMedia', vi.fn(() => media));
		const { container } = renderHeader();
		expect(container.querySelector('.sheet-layout')).toBeTruthy();
		media.matches = false;
		change();
		await tick();
		expect(container.querySelector('.sheet-layout')).toBeNull();
	});

	it('tracks a touch drag and dismisses on a downward swipe, returning focus', async () => {
		const handle = await sheet();
		await fireEvent.pointerDown(handle, { pointerId: 7, clientY: 100, button: 0 });
		expect(handle.setPointerCapture).toHaveBeenCalledWith(7);
		await fireEvent.pointerMove(handle, { pointerId: 7, clientY: 240 });
		expect(panel().style.getPropertyValue('--sheet-offset')).toBe('140px');
		await fireEvent.pointerUp(handle, { pointerId: 7, clientY: 240 });
		expect(queryPanel()).toBeNull();
		expect(document.activeElement).toBe(trigger());
	});

	it('snaps back after a short drag and suppresses the resulting click', async () => {
		const handle = await sheet();
		await fireEvent.pointerDown(handle, { clientY: 100 });
		await fireEvent.pointerMove(handle, { clientY: 130 });
		await fireEvent.pointerUp(handle, { clientY: 130 });
		await fireEvent.click(handle, { detail: 1 });
		expect(panel().style.getPropertyValue('--sheet-offset')).toBe('0px');
		await fireEvent.click(handle, { detail: 0 });
		expect(queryPanel()).toBeNull();
	});

	it('cancels a drag without dismissing and never drags from the panel content', async () => {
		const handle = await sheet();
		await fireEvent.pointerDown(panel(), { clientY: 100 });
		await fireEvent.pointerMove(panel(), { clientY: 300 });
		expect(panel().style.getPropertyValue('--sheet-offset')).toBe('0px');
		await fireEvent.pointerDown(handle, { clientY: 100 });
		await fireEvent.pointerMove(handle, { clientY: 300 });
		await fireEvent.pointerCancel(handle);
		expect(panel().style.getPropertyValue('--sheet-offset')).toBe('0px');
	});

	it('allows tapping/keyboard activation of the handle to close', async () => {
		await sheet();
		await fireEvent.click(screen.getByRole('button', { name: 'Close Git panel (drag down or tap)' }));
		expect(queryPanel()).toBeNull();
	});
});

// ── Handler wiring ──────────────────────────────────────────────────────────

describe('GitHeaderActions handler wiring', () => {
	it('a Commit row click fires the handler with no option bag (defaults) and keeps the panel open', async () => {
		dirtyFeatureBranch();
		const { handlers } = renderHeader();
		await openPanel();
		await fireEvent.click(row('Commit'));
		expect(handlers.onClickCommit).toHaveBeenCalledTimes(1);
		expect(handlers.onClickCommit).toHaveBeenCalledWith();
		// Progress renders in the panel, so it stays open on the overview.
		expect(panel()).toBeTruthy();
		expect(panel().querySelector('.gp-rows')).toBeTruthy();
	});

	it('popover Commit primary fires the handler with an explicit bag', async () => {
		dirtyFeatureBranch();
		const { handlers } = renderHeader();
		await openPanel();
		await fireEvent.click(row('Commit options'));
		await fireEvent.click(within(popover('commit')).getByRole('button', { name: 'Commit' }));
		expect(handlers.onClickCommit).toHaveBeenCalledTimes(1);
		const arg = handlers.onClickCommit.mock.calls[0][0];
		expect(arg).toMatchObject({
			includeUnstaged: expect.any(Boolean),
			nextStep: expect.any(String)
		});
		// Submitting leaves the editor and returns to the panel overview.
		expect(queryPopover('commit')).toBeNull();
		expect(panel().querySelector('.gp-rows')).toBeTruthy();
	});

	it('the private-access row opens the access dialog', async () => {
		dirtyFeatureBranch();
		const open = vi.fn();
		chatAccessSummary.set({ sessionId: 's1', pending: 0, active: 1, saved: 0, error: false, open });
		renderHeader();
		await openPanel();
		const accessRow = row('Private access');
		expect(accessRow.textContent).toContain('1 active grant');
		await fireEvent.click(accessRow);
		expect(open).toHaveBeenCalledOnce();
		expect(queryPanel()).toBeNull();
	});

	it('disables the private-access row when the chat has none', async () => {
		dirtyFeatureBranch();
		chatAccessSummary.set({ sessionId: 's1', pending: 0, active: 0, saved: 0, error: false, open: vi.fn() });
		renderHeader();
		await openPanel();
		expect(row('Private access').disabled).toBe(true);
	});

	it('omits the private-access row when no chat is mounted', async () => {
		dirtyFeatureBranch();
		renderHeader();
		await openPanel();
		expect(queryRow('Private access')).toBeNull();
	});
});

// ── Persistence ─────────────────────────────────────────────────────────────

describe('GitHeaderActions option persistence', () => {
	it('toggling include-unstaged persists through gitActionPreferences', async () => {
		dirtyFeatureBranch();
		renderHeader();
		await openPanel();
		expect(gitActionPreferences.current.includeUnstaged).toBe(true);
		await fireEvent.click(row('Commit options'));
		await fireEvent.click(within(popover('commit')).getByText('Include unstaged changes'));
		expect(gitActionPreferences.current.includeUnstaged).toBe(false);
	});

	it('changing the next-step radio persists', async () => {
		dirtyFeatureBranch();
		renderHeader();
		await openPanel();
		expect(gitActionPreferences.current.nextStep).toBe('push');
		await fireEvent.click(row('Commit options'));
		await fireEvent.click(within(popover('commit')).getByRole('button', { name: /Commit only/ }));
		expect(gitActionPreferences.current.nextStep).toBe('commit');
	});

	it('changing the pull strategy persists', async () => {
		behindBranch();
		renderHeader();
		await openPanel();
		expect(gitActionPreferences.current.pullStrategy).toBe('rebase');
		await fireEvent.click(row('Pull options'));
		await fireEvent.click(within(popover('pull')).getByRole('button', { name: /Merge/ }));
		expect(gitActionPreferences.current.pullStrategy).toBe('merge');
	});

	it('force push is per-invocation only and never persisted', async () => {
		cleanAheadBranch();
		const { handlers } = renderHeader();
		await openPanel();
		await fireEvent.click(row('Push options'));
		const dialog = popover('push');
		await fireEvent.click(within(dialog).getByText('Force push'));
		await fireEvent.click(within(dialog).getByRole('button', { name: 'Force push' }));
		expect(handlers.onClickPush).toHaveBeenCalledWith({ force: true });
		// No such thing as a persisted force-push preference.
		expect((gitActionPreferences.current as unknown as Record<string, unknown>).forcePush).toBeUndefined();
	});
});

// ── Busy gating ─────────────────────────────────────────────────────────────

describe('GitHeaderActions busy gating', () => {
	it('disables the row and its chevron while streaming', async () => {
		dirtyFeatureBranch();
		renderHeader({ isStreaming: true });
		await openPanel();
		expect(row('Commit').disabled).toBe(true);
		expect(row('Commit options').disabled).toBe(true);
	});

	it('the chevron does not open a popover while an operation runs', async () => {
		dirtyFeatureBranch();
		renderHeader({ operationInProgress: true });
		await openPanel();
		await fireEvent.click(row('Commit options'));
		expect(queryPopover('commit')).toBeNull();
	});
});
