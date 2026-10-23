import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import SessionSidePanel from './SessionSidePanel.test.svelte';
import { createLazyComponent } from '../deck/lazy/lazy-component';
import { registerShortcut, clearAllShortcuts } from '$lib/services/keyboard';
import { closeModalShortcut } from './close-modal-shortcut';
import ConfirmDialog from '../ui/ConfirmDialog.svelte';
import Stub from '../deck/lazy/__fixtures__/StubPanel.svelte';

/** The panel decides once the key event has reached every listener. */
const afterDispatch = () => new Promise((resolve) => setTimeout(resolve, 0));
async function pressEscape(target: Window | Element = window) {
	await fireEvent.keyDown(target, { key: 'Escape' });
	await afterDispatch();
}

afterEach(() => { cleanup(); clearAllShortcuts(); });
function mount(active: 'browser' | 'changes' | 'phone' | 'terminal' | 'artifact' = 'changes') {
	const props = { active, expanded: false, width: 640,
		onClose: vi.fn(), onResize: vi.fn(), onExpand: vi.fn() };
	return { ...render(SessionSidePanel, props), props };
}
describe('session side panel controls', () => {
	it('keeps close available while loading and offers recovery instead of a blank panel', async () => {
		const log = vi.spyOn(console, 'error').mockImplementation(() => {});
		let reject: (error: Error) => void = () => {};
		const loader = vi.fn().mockImplementationOnce(() => new Promise((_, fail) => { reject = fail; }))
			.mockResolvedValueOnce({ default: Stub });
		const handle = createLazyComponent('changed-files', loader);
		const { props, rerender } = mount();
		await rerender({ ...props, handle });
		expect(await screen.findByLabelText('Loading changed files')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Close side panel' }));
		expect(props.onClose).toHaveBeenCalledOnce();
		reject(new Error('Failed to fetch dynamically imported module'));
		expect(await screen.findByRole('alert')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Reload app' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Close side panel' })).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
		expect(await screen.findByTestId('stub-panel')).toBeTruthy();
		expect(loader).toHaveBeenCalledTimes(2);
		expect(screen.queryByRole('alert')).toBeNull();
		log.mockRestore();
	});
	it('puts controls in the content toolbar without a tab row', async () => {
		const { props } = mount();
		expect(screen.getAllByRole('complementary')).toHaveLength(1);
		expect(screen.queryByRole('group', { name: 'Side panel view' })).toBeNull();
		expect(screen.queryByRole('button', { name: 'Changes' })).toBeNull();
		expect(screen.queryByRole('button', { name: 'Browser' })).toBeNull();
		expect(document.querySelector('.side-panel-header')).toBeNull();
		expect(screen.getByRole('banner').contains(screen.getByRole('button', { name: 'Close side panel' }))).toBe(true);
		await fireEvent.click(screen.getByRole('button', { name: 'Close side panel' }));
		expect(props.onClose).toHaveBeenCalledOnce();
	});
	it('expands and restores either view', async () => {
		const { props, rerender } = mount('browser');
		await fireEvent.click(screen.getByRole('button', { name: 'Expand side panel' }));
		expect(props.onExpand).toHaveBeenCalledOnce();
		await rerender({ ...props, expanded: true });
		expect(screen.getByRole('button', { name: 'Restore side panel' })).toBeTruthy();
		expect(screen.queryByRole('separator')).toBeNull();
	});
	it('closes browser with Escape but leaves changes confirmations to the diff view', async () => {
		const { props, rerender } = mount('browser');
		await pressEscape();
		expect(props.onClose).toHaveBeenCalledOnce();
		await rerender({ ...props, active: 'changes' });
		await pressEscape();
		expect(props.onClose).toHaveBeenCalledOnce();
	});
	it('leaves Escape inside the terminal to the shell', async () => {
		const { props, container } = mount('terminal');
		const xterm = container.ownerDocument.createElement('div');
		xterm.className = 'xterm';
		xterm.tabIndex = 0;
		container.appendChild(xterm);
		await pressEscape(xterm);
		expect(props.onClose).not.toHaveBeenCalled();
		await pressEscape();
		expect(props.onClose).toHaveBeenCalledOnce();
	});
	it('keeps close in the content toolbar on mobile without resizing', async () => {
		const { props, rerender } = mount();
		await rerender({ ...props, mobile: true });
		expect(screen.queryByRole('button', { name: 'Browser' })).toBeNull();
		expect(screen.queryByRole('button', { name: 'Expand side panel' })).toBeNull();
		expect(screen.queryByRole('separator')).toBeNull();
		expect(screen.getByRole('button', { name: 'Close side panel' })).toBeTruthy();
	});
	// AppPage registers the global Escape shortcut (`close-modal`) BEFORE the
	// side panel mounts, and the shortcut manager's window listener is attached
	// first. It used to preventDefault every Escape, so the panel never saw one.
	// This drives AppPage's real shortcut definition.
	describe('with the app\'s global Escape shortcut registered first', () => {
		let modalOpen = false;
		const registerCloseModal = () => registerShortcut(closeModalShortcut([
			{ isOpen: () => false, close: () => {} },
			{ isOpen: () => modalOpen, close: () => { modalOpen = false; } },
		]));

		it.each(['phone', 'browser', 'terminal', 'artifact'] as const)('Escape closes the %s panel when no modal is open', async (view) => {
			modalOpen = false;
			registerCloseModal();
			const { props } = mount(view);
			await pressEscape();
			expect(props.onClose).toHaveBeenCalledOnce();
		});

		it('with a modal open, Escape closes only the modal and the panel stays', async () => {
			modalOpen = true;
			registerCloseModal();
			const { props } = mount('phone');
			await pressEscape();
			expect(modalOpen).toBe(false);
			expect(props.onClose).not.toHaveBeenCalled();
			// The next Escape reaches the panel.
			await pressEscape();
			expect(props.onClose).toHaveBeenCalledOnce();
		});
	});

	// A dialog opened over the panel attaches its window listener AFTER the
	// panel's, so the panel sees the Escape first. One Escape still closes only
	// the dialog.
	describe('with a dialog opened after the panel', () => {
		it.each(['browser', 'phone', 'terminal', 'artifact'] as const)('Escape closes only the dialog over the %s panel; the next closes the panel', async (view) => {
			registerShortcut(closeModalShortcut([]));
			const { props } = mount(view);
			const onCancel = vi.fn();
			const dialog = render(ConfirmDialog, { open: true, message: 'Delete it?', onConfirm: vi.fn(), onCancel });
			await pressEscape();
			expect(onCancel).toHaveBeenCalledOnce();
			expect(props.onClose).not.toHaveBeenCalled();
			await dialog.rerender({ open: false, message: 'Delete it?', onConfirm: vi.fn(), onCancel });
			await pressEscape();
			expect(onCancel).toHaveBeenCalledOnce();
			expect(props.onClose).toHaveBeenCalledOnce();
		});
	});
});
