import { render, fireEvent, cleanup } from '@testing-library/svelte';
import { afterEach, describe, it, expect, vi } from 'vitest';
import BrowserWorkspaceToolbar from './BrowserWorkspaceToolbar.svelte';
import type { BrowserWorkspaceState } from '$lib/browser-panel/workspace';
afterEach(cleanup);
const state: BrowserWorkspaceState = {
	runtime_id: 'r',
	revision: 1,
	available: true,
	target_id: null,
	active_tab_id: null,
	tabs: [],
	viewport: { mode: 'fixed', width: 1280, height: 800 },
	control: { owner: 'human', viewer_id: 'v', pending_viewer_id: null },
	busy: false,
};
describe('shared workspace toolbar', () => {
 it('hides the last tab close button and closes only the requested extra tab', async () => {
  const onCommand = vi.fn();
  const tab = { id: 'a', title: 'Alpha', url: 'https://a.test', current: true };
  const view = render(BrowserWorkspaceToolbar, { workspace: { ...state, active_tab_id: 'a', tabs: [tab] }, viewerId: 'v', connected: true, onCommand });
  expect(view.queryByRole('button', { name: 'Close Alpha' })).toBeNull();
  await view.rerender({ workspace: { ...state, active_tab_id: 'a', tabs: [tab, { ...tab, id: 'b', title: 'Beta', current: false }] } });
  await fireEvent.click(view.getByRole('button', { name: 'Close Beta' }));
  expect(onCommand).toHaveBeenCalledWith('tabs', { action: 'close', tab_id: 'b' });
  expect(view.getByRole('navigation').contains(view.getByRole('button', { name: 'New tab' }))).toBe(true);
 });
	it('allows first tab creation without a stream frame', async () => {
		const onCommand = vi.fn();
		const view = render(BrowserWorkspaceToolbar, {
			workspace: state,
			viewerId: 'v',
			connected: true,
			onCommand,
		});
		await fireEvent.click(view.getByRole('button', { name: 'New tab' }));
		expect(onCommand).toHaveBeenCalledWith('tabs', { action: 'create' });
		expect(view.getByText('You control')).toBeTruthy();
	});
	it('is read-only for another viewer and never applies defaults on mount', () => {
		const onCommand = vi.fn();
		const view = render(BrowserWorkspaceToolbar, {
			workspace: state,
			viewerId: 'other',
			connected: true,
			onCommand,
		});
		expect((view.getByRole('button', { name: 'New tab' }) as HTMLButtonElement).disabled).toBe(
			true,
		);
		expect(view.getByText('Another viewer controls')).toBeTruthy();
		expect(onCommand).not.toHaveBeenCalled();
	});
	it('shows pending control without manual ownership buttons', () => {
		const view = render(BrowserWorkspaceToolbar, {
			workspace: {
				...state,
				control: { owner: 'agent', viewer_id: null, pending_viewer_id: 'v' },
				busy: true,
			},
			viewerId: 'v',
			connected: true,
			onCommand: vi.fn(),
		});
		expect(view.getByText(/Requesting control/)).toBeTruthy();
		expect(view.queryByText('Return control')).toBeNull();
		expect(view.queryByText('Take control')).toBeNull();
	});
});
