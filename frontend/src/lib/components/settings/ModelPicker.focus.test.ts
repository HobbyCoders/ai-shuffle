/**
 * Where focus goes when ModelPicker opens and closes (#1339), and closing the
 * panel when the picker stops being shown (#1345).
 *
 * The panel moves its highlight with the arrow keys from its search box. With
 * a mouse and keyboard, opening the picker focuses that box so Down and Up
 * work at once; on a touch-first (coarse pointer) device focus stays on the
 * trigger so no on-screen keyboard covers the list.
 */
import { cleanup, fireEvent, render } from '@testing-library/svelte';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get } }));

import { hydrateModelRegistry } from '$lib/constants/models';
import ModelPicker from './ModelPicker.svelte';

beforeAll(async () => {
	mocks.get.mockResolvedValueOnce({
		models: [
			{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports_1m: false },
			{ value: 'haiku', label: 'Haiku', family: 'haiku', supports_1m: false }
		],
		default: 'sonnet',
		legacy_map: {},
		claude: { auth_kind: 'subscription', plan_type: null },
		providers: []
	});
	await hydrateModelRegistry();
});

afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
	document.body.innerHTML = '';
});

function pointer(kind: 'fine' | 'coarse') {
	vi.stubGlobal('matchMedia', (query: string) => ({
		matches: query === `(pointer: ${kind})`,
		media: query,
		addEventListener: vi.fn(),
		removeEventListener: vi.fn()
	}));
}

async function openPicker() {
	const utils = render(ModelPicker, { props: { value: 'sonnet', onSelect: vi.fn(), allowCustom: false } });
	const trigger = utils.getByRole('button', { name: 'Sonnet' });
	trigger.focus();
	await fireEvent.click(trigger);
	return { ...utils, trigger };
}

const highlightedRow = (root: HTMLElement) => root.querySelector<HTMLElement>('.mpp-row.highlighted');

describe('ModelPicker focus on open (#1339)', () => {
	it('focuses the search box with a fine pointer, so Down and Up move the highlight at once', async () => {
		pointer('fine');
		const { container, getByLabelText } = await openPicker();
		const search = getByLabelText('Search models');
		expect(document.activeElement).toBe(search);

		await fireEvent.keyDown(document.activeElement as Element, { key: 'ArrowDown' });
		const first = highlightedRow(container);
		expect(first).not.toBeNull();
		expect(search.getAttribute('aria-activedescendant')).toBe(first?.id);

		await fireEvent.keyDown(document.activeElement as Element, { key: 'ArrowDown' });
		const second = highlightedRow(container);
		expect(second).not.toBeNull();
		expect(second).not.toBe(first);

		await fireEvent.keyDown(document.activeElement as Element, { key: 'ArrowUp' });
		expect(highlightedRow(container)).toBe(first);
	});

	it('leaves focus on the trigger with a coarse pointer, so no on-screen keyboard opens', async () => {
		pointer('coarse');
		const { container, trigger } = await openPicker();
		expect(container.querySelector('.mpk-panel')).not.toBeNull();
		expect(document.activeElement).toBe(trigger);
	});

	it('Escape from the search box closes the panel and returns focus to the trigger', async () => {
		pointer('fine');
		const { container, trigger } = await openPicker();
		expect(document.activeElement).not.toBe(trigger);

		await fireEvent.keyDown(document.activeElement as Element, { key: 'Escape' });
		expect(container.querySelector('.mpk-panel')).toBeNull();
		expect(trigger.getAttribute('aria-expanded')).toBe('false');
		expect(document.activeElement).toBe(trigger);
	});
});

describe('ModelPicker closes when hidden (#1345)', () => {
	it('closes when the picker stops being shown, as when Settings is hidden behind the chat', async () => {
		pointer('fine');
		const observer = { notify: () => {} };
		vi.stubGlobal(
			'ResizeObserver',
			class {
				constructor(cb: () => void) {
					observer.notify = cb;
				}
				observe() {}
				disconnect() {}
			}
		);
		const { container, trigger } = await openPicker();
		expect(trigger.getAttribute('aria-expanded')).toBe('true');
		const root = container.querySelector('.mpk') as HTMLElement;

		// Still shown: a resize alone does not close it.
		root.getClientRects = () => [{}] as unknown as DOMRectList;
		observer.notify();
		await Promise.resolve();
		expect(container.querySelector('.mpk-panel')).not.toBeNull();

		// `display: none` on an ancestor: no boxes.
		root.getClientRects = () => [] as unknown as DOMRectList;
		observer.notify();
		await vi.waitFor(() => expect(trigger.getAttribute('aria-expanded')).toBe('false'));
		expect(container.querySelector('.mpk-panel')).toBeNull();
	});
});
