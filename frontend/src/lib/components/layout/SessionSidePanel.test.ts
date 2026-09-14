import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import SessionSidePanel from './SessionSidePanel.test.svelte';
import { createLazyComponent } from '../deck/lazy/lazy-component';
import Stub from '../deck/lazy/__fixtures__/StubPanel.svelte';

afterEach(cleanup);
function mount(active: 'browser' | 'changes' = 'changes') {
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
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(props.onClose).toHaveBeenCalledOnce();
		await rerender({ ...props, active: 'changes' });
		await fireEvent.keyDown(window, { key: 'Escape' });
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
});
