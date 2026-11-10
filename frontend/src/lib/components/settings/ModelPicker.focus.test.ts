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

describe('ModelPicker closes when focus Tabs out of it (#1407)', () => {
	it('closes when focus moves to a control outside the picker, and leaves focus there', async () => {
		pointer('fine');
		const back = document.createElement('button');
		back.textContent = 'Back to Default models';
		document.body.prepend(back);
		const { container, trigger } = await openPicker();
		const search = document.activeElement as HTMLElement;

		// Shift+Tab to the trigger: still the picker, still open.
		trigger.focus();
		await fireEvent.focusOut(search, { relatedTarget: trigger });
		expect(container.querySelector('.mpk-panel')).not.toBeNull();

		// Shift+Tab again, out to the page.
		back.focus();
		await fireEvent.focusOut(trigger, { relatedTarget: back });
		expect(container.querySelector('.mpk-panel')).toBeNull();
		expect(trigger.getAttribute('aria-expanded')).toBe('false');
		expect(document.activeElement).toBe(back);
	});

	it('stays open when focus moves within the panel or goes nowhere', async () => {
		pointer('fine');
		const { container } = await openPicker();
		const search = document.activeElement as HTMLElement;
		const star = container.querySelector<HTMLButtonElement>('.mpk-panel .mpp-row button')!;

		// A favourite star keeps focus when pressed.
		star.focus();
		await fireEvent.focusOut(search, { relatedTarget: star });
		// A press on plain panel text, or the window losing focus: no new target.
		await fireEvent.focusOut(star, { relatedTarget: null });
		expect(container.querySelector('.mpk-panel')).not.toBeNull();
	});

	it('does not react to focus moves while closed', async () => {
		pointer('fine');
		const outside = document.createElement('button');
		document.body.append(outside);
		const { container, trigger } = await openPicker();
		await fireEvent.keyDown(document.activeElement as Element, { key: 'Escape' });
		expect(document.activeElement).toBe(trigger);

		outside.focus();
		await fireEvent.focusOut(trigger, { relatedTarget: outside });
		expect(container.querySelector('.mpk-panel')).toBeNull();
		expect(document.activeElement).toBe(outside);
	});
});

describe('ModelPicker keeps the keyboard after a press inside the panel', () => {
	/** True when the press's default (moving focus) was cancelled. */
	const press = (el: Element, button = 0) => !el.dispatchEvent(new MouseEvent('mousedown', { button, bubbles: true, cancelable: true }));

	it('keeps focus in place on a press on a group name or the list, not on fields, stars or rows', async () => {
		pointer('fine');
		const { container, getByLabelText } = await openPicker();
		const panel = container.querySelector<HTMLElement>('.mpk-panel')!;
		expect(press(panel.querySelector('.mpp-group-name')!)).toBe(true);
		expect(press(panel.querySelector('.mpp-list')!)).toBe(true);
		// The caret and text selection in the search box, the stars and the rows
		// keep the browser's own press.
		expect(press(getByLabelText('Search models'))).toBe(false);
		expect(press(panel.querySelector('.mpp-row .mpp-fav')!)).toBe(false);
		expect(press(panel.querySelector('.mpp-row .mpp-row-label')!)).toBe(false);
		// Middle and right presses move focus too; cancelling the press does not
		// stop the context menu, which has its own event.
		expect(press(panel.querySelector('.mpp-group-name')!, 1)).toBe(true);
		expect(press(panel.querySelector('.mpp-group-name')!, 2)).toBe(true);
		expect(container.querySelector('.mpk-panel')).not.toBeNull();
	});

	it('leaves a touch tap alone, so a tap on plain text still closes the on-screen keyboard', async () => {
		pointer('fine');
		const { container } = await openPicker();
		const label = container.querySelector('.mpk-panel .mpp-group-name')!;
		const pointerDown = (pointerType: string) =>
			label.dispatchEvent(new PointerEvent('pointerdown', { pointerType, bubbles: true }));
		pointerDown('touch');
		expect(press(label)).toBe(false);
		pointerDown('mouse');
		expect(press(label)).toBe(true);
	});

	it('Escape returns focus to the trigger even after focus was lost to the page', async () => {
		pointer('fine');
		const { container, trigger } = await openPicker();
		(document.activeElement as HTMLElement).blur();
		expect(document.activeElement).toBe(document.body);
		await fireEvent.keyDown(document.body, { key: 'Escape' });
		expect(container.querySelector('.mpk-panel')).toBeNull();
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
