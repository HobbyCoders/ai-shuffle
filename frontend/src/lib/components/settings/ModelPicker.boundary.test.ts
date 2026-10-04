/**
 * ModelPicker sizes and flips its panel against the part of the page that is
 * actually visible: the viewport narrowed by any scrolling ancestor, such as
 * a dialog body that ends at the footer (#1811).
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
		// Below the trigger only 600 - 456 - 12 = 132px are visible; above, 296px.
		expect(panel.classList.contains('mpk-panel-up')).toBe(true);
		expect(height).toBe(296);
		// The whole panel stays inside the body: its top is at or below 112px.
		expect(420 - 4 - height).toBeGreaterThanOrEqual(112);
	});

	it('opens downward, capped to the body, when there is room below', async () => {
		const { panel, height } = await openPicker({ triggerTop: 150 });
		expect(panel.classList.contains('mpk-panel-up')).toBe(false);
		// 600 - 186 - 12 = 402 visible, so the preferred 346px fits.
		expect(height).toBe(346);
	});

	it('measures against the window when no ancestor scrolls', async () => {
		const { panel, height } = await openPicker({ scrolls: false });
		// The window has 800 - 456 - 12 = 332px below: open down at that height.
		expect(panel.classList.contains('mpk-panel-up')).toBe(false);
		expect(height).toBe(332);
	});
});
