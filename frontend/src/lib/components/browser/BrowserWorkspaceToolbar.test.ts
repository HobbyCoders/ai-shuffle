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
		expect(view.queryByRole('button', { name: 'Return control' })).toBeNull();
		expect(onCommand).not.toHaveBeenCalled();
	});
	it('allows the pending viewer to cancel without requesting another takeover', async () => {
		const onCommand = vi.fn();
		const view = render(BrowserWorkspaceToolbar, {
			workspace: {
				...state,
				control: { owner: 'agent', viewer_id: null, pending_viewer_id: 'v' },
				busy: true,
			},
			viewerId: 'v',
			connected: true,
			onCommand,
		});
		expect(view.getByText(/Requesting control/)).toBeTruthy();
		await fireEvent.click(view.getByRole('button', { name: 'Return control' }));
		expect(onCommand).toHaveBeenCalledWith('release', {});
		expect(view.queryByText('Take control')).toBeNull();
	});
	it('explains microphone limits and links to the active page in a normal browser', async () => {
		const onCommand = vi.fn();
		const view = render(BrowserWorkspaceToolbar, {
			workspace: { ...state, active_tab_id: 'a', tabs: [{ id: 'a', title: 'Voice', url: 'https://voice.test', current: true }] },
			viewerId: 'v', connected: true, onCommand,
		});
		await fireEvent.click(view.getByLabelText('Browser settings'));
		expect(view.getByText('Microphone capture is unsupported in this panel.')).toBeTruthy();
		expect(view.getByText(/worker, which may be another machine/)).toBeTruthy();
		expect(view.getByText(/Desktop app microphone permissions are separate/)).toBeTruthy();
		const link = view.getByRole('link', { name: 'Open this page in your normal browser to use your microphone' });
		expect(link.getAttribute('href')).toBe('https://voice.test');
		expect(link.getAttribute('target')).toBe('_blank');
		expect(link.getAttribute('rel')).toBe('noopener noreferrer');
		expect(onCommand).not.toHaveBeenCalled();
	});
	it.each(['javascript:alert(1)', 'file:///private', 'chrome://settings'])('does not link to an unsafe capture alternative: %s', async (url) => {
		const view = render(BrowserWorkspaceToolbar, {
			workspace: { ...state, active_tab_id: 'a', tabs: [{ id: 'a', title: 'Page', url, current: true }] },
			viewerId: 'v', connected: true, onCommand: vi.fn(),
		});
		await fireEvent.click(view.getByLabelText('Browser settings'));
		expect(view.queryByRole('link')).toBeNull();
		expect(view.getByText('Open the site in your normal browser to use your microphone.')).toBeTruthy();
	});
});
