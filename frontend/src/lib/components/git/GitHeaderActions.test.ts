/**
 * Behavior tests for GitHeaderActions — the split-button git controls.
 *
 * These mount the real header with its git/github stores mocked (but the real
 * header-dropdowns and gitActionPreferences singletons) so we can drive the
 * chevron option popovers end-to-end. Covered:
 *  - the button-visibility matrix (Commit / Create PR / Finish / Push / Pull /
 *    Ship It appear for the right repo state)
 *  - a chevron opens its popover; Escape and outside-click close it; opening one
 *    popover closes another (shared single-open store)
 *  - persisted options (include-unstaged, next-step, pull strategy) write through
 *    gitActionPreferences; a per-invocation option (force push) does NOT persist
 *  - the main button fires its handler with defaults (no option bag); the popover
 *    primary fires with an explicit bag
 *  - everything disables under actionsBusy (isStreaming)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, within } from '@testing-library/svelte';
import { tick } from 'svelte';

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
		githubRepoInfo: store<Record<string, unknown> | null>({ default_branch: 'main' }),
		githubChecks: store<Record<string, unknown> | null>(null),
		githubChecksStale: store<boolean>(false),
		loadBranches: vi.fn(),
		refreshDiffStats: vi.fn(),
		refreshChangedFiles: vi.fn(),
		startBackgroundRefresh: vi.fn(),
		stopBackgroundRefresh: vi.fn(),
		checkoutBranchWithToast: vi.fn()
	};
});

vi.mock('$lib/stores/git.svelte', () => ({
	git: {
		loadBranches: mocks.loadBranches,
		refreshDiffStats: mocks.refreshDiffStats,
		refreshChangedFiles: mocks.refreshChangedFiles,
		startBackgroundRefresh: mocks.startBackgroundRefresh,
		stopBackgroundRefresh: mocks.stopBackgroundRefresh
	},
	gitSnapshot: mocks.gitSnapshot,
	gitMode: mocks.gitMode
}));
vi.mock('$lib/stores/github.svelte', () => ({
	githubRepoInfo: mocks.githubRepoInfo,
	githubChecks: mocks.githubChecks,
	githubChecksStale: mocks.githubChecksStale
}));
vi.mock('$lib/stores/gitCheckoutAction', () => ({
	checkoutBranchWithToast: mocks.checkoutBranchWithToast
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

// ── Scenario setup ──────────────────────────────────────────────────────────

/** Dirty tree on a feature branch → Commit + Ship It show. */
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
		changedFiles: [{ file: 'a.ts' }],
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

/** Behind upstream → Pull shows. */
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

const noop = () => {};

function baseHandlers() {
	return {
		onClickCommit: vi.fn(),
		onClickCreatePR: vi.fn(),
		onClickFinish: vi.fn(),
		onClickPush: vi.fn(),
		onClickPull: vi.fn(),
		onClickNewBranch: vi.fn(),
		onClickShipIt: vi.fn(),
		onClickInitRepo: vi.fn()
	};
}

function renderHeader(props: Record<string, unknown> = {}, handlers = baseHandlers()) {
	const result = render(GitHeaderActions, {
		props: {
			projectId: 'p1',
			showBranchSelector: false,
			isGitRepo: true,
			...handlers,
			...props
		}
	});
	return { ...result, handlers };
}

/** Wait one animation frame — the popover registers its outside-click listener
 *  on rAF so the opening click can't immediately close it. */
const nextFrame = () => new Promise((r) => requestAnimationFrame(() => r(null)));

beforeEach(() => {
	localStorage.clear();
	gitActionPreferences.reset();
	openHeaderDropdown.set(null);
	mocks.githubChecks.set(null);
	mocks.githubChecksStale.set(false);
	vi.clearAllMocks();
});

afterEach(() => {
	cleanup();
	openHeaderDropdown.set(null);
});

// ── Visibility matrix ───────────────────────────────────────────────────────

describe('GitHeaderActions button visibility', () => {
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
		expect(screen.queryByText('Create PR')).toBeNull();
		expect(screen.queryByText('Ship It')).toBeNull();

		setSnapshot({ ...snapshot, diffStats: { additions: 1, deletions: 0, files_changed: 1 } });
		await tick();
		expect(screen.getByText('Commit')).toBeTruthy();
		expect(screen.getByText('Ship It')).toBeTruthy();

		setSnapshot({ ...snapshot, status: { ...snapshot.status, head_commit: 'b'.repeat(12) } });
		await tick();
		expect(screen.getByText('Create PR')).toBeTruthy();
		expect(screen.getByText('Ship It')).toBeTruthy();
	});


	// Note: main buttons carry a count badge (e.g. "Push 2"), so their accessible
	// name isn't a clean label — query the label text; query chevrons by aria-label.
	it('shows Commit (with chevron) and Ship It for a dirty feature branch', () => {
		dirtyFeatureBranch();
		renderHeader();
		expect(screen.getByText('Commit')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Commit options' })).toBeTruthy();
		expect(screen.getByText('Ship It')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Ship It options' })).toBeTruthy();
	});

	describe('githubAvailable gate (G10c)', () => {
		// The header itself no longer requires GitHub auth — local git has to keep
		// working when `gh` is down. That means the gh-BACKED actions now need
		// their own gate, or they would render as buttons guaranteed to fail.
		it('hides Create PR and Ship It when GitHub is unavailable', () => {
			cleanAheadBranch();
			renderHeader({ githubAvailable: false });

			expect(screen.queryByText('Create PR')).toBeNull();
			expect(screen.queryByText('Ship It')).toBeNull();
		});

		it('still offers the LOCAL actions when GitHub is unavailable', () => {
			cleanAheadBranch();
			renderHeader({ githubAvailable: false });

			// Push is plain git — losing gh must not take it away.
			expect(screen.getByText('Push')).toBeTruthy();
		});

		it('hides Finish (PR merge) when GitHub is unavailable', () => {
			cleanAheadBranch();
			renderHeader({
				githubAvailable: false,
				openPR: { number: 12, title: 'A PR', url: 'u', mergeable: true }
			});

			expect(screen.queryByText('Finish')).toBeNull();
		});

		it('defaults to available so existing call sites are unchanged', () => {
			cleanAheadBranch();
			renderHeader();

			expect(screen.getByText('Create PR')).toBeTruthy();
		});
	});

	it('shows Push and Create PR for a clean branch ahead of upstream', () => {
		cleanAheadBranch();
		renderHeader();
		expect(screen.getByText('Push')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Push options' })).toBeTruthy();
		expect(screen.getByText('Create PR')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Pull request options' })).toBeTruthy();
	});

	it('shows Finish (merge) when an open PR is passed', () => {
		cleanAheadBranch();
		renderHeader({ openPR: { number: 42, title: 'x', url: 'http://x' } });
		expect(screen.getByText('Finish')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Merge options' })).toBeTruthy();
	});

	it('shows Pull when the branch is behind upstream', () => {
		behindBranch();
		renderHeader();
		expect(screen.getByText('Pull')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Pull options' })).toBeTruthy();
	});

	it('disables Finish when the open PR is explicitly not mergeable', () => {
		cleanAheadBranch();
		renderHeader({
			openPR: { number: 42, title: 'x', url: 'http://x', mergeable: false }
		});
		const finishBtn = screen.getByText('Finish').closest('button') as HTMLButtonElement;
		expect(finishBtn.disabled).toBe(true);
		// Warning icon: title explains the mergeability block.
		expect(finishBtn.title).toContain('not mergeable');
	});

	it('keeps Finish enabled when mergeability is unknown (null)', () => {
		cleanAheadBranch();
		renderHeader({
			openPR: { number: 42, title: 'x', url: 'http://x', mergeable: null }
		});
		const finishBtn = screen.getByText('Finish').closest('button') as HTMLButtonElement;
		expect(finishBtn.disabled).toBe(false);
	});

	it('does not render the informational PR chip even with an open PR', () => {
		cleanAheadBranch();
		renderHeader({ openPR: { number: 42, title: 'My PR', url: 'http://x/pr/42' } });
		expect(screen.queryByRole('link', { name: /PR #/ })).toBeNull();
		// Finish stays as the PR entry point.
		expect(screen.getByText('Finish')).toBeTruthy();
	});

	it('shows Create PR when differsFromBase even with zero commits ahead', () => {
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
		expect(screen.getByText('Create PR')).toBeTruthy();
	});
});

// ── Default branch comes from /git/status (finding G3) ──────────────────────

describe('GitHeaderActions default branch', () => {
	it('uses status.default_branch, not a hardcoded "main"', () => {
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
		expect(screen.queryByText('Create PR')).toBeNull();
		expect(screen.queryByText('Ship It')).toBeNull();
	});

	it('still offers Create PR on a non-default branch of a master-default repo', () => {
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
		expect(screen.getByText('Create PR')).toBeTruthy();
	});
});

// ── Snapshot coherence on a session switch (finding G4) ─────────────────────

describe('GitHeaderActions snapshot coherence', () => {
	it('holds the previous action while a switch revalidates — never an intermediate value', async () => {
		// Coherent state: an open PR ⇒ Finish.
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
		expect(screen.getByText('Finish')).toBeTruthy();

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

		// Still Finish — held from the last coherent snapshot — and disabled so it
		// can't be fired against unconfirmed state.
		expect(screen.getByText('Finish')).toBeTruthy();
		expect(screen.queryByText('Create PR')).toBeNull();
		expect((screen.getByText('Finish').closest('button') as HTMLButtonElement).disabled).toBe(true);
	});

	it('never renders Ship It against a carried-over placeholder', async () => {
		dirtyFeatureBranch();
		renderHeader();
		expect(screen.getByText('Ship It')).toBeTruthy();

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
			changedFiles: [{ file: 'a.ts' }],
			branches: [
				{ name: 'feature/x', current: true, remote: false, upstream: 'origin/feature/x', ahead: 0, behind: 0 }
			],
			stale: true,
			hydrated: false
		});
		await tick();
		// The HELD decision still shows Ship It (it was coherent a moment ago),
		// but the button is disabled until the new context confirms.
		const shipBtn = screen.getByText('Ship It').closest('button') as HTMLButtonElement;
		expect(shipBtn.disabled).toBe(true);
	});
});

// ── Failed PR lookup keeps the PR (findings G2 / G3) ───────────────────────

describe('GitHeaderActions failed PR lookup', () => {
	it('keeps "Finish" and marks it last-known when the lookup failed', async () => {
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

		// NEVER "Create PR" because a lookup blipped.
		expect(screen.queryByText('Create PR')).toBeNull();
		expect(screen.getByText('Finish')).toBeTruthy();
		// …and the user is told the value is unconfirmed.
		expect(screen.getByLabelText('Last known PR state — GitHub unreachable')).toBeTruthy();
		expect(
			(screen.getByText('Finish').closest('button') as HTMLButtonElement).title
		).toContain('last known state');
	});

	it('shows no last-known marker once the lookup succeeds again', async () => {
		cleanAheadBranch();
		renderHeader({ openPR: { number: 12, title: 'x', url: 'http://x', mergeable: null } });
		await tick();
		expect(screen.getByText('Finish')).toBeTruthy();
		expect(screen.queryByLabelText('Last known PR state — GitHub unreachable')).toBeNull();
	});
});

// ── Open / close coordination ───────────────────────────────────────────────

describe('GitHeaderActions option popover open/close', () => {
	it('opens the commit popover on chevron click and closes on Escape', async () => {
		dirtyFeatureBranch();
		renderHeader();
		await fireEvent.click(screen.getByRole('button', { name: 'Commit options' }));
		expect(screen.getByRole('dialog')).toBeTruthy();
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('closes the popover on an outside click', async () => {
		dirtyFeatureBranch();
		renderHeader();
		await fireEvent.click(screen.getByRole('button', { name: 'Commit options' }));
		expect(screen.getByRole('dialog')).toBeTruthy();
		await nextFrame();
		await fireEvent.click(document.body);
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('opening the Ship It popover closes the Commit popover (single-open)', async () => {
		dirtyFeatureBranch();
		renderHeader();
		await fireEvent.click(screen.getByRole('button', { name: 'Commit options' }));
		expect(within(screen.getByRole('dialog')).getByText('Commit options')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Ship It options' }));
		// Still exactly one dialog, and it's now the Ship It one (its step preview
		// row "Push to remote" is unique to the Ship It popover).
		const dialog = screen.getByRole('dialog');
		expect(within(dialog).getByText('Push to remote')).toBeTruthy();
	});
});

// ── Handler wiring ──────────────────────────────────────────────────────────

describe('GitHeaderActions handler wiring', () => {
	it('main Commit click fires the handler with no option bag (defaults)', async () => {
		dirtyFeatureBranch();
		const { handlers } = renderHeader();
		await fireEvent.click(screen.getByRole('button', { name: 'Commit' }));
		expect(handlers.onClickCommit).toHaveBeenCalledTimes(1);
		expect(handlers.onClickCommit).toHaveBeenCalledWith();
	});

	it('popover Commit primary fires the handler with an explicit bag', async () => {
		dirtyFeatureBranch();
		const { handlers } = renderHeader();
		await fireEvent.click(screen.getByRole('button', { name: 'Commit options' }));
		const dialog = screen.getByRole('dialog');
		await fireEvent.click(within(dialog).getByRole('button', { name: 'Commit' }));
		expect(handlers.onClickCommit).toHaveBeenCalledTimes(1);
		const arg = handlers.onClickCommit.mock.calls[0][0];
		expect(arg).toMatchObject({
			includeUnstaged: expect.any(Boolean),
			nextStep: expect.any(String)
		});
	});
});

// ── Persistence ─────────────────────────────────────────────────────────────

describe('GitHeaderActions option persistence', () => {
	it('toggling include-unstaged persists through gitActionPreferences', async () => {
		dirtyFeatureBranch();
		renderHeader();
		expect(gitActionPreferences.current.includeUnstaged).toBe(true);
		await fireEvent.click(screen.getByRole('button', { name: 'Commit options' }));
		const dialog = screen.getByRole('dialog');
		await fireEvent.click(within(dialog).getByText('Include unstaged changes'));
		expect(gitActionPreferences.current.includeUnstaged).toBe(false);
	});

	it('changing the next-step radio persists', async () => {
		dirtyFeatureBranch();
		renderHeader();
		expect(gitActionPreferences.current.nextStep).toBe('push');
		await fireEvent.click(screen.getByRole('button', { name: 'Commit options' }));
		const dialog = screen.getByRole('dialog');
		await fireEvent.click(within(dialog).getByRole('button', { name: /Commit only/ }));
		expect(gitActionPreferences.current.nextStep).toBe('commit');
	});

	it('changing the pull strategy persists', async () => {
		behindBranch();
		renderHeader();
		expect(gitActionPreferences.current.pullStrategy).toBe('rebase');
		await fireEvent.click(screen.getByRole('button', { name: 'Pull options' }));
		const dialog = screen.getByRole('dialog');
		await fireEvent.click(within(dialog).getByRole('button', { name: /Merge/ }));
		expect(gitActionPreferences.current.pullStrategy).toBe('merge');
	});

	it('force push is per-invocation only and never persisted', async () => {
		cleanAheadBranch();
		const { handlers } = renderHeader();
		await fireEvent.click(screen.getByRole('button', { name: 'Push options' }));
		const dialog = screen.getByRole('dialog');
		await fireEvent.click(within(dialog).getByText('Force push'));
		await fireEvent.click(within(dialog).getByRole('button', { name: 'Force push' }));
		expect(handlers.onClickPush).toHaveBeenCalledWith({ force: true });
		// No such thing as a persisted force-push preference.
		expect((gitActionPreferences.current as unknown as Record<string, unknown>).forcePush).toBeUndefined();
	});
});

// ── Busy gating ─────────────────────────────────────────────────────────────

describe('GitHeaderActions busy gating', () => {
	it('disables the main button and chevron while streaming', () => {
		dirtyFeatureBranch();
		renderHeader({ isStreaming: true });
		expect((screen.getByRole('button', { name: 'Commit' }) as HTMLButtonElement).disabled).toBe(true);
		expect((screen.getByRole('button', { name: 'Commit options' }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('the chevron does not open a popover while streaming', async () => {
		dirtyFeatureBranch();
		renderHeader({ operationInProgress: true });
		await fireEvent.click(screen.getByRole('button', { name: 'Commit options' }));
		expect(screen.queryByRole('dialog')).toBeNull();
	});
});

// Keep noop referenced (documents the no-op handler default shape).
void noop;
