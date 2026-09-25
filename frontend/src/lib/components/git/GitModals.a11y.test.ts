/**
 * Git set-up / branch modals: named dialogs that take focus, keep Tab inside,
 * and (Set up Git) do not turn Enter on a focused button into a submit.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
const gitApi = vi.hoisted(() => ({ initRepository: vi.fn(async () => ({})), cloneRepository: vi.fn(async () => ({})) }));
vi.mock('$lib/api/git', () => gitApi);
vi.mock('$lib/stores/git.svelte', () => ({ git: { branches: [], currentBranch: 'main', createBranch: vi.fn() } }));
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import InitRepoModal from './InitRepoModal.svelte';
import CreateBranchModal from './CreateBranchModal.svelte';

async function settle() {
	await tick();
	await Promise.resolve();
}

afterEach(() => {
	cleanup();
	document.body.innerHTML = '';
	vi.clearAllMocks();
});

describe('InitRepoModal keyboard', () => {
	it('Enter on the focused Close button closes instead of initialising the repo', async () => {
		const onClose = vi.fn();
		render(InitRepoModal, { props: { open: true, projectId: 'p1', onClose } });
		await settle();
		expect(screen.getByRole('dialog', { name: 'Set up Git' })).toBeTruthy();

		const close = screen.getByRole('button', { name: 'Close' });
		close.focus();
		const notPrevented = await fireEvent.keyDown(close, { key: 'Enter' });

		expect(notPrevented).toBe(true);
		expect(gitApi.initRepository).not.toHaveBeenCalled();
	});

	it('Enter from outside a button still submits', async () => {
		render(InitRepoModal, { props: { open: true, projectId: 'p1', onClose: vi.fn() } });
		await settle();
		await fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Enter' });
		expect(gitApi.initRepository).toHaveBeenCalledOnce();
	});
});

describe('CreateBranchModal focus', () => {
	it('is a named dialog that takes focus and returns it to the opener', async () => {
		const opener = document.createElement('button');
		document.body.appendChild(opener);
		opener.focus();
		const props = { open: true, onClose: vi.fn() };
		const view = render(CreateBranchModal, { props });
		await settle();
		const dialog = screen.getByRole('dialog', { name: 'Create a branch' });
		expect(dialog.contains(document.activeElement)).toBe(true);
		await view.rerender({ ...props, open: false });
		await settle();
		expect(document.activeElement).toBe(opener);
	});
});
