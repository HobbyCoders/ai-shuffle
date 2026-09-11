import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

const settings = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/voice', () => ({ loadVoiceSettings: settings }));
vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('./keyboard', () => ({ registerShortcut: vi.fn() }));
vi.mock('$lib/stores/hotkeyPreferences.svelte', () => ({ hotkeyPreferences: {} }));
vi.mock('$lib/utils/earcon', () => ({ playEarcon: vi.fn(), primeEarcons: vi.fn(), setEarconsEnabled: vi.fn() }));
vi.mock('./sttStream', () => ({
	fetchSttCapability: vi.fn().mockResolvedValue({ supported: false, accepts_compressed: true }),
	startSttStream: vi.fn(), SttUnsupportedError: class extends Error {}
}));
import { cancelVoiceInput, globalSttState, triggerToggle, toggleVoiceInput } from './globalStt';
import { playEarcon } from '$lib/utils/earcon';

describe('batch voice input automatic finish', () => {
	let volume: number;
	let stopTrack: ReturnType<typeof vi.fn>;
	let resume: ReturnType<typeof vi.fn>;
	let close: ReturnType<typeof vi.fn>;
	let writeText: ReturnType<typeof vi.fn>;
	let getUserMedia: ReturnType<typeof vi.fn>;
	let requests: FakeRequest[];

	class FakeRecorder extends EventTarget {
		static isTypeSupported() { return true; }
		state = 'inactive';
		mimeType = 'audio/webm';
		ondataavailable: ((event: BlobEvent) => void) | null = null;
		start() { this.state = 'recording'; }
		requestData() { this.ondataavailable?.({ data: new Blob(['recording']) } as BlobEvent); }
		stop() {
			this.state = 'inactive';
			setTimeout(() => this.dispatchEvent(new Event('stop')), 0);
		}
	}
	class FakeRequest {
		upload = {};
		status = 200;
		responseText = JSON.stringify({ text: 'Batch transcript.' });
		onload = () => {};
		onabort = () => {};
		open() {}
		send() { requests.push(this); }
		abort() { this.onabort(); }
	}

	beforeEach(() => {
		vi.useFakeTimers();
		settings.mockResolvedValue({voice_input_mode:'auto', voice_input_silence_seconds:5});
		volume = 0;
		requests = [];
		stopTrack = vi.fn();
		resume = vi.fn().mockResolvedValue(undefined);
		close = vi.fn().mockResolvedValue(undefined);
		writeText = vi.fn().mockResolvedValue(undefined);
		getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [{ stop: stopTrack }] });
		vi.stubGlobal('navigator', { mediaDevices: { getUserMedia }, clipboard: { writeText } });
		vi.stubGlobal('MediaRecorder', FakeRecorder);
		vi.stubGlobal('XMLHttpRequest', FakeRequest);
		// No animation frames ever arrive, as with a hidden or occluded page.
		vi.stubGlobal('requestAnimationFrame', vi.fn());
		vi.stubGlobal('AudioContext', class {
			state = 'suspended';
			resume = resume;
			close = close;
			createMediaStreamSource() { return { connect: vi.fn() }; }
			createAnalyser() {
				return {
					fftSize: 256, frequencyBinCount: 128,
					getByteFrequencyData: (data: Uint8Array) => data.fill(180),
					getFloatTimeDomainData: (data: Float32Array) => data.fill(volume)
				};
			}
		});
	});
	afterEach(() => {
		cancelVoiceInput();
		vi.useRealTimers();
		vi.unstubAllGlobals();
		vi.clearAllMocks();
	});

	it('composer toggle ignores silence until a second click and still uploads the recording', async () => {
		settings.mockResolvedValue({voice_input_mode:'toggle'});
		const sink = vi.fn();
		toggleVoiceInput(sink);
		await vi.advanceTimersByTimeAsync(13000);
		expect(get(globalSttState).isRecording).toBe(true);
		volume = 0.1;
		await vi.advanceTimersByTimeAsync(600);
		volume = 0;
		await vi.advanceTimersByTimeAsync(10000);
		expect(requests).toHaveLength(0);
		toggleVoiceInput(sink);
		await vi.advanceTimersByTimeAsync(1);
		expect(requests).toHaveLength(1);
		requests[0].onload();
		await vi.advanceTimersByTimeAsync(0);
		expect(sink).toHaveBeenCalledWith('Batch transcript.');
	});
	it('composer auto uses the saved silence timeout', async () => {
		settings.mockResolvedValue({voice_input_mode:'auto', voice_input_silence_seconds:2});
		toggleVoiceInput(vi.fn());
		await vi.advanceTimersByTimeAsync(0);
		volume = 0.1;
		await vi.advanceTimersByTimeAsync(600);
		volume = 0;
		await vi.advanceTimersByTimeAsync(2049);
		expect(get(globalSttState).isRecording).toBe(true);
		await vi.advanceTimersByTimeAsync(2);
		expect(requests).toHaveLength(1);
	});
	it('resumes the audio context and automatically transcribes after quiet without animation frames', async () => {
		triggerToggle();
		await vi.advanceTimersByTimeAsync(0);
		expect(resume).toHaveBeenCalledOnce();
		expect(get(globalSttState).isRecording).toBe(true);
		volume = 0.1;
		await vi.advanceTimersByTimeAsync(600);
		volume = 0;
		await vi.advanceTimersByTimeAsync(5049);
		expect(get(globalSttState).isRecording).toBe(true);
		expect(requests).toHaveLength(0);
		await vi.advanceTimersByTimeAsync(2);
		expect(get(globalSttState).isRecording).toBe(false);
		expect(get(globalSttState).isTranscribing).toBe(true);
		expect(stopTrack).toHaveBeenCalledOnce();
		expect(close).toHaveBeenCalledOnce();
		expect(requests).toHaveLength(1);
		requests[0].onload();
		await vi.advanceTimersByTimeAsync(0);
		expect(writeText).toHaveBeenCalledWith('Batch transcript.');
		expect(get(globalSttState).isTranscribing).toBe(false);
		expect(get(globalSttState).mode).toBe('idle');
		vi.mocked(playEarcon).mockClear();
		cancelVoiceInput({ discard: false });
		cancelVoiceInput({ discard: false });
		expect(playEarcon).not.toHaveBeenCalled();
	});

	it('waits for first speech, resets the quiet countdown on resumed speech, and clears timers on discard', async () => {
		triggerToggle();
		await vi.advanceTimersByTimeAsync(3000);
		expect(get(globalSttState).isRecording).toBe(true);
		volume = 0.1;
		await vi.advanceTimersByTimeAsync(500);
		volume = 0;
		await vi.advanceTimersByTimeAsync(1500);
		volume = 0.1;
		await vi.advanceTimersByTimeAsync(2000);
		expect(get(globalSttState).isRecording).toBe(true);
		volume = 0;
		cancelVoiceInput();
		await vi.advanceTimersByTimeAsync(12000);
		expect(requests).toHaveLength(0);
		expect(writeText).not.toHaveBeenCalled();
		expect(get(globalSttState).mode).toBe('idle');
	});

	it('does not restart capture when microphone permission resolves after discard', async () => {
		let grant!: (stream: unknown) => void;
		getUserMedia.mockImplementation(() => new Promise(resolve => { grant = resolve; }));
		triggerToggle();
		await vi.advanceTimersByTimeAsync(0);
		cancelVoiceInput();
		grant({ getTracks: () => [{ stop: stopTrack }] });
		await vi.advanceTimersByTimeAsync(0);
		expect(stopTrack).toHaveBeenCalledOnce();
		expect(resume).not.toHaveBeenCalled();
		expect(get(globalSttState).isRecording).toBe(false);
	});
});
