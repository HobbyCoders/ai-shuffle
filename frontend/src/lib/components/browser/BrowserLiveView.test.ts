import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
	cleanup,
	fireEvent,
	render,
	screen,
	waitFor,
} from '@testing-library/svelte';
import { tick } from 'svelte';
import BrowserLiveView from './BrowserLiveView.svelte';

beforeEach(() => {
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
		callback(0);
		return 1;
	});
	vi.stubGlobal(
		'createImageBitmap',
		vi.fn().mockResolvedValue({ close: vi.fn() }),
	);
	vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
	vi.spyOn(
		HTMLCanvasElement.prototype,
		'getBoundingClientRect',
	).mockReturnValue({
		left: 0,
		top: 0,
		width: 300,
		height: 500,
		right: 300,
		bottom: 500,
		x: 0,
		y: 0,
		toJSON() {},
	});
});
afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

let keyboardView: BrowserLiveView;

async function setup(
	props: { interactive?: boolean; inspecting?: boolean; keyboardEnabled?: boolean } = {},
) {
	const onInput = vi.fn();
	const onInspect = vi.fn();
	const view = render(BrowserLiveView, {
		props: {
			frame: {
				data: 'AA==',
				metadata: { deviceWidth: 300, deviceHeight: 500 },
			},
			onInput,
			onInspect,
			...props,
		},
	});
	keyboardView = view.component;
	await tick();
	await Promise.resolve();
	const surface = screen.getByRole('application');
	const input = screen.getByRole('textbox') as HTMLTextAreaElement;
	return { ...view, onInput, onInspect, surface, input };
}
function pointer(target: HTMLElement, type: string, x = 50, y = 60) {
	const event = new Event(type, { bubbles: true, cancelable: true });
	Object.assign(event, {
		pointerType: 'touch',
		pointerId: 1,
		clientX: x,
		clientY: y,
	});
	target.dispatchEvent(event);
}
function before(input: HTMLTextAreaElement, inputType: string) {
	const event = new InputEvent('beforeinput', {
		bubbles: true,
		cancelable: true,
		inputType,
	});
	input.dispatchEvent(event);
	return event;
}
async function open() {
	keyboardView.openKeyboard();
	await tick();
}
async function text(
	input: HTMLTextAreaElement,
	value: string,
	inputType = 'insertText',
) {
	input.value = value;
	await fireEvent.input(input, { data: value, inputType });
}

describe('authoritative viewport frame gate', () => {
	it.each([false, true])('blocks stale mouse/touch/inspect until matching decode (inspect=%s)', async (inspecting) => {
		const { surface, onInput, onInspect, rerender } = await setup({ inspecting });
		await rerender({ viewport: { width: 600, height: 500 } });
		const act = () => {
			for (const pointerType of ['mouse', 'touch']) {
				for (const type of ['pointerdown', 'pointermove', 'pointerup']) {
					const e = new Event(type, { bubbles: true, cancelable: true });
					Object.assign(e, { pointerType, pointerId: 1, clientX: 50, clientY: 200 });
					surface.dispatchEvent(e);
				}
			}
		};
		act();
		expect(onInput).not.toHaveBeenCalled();
		expect(onInspect).not.toHaveBeenCalled();
		expect(screen.getByRole('status').textContent).toContain('Waiting for an updated frame');
		let resolve!: (bitmap: ImageBitmap) => void;
		vi.mocked(createImageBitmap).mockImplementationOnce(() => new Promise(r => { resolve = r; }));
		await rerender({ frame: { data: 'AA==', metadata: { deviceWidth: 600, deviceHeight: 500 } } });
		act(); // Metadata arrived, but the old JPEG is still on screen.
		expect(onInput).not.toHaveBeenCalled();
		expect(onInspect).not.toHaveBeenCalled();
		resolve({ width: 300, height: 250, close: vi.fn() } as unknown as ImageBitmap);
		await waitFor(() => expect(screen.queryByRole('status')).toBeNull());
		act(); // Downscaled JPEG is fine: device dimensions govern the mapping.
		if (inspecting) expect(onInspect).toHaveBeenCalledWith(100, 150, 'select');
		else expect(onInput).toHaveBeenCalledWith(expect.objectContaining({ kind: 'mouse', event: 'down', x: 100, y: 150 }));
	});
});


describe('target identity frame gate', () => {
 it('keeps the canvas but blocks old target input until the replacement decodes', async () => {
  const { container, rerender, onInput } = await setup();
  await rerender({ frame: { data: 'AA==', metadata: { deviceWidth: 300, deviceHeight: 500, runtime_id: 'r', tab_id: 'a' } } });
  await tick(); await Promise.resolve();
  const canvas = container.querySelector('canvas');
  let resolve!: (bitmap: ImageBitmap) => void;
  vi.mocked(createImageBitmap).mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  await rerender({ frame: { data: 'AA==', metadata: { deviceWidth: 300, deviceHeight: 500, runtime_id: 'r', tab_id: 'b' } } });
  pointer(screen.getByRole('application'), 'pointerdown'); pointer(screen.getByRole('application'), 'pointerup');
  expect(onInput).not.toHaveBeenCalled();
  expect(container.querySelector('canvas')).toBe(canvas);
  expect(screen.getByRole('status')).toBeTruthy();
  resolve({ close: vi.fn() } as unknown as ImageBitmap);
  await waitFor(() => expect(screen.queryByRole('status')).toBeNull());
 });
});

describe('native remote-browser keyboard bridge', () => {
	it('sends a remote tap without opening the native keyboard', async () => {
		const { surface, input, onInput } = await setup();
		pointer(surface, 'pointerdown');
		expect(document.activeElement).not.toBe(input);
		pointer(surface, 'pointerup');
		expect(document.activeElement).not.toBe(input);
		expect(onInput.mock.calls.map(([p]) => p.event)).toEqual([
			'move',
			'down',
			'up',
		]);
	});
	it('preserves touch scrolling without opening the keyboard, including a drag back to its start', async () => {
		const { surface, input, onInput } = await setup();
		pointer(surface, 'pointerdown');
		pointer(surface, 'pointermove', 50, 100);
		pointer(surface, 'pointermove', 50, 60);
		pointer(surface, 'pointerup');
		expect(document.activeElement).not.toBe(input);
		expect(onInput.mock.calls.map(([p]) => p.event)).toEqual([
			'wheel',
			'wheel',
		]);
	});
	it('does not open on cancelled touch', async () => {
		const { surface, input, onInput } = await setup();
		pointer(surface, 'pointerdown');
		pointer(surface, 'pointercancel');
		pointer(surface, 'pointerup');
		expect(document.activeElement).not.toBe(input);
		expect(onInput).not.toHaveBeenCalled();
	});
	it('exposes synchronous toolbar controls without a viewport overlay', async () => {
		const { input, surface, onInput } = await setup();
		keyboardView.openKeyboard();
		expect(document.activeElement).toBe(input);
		expect(surface.querySelector('button')).toBeNull();
		keyboardView.dismissKeyboard();
		expect(document.activeElement).not.toBe(input);
		expect(onInput).not.toHaveBeenCalled();
	});
	it.each(['touch', 'mouse'])('preserves explicitly opened focus through repeated %s clicks and frame resize', async (pointerType) => {
		const { input, surface, rerender, onInput } = await setup({ keyboardEnabled: true });
		await open();
		const focus = vi.spyOn(input, 'focus');
		const click = () => {
			for (const type of ['pointerdown', 'pointerup']) {
				const e = new Event(type, { bubbles: true, cancelable: true });
				Object.assign(e, { pointerType, pointerId: 1, clientX: 50, clientY: 60 });
				surface.dispatchEvent(e);
				if (type === 'pointerdown') expect(e.defaultPrevented).toBe(true);
			}
			expect(document.activeElement).toBe(input);
		};
		click(); click();
		onInput.mockClear();
		await rerender({ interactive: false, viewport: { width: 600, height: 500 } });
		click();
		expect(input.disabled).toBe(false);
		expect(onInput).not.toHaveBeenCalled();
		await fireEvent.compositionStart(input);
		await fireEvent.compositionEnd(input, { data: '日本' });
		await text(input, '日本', 'insertFromComposition');
		expect(onInput).not.toHaveBeenCalled(); // Keep focus, but never send text against stale geometry.
		await rerender({ interactive: true, viewport: { width: 300, height: 500 } });
		click();
		expect(focus).not.toHaveBeenCalled();
	});
	it.each(['hide', 'blur'])('never reopens on a tap after %s', async (dismissal) => {
		const { input, surface, rerender } = await setup({ keyboardEnabled: true });
		await open();
		if (dismissal === 'hide') keyboardView.dismissKeyboard();
		else input.blur();
		await tick();
		const focus = vi.spyOn(input, 'focus');
		await rerender({ interactive: false });
		await rerender({ interactive: true });
		pointer(surface, 'pointerdown'); pointer(surface, 'pointerup');
		expect(document.activeElement).not.toBe(input);
		expect(focus).not.toHaveBeenCalled();
	});
	it('dismisses on ownership loss and discards an unfinished IME commit', async () => {
		const { input, rerender, onInput } = await setup({ keyboardEnabled: true });
		await open();
		await fireEvent.compositionStart(input);
		input.value = 'draft';
		await rerender({ keyboardEnabled: false });
		await fireEvent.compositionEnd(input, { data: 'draft' });
		await text(input, 'wrong target');
		expect(document.activeElement).not.toBe(input);
		expect(input.disabled).toBe(true);
		expect(onInput).not.toHaveBeenCalled();
	});
	it('can explicitly hide while waiting for a resized frame', async () => {
		const { input, rerender } = await setup({ keyboardEnabled: true });
		await open();
		await rerender({ interactive: false });
		keyboardView.dismissKeyboard();
		expect(document.activeElement).not.toBe(input);
	});
	it('sends Unicode text once, clears the buffer and disables native text assistance', async () => {
		const { input, onInput } = await setup();
		await open();
		await fireEvent.keyDown(input, { key: 'a', code: 'KeyA' });
		await text(input, 'a');
		await fireEvent.keyUp(input, { key: 'a', code: 'KeyA' });
		await text(input, '👩🏽‍💻');
		expect(onInput.mock.calls.map(([p]) => p)).toEqual([
			{ type: 'input', kind: 'text', text: 'a' },
			{ type: 'input', kind: 'text', text: '👩🏽‍💻' },
		]);
		expect(input.value).toBe('');
		for (const attr of ['autocomplete', 'autocorrect', 'autocapitalize'])
			expect(input.getAttribute(attr)).toBe('off');
		expect(input.getAttribute('spellcheck')).toBe('false');
	});
	it.each([
		'a'.repeat(16384),
		'a'.repeat(16384 * 2 + 7),
		'漢'.repeat(12000),
		'\n\u0000\\"'.repeat(12000),
		'a'.repeat(16383) + '😀' + 'b'.repeat(16382) + '🚀',
	])(
		'chunks a long paste without duplicate text or split surrogate pairs (%#)',
		async (value) => {
			const { input, onInput } = await setup();
			await open();
			expect(before(input, 'insertFromPaste').defaultPrevented).toBe(false);
			await text(input, value, 'insertFromPaste');
			const chunks = onInput.mock.calls.map(([payload]) => {
				expect(payload).toEqual({
					type: 'input',
					kind: 'text',
					text: expect.any(String),
				});
				return payload.text as string;
			});
			expect(chunks.join('')).toBe(value);
			for (const chunk of chunks) {
				expect(chunk.length).toBeGreaterThan(0);
				expect(chunk.length).toBeLessThanOrEqual(16384);
				expect(new TextEncoder().encode(JSON.stringify({ type: 'input', kind: 'text', text: chunk,
					id: '𐐀'.repeat(128), runtime_id: '𐐀'.repeat(128), tab_id: '𐐀'.repeat(128)
				})).length).toBeLessThanOrEqual(32768);
				expect(chunk).not.toMatch(/^[\uDC00-\uDFFF]|[\uD800-\uDBFF]$/u);
			}
			expect(input.value).toBe('');
			await text(input, '');
			expect(onInput).toHaveBeenCalledTimes(chunks.length);
		},
	);
	it.each([false, true])(
		'forwards Tab with shift=%s once and keeps native focus',
		async (shiftKey) => {
			const { input, onInput } = await setup();
			await open();
			for (const event of ['keydown', 'keyup']) {
				const key = new KeyboardEvent(event, {
					key: 'Tab',
					code: 'Tab',
					keyCode: 9,
					shiftKey,
					bubbles: true,
					cancelable: true,
				});
				input.dispatchEvent(key);
				expect(key.defaultPrevented).toBe(true);
			}
			expect(document.activeElement).toBe(input);
			expect(onInput.mock.calls.map(([payload]) => payload)).toEqual(
				['down', 'up'].map((event) => ({
					type: 'input',
					kind: 'key',
					event,
					key: 'Tab',
					code: 'Tab',
					keyCode: 9,
					text: '',
					modifiers: shiftKey ? 8 : 0,
				})),
			);
		},
	);
	it('commits composition exactly once whether input precedes or follows compositionend', async () => {
		const { input, onInput } = await setup();
		await open();
		await fireEvent.compositionStart(input);
		input.value = 'に';
		await fireEvent.input(input, {
			data: 'に',
			isComposing: true,
			inputType: 'insertCompositionText',
		});
		expect(onInput).not.toHaveBeenCalled();
		input.value = '日本';
		await fireEvent.input(input, { data: '日本', isComposing: false });
		await fireEvent.compositionEnd(input, { data: '日本' });
		await text(input, '日本', 'insertFromComposition');
		expect(onInput.mock.calls.map(([p]) => p)).toEqual([
			{ type: 'input', kind: 'text', text: '日本' },
		]);
		expect(input.value).toBe('');
		await fireEvent.keyDown(input, { key: '日' });
		await text(input, '日');
		expect(onInput).toHaveBeenCalledTimes(2);
	});
	it('forwards deletion from an empty buffer and soft Enter via beforeinput', async () => {
		const { input, onInput } = await setup();
		await open();
		for (const kind of [
			'deleteContentBackward',
			'deleteContentForward',
			'insertLineBreak',
		]) {
			expect(before(input, kind).defaultPrevented).toBe(true);
		}
		expect(onInput.mock.calls.map(([p]) => [p.key, p.event])).toEqual([
			['Backspace', 'down'],
			['Backspace', 'up'],
			['Delete', 'down'],
			['Delete', 'up'],
			['Enter', 'down'],
			['Enter', 'up'],
		]);
	});
	it('handles named keys separately without bubbling duplicates, and Escape dismisses', async () => {
		const { input, onInput } = await setup();
		await open();
		for (const key of ['Enter', 'ArrowLeft', 'Backspace']) {
			expect(await fireEvent.keyDown(input, { key })).toBe(false);
			await fireEvent.keyUp(input, { key });
		}
		expect(onInput).toHaveBeenCalledTimes(6);
		expect(
			onInput.mock.calls.every(([p]) => p.kind === 'key' && p.text === ''),
		).toBe(true);
		await fireEvent.keyDown(input, { key: 'Escape' });
		expect(document.activeElement).not.toBe(input);
	});
	it.each([{ inspecting: true }, { interactive: false }])(
		'blocks text/tap keyboard when %o',
		async (props) => {
			const { surface, input, onInput, onInspect } = await setup(props);
			pointer(surface, 'pointerdown');
			pointer(surface, 'pointerup');
			await text(input, 'not sent');
			expect(input.disabled).toBe(true);
			expect(document.activeElement).not.toBe(input);
			expect(onInput).not.toHaveBeenCalled();
			if (props.inspecting)
				expect(onInspect).toHaveBeenCalledWith(50, 60, 'select');
		},
	);
	it('blurs and discards pending composition on entering inspect mode', async () => {
		const { input, rerender, onInput } = await setup();
		await open();
		await fireEvent.compositionStart(input);
		input.value = 'draft';
		await rerender({ inspecting: true });
		await fireEvent.compositionEnd(input, { data: 'draft' });
		expect(input.value).toBe('');
		expect(document.activeElement).not.toBe(input);
		expect(onInput).not.toHaveBeenCalled();
	});
	it('leaves desktop wrapper key forwarding unchanged', async () => {
		const { surface, onInput } = await setup();
		surface.focus();
		await fireEvent.keyDown(surface, { key: 'a', code: 'KeyA', keyCode: 65 });
		await fireEvent.keyUp(surface, { key: 'a', code: 'KeyA', keyCode: 65 });
		expect(onInput.mock.calls.map(([p]) => [p.kind, p.event, p.text])).toEqual([
			['key', 'down', 'a'],
			['key', 'up', ''],
		]);
	});
});
describe('frame lifetime', () => {
	it('discards an older decode that finishes after the newest frame', async () => {
		const decodes: ((image: ImageBitmap) => void)[] = [];
		vi.mocked(createImageBitmap).mockImplementation(
			() => new Promise<ImageBitmap>((resolve) => decodes.push(resolve)),
		);
		const view = render(BrowserLiveView, {
			frame: {
				data: 'AA==',
				metadata: { deviceWidth: 1280, deviceHeight: 800 },
			},
			onInput: vi.fn(),
		});
		await tick();
		await view.rerender({
			frame: {
				data: 'AQ==',
				metadata: { deviceWidth: 390, deviceHeight: 844 },
			},
		});
		await tick();
		expect(decodes).toHaveLength(2);
		const old = { close: vi.fn() } as unknown as ImageBitmap;
		const current = { close: vi.fn() } as unknown as ImageBitmap;
		decodes[1](current);
		await Promise.resolve();
		decodes[0](old);
		await Promise.resolve();
		await waitFor(() => expect(old.close).toHaveBeenCalledOnce());
		expect(current.close).not.toHaveBeenCalled();
	});
	it('discards an outstanding decode after tab teardown', async () => {
		let finish!: (image: ImageBitmap) => void;
		vi.mocked(createImageBitmap).mockImplementation(
			() => new Promise<ImageBitmap>((resolve) => (finish = resolve)),
		);
		const view = render(BrowserLiveView, {
			frame: {
				data: 'AA==',
				metadata: { deviceWidth: 390, deviceHeight: 844 },
			},
			onInput: vi.fn(),
		});
		await tick();
		view.unmount();
		const image = { close: vi.fn() } as unknown as ImageBitmap;
		finish(image);
		await Promise.resolve();
		await waitFor(() => expect(image.close).toHaveBeenCalledOnce());
	});
});
