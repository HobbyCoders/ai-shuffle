/**
 * Add Project is a modal: it names itself, takes focus, closes on Escape, has
 * a named close button, and returns focus to the opener.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
vi.mock('$lib/api/auth', () => ({ getWorkspaceConfig: vi.fn(async () => ({ is_local_mode: false })) }));
vi.mock('$lib/components/DirectoryBrowser.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import AddProjectModal from './AddProjectModal.svelte';

afterEach(() => {
	cleanup();
	document.body.innerHTML = '';
});

describe('AddProjectModal keyboard and focus', () => {
	it('is a named dialog that takes focus, closes on Escape and restores focus', async () => {
		const opener = document.createElement('button');
		document.body.appendChild(opener);
		opener.focus();
		const onClose = vi.fn();
		const view = render(AddProjectModal, { props: { open: true, onClose } });
		await tick();
		await Promise.resolve();

		const dialog = screen.getByRole('dialog', { name: 'Add Project' });
		expect(dialog.contains(document.activeElement)).toBe(true);

		await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
		expect(onClose).toHaveBeenCalledTimes(1);
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(onClose).toHaveBeenCalledTimes(2);

		await view.rerender({ open: false, onClose });
		await tick();
		expect(document.activeElement).toBe(opener);
	});
});
