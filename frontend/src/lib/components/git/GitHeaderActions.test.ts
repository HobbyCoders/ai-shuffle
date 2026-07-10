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
			}
		};
	}
	return {
		gitStatus: store<Record<string, unknown> | null>(null),
		diffStats: store<Record<string, unknown> | null>(null),
		branches: store<Record<string, unknown>[]>([]),
		gitMode: store<string>('project'),
		changedFiles: store<unknown[]>([]),
		githubRepoInfo: store<Record<string, unknown> | null>({ default_branch: 'main' }),
		loadBranches: vi.fn(),
		refreshDiffStats: vi.fn(),
		refreshChangedFiles: vi.fn(),
		checkoutBranchWithToast: vi.fn()
	};
});

vi.mock('$lib/stores/git.svelte', () => ({
	git: {
		loadBranches: mocks.loadBranches,
		refreshDiffStats: mocks.refreshDiffStats,
		refreshChangedFiles: mocks.refreshChangedFiles
	},
	gitStatus: mocks.gitStatus,
	diffStats: mocks.diffStats,
	branches: mocks.branches,
	gitMode: mocks.gitMode,
	changedFiles: mocks.changedFiles
}));
vi.mock('$lib/stores/github.svelte', () => ({ githubRepoInfo: mocks.githubRepoInfo }));
vi.mock('$lib/stores/gitCheckoutAction', () => ({
	checkoutBranchWithToast: mocks.checkoutBranchWithToast
}));

// Imported AFTER the mocks are registered.
import GitHeaderActions from './GitHeaderActions.svelte';
import { gitActionPreferences } from '$lib/stores/gitActionPreferences.svelte';
import { openHeaderDropdown } from '$lib/stores/header-dropdowns';

// ── Scenario setup ──────────────────────────────────────────────────────────

/** Dirty tree on a feature branch → Commit + Ship It show. */
function dirtyFeatureBranch() {
	mocks.gitStatus.set({
		current_branch: 'feature/x',
		ahead: 0,
		differs_from_base: true,
		is_git_repo: true
	});
	mocks.diffStats.set({ additions: 5, deletions: 2, files_changed: 1 });
	mocks.changedFiles.set([{ file: 'a.ts' }]);
	mocks.branches.set([
		{ name: 'feature/x', current: true, remote: false, upstream: 'origin/feature/x', ahead: 0, behind: 0 },
		{ name: 'main', current: false, remote: false }
	]);
	mocks.githubRepoInfo.set({ default_branch: 'main' });
}

/** Clean tree, commits ahead of upstream, not behind → Push + Create PR + Ship It. */
function cleanAheadBranch() {
	mocks.gitStatus.set({
		current_branch: 'feature/x',
		ahead: 2,
		differs_from_base: true,
		is_git_repo: true
	});
	mocks.diffStats.set({ additions: 0, deletions: 0, files_changed: 0 });
	mocks.changedFiles.set([]);
	mocks.branches.set([
		{ name: 'feature/x', current: true, remote: false, upstream: 'origin/feature/x', ahead: 2, behind: 0 },
		{ name: 'main', current: false, remote: false }
	]);
	mocks.githubRepoInfo.set({ default_branch: 'main' });
}

/** Behind upstream → Pull shows. */
function behindBranch() {
	mocks.gitStatus.set({ current_branch: 'feature/x', ahead: 0, differs_from_base: true, is_git_repo: true });
	mocks.diffStats.set({ additions: 0, deletions: 0, files_changed: 0 });
	mocks.branches.set([
		{ name: 'feature/x', current: true, remote: false, upstream: 'origin/feature/x', ahead: 0, behind: 3 }
	]);
	mocks.githubRepoInfo.set({ default_branch: 'main' });
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
	vi.clearAllMocks();
});

afterEach(() => {
	cleanup();
	openHeaderDropdown.set(null);
});

// ── Visibility matrix ───────────────────────────────────────────────────────

describe('GitHeaderActions button visibility', () => {
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
