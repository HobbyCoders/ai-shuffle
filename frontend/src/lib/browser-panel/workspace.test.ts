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
	it('accepts optional text metrics within the runtime limits', () => {
		const field = { id: 'f', type: 'text', label: 'Name', value: '', x: 0, y: 0, width: 100, height: 20 };
		const style = { fontSize: 14, lineHeight: null, paddingLeft: 12, paddingTop: 4, fontFamily: 'Arial, sans-serif' };
		for (const s of [style, { ...style, lineHeight: 21 }, { ...style, borderLeft: 2, borderTop: 0 },
			// Real pages: font-size:0 hidden inputs, huge type, wide padding and borders.
			{ ...style, fontSize: 0 }, { ...style, fontSize: 450 }, { ...style, paddingLeft: 420 }, { ...style, borderLeft: 120 }, { ...style, fontSize: 1000, fontFamily: 'x'.repeat(200) }]) {
			const editors = [{ ...field, style: s }];
			expect(isBrowserWorkspaceState({ ...state, editors })).toBe(true);
			expect(editors[0].style).toEqual(s);
		}
	});
	it('drops an unusable style from that field only and never fails the update', () => {
		const style = { fontSize: 14, lineHeight: null, paddingLeft: 12, paddingTop: 4, fontFamily: 'Arial, sans-serif' };
		const field = (id: string, s?: unknown) => ({ id, type: 'text', label: id, value: '', x: 0, y: 0, width: 100, height: 20, ...(s === undefined ? {} : { style: s }) });
		for (const bad of [null, 'big', { ...style, fontSize: -1 }, { ...style, fontSize: 1001 }, { ...style, paddingLeft: -1 }, { ...style, lineHeight: 'normal' },
			{ ...style, fontFamily: 'x'.repeat(201) }, { ...style, paddingTop: Infinity }, { ...style, borderLeft: NaN }, { ...style, borderTop: '1px' }, { ...style, borderTop: null }]) {
			const value = { ...state, editors: [field('good', style), field('bad', bad)] };
			expect(isBrowserWorkspaceState(value)).toBe(true);
			expect(value.editors[0].style).toEqual(style);
			expect('style' in value.editors[1]).toBe(false);
		}
		// The rest of a field is still checked as strictly as before.
		expect(isBrowserWorkspaceState({ ...state, editors: [{ ...field('f', style), width: 0 }] })).toBe(false);
	});
	it('accepts a short plain notice and rejects anything else', () => {
		expect(isBrowserWorkspaceState({ ...state, notice: 'This step needs your own browser.' })).toBe(true);
		for (const notice of [3, null, 'x'.repeat(513)]) {
			expect(isBrowserWorkspaceState({ ...state, notice })).toBe(false);
		}
	});
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
