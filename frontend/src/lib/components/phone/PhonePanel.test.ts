import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PhonePanel from './PhonePanel.svelte';
import { parseStatus, type EmulatorStatus } from '$lib/emulator/protocol';
import type { EmulatorStreamConfig } from '$lib/emulator/stream-client';

const mocks = vi.hoisted(() => ({
	status: null as unknown,
	config: null as EmulatorStreamConfig | null,
	sent: [] as unknown[],
	open: true,
	retry: vi.fn(),
}));
vi.mock('$lib/api/emulator', async (orig) => {
	const real = await orig<typeof import('$lib/api/emulator')>();
	return {
		...real,
		fetchEmulatorStatus: vi.fn(async () => mocks.status as EmulatorStatus),
		setupEmulator: vi.fn(async () => {}),
		startDevice: vi.fn(async () => {}),
		stopDevice: vi.fn(async () => {}),
		installApk: vi.fn(async () => ({ package: 'com.example.demo' })),
	};
});
vi.mock('$lib/emulator/stream-client', () => ({
	createEmulatorStream: (config: EmulatorStreamConfig) => {
		mocks.config = config;
		return {
			connect: () => config.onLink?.('connecting'),
			send: (m: unknown) => { if (!mocks.open) return false; mocks.sent.push(m); return true; },
			isOpen: () => mocks.open,
			retry: mocks.retry,
			destroy: () => {},
		};
	},
}));
import * as emulatorApi from '$lib/api/emulator';

const device = (state: string) => ({ id: 'pixel', name: 'Pixel 8', state, api: 35, width: 1080, height: 2400 });
const ready = (state = 'running') => parseStatus({ available: true, reason: 'ready', message: '', install: null, license_url: 'https://example.test/license', devices: [device(state)] });

async function mount(status: EmulatorStatus) {
	mocks.status = status;
	const view = render(PhonePanel, {});
	await waitFor(() => expect(emulatorApi.fetchEmulatorStatus).toHaveBeenCalled());
	await tick();
	await tick();
	return view;
}

/** Bring the stream up and paint one 1080×2400 frame into a 540×1200 box. */
async function goLive() {
	mocks.config!.onLink?.('live');
	mocks.config!.onFrame({ type: 'frame', format: 'jpeg', data: btoa('jpeg'), width: 1080, height: 2400, seq: 1 });
	await tick();
	await new Promise((r) => setTimeout(r, 0));
	await tick();
}

beforeEach(() => {
	vi.clearAllMocks();
	mocks.config = null;
	mocks.sent = [];
	mocks.open = true;
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 1; });
	vi.stubGlobal('createImageBitmap', vi.fn().mockResolvedValue({ close: vi.fn() }));
	vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({
		x: 0, y: 0, left: 0, top: 0, right: 540, bottom: 1200, width: 540, height: 1200, toJSON() {},
	}));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('setup and host states', () => {
	it('requires the license before setup, links it, mentions the size, then shows progress', async () => {
		await mount(parseStatus({ available: false, reason: 'sdk_missing', message: '', install: null, license_url: 'https://example.test/license', devices: [] }));
		const button = screen.getByRole('button', { name: 'Set up Android emulator' }) as HTMLButtonElement;
		expect(button.disabled).toBe(true);
		expect(screen.getByRole('link', { name: /License Agreement/ }).getAttribute('href')).toBe('https://example.test/license');
		expect(screen.getByText('2.1 GB')).toBeTruthy();
		expect(screen.getByText('6.5 GB')).toBeTruthy();
		expect(screen.getByText('8.6 GB')).toBeTruthy();

		await fireEvent.click(screen.getByRole('checkbox'));
		expect(button.disabled).toBe(false);
		mocks.status = parseStatus({ available: false, reason: 'installing', message: '', install: { state: 'downloading', progress: 0.42, message: 'System image' }, license_url: '', devices: [] });
		await fireEvent.click(button);
		expect(emulatorApi.setupEmulator).toHaveBeenCalledOnce();
		const bar = await screen.findByRole('progressbar');
		expect(bar.getAttribute('aria-valuenow')).toBe('42');
		expect(screen.getByText('42%')).toBeTruthy();
		expect(screen.getByText('System image')).toBeTruthy();
	});

	it.each([
		['no_kvm', '--device /dev/kvm'],
		['kvm_permission', 'sudo usermod -aG kvm $USER'],
		['unsupported_arch', 'x86-64'],
	])('%s explains how to enable it', async (reason, text) => {
		await mount(parseStatus({ available: false, reason, message: 'from worker', install: null, license_url: '', devices: [] }));
		expect(screen.getByText(text, { exact: false })).toBeTruthy();
		expect(screen.getByText('from worker')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Start phone' })).toBeNull();
	});

	it('shows an error with a retry that re-runs a failed setup', async () => {
		await mount(parseStatus({ available: false, reason: 'error', message: 'Download failed: disk full', install: { state: 'error', progress: 0.3, message: '' }, license_url: '', devices: [] }));
		expect(screen.getByText('Download failed: disk full')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
		await waitFor(() => expect(emulatorApi.setupEmulator).toHaveBeenCalledOnce());
		await waitFor(() => expect(emulatorApi.fetchEmulatorStatus).toHaveBeenCalledTimes(2));
	});
});

describe('device view', () => {
	it('starts a stopped phone and shows the booting state', async () => {
		await mount(ready('stopped'));
		expect(screen.getByText('Pixel 8 is off')).toBeTruthy();
		mocks.status = ready('booting');
		await fireEvent.click(screen.getByRole('button', { name: 'Start phone' }));
		expect(emulatorApi.startDevice).toHaveBeenCalledWith('pixel');
		expect(await screen.findByText('Starting Android')).toBeTruthy();
		// Booting already streams, so the first frames show up as soon as they exist.
		expect(mocks.config?.deviceId).toBe('pixel');
	});

	it('maps a click to a device-pixel tap and the nav bar to Android keys', async () => {
		await mount(ready());
		await goLive();
		const canvas = screen.getByTestId('phone-canvas');
		await fireEvent.pointerDown(canvas, { clientX: 270, clientY: 600, pointerId: 1, pointerType: 'mouse', button: 0 });
		await fireEvent.pointerUp(canvas, { clientX: 271, clientY: 601, pointerId: 1, pointerType: 'mouse', button: 0 });
		expect(mocks.sent).toContainEqual({ type: 'tap', x: 540, y: 1200 });

		await fireEvent.click(screen.getByRole('button', { name: 'Back' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Home' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Recent apps' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Rotate' }));
		expect(mocks.sent.slice(-4)).toEqual([
			{ type: 'key', key: 'BACK' }, { type: 'key', key: 'HOME' }, { type: 'key', key: 'APP_SWITCH' }, { type: 'rotate' },
		]);
	});

	it('turns a drag into a swipe and keys into text / Android keys', async () => {
		await mount(ready());
		await goLive();
		const canvas = screen.getByTestId('phone-canvas');
		await fireEvent.pointerDown(canvas, { clientX: 270, clientY: 900, pointerId: 2, pointerType: 'touch' });
		await fireEvent.pointerMove(canvas, { clientX: 270, clientY: 500, pointerId: 2, pointerType: 'touch' });
		await fireEvent.pointerUp(canvas, { clientX: 270, clientY: 300, pointerId: 2, pointerType: 'touch' });
		expect(mocks.sent.at(-1)).toMatchObject({ type: 'swipe', x1: 540, y1: 1800, x2: 540, y2: 600 });

		const surface = screen.getByRole('application');
		await fireEvent.keyDown(surface, { key: 'h' });
		await fireEvent.keyDown(surface, { key: 'Enter' });
		await fireEvent.keyDown(surface, { key: 'Backspace' });
		expect(mocks.sent.slice(-3)).toEqual([{ type: 'text', text: 'h' }, { type: 'key', key: 'ENTER' }, { type: 'key', key: 'DEL' }]);
	});

	it('bridges the soft keyboard: typed text, Backspace and Enter', async () => {
		await mount(ready());
		await goLive();
		await fireEvent.click(screen.getByRole('button', { name: 'Show keyboard' }));
		const bridge = screen.getByLabelText('Type on the phone') as HTMLTextAreaElement;
		expect(document.activeElement).toBe(bridge);
		bridge.value = 'hey';
		await fireEvent.input(bridge);
		await fireEvent(bridge, new InputEvent('beforeinput', { inputType: 'deleteContentBackward', cancelable: true, bubbles: true }));
		await fireEvent(bridge, new InputEvent('beforeinput', { inputType: 'insertLineBreak', cancelable: true, bubbles: true }));
		expect(mocks.sent.slice(-3)).toEqual([{ type: 'text', text: 'hey' }, { type: 'key', key: 'DEL' }, { type: 'key', key: 'ENTER' }]);
		expect(bridge.value).toBe('');
	});

	it('installs an APK and shows the package name', async () => {
		await mount(ready());
		await goLive();
		const input = document.querySelector('input[type="file"]') as HTMLInputElement;
		const file = new File(['apk'], 'demo.apk', { type: 'application/vnd.android.package-archive' });
		Object.defineProperty(input, 'files', { value: [file], configurable: true });
		await fireEvent.change(input);
		expect(emulatorApi.installApk).toHaveBeenCalledWith('pixel', file);
		expect(await screen.findByText('com.example.demo')).toBeTruthy();
	});

	it('marks the frame stale while the socket reconnects', async () => {
		await mount(ready());
		await goLive();
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
		try {
			expect(screen.queryByText(/last image is stale/)).toBeNull();
			mocks.config!.onLink?.('reconnecting');
			await tick();
			vi.advanceTimersByTime(1600);
			await tick();
			expect(screen.getByText(/last image is stale/)).toBeTruthy();
			expect((screen.getByRole('button', { name: 'Home' }) as HTMLButtonElement).disabled).toBe(true);
			mocks.config!.onLink?.('live');
			await tick();
			expect(screen.queryByText(/last image is stale/)).toBeNull();
		} finally {
			vi.useRealTimers();
		}
	});

	it('shows an input error as a notice that goes away, and keeps the phone usable', async () => {
		await mount(ready());
		await goLive();
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
		try {
			mocks.config!.onError?.({ type: 'error', message: 'Point is off the screen', code: 'bad_input' });
			await tick();
			expect(screen.getByText('Point is off the screen')).toBeTruthy();
			expect((screen.getByRole('button', { name: 'Home' }) as HTMLButtonElement).disabled).toBe(false);
			vi.advanceTimersByTime(4100);
			await tick();
			expect(screen.queryByText('Point is off the screen')).toBeNull();
		} finally {
			vi.useRealTimers();
		}
	});

	it('stops on a terminal close and reconnects only when asked', async () => {
		await mount(ready());
		await goLive();
		mocks.config!.onLink?.('failed', { failure: 'This phone no longer exists on the worker.', code: 4004 });
		await tick();
		const alert = screen.getByRole('alert');
		expect(alert.textContent).toContain('Disconnected from the phone');
		expect(alert.textContent).toContain('This phone no longer exists on the worker.');
		expect(screen.queryByText(/Reconnecting · last image/)).toBeNull();
		expect((screen.getByRole('button', { name: 'Home' }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }));
		expect(mocks.retry).toHaveBeenCalledOnce();
		await waitFor(() => expect(emulatorApi.fetchEmulatorStatus).toHaveBeenCalledTimes(2));
		mocks.config!.onLink?.('live');
		await tick();
		expect(screen.queryByText('Disconnected from the phone')).toBeNull();
	});

	it('shows the device error the stream reports', async () => {
		await mount(ready());
		mocks.config!.onState?.({ type: 'state', state: 'error', message: 'Emulator process exited (code 1)' });
		await tick();
		expect(screen.getByText('The phone stopped with an error')).toBeTruthy();
		expect(screen.getByText('Emulator process exited (code 1)')).toBeTruthy();
	});

	it('stops a running phone', async () => {
		await mount(ready());
		mocks.status = ready('stopped');
		await fireEvent.click(screen.getByRole('button', { name: 'Stop phone' }));
		expect(emulatorApi.stopDevice).toHaveBeenCalledWith('pixel');
		expect(await screen.findByText('Pixel 8 is off')).toBeTruthy();
	});
});
