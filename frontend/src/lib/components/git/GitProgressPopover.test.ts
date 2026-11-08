/**
 * GitProgressPopover is where a Git panel action reports back. A stopped pull
 * must name its conflicting files, keep its follow-up buttons usable (or say
 * why not), and stay until Dismiss. A pinned success needs Dismiss too.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { gitOperations } from '$lib/stores/gitOperations.svelte';
import GitProgressPopover from './GitProgressPopover.svelte';

beforeEach(() => { gitOperations.clear(); });
afterEach(() => { cleanup(); gitOperations.clear(); vi.useRealTimers(); });

describe('GitProgressPopover outcomes', () => {
	it('shows a stopped pull with its conflicting files and a working "Resolve with agent"', async () => {
		const resolve = vi.fn();
		render(GitProgressPopover);
		gitOperations.begin('Pull', [{ id: 'pull', label: 'Pull from upstream' }]);
		gitOperations.setStep('pull', 'running');
		gitOperations.fail('Pull from origin/main was stopped due to conflicts in README.md, app/x.cs. Nothing was changed.', {
			stepId: 'pull',
			files: { label: 'Conflicting files', paths: ['README.md', 'app/x.cs'] },
			actions: [{ id: 'resolve-agent', label: 'Resolve with agent', primary: true, onClick: resolve }]
		});

		const status = await screen.findByRole('status');
		expect(status.textContent).toContain('Nothing was changed.');
		const list = screen.getByRole('list', { name: 'Conflicting files' });
		expect(list.textContent).toContain('README.md');
		expect(list.textContent).toContain('app/x.cs');
		const button = screen.getByRole('button', { name: 'Resolve with agent' });
		expect((button as HTMLButtonElement).disabled).toBe(false);
		await fireEvent.click(button);
		expect(resolve).toHaveBeenCalledTimes(1);
		// The outcome stays until the user dismisses it.
		expect(screen.getByRole('button', { name: 'Dismiss' })).toBeTruthy();
	});

	it('keeps an unavailable follow-up on screen, disabled, with the reason beside it', async () => {
		const resolve = vi.fn();
		render(GitProgressPopover);
		gitOperations.report('Pull', 'Pull stopped on conflicts.', {
			actions: [{ id: 'resolve-agent', label: 'Resolve with agent', onClick: resolve, disabledReason: 'Available when the current reply finishes.' }]
		});
		const button = await screen.findByRole('button', { name: 'Resolve with agent' });
		expect((button as HTMLButtonElement).disabled).toBe(true);
		expect(button.getAttribute('aria-describedby')).toBeTruthy();
		expect(document.getElementById(button.getAttribute('aria-describedby')!)?.textContent).toContain('Available when the current reply finishes.');

		// Availability updates in place once the reply finishes.
		gitOperations.setAction('resolve-agent', { disabledReason: null });
		await vi.waitFor(() => expect((screen.getByRole('button', { name: 'Resolve with agent' }) as HTMLButtonElement).disabled).toBe(false));
		expect(screen.queryByText(/Available when the current reply finishes/)).toBeNull();
	});

	it('offers "Pull first" with a rejected push and dismisses cleanly', async () => {
		const pull = vi.fn();
		render(GitProgressPopover);
		gitOperations.begin('Push', [{ id: 'push', label: 'Push to remote' }]);
		gitOperations.setStep('push', 'running');
		gitOperations.fail('! [rejected] main -> main (fetch first)', {
			stepId: 'push',
			actions: [{ id: 'pull-first', label: 'Pull first', primary: true, onClick: pull }]
		});
		await fireEvent.click(await screen.findByRole('button', { name: 'Pull first' }));
		expect(pull).toHaveBeenCalledTimes(1);
		await fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
		await vi.waitFor(() => expect(screen.queryByRole('status')).toBeNull());
	});

	it('pins a success that needs attention until Dismiss, and lets a plain success collapse', async () => {
		vi.useFakeTimers();
		render(GitProgressPopover);
		gitOperations.begin('Pull', [{ id: 'pull', label: 'Pull from upstream' }]);
		gitOperations.setStep('pull', 'warning', { detail: 'Pull from origin/main succeeded but failed to restore stashed changes' });
		gitOperations.succeed(null, { pin: true });
		await vi.advanceTimersByTimeAsync(10_000);
		expect(screen.getByRole('status').textContent).toContain('failed to restore stashed changes');
		expect(screen.getByRole('button', { name: 'Dismiss' })).toBeTruthy();

		gitOperations.begin('Pull', [{ id: 'pull', label: 'Pull from upstream' }]);
		gitOperations.setStep('pull', 'done', { detail: 'Pulled from origin/main using rebase' });
		gitOperations.succeed();
		await vi.advanceTimersByTimeAsync(0);
		expect(screen.getByRole('status').textContent).toContain('Pulled from origin/main');
		expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull();
		await vi.advanceTimersByTimeAsync(5_000);
		expect(screen.queryByRole('status')).toBeNull();
	});
});
