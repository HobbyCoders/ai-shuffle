import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import SessionSidePanel from './SessionSidePanel.svelte';

afterEach(cleanup);
function mount(active: 'browser' | 'changes' = 'changes') {
	const props = { active, expanded: false, width: 640, browserAvailable: true, changesAvailable: true,
		onSelect: vi.fn(), onClose: vi.fn(), onResize: vi.fn(), onExpand: vi.fn(),
		children: createRawSnippet(() => ({ render: () => '<div>Panel content</div>' })) };
	return { ...render(SessionSidePanel, props), props };
}
describe('session side panel controls', () => {
	it('keeps one close button and switches content through the shared header', async () => {
		const { props } = mount();
		expect(screen.getAllByRole('complementary')).toHaveLength(1);
		expect(screen.getByRole('button', { name: 'Changes' }).getAttribute('aria-pressed')).toBe('true');
		await fireEvent.click(screen.getByRole('button', { name: 'Browser' }));
		expect(props.onSelect).toHaveBeenCalledWith('browser');
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
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(props.onClose).toHaveBeenCalledOnce();
		await rerender({ ...props, active: 'changes' });
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(props.onClose).toHaveBeenCalledOnce();
	});
	it('keeps the close and switch controls on mobile without resizing', async () => {
		const { props, rerender } = mount();
		await rerender({ ...props, mobile: true, browserAvailable: false });
		expect(screen.queryByRole('button', { name: 'Browser' })).toBeNull();
		expect(screen.queryByRole('button', { name: 'Expand side panel' })).toBeNull();
		expect(screen.queryByRole('separator')).toBeNull();
		expect(screen.getByRole('button', { name: 'Close side panel' })).toBeTruthy();
	});
});
