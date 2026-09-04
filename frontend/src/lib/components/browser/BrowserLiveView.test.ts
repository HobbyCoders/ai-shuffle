import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import BrowserLiveView from './BrowserLiveView.svelte';

beforeEach(() => {
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('createImageBitmap', vi.fn().mockResolvedValue({ close: vi.fn() }));
	vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
	vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue({
		left: 0, top: 0, width: 300, height: 500, right: 300, bottom: 500, x: 0, y: 0, toJSON() {},
	});
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function setup(props: { interactive?: boolean; inspecting?: boolean } = {}) {
	const onInput = vi.fn();
	const onInspect = vi.fn();
	const view = render(BrowserLiveView, { props: {
		frame: { data: 'AA==', metadata: { deviceWidth: 300, deviceHeight: 500 } },
		onInput, onInspect, ...props,
	} });
	await tick();
	await Promise.resolve();
	const surface = screen.getByRole('application');
	const input = screen.getByRole('textbox') as HTMLTextAreaElement;
	return { ...view, onInput, onInspect, surface, input };
}
function pointer(target: HTMLElement, type: string, x = 50, y = 60) {
	const event = new Event(type, { bubbles: true, cancelable: true });
	Object.assign(event, { pointerType: 'touch', pointerId: 1, clientX: x, clientY: y });
	target.dispatchEvent(event);
}
function before(input: HTMLTextAreaElement, inputType: string) {
	const event = new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType });
	input.dispatchEvent(event);
	return event;
}
async function open() { await fireEvent.click(screen.getByRole('button', { name: 'Open keyboard' })); }
async function text(input: HTMLTextAreaElement, value: string, inputType = 'insertText') {
	input.value = value;
	await fireEvent.input(input, { data: value, inputType });
}

describe('native remote-browser keyboard bridge', () => {
	it('focuses a native editable synchronously on touch tap, after the remote click', async () => {
		const { surface, input, onInput } = await setup();
		pointer(surface, 'pointerdown');
		expect(document.activeElement).not.toBe(input);
		pointer(surface, 'pointerup');
		expect(document.activeElement).toBe(input);
		expect(onInput.mock.calls.map(([p]) => p.event)).toEqual(['move', 'down', 'up']);
	});
	it('preserves touch scrolling without opening the keyboard, including a drag back to its start', async () => {
		const { surface, input, onInput } = await setup();
		pointer(surface, 'pointerdown');
		pointer(surface, 'pointermove', 50, 100);
		pointer(surface, 'pointermove', 50, 60);
		pointer(surface, 'pointerup');
		expect(document.activeElement).not.toBe(input);
		expect(onInput.mock.calls.map(([p]) => p.event)).toEqual(['wheel', 'wheel']);
	});
	it('does not open on cancelled touch', async () => {
		const { surface, input, onInput } = await setup();
		pointer(surface, 'pointerdown'); pointer(surface, 'pointercancel'); pointer(surface, 'pointerup');
		expect(document.activeElement).not.toBe(input);
		expect(onInput).not.toHaveBeenCalled();
	});
	it('offers an accessible open/dismiss toggle without remote pointer forwarding', async () => {
		const { input, onInput } = await setup();
		await open();
		expect(document.activeElement).toBe(input);
		const dismiss = screen.getByRole('button', { name: 'Dismiss keyboard' });
		pointer(dismiss, 'pointerdown'); pointer(dismiss, 'pointerup');
		await fireEvent.click(dismiss);
		expect(document.activeElement).not.toBe(input);
		expect(onInput).not.toHaveBeenCalled();
		expect(screen.getByRole('button', { name: 'Open keyboard' }).getAttribute('aria-pressed')).toBe('false');
	});
	it('sends Unicode text once, clears the buffer and disables native text assistance', async () => {
		const { input, onInput } = await setup(); await open();
		await fireEvent.keyDown(input, { key: 'a', code: 'KeyA' });
		await text(input, 'a');
		await fireEvent.keyUp(input, { key: 'a', code: 'KeyA' });
		await text(input, '👩🏽‍💻');
		expect(onInput.mock.calls.map(([p]) => p)).toEqual([
			{ type: 'input', kind: 'text', text: 'a' }, { type: 'input', kind: 'text', text: '👩🏽‍💻' },
		]);
		expect(input.value).toBe('');
		for (const attr of ['autocomplete', 'autocorrect', 'autocapitalize']) expect(input.getAttribute(attr)).toBe('off');
		expect(input.getAttribute('spellcheck')).toBe('false');
	});
	it.each([
		'a'.repeat(16384),
		'a'.repeat(16384 * 2 + 7),
		'a'.repeat(16383) + '😀' + 'b'.repeat(16382) + '🚀',
	])('chunks a long paste without duplicate text or split surrogate pairs (%#)', async (value) => {
		const { input, onInput } = await setup(); await open();
		expect(before(input, 'insertFromPaste').defaultPrevented).toBe(false);
		await text(input, value, 'insertFromPaste');
		const chunks = onInput.mock.calls.map(([payload]) => {
			expect(payload).toEqual({ type: 'input', kind: 'text', text: expect.any(String) });
			return payload.text as string;
		});
		expect(chunks.join('')).toBe(value);
		expect(chunks.length).toBe(Math.ceil(value.length / 16384));
		for (const chunk of chunks) {
			expect(chunk.length).toBeGreaterThan(0);
			expect(chunk.length).toBeLessThanOrEqual(16384);
			expect(chunk).not.toMatch(/^[\uDC00-\uDFFF]|[\uD800-\uDBFF]$/u);
		}
		expect(input.value).toBe('');
		await text(input, '');
		expect(onInput).toHaveBeenCalledTimes(chunks.length);
	});
	it.each([false, true])('forwards Tab with shift=%s once and keeps native focus', async (shiftKey) => {
		const { input, onInput } = await setup(); await open();
		for (const event of ['keydown', 'keyup']) {
			const key = new KeyboardEvent(event, { key: 'Tab', code: 'Tab', keyCode: 9, shiftKey, bubbles: true, cancelable: true });
			input.dispatchEvent(key);
			expect(key.defaultPrevented).toBe(true);
		}
		expect(document.activeElement).toBe(input);
		expect(onInput.mock.calls.map(([payload]) => payload)).toEqual(['down', 'up'].map((event) => ({
			type: 'input', kind: 'key', event, key: 'Tab', code: 'Tab', keyCode: 9, text: '', modifiers: shiftKey ? 8 : 0,
		})));
	});
	it('commits composition exactly once whether input precedes or follows compositionend', async () => {
		const { input, onInput } = await setup(); await open();
		await fireEvent.compositionStart(input);
		input.value = 'に';
		await fireEvent.input(input, { data: 'に', isComposing: true, inputType: 'insertCompositionText' });
		expect(onInput).not.toHaveBeenCalled();
		input.value = '日本';
		await fireEvent.input(input, { data: '日本', isComposing: false });
		await fireEvent.compositionEnd(input, { data: '日本' });
		await text(input, '日本', 'insertFromComposition');
		expect(onInput.mock.calls.map(([p]) => p)).toEqual([{ type: 'input', kind: 'text', text: '日本' }]);
		expect(input.value).toBe('');
		await fireEvent.keyDown(input, { key: '日' });
		await text(input, '日');
		expect(onInput).toHaveBeenCalledTimes(2);
	});
	it('forwards deletion from an empty buffer and soft Enter via beforeinput', async () => {
		const { input, onInput } = await setup(); await open();
		for (const kind of ['deleteContentBackward', 'deleteContentForward', 'insertLineBreak']) {
			expect(before(input, kind).defaultPrevented).toBe(true);
		}
		expect(onInput.mock.calls.map(([p]) => [p.key, p.event])).toEqual([
			['Backspace', 'down'], ['Backspace', 'up'], ['Delete', 'down'], ['Delete', 'up'], ['Enter', 'down'], ['Enter', 'up'],
		]);
	});
	it('handles named keys separately without bubbling duplicates, and Escape dismisses', async () => {
		const { input, onInput } = await setup(); await open();
		for (const key of ['Enter', 'ArrowLeft', 'Backspace']) {
			expect(await fireEvent.keyDown(input, { key })).toBe(false);
			await fireEvent.keyUp(input, { key });
		}
		expect(onInput).toHaveBeenCalledTimes(6);
		expect(onInput.mock.calls.every(([p]) => p.kind === 'key' && p.text === '')).toBe(true);
		await fireEvent.keyDown(input, { key: 'Escape' });
		expect(document.activeElement).not.toBe(input);
	});
	it.each([{ inspecting: true }, { interactive: false }])('blocks text/tap keyboard when %o', async (props) => {
		const { surface, input, onInput, onInspect } = await setup(props);
		pointer(surface, 'pointerdown'); pointer(surface, 'pointerup');
		await text(input, 'not sent');
		expect(input.disabled).toBe(true);
		expect(document.activeElement).not.toBe(input);
		expect(onInput).not.toHaveBeenCalled();
		if (props.inspecting) expect(onInspect).toHaveBeenCalledWith(50, 60, 'select');
	});
	it('blurs and discards pending composition on entering inspect mode', async () => {
		const { input, rerender, onInput } = await setup(); await open();
		await fireEvent.compositionStart(input); input.value = 'draft';
		await rerender({ inspecting: true });
		await fireEvent.compositionEnd(input, { data: 'draft' });
		expect(input.value).toBe('');
		expect(document.activeElement).not.toBe(input);
		expect(onInput).not.toHaveBeenCalled();
	});
	it('leaves desktop wrapper key forwarding unchanged', async () => {
		const { surface, onInput } = await setup(); surface.focus();
		await fireEvent.keyDown(surface, { key: 'a', code: 'KeyA', keyCode: 65 });
		await fireEvent.keyUp(surface, { key: 'a', code: 'KeyA', keyCode: 65 });
		expect(onInput.mock.calls.map(([p]) => [p.kind, p.event, p.text])).toEqual([
			['key', 'down', 'a'], ['key', 'up', ''],
		]);
	});
});
