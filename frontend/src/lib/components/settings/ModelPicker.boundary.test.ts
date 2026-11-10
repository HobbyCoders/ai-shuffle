/**
 * ModelPicker sizes and flips its panel against the part of the page that is
 * actually visible: the viewport narrowed by any scrolling ancestor, such as
 * a dialog body that ends at the footer (#1811). Sideways it keeps inside any
 * ancestor that clips its overflow, such as the Settings detail pane (#1338).
 */
import { cleanup, fireEvent, render } from '@testing-library/svelte';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get } }));

import { hydrateModelRegistry } from '$lib/constants/models';
import ModelPicker from './ModelPicker.svelte';

beforeAll(async () => {
	mocks.get.mockResolvedValueOnce({
		models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports_1m: false }],
		default: 'sonnet',
		legacy_map: {},
		claude: { auth_kind: 'subscription', plan_type: null },
		providers: []
	});
	await hydrateModelRegistry();
});

afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
	document.body.innerHTML = '';
});

function box(top: number, bottom: number): DOMRect {
	return { top, bottom, left: 0, right: 390, width: 390, height: bottom - top, x: 0, y: top, toJSON: () => ({}) } as DOMRect;
}

/**
 * The 390x800 support dialog: its scrolling body runs from 112px to 600px
 * (the footer and bottom nav sit below), and the picker trigger is at 420px.
 * `scrolls` false renders the same trigger with no scrolling ancestor.
 */
async function openPicker({ scrolls = true, triggerTop = 420 } = {}) {
	vi.spyOn(window, 'innerHeight', 'get').mockReturnValue(800);
	const body = document.createElement('div');
	body.style.overflowY = scrolls ? 'auto' : 'visible';
	document.body.appendChild(body);
	body.getBoundingClientRect = () => box(112, 600);

	const { container, getByRole } = render(ModelPicker, { target: body, props: { value: 'sonnet', onSelect: vi.fn() } });
	const root = container.querySelector('.mpk') as HTMLElement;
	root.getBoundingClientRect = () => box(triggerTop, triggerTop + 36);

	await fireEvent.click(getByRole('button', { name: 'Sonnet' }));
	const panel = container.querySelector('.mpk-panel') as HTMLElement;
	return { panel, height: parseFloat(panel.style.getPropertyValue('--mpp-height')) };
}

describe('ModelPicker panel bounds', () => {
	it('opens upward inside a scrolling dialog body when the footer leaves no room below', async () => {
		const { panel, height } = await openPicker();
		// Below the trigger only 600 - 456 - 24 = 120px are visible; above, 284px.
		expect(panel.classList.contains('mpk-panel-up')).toBe(true);
		expect(height).toBe(284);
		// The whole panel stays inside the body: its top is at or below 112px.
		expect(420 - 4 - height).toBeGreaterThanOrEqual(112);
	});

	it('opens downward, capped to the body, when there is room below', async () => {
		const { panel, height } = await openPicker({ triggerTop: 150 });
		expect(panel.classList.contains('mpk-panel-up')).toBe(false);
		// 600 - 186 - 24 = 390 visible, so the preferred 346px fits.
		expect(height).toBe(346);
	});

	it('measures against the window when no ancestor scrolls', async () => {
		const { panel, height } = await openPicker({ scrolls: false });
		// The window has 800 - 456 - 24 = 320px below: open down at that height.
		expect(panel.classList.contains('mpk-panel-up')).toBe(false);
		expect(height).toBe(320);
	});
});

function rect(left: number, right: number, top = 100, bottom = 700): DOMRect {
	return { top, bottom, left, right, width: right - left, height: bottom - top, x: left, y: top, toJSON: () => ({}) } as DOMRect;
}

/**
 * A pane that clips sideways (like the Settings detail pane) from `paneLeft`
 * to `paneRight`, holding a trigger from `triggerLeft` to `triggerRight`.
 */
async function openInPane(opts: { viewport: number; paneLeft: number; paneRight: number; triggerLeft: number; triggerRight: number }) {
	vi.spyOn(window, 'innerHeight', 'get').mockReturnValue(800);
	vi.spyOn(window, 'innerWidth', 'get').mockReturnValue(opts.viewport);
	const pane = document.createElement('div');
	pane.style.overflowX = 'hidden';
	document.body.appendChild(pane);
	pane.getBoundingClientRect = () => rect(opts.paneLeft, opts.paneRight);

	const { container, getByRole } = render(ModelPicker, { target: pane, props: { value: 'sonnet', onSelect: vi.fn() } });
	const root = container.querySelector('.mpk') as HTMLElement;
	root.getBoundingClientRect = () => rect(opts.triggerLeft, opts.triggerRight, 200, 236);

	await fireEvent.click(getByRole('button', { name: 'Sonnet' }));
	const panel = container.querySelector('.mpk-panel') as HTMLElement;
	const width = panel.style.getPropertyValue('--mpp-width');
	return { panel, startAligned: panel.classList.contains('mpk-panel-start'), width: width ? parseFloat(width) : null };
}

describe('ModelPicker panel sideways bounds (#1338)', () => {
	it('keeps the right-anchored panel when it fits to the left of the trigger end', async () => {
		const { startAligned, width } = await openInPane({ viewport: 1280, paneLeft: 300, paneRight: 1270, triggerLeft: 700, triggerRight: 1000 });
		expect(startAligned).toBe(false);
		expect(width).toBeNull();
	});

	it('grows rightward when growing leftward would pass the pane edge', async () => {
		// The API user form at 1280: the pane starts at 714, so a 360px panel
		// ending at 1060 would start at 700, behind the pane edge.
		const { startAligned, width } = await openInPane({ viewport: 1280, paneLeft: 714, paneRight: 1270, triggerLeft: 880, triggerRight: 1060 });
		expect(startAligned).toBe(true);
		// Rightward it ends at 880 + 360 = 1240, inside 1270 - 8, at full width.
		expect(width).toBeNull();
	});

	it('narrows the panel to the roomier side when neither side fits', async () => {
		// Phone width: 292px room leftward (300 - 8), 282px rightward (390 - 8 - 100).
		const { startAligned, width } = await openInPane({ viewport: 390, paneLeft: 0, paneRight: 390, triggerLeft: 100, triggerRight: 300 });
		expect(startAligned).toBe(false);
		expect(width).toBe(292);
	});
});
