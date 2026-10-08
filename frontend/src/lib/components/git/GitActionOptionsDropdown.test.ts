/**
 * Git action options popover (Commit / Push / Pull / Create PR / Ship It):
 * each submit fires its callback with the right option bag and closes;
 * persisted toggles write through gitActionPreferences; busy disables submit;
 * Escape and outside clicks close.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';

const save = vi.hoisted(() => vi.fn());
vi.mock('$lib/services/preferences', () => ({ preferenceSync: { save, refresh: vi.fn(), register: vi.fn(), registerPrefix: vi.fn() } }));
import { gitActionPreferences } from '$lib/stores/gitActionPreferences.svelte';
import GitActionOptionsDropdown from './GitActionOptionsDropdown.svelte';
vi.mock('$lib/constants/model-picker', () => ({ buildModelPickerCatalog: () => ({
	groups: [{ key: 'strong', name: 'Provider', subtext: 'api' }],
	options: [{ value: 'prov:strong:astra', label: 'Astra', groupKey: 'strong' }]
}) }));

let onClose: ReturnType<typeof vi.fn<() => void>>;
beforeEach(() => {
	gitActionPreferences.reset();
	vi.clearAllMocks();
	onClose = vi.fn();
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

function mount(action: string, extra: Record<string, unknown> = {}) {
	return render(GitActionOptionsDropdown, {
		props: { action, currentBranch: 'feat/login', defaultBranch: 'main', hasChanges: true, onClose, ...extra }
	} as never);
}
const submit = (name: string) => screen.getAllByRole('button', { name }).find((b) => b.classList.contains('opt-submit')) as HTMLButtonElement;

describe('generation binding', () => {
	it.each([
		['commit', 'Commit', 'onCommit'],
		['pr', 'Create PR', 'onCreatePR'],
		['shipit', 'Ship It', 'onShipIt']
	])('passes the selected model for %s without saving a preference', async (action, label, callback) => {
		const onSubmit = vi.fn();
		mount(action, { [callback]: onSubmit });
		const picker = screen.getByRole('combobox', { name: /Generation model/ });
		await fireEvent.change(picker, { target: { value: 'prov:strong:astra' } });
		await fireEvent.click(submit(label));
		expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ generationModel: 'prov:strong:astra' }));
		expect(save).not.toHaveBeenCalled();
	});
	it.each([
		['Commit on new branch', { newBranch: 'fix/login' }],
		['Commit only', { pushToDefaultBranch: false }]
	])('passes the selected model when committing on the default branch (%s)', async (label, expected) => {
		const onCommit = vi.fn();
		mount('commit', { currentBranch: 'main', defaultBranch: 'main', onCommit });
		await fireEvent.change(screen.getByRole('combobox', { name: /Generation model/ }), { target: { value: 'prov:strong:astra' } });
		if (label === 'Commit only') await fireEvent.click(screen.getByRole('button', { name: 'main' }));
		else await fireEvent.input(screen.getByLabelText('Branch name'), { target: { value: 'fix/login' } });
		await fireEvent.click(submit(label));
		expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ generationModel: 'prov:strong:astra', ...expected }));
	});
	it('disables the model picker while the action is busy', () => {
		mount('commit', { busy: true });
		expect((screen.getByRole('combobox', { name: /Generation model/ }) as HTMLSelectElement).disabled).toBe(true);
	});
});

describe('commit options on the default branch (#1659)', () => {
	const onMain = { currentBranch: 'main', defaultBranch: 'main' };

	it('defaults to committing on a new branch named by the user', async () => {
		const onCommit = vi.fn();
		mount('commit', { ...onMain, onCommit });
		expect(screen.getByRole('note').textContent).toContain('on main');
		await fireEvent.input(screen.getByLabelText('Branch name'), { target: { value: ' fix/login ' } });
		await fireEvent.click(submit('Commit on new branch'));
		expect(onCommit).toHaveBeenCalledWith({ message: undefined, includeUnstaged: true, nextStep: 'push', newBranch: 'fix/login' });
	});

	it('pushes to the default branch only as an explicit choice and keeps saved prefs', async () => {
		const onCommit = vi.fn();
		mount('commit', { ...onMain, onCommit });
		await fireEvent.click(screen.getByRole('button', { name: 'main' }));
		expect(submit('Commit only')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Commit + push to main' }));
		const go = submit('Commit + push to main');
		expect(go.classList.contains('warn')).toBe(true);
		await fireEvent.click(go);
		expect(onCommit).toHaveBeenCalledWith({ message: undefined, includeUnstaged: true, nextStep: 'push', pushToDefaultBranch: true });
		expect(gitActionPreferences.current.nextStep).toBe('push');
		expect(save).not.toHaveBeenCalled();
	});

	it('offers no new-branch choice for a branch-bound checkout', async () => {
		const onCommit = vi.fn();
		mount('commit', { ...onMain, canCommitOnNewBranch: false, onCommit });
		expect(screen.queryByLabelText('Branch name')).toBeNull();
		await fireEvent.click(submit('Commit only'));
		expect(onCommit).toHaveBeenCalledWith({ message: undefined, includeUnstaged: true, nextStep: 'commit', pushToDefaultBranch: false });
	});
});

describe('commit options', () => {
	it('sends the trimmed message with the saved defaults and closes', async () => {
		const onCommit = vi.fn();
		mount('commit', { onCommit });
		expect(screen.getByRole('region', { name: 'commit options' })).toBeTruthy();
		await fireEvent.input(screen.getByLabelText('Message'), { target: { value: '  fix login  ' } });
		await fireEvent.click(submit('Commit'));
		expect(onCommit).toHaveBeenCalledWith({ message: 'fix login', includeUnstaged: true, nextStep: 'push' });
		expect(onClose).toHaveBeenCalledOnce();
	});

	it('persists the toggles it changes and leaves an empty message for the AI', async () => {
		const onCommit = vi.fn();
		mount('commit', { onCommit });
		const unstaged = screen.getByRole('switch', { name: 'Include unstaged changes' });
		await fireEvent.click(unstaged);
		expect(unstaged.getAttribute('aria-checked')).toBe('false');
		await fireEvent.click(screen.getByRole('button', { name: 'Commit + push + PR' }));
		expect(screen.getByRole('button', { name: 'Commit + push + PR' }).getAttribute('aria-pressed')).toBe('true');
		expect(gitActionPreferences.current).toMatchObject({ includeUnstaged: false, nextStep: 'pr' });
		expect(save).toHaveBeenCalled();
		await fireEvent.click(submit('Commit'));
		expect(onCommit).toHaveBeenCalledWith({ message: undefined, includeUnstaged: false, nextStep: 'pr' });
	});

	it('disables submit while busy', () => {
		mount('commit', { busy: true, onCommit: vi.fn() });
		expect(submit('Commit').disabled).toBe(true);
	});
});

describe('push / pull options', () => {
	it('pushes normally, or force-pushes once the danger switch is on', async () => {
		const onPush = vi.fn();
		mount('push', { onPush });
		expect(screen.getByText('feat/login')).toBeTruthy();
		await fireEvent.click(screen.getByRole('switch', { name: /Force push/ }));
		await fireEvent.click(submit('Force push'));
		expect(onPush).toHaveBeenCalledWith({ force: true });
		expect(onClose).toHaveBeenCalledOnce();
	});

	it('pulls with the chosen strategy and auto-stash', async () => {
		const onPull = vi.fn();
		mount('pull', { onPull });
		await fireEvent.click(screen.getByRole('button', { name: 'Merge' }));
		await fireEvent.click(screen.getByRole('switch', { name: 'Auto-stash local changes' }));
		await fireEvent.click(submit('Pull'));
		expect(onPull).toHaveBeenCalledWith({ strategy: 'merge', autoStash: true });
	});
});

describe('pull request options', () => {
	it('defaults the base to the repo default branch and sends title, body, base and draft', async () => {
		const onCreatePR = vi.fn();
		mount('pr', { onCreatePR, baseBranches: ['develop', 'main'] });
		const base = screen.getByLabelText('Base branch') as HTMLSelectElement;
		expect(base.value).toBe('main');
		expect([...base.options].map((o) => o.value)).toEqual(['main', 'develop']);
		await fireEvent.input(screen.getByLabelText('Title'), { target: { value: ' Login fix ' } });
		await fireEvent.change(base, { target: { value: 'develop' } });
		await fireEvent.click(screen.getByRole('switch', { name: 'Open as draft' }));
		await fireEvent.click(submit('Create PR'));
		expect(onCreatePR).toHaveBeenCalledWith({ title: 'Login fix', body: undefined, base: 'develop', draft: true });
	});
});

describe('ship it options', () => {
	it('lists the steps for the current state and ships with the merge settings', async () => {
		const onShipIt = vi.fn();
		mount('shipit', { onShipIt, openPR: { number: 12 }, unpushed: 0, isWorktreeMode: true });
		expect(screen.getAllByRole('listitem').map((li) => li.textContent)).toEqual([
			'Commit changes', 'Push to remote', 'Merge PR #12', 'Clean up branch'
		]);
		await fireEvent.click(screen.getByRole('button', { name: 'Rebase' }));
		const cleanupSwitch = screen.getByRole('switch', { name: /^Delete worktree after merge/ });
		expect(cleanupSwitch.textContent).toContain('Stops any session still running in it');
		await fireEvent.click(cleanupSwitch);
		expect(screen.queryByText('Clean up branch')).toBeNull();
		await fireEvent.click(submit('Ship It'));
		expect(onShipIt).toHaveBeenCalledWith({ mergeMethod: 'rebase', cleanup: false, includeUnstaged: true });
	});

	it('only warns about stopping sessions when cleanup removes a worktree', () => {
		mount('shipit', { isWorktreeMode: false });
		expect(screen.getByRole('switch', { name: 'Delete branch after merge' })).toBeTruthy();
		expect(screen.queryByText('Stops any session still running in it')).toBeNull();
	});

	it('does not ship while busy', async () => {
		const onShipIt = vi.fn();
		mount('shipit', { onShipIt, busy: true });
		await fireEvent.click(submit('Ship It'));
		expect(onShipIt).not.toHaveBeenCalled();
	});

	it('blocks only the submit when submitting is blocked, keeping the settings editable', async () => {
		const onShipIt = vi.fn();
		mount('shipit', { onShipIt, openPR: { number: 12 }, submitBlocked: 'PR #12 has merge conflicts.' });
		expect(screen.getByRole('status').textContent).toBe('PR #12 has merge conflicts.');
		expect(screen.queryByText(/Return to Git to check its status/)).toBeNull();
		expect(submit('Ship It').disabled).toBe(true);
		await fireEvent.click(submit('Ship It'));
		expect(onShipIt).not.toHaveBeenCalled();
		const rebase = screen.getByRole('button', { name: 'Rebase' }) as HTMLButtonElement;
		expect(rebase.matches(':disabled')).toBe(false);
		await fireEvent.click(rebase);
		const cleanupSwitch = screen.getByRole('switch', { name: 'Delete branch after merge' }) as HTMLButtonElement;
		expect(cleanupSwitch.matches(':disabled')).toBe(false);
		await fireEvent.click(cleanupSwitch);
		expect(gitActionPreferences.current).toMatchObject({ mergeMethod: 'rebase', cleanupAfterMerge: false });
	});
});

describe('inline workspace navigation', () => {
	it('has no nested dialog or outside-click handler; Back and Escape leave the editor', async () => {
		mount('push', { onPush: vi.fn() });
		expect(screen.queryByRole('dialog')).toBeNull();
		await fireEvent.click(document.body);
		expect(onClose).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Back to Git actions' }));
		expect(onClose).toHaveBeenCalledOnce();
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(onClose).toHaveBeenCalledTimes(2);
	});
	it('locks the whole editor if its worktree becomes busy, while Back stays available', async () => {
		const onCommit = vi.fn();
		const view = mount('commit', { onCommit });
		await view.rerender({ action: 'commit', currentBranch: 'feat/login', defaultBranch: 'main', hasChanges: true, onClose, onCommit, busy: true });
		expect((screen.getByLabelText('Message') as HTMLTextAreaElement).matches(':disabled')).toBe(true);
		await fireEvent.click(submit('Commit'));
		expect(onCommit).not.toHaveBeenCalled();
		expect((screen.getByRole('button', { name: 'Back to Git actions' }) as HTMLButtonElement).disabled).toBe(false);
	});
});
