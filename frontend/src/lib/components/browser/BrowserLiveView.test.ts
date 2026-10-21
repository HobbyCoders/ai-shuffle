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
import type { BrowserEditor } from '$lib/browser-panel/workspace';

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
	props: { interactive?: boolean; inspecting?: boolean; keyboardEnabled?: boolean; onCopy?: () => void; editors?: BrowserEditor[]; onEdit?: () => void } = {},
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
	const input = screen.getByLabelText('Type in the remote browser') as HTMLTextAreaElement;
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
	it('asks for a copy on Ctrl+C or Cmd+C and still forwards the key to the page', async () => {
		const onCopy = vi.fn();
		const { surface, onInput } = await setup({ onCopy });
		surface.focus();
		await fireEvent.keyDown(surface, { key: 'c', code: 'KeyC', keyCode: 67, ctrlKey: true });
		await fireEvent.keyDown(surface, { key: 'c', code: 'KeyC', keyCode: 67, metaKey: true });
		await fireEvent.keyDown(surface, { key: 'c', code: 'KeyC', keyCode: 67 });
		await fireEvent.keyDown(surface, { key: 'C', code: 'KeyC', keyCode: 67, ctrlKey: true, shiftKey: true });
		expect(onCopy).toHaveBeenCalledTimes(2);
		expect(onInput.mock.calls.filter(([p]) => p.kind === 'key' && p.event === 'down')).toHaveLength(4);
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

function mouse(target: HTMLElement, type: string, init: { x?: number; y?: number; button?: number; buttons?: number; metaKey?: boolean; pointerType?: string } = {}) {
	const event = new Event(type, { bubbles: true, cancelable: true });
	Object.assign(event, {
		pointerType: init.pointerType ?? 'mouse',
		pointerId: 7,
		clientX: init.x ?? 50,
		clientY: init.y ?? 60,
		button: init.button ?? -1,
		buttons: init.buttons ?? 0,
		altKey: false,
		ctrlKey: false,
		metaKey: init.metaKey ?? false,
		shiftKey: false,
	});
	target.dispatchEvent(event);
	return event;
}
const field: BrowserEditor = { id: 'name', type: 'text', label: 'Name', value: '', x: 100, y: 100, width: 120, height: 24 };
function captureOn(surface: HTMLElement) {
	let captured: number | null = null;
	Object.assign(surface, {
		setPointerCapture: vi.fn((id: number) => { captured = id; }),
		hasPointerCapture: (id: number) => captured === id,
		releasePointerCapture: () => { captured = null; },
	});
	return () => { captured = null; };
}
const mouseCalls = (onInput: ReturnType<typeof vi.fn>) => onInput.mock.calls.map(([p]) => p).filter((p) => p.kind === 'mouse');

describe('desktop mouse input', () => {
	it('reports hover as no button held and a drag as the held button', async () => {
		const { surface, onInput } = await setup();
		mouse(surface, 'pointermove', { x: 10, y: 20 });
		mouse(surface, 'pointerdown', { x: 10, y: 20, button: 0, buttons: 1 });
		mouse(surface, 'pointermove', { x: 30, y: 20, buttons: 1 });
		expect(mouseCalls(onInput).map((p) => [p.event, p.button, p.buttons, p.clickCount])).toEqual([
			['move', 'none', 0, 0],
			['down', 'left', 1, 1],
			['move', 'left', 1, 0],
		]);
	});

	it('still releases a drag that ends over a field overlay, via pointer capture', async () => {
		const { surface, onInput } = await setup({ editors: [field], onEdit: vi.fn() });
		const overlay = screen.getByRole('textbox', { name: 'Name' });
		captureOn(surface);
		mouse(surface, 'pointerdown', { x: 20, y: 20, button: 0, buttons: 1 });
		expect(surface.setPointerCapture).toHaveBeenCalledWith(7);
		mouse(overlay, 'pointermove', { x: 110, y: 110, buttons: 1 });
		mouse(overlay, 'pointerup', { x: 110, y: 110, button: 0, buttons: 0 });
		expect(mouseCalls(onInput).map((p) => [p.event, p.x, p.y])).toEqual([
			['down', 20, 20],
			['move', 110, 110],
			['up', 110, 110],
		]);
	});

	it('sends the release when capture is lost without a pointerup', async () => {
		const { surface, onInput } = await setup();
		const lose = captureOn(surface);
		mouse(surface, 'pointerdown', { x: 20, y: 20, button: 0, buttons: 1 });
		mouse(surface, 'pointermove', { x: 40, y: 20, buttons: 1 });
		lose();
		mouse(surface, 'lostpointercapture');
		mouse(surface, 'lostpointercapture'); // already released: nothing more
		expect(mouseCalls(onInput).map((p) => [p.event, p.button, p.buttons, p.x])).toEqual([
			['down', 'left', 1, 20],
			['move', 'left', 1, 40],
			['up', 'left', 0, 40],
		]);
	});

	it('lands a pending hover move before the press', async () => {
		const frames: FrameRequestCallback[] = [];
		const { surface, onInput } = await setup();
		vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => frames.push(cb));
		mouse(surface, 'pointermove', { x: 80, y: 90 });
		mouse(surface, 'pointerdown', { x: 80, y: 90, button: 0, buttons: 1 });
		mouse(surface, 'pointerup', { x: 80, y: 90, button: 0 });
		for (const cb of frames) cb(0); // the late animation frame sends nothing twice
		expect(mouseCalls(onInput).map((p) => [p.event, p.x, p.y])).toEqual([
			['move', 80, 90],
			['down', 80, 90],
			['up', 80, 90],
		]);
	});

	it('counts double and triple clicks itself (Chrome reports detail 0)', async () => {
		const { surface, onInput } = await setup();
		const click = (x: number) => {
			mouse(surface, 'pointerdown', { x, y: 60, button: 0, buttons: 1 });
			mouse(surface, 'pointerup', { x, y: 60, button: 0 });
		};
		click(50); click(51); click(50); click(150);
		expect(mouseCalls(onInput).map((p) => `${p.event}${p.clickCount}`)).toEqual([
			'down1', 'up1', 'down2', 'up2', 'down3', 'up3', 'down1', 'up1',
		]);
	});

	it('never releases a button the page did not see pressed', async () => {
		const { surface, onInput } = await setup();
		mouse(surface, 'pointerup', { button: 0 });
		mouse(surface, 'pointerdown', { button: 2, buttons: 2 });
		mouse(surface, 'pointerup', { button: 0 });
		mouse(surface, 'pointerup', { button: 2 });
		expect(mouseCalls(onInput).map((p) => [p.event, p.button])).toEqual([
			['down', 'right'],
			['up', 'right'],
		]);
	});

	it('drops a pen eraser bit the remote does not know', async () => {
		const { surface, onInput } = await setup();
		mouse(surface, 'pointerdown', { button: 0, buttons: 33, pointerType: 'pen' });
		mouse(surface, 'pointermove', { buttons: 33, pointerType: 'pen' });
		expect(mouseCalls(onInput).map((p) => p.buttons)).toEqual([1, 1]);
	});

	it('gives double taps a finger-sized tolerance', async () => {
		const { surface, onInput } = await setup();
		pointer(surface, 'pointerdown', 50, 60); pointer(surface, 'pointerup', 50, 60);
		pointer(surface, 'pointerdown', 60, 66); pointer(surface, 'pointerup', 60, 66);
		expect(mouseCalls(onInput).filter((p) => p.event === 'down').map((p) => p.clickCount)).toEqual([1, 2]);
	});

	it('sends Cmd+click as Ctrl+click and ignores Back/Forward buttons', async () => {
		const { surface, onInput } = await setup();
		mouse(surface, 'pointerdown', { button: 0, buttons: 1, metaKey: true });
		mouse(surface, 'pointerup', { button: 0, metaKey: true });
		mouse(surface, 'pointerdown', { button: 3, buttons: 8 });
		mouse(surface, 'pointerup', { button: 3 });
		expect(mouseCalls(onInput).map((p) => [p.event, p.modifiers])).toEqual([
			['down', 2],
			['up', 2],
		]);
	});
});

describe('field overlay fill', () => {
	it('wears the page field colour read from the frame, not its text colour', async () => {
		vi.stubGlobal('matchMedia', (query: string) => ({ matches: query === '(pointer: fine)', addEventListener: vi.fn(), removeEventListener: vi.fn() }));
		const getImageData = vi.fn((_x: number, _y: number, w: number, h: number) => {
			const data = new Uint8ClampedArray(w * h * 4);
			for (let i = 0; i < w * h; i++) {
				const text = i % 5 === 0; // a fifth of the pixels are dark text
				data.set(text ? [20, 20, 20, 255] : [250, 248, 240, 255], i * 4);
			}
			return { data };
		});
		vi.mocked(HTMLCanvasElement.prototype.getContext).mockReturnValue({
			getImageData, setTransform: vi.fn(), clearRect: vi.fn(), drawImage: vi.fn(), save: vi.fn(), restore: vi.fn(),
		} as unknown as CanvasRenderingContext2D);
		await setup({ editors: [field], onEdit: vi.fn() });
		await waitFor(() => expect(screen.getByRole('application').querySelector('canvas')!.width).toBeGreaterThan(0));
		const overlay = screen.getByRole('textbox', { name: 'Name' }) as HTMLInputElement;
		overlay.focus();
		await tick();
		expect(getImageData).toHaveBeenCalled();
		expect(overlay.style.getPropertyValue('--field-bg')).toBe('rgb(250 248 240)');
		expect(overlay.style.getPropertyValue('--field-fg')).toBe('#141414');
	});
});

describe('desktop keyboard input', () => {
	const sent = (onInput: ReturnType<typeof vi.fn>) => onInput.mock.calls.map(([p]) => p);
	it('sends Cmd shortcuts as Ctrl without typing their letter', async () => {
		const { surface, onInput } = await setup();
		surface.focus();
		await fireEvent.keyDown(surface, { key: 's', code: 'KeyS', keyCode: 83, metaKey: true });
		expect(sent(onInput)).toEqual([
			{ type: 'input', kind: 'key', event: 'down', key: 's', code: 'KeyS', keyCode: 83, text: '', modifiers: 2 },
		]);
	});

	it('types AltGr characters as text', async () => {
		const { surface, onInput } = await setup();
		surface.focus();
		const e = new KeyboardEvent('keydown', { key: '@', code: 'KeyQ', keyCode: 81, ctrlKey: true, altKey: true, bubbles: true, cancelable: true });
		Object.defineProperty(e, 'getModifierState', { value: (m: string) => m === 'AltGraph' });
		surface.dispatchEvent(e);
		expect(sent(onInput)).toEqual([
			{ type: 'input', kind: 'key', event: 'down', key: '@', code: 'KeyQ', keyCode: 81, text: '@', modifiers: 0 },
		]);
	});

	it('pastes this device\'s clipboard text instead of forwarding Ctrl+V', async () => {
		const { surface, onInput } = await setup();
		surface.focus();
		// The shortcut is left alone so the browser raises its paste event.
		expect(await fireEvent.keyDown(surface, { key: 'v', code: 'KeyV', keyCode: 86, ctrlKey: true })).toBe(true);
		await fireEvent.keyUp(surface, { key: 'v', code: 'KeyV', keyCode: 86, ctrlKey: true });
		const paste = (text: string) => {
			const e = new Event('paste', { bubbles: true, cancelable: true });
			Object.assign(e, { clipboardData: { getData: (t: string) => (t === 'text/plain' ? text : '') } });
			surface.dispatchEvent(e);
			return e;
		};
		expect(paste('hello world').defaultPrevented).toBe(true);
		expect(sent(onInput)).toEqual([{ type: 'input', kind: 'text', text: 'hello world' }]);
		onInput.mockClear();
		paste(''); // an image or empty clipboard: the remote pastes its own
		expect(sent(onInput).map((p) => [p.kind, p.event, p.key, p.modifiers])).toEqual([
			['key', 'down', 'v', 2],
			['key', 'up', 'v', 2],
		]);
	});

	it('pastes through Safari: beforepaste is allowed, and a missing paste event falls back to Ctrl+V', async () => {
		const { surface, onInput } = await setup();
		surface.focus();
		const before = new Event('beforepaste', { bubbles: true, cancelable: true });
		surface.dispatchEvent(before);
		expect(before.defaultPrevented).toBe(true);
		vi.useFakeTimers();
		try {
			await fireEvent.keyDown(surface, { key: 'v', code: 'KeyV', keyCode: 86, metaKey: true });
			vi.advanceTimersByTime(99);
			expect(onInput).not.toHaveBeenCalled();
			vi.advanceTimersByTime(1);
			expect(sent(onInput).map((p) => [p.event, p.key, p.modifiers])).toEqual([['down', 'v', 2], ['up', 'v', 2]]);
			onInput.mockClear();
			// A paste event in time cancels the fallback.
			await fireEvent.keyDown(surface, { key: 'v', code: 'KeyV', keyCode: 86, ctrlKey: true });
			const e = new Event('paste', { bubbles: true, cancelable: true });
			Object.assign(e, { clipboardData: { getData: () => 'hi' } });
			surface.dispatchEvent(e);
			vi.advanceTimersByTime(500);
			expect(sent(onInput)).toEqual([{ type: 'input', kind: 'text', text: 'hi' }]);
		} finally {
			vi.useRealTimers();
		}
	});

	it('caps a giant paste and says so', async () => {
		const { surface, onInput } = await setup();
		surface.focus();
		const e = new Event('paste', { bubbles: true, cancelable: true });
		Object.assign(e, { clipboardData: { getData: () => 'a'.repeat(65535) + '😀' + 'b'.repeat(10) } });
		surface.dispatchEvent(e);
		const text = sent(onInput).map((p) => p.text).join('');
		expect(text).toBe('a'.repeat(65535)); // never half an emoji
		await tick();
		expect(screen.getByRole('status').textContent).toContain('first 65,536 characters');
	});

	it('forwards navigation keys typed in a field overlay and hands Tab back to the live view', async () => {
		const { surface, onInput } = await setup({ editors: [field], onEdit: vi.fn() });
		const overlay = screen.getByRole('textbox', { name: 'Name' });
		overlay.focus();
		await fireEvent.keyDown(overlay, { key: 'ArrowDown', code: 'ArrowDown' });
		await fireEvent.keyDown(overlay, { key: 'Tab', code: 'Tab', shiftKey: true });
		expect(sent(onInput).filter((p) => p.kind === 'key').map((p) => [p.event, p.key, p.keyCode, p.modifiers])).toEqual([
			['down', 'ArrowDown', 40, 0],
			['up', 'ArrowDown', 40, 0],
			['down', 'Tab', 9, 8],
			['up', 'Tab', 9, 8],
		]);
		expect(document.activeElement).toBe(surface);
	});
});
