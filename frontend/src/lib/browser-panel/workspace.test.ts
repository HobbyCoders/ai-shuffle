import { describe, expect, it } from 'vitest';
import { isBrowserWorkspaceState, validViewport } from './workspace';
const state = {
	runtime_id: 'runtime',
	revision: 1,
	available: true,
	busy: false,
	target_id: 'cdp-target',
	active_tab_id: 'tab',
	tabs: [{ id: 'tab', url: 'about:blank', title: 'Tab', current: true }],
	viewport: { mode: 'fixed', width: 1280, height: 800 },
	control: { owner: 'agent', viewer_id: null, pending_viewer_id: null },
};
describe('workspace wire validation', () => {
	it('accepts agent resize dimensions without allowing them as panel defaults', () => {
		for (const viewport of [
			{ mode: 'fixed', width: 1, height: 4096 },
			{ mode: 'fixed', width: 4096, height: 1 },
		]) {
			expect(isBrowserWorkspaceState({ ...state, viewport })).toBe(true);
			expect(validViewport(viewport)).toBe(false);
		}
		expect(isBrowserWorkspaceState({ ...state, viewport: { mode: 'fixed', width: 4097, height: 800 } })).toBe(false);
	});
	it('accepts authoritative occupied and empty states and both owners', () => {
		expect(isBrowserWorkspaceState(state)).toBe(true);
		expect(
			isBrowserWorkspaceState({
				...state,
				tabs: [],
				active_tab_id: null,
				target_id: null,
				available: false,
			}),
		).toBe(true);
		expect(
			isBrowserWorkspaceState({
				...state,
				control: {
					owner: 'human',
					viewer_id: 'viewer',
					pending_viewer_id: null,
				},
			}),
		).toBe(true);
		expect(
			isBrowserWorkspaceState({
				...state,
				control: {
					owner: 'agent',
					viewer_id: null,
					pending_viewer_id: 'viewer',
				},
			}),
		).toBe(true);
	});
	it.each([
		null,
		[],
		{},
		...[
			{ runtime_id: '' },
			{ runtime_id: 3 },
			{ revision: NaN },
			{ revision: Infinity },
			{ revision: -1 },
			{ revision: 1.5 },
			{ revision: Number.MAX_SAFE_INTEGER + 1 },
			{ available: 'yes' },
			{ busy: 0 },
			{ target_id: {} },
			{ active_tab_id: 'absent' },
			{ tabs: [] },
			{ tabs: [...state.tabs, ...state.tabs] },
			{ tabs: [{ ...state.tabs[0], current: false }] },
			{ tabs: [{ ...state.tabs[0], title: 0 }] },
			{ tabs: [{ ...state.tabs[0], url: null }] },
			{ tabs: [{ ...state.tabs[0], id: '' }] },
			{ tabs: state.tabs, active_tab_id: null },
			{ viewport: null },
			{ viewport: { mode: 'fixed', width: 1280, height: Infinity } },
			{ control: null },
			{ control: { owner: 'robot', viewer_id: null, pending_viewer_id: null } },
			{ control: { owner: 'human', viewer_id: null, pending_viewer_id: null } },
			{
				control: {
					owner: 'agent',
					viewer_id: 'viewer',
					pending_viewer_id: null,
				},
			},
			{ control: { owner: 'agent', viewer_id: null, pending_viewer_id: '' } },
			{ control: { owner: 'agent', viewer_id: null } },
		].map((patch) => ({ ...state, ...patch })),
	])('rejects malformed state %#', (value) => {
		expect(isBrowserWorkspaceState(value)).toBe(false);
	});
	it.each([
		{ mode: 'auto', width: 1280, height: 800 },
		{ mode: 'fixed', width: 239, height: 800 },
		{ mode: 'fixed', width: 3841, height: 800 },
		{ mode: 'fixed', width: 1280, height: 239 },
		{ mode: 'fixed', width: 1280, height: 2161 },
		{ mode: 'fit', width: 240.5, height: 800 },
		{ mode: 'fit', width: '240', height: 800 },
	])('rejects invalid viewport %#', (value) =>
		expect(validViewport(value)).toBe(false),
	);
});
