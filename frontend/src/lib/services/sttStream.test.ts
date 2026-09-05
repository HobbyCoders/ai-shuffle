import { afterEach, describe, expect, it, vi } from 'vitest';
import { startSttStream } from './sttStream';

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
