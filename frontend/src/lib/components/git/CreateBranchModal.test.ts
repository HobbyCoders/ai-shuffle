/**
 * Create-branch modal: name validation, prefix chips, create vs stage-only,
 * worker errors, keyboard (Enter / Escape) and reset on reopen.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const git = vi.hoisted(() => ({ createBranch: vi.fn() }));
vi.mock('$lib/stores/git.svelte', () => ({ git }));
import CreateBranchModal from './CreateBranchModal.svelte';

const input = () => screen.getByLabelText('Branch name') as HTMLInputElement;
const cont = () => screen.getByRole('button', { name: /Continue|Creating/ }) as HTMLButtonElement;
async function type(value: string) { await fireEvent.input(input(), { target: { value } }); }

let onClose: Mock<() => void>;
let onCreated: Mock<(name: string) => void>;
beforeEach(() => {
	vi.clearAllMocks();
	git.createBranch.mockResolvedValue(undefined);
	onClose = vi.fn();
	onCreated = vi.fn();
});
afterEach(() => cleanup());

describe('CreateBranchModal', () => {
	it('renders nothing while closed', () => {
		render(CreateBranchModal, { props: { open: false, onClose } });
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('needs a non-blank name, then creates the branch and closes', async () => {
		render(CreateBranchModal, { props: { open: true, onClose, onCreated } });
		expect(cont().disabled).toBe(true);
		await type('   ');
		expect(cont().disabled).toBe(true);
		await type(' feature-x ');
		expect(cont().disabled).toBe(false);
		await fireEvent.click(cont());
		await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
		expect(git.createBranch).toHaveBeenCalledWith('feature-x');
		expect(onCreated).toHaveBeenCalledWith('feature-x');
	});

	it('creates on Enter and ignores a second Enter while creating', async () => {
		let resolve!: () => void;
		git.createBranch.mockReturnValue(new Promise<void>((r) => (resolve = r)));
		render(CreateBranchModal, { props: { open: true, onClose, onCreated } });
		await type('topic');
		await fireEvent.keyDown(input(), { key: 'Enter' });
		expect(cont().textContent).toContain('Creating...');
		expect(input().disabled).toBe(true);
		await fireEvent.keyDown(input(), { key: 'Enter' });
		expect(git.createBranch).toHaveBeenCalledOnce();
		resolve();
		await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
	});

	it('shows the worker error, stays open and re-enables the field', async () => {
		git.createBranch.mockRejectedValue({ detail: "a branch named 'topic' already exists" });
		render(CreateBranchModal, { props: { open: true, onClose, onCreated } });
		await type('topic');
		await fireEvent.click(cont());
		expect((await screen.findByRole('alert')).textContent).toBe("a branch named 'topic' already exists");
		expect(onClose).not.toHaveBeenCalled();
		expect(onCreated).not.toHaveBeenCalled();
		expect(input().disabled).toBe(false);
		expect(cont().disabled).toBe(false);
	});

	it('hands the name back without creating it in stage-only mode', async () => {
		render(CreateBranchModal, { props: { open: true, stageOnly: true, onClose, onCreated } });
		await type('cloud-work');
		await fireEvent.click(cont());
		expect(git.createBranch).not.toHaveBeenCalled();
		expect(onCreated).toHaveBeenCalledWith('cloud-work');
		expect(onClose).toHaveBeenCalledOnce();
	});

	it('adds, swaps and removes a prefix', async () => {
		render(CreateBranchModal, { props: { open: true, onClose } });
		await type('login-bug');
		await fireEvent.click(screen.getByRole('button', { name: 'Set prefix' }));
		expect(input().value).toBe('codex/login-bug');
		await fireEvent.click(screen.getByRole('button', { name: 'fix/' }));
		expect(input().value).toBe('fix/login-bug');
		await fireEvent.click(screen.getByRole('button', { name: 'Remove prefix' }));
		expect(input().value).toBe('login-bug');
		expect(screen.queryByRole('button', { name: 'fix/' })).toBeNull();
	});

	it('closes on Escape, the close button and the backdrop', async () => {
		render(CreateBranchModal, { props: { open: true, onClose } });
		await fireEvent.keyDown(window, { key: 'Escape' });
		await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Close dialog' }));
		expect(onClose).toHaveBeenCalledTimes(3);
		expect(git.createBranch).not.toHaveBeenCalled();
	});

	it('pre-fills a branch-safe default name and resets on reopen', async () => {
		git.createBranch.mockRejectedValue({ detail: 'nope' });
		const props = { open: true, defaultName: 'Fix: Login Bug!', onClose };
		const view = render(CreateBranchModal, { props });
		expect(input().value).toBe('fix-login-bug');
		await type('other');
		await fireEvent.click(cont());
		await screen.findByRole('alert');
		await view.rerender({ ...props, open: false });
		await view.rerender({ ...props, open: true });
		expect(input().value).toBe('fix-login-bug');
		expect(screen.queryByRole('alert')).toBeNull();
	});
});
