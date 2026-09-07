import { afterEach, describe, expect, it, vi } from 'vitest';
import { startSttStream, SttUnsupportedError } from './sttStream';

vi.mock('$app/environment', () => ({ browser: true }));
class FakeSocket {
	static OPEN = 1;
	static last: FakeSocket;
	readyState = 1;
	bufferedAmount = 0;
	onmessage?: (event: { data: string }) => void;
	onclose?: (event: { code: number; reason: string }) => void;
	send = vi.fn();
	close = vi.fn();
	constructor() { FakeSocket.last = this; queueMicrotask(() => this.emit({ type: 'ready' })); }
	emit(message: object) { this.onmessage?.({ data: JSON.stringify(message) }); }
}
const node = () => ({ connect: vi.fn(), disconnect: vi.fn(), gain: { value: 0 }, frequencyBinCount: 128 });
class FakeAudioContext {
	state = 'running'; sampleRate = 24000; destination = {};
	createMediaStreamSource = node; createAnalyser = node; createScriptProcessor = node; createGain = node;
	close = vi.fn().mockResolvedValue(undefined);
}
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('GPT Live Transcribe startup failures', () => {
	function setup(getUserMedia: () => Promise<unknown>) {
		vi.stubGlobal('WebSocket', FakeSocket);
		vi.stubGlobal('AudioContext', FakeAudioContext);
		vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } });
		vi.stubGlobal('requestAnimationFrame', vi.fn());
		vi.stubGlobal('cancelAnimationFrame', vi.fn());
	}
	it('releases a late microphone and preserves the model rejection after an early ready', async () => {
		let openMic!: (value: unknown) => void;
		const stop = vi.fn();
		setup(() => new Promise(resolve => { openMic = resolve; }));
		const onCapture = vi.fn();
		const started = startSttStream({ onCapture });
		const rejection = expect(started).rejects.toThrow('GPT Live Transcribe is unavailable');
		await Promise.resolve();
		FakeSocket.last.emit({ type: 'error', fatal: true, message: 'GPT Live Transcribe is unavailable' });
		FakeSocket.last.emit({ type: 'closed' });
		await rejection;
		openMic({ getTracks: () => [{ stop }] });
		await Promise.resolve();
		expect(stop).toHaveBeenCalledOnce();
		expect(onCapture).not.toHaveBeenCalled();
	});
	it('retains the typed batch fallback when the socket rejects an unsupported model', async () => {
		setup(async () => ({ getTracks: () => [{ stop: vi.fn() }] }));
		const started = startSttStream();
		FakeSocket.last.onclose?.({ code: 4003, reason: 'Batch-only model' });
		await expect(started).rejects.toBeInstanceOf(SttUnsupportedError);
	});
	it('closes capture immediately on a fatal model event even if the peer stays open', async () => {
		const stop = vi.fn();
		setup(async () => ({ getTracks: () => [{ stop }] }));
		const onClosed = vi.fn();
		const session = await startSttStream({ onClosed });
		FakeSocket.last.emit({ type: 'error', fatal: true, message: 'Model unavailable' });
		expect(stop).toHaveBeenCalledOnce();
		expect(onClosed).toHaveBeenCalledOnce();
		await expect(session.stop()).rejects.toThrow('Model unavailable');
	});
});

describe('stream session stop drain', () => {
	it('publishes words before stopping and waits past the first final for all tail turns', async () => {
		vi.useFakeTimers();
		vi.stubGlobal('WebSocket', FakeSocket);
		vi.stubGlobal('AudioContext', FakeAudioContext);
		vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] }) } });
		vi.stubGlobal('requestAnimationFrame', vi.fn()); vi.stubGlobal('cancelAnimationFrame', vi.fn());
		const onTranscript = vi.fn();
		const session = await startSttStream({ onTranscript });
		const socket = FakeSocket.last;
		socket.emit({ type: 'item_started', item_id: 'a' });
		socket.emit({ type: 'delta', item_id: 'a', text: 'First' });
		expect(onTranscript).toHaveBeenLastCalledWith('First');
		socket.emit({ type: 'item_started', item_id: 'b' });
		const stopped = session.stop();
		socket.emit({ type: 'final', item_id: 'a', text: 'First sentence.' });
		await Promise.resolve();
		expect(socket.close).not.toHaveBeenCalled();
		socket.emit({ type: 'final', item_id: 'b', text: 'Last words.' });
		socket.emit({ type: 'closed' });
		expect(await stopped).toBe('First sentence. Last words.');
	});
});
