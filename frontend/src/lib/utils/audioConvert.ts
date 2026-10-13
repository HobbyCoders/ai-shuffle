/**
 * Audio conversion helpers.
 *
 * Browsers' MediaRecorder only reliably produces WebM/Opus (Chromium, Firefox)
 * or MP4/AAC (Safari). Several fal.ai STT models (e.g. ElevenLabs Scribe) reject
 * WebM outright — they only accept .mp3/.wav/.m4a/.ogg/.flac/.aac. To make voice
 * input work regardless of which STT model is configured, we decode the recording
 * with the Web Audio API and re-encode it as a 16 kHz mono 16-bit PCM WAV, which
 * every fal STT model accepts. 16 kHz mono also matches what Whisper-class models
 * downsample to internally, so we lose no useful information and keep the upload small.
 */

const TARGET_SAMPLE_RATE = 16000;

type AudioContextCtor = typeof AudioContext;

function getAudioContextCtor(): AudioContextCtor | null {
	if (typeof window === 'undefined') return null;
	return (
		window.AudioContext ||
		(window as unknown as { webkitAudioContext?: AudioContextCtor }).webkitAudioContext ||
		null
	);
}

/**
 * Decode an arbitrary audio Blob and re-encode it as a 16 kHz mono WAV Blob.
 *
 * Throws if the audio cannot be decoded (caller should fall back to uploading
 * the original blob in that case).
 */
export async function convertToWav(blob: Blob): Promise<Blob> {
	const Ctor = getAudioContextCtor();
	if (!Ctor || typeof OfflineAudioContext === 'undefined') {
		throw new Error('Web Audio API not available');
	}

	const arrayBuffer = await blob.arrayBuffer();

	// decodeAudioData detaches the buffer it's given, so hand it a copy.
	const decodeCtx = new Ctor();
	let decoded: AudioBuffer;
	try {
		decoded = await decodeCtx.decodeAudioData(arrayBuffer.slice(0));
	} finally {
		decodeCtx.close().catch(() => {});
	}

	if (decoded.length === 0) {
		throw new Error('Decoded audio is empty');
	}

	// Resample to mono at the target rate via an OfflineAudioContext.
	const frameCount = Math.max(1, Math.ceil(decoded.duration * TARGET_SAMPLE_RATE));
	const offline = new OfflineAudioContext(1, frameCount, TARGET_SAMPLE_RATE);
	const source = offline.createBufferSource();
	source.buffer = decoded;
	source.connect(offline.destination);
	source.start(0);
	const rendered = await offline.startRendering();

	return encodeWav(rendered.getChannelData(0), TARGET_SAMPLE_RATE);
}

/**
 * Encode a Float32 sample array as a 16-bit PCM mono WAV Blob.
 */
function encodeWav(samples: Float32Array, sampleRate: number): Blob {
	const bytesPerSample = 2;
	const blockAlign = bytesPerSample; // mono
	const byteRate = sampleRate * blockAlign;
	const dataSize = samples.length * bytesPerSample;
	const buffer = new ArrayBuffer(44 + dataSize);
	const view = new DataView(buffer);

	const writeString = (offset: number, str: string) => {
		for (let i = 0; i < str.length; i++) {
			view.setUint8(offset + i, str.charCodeAt(i));
		}
	};

	// RIFF header
	writeString(0, 'RIFF');
	view.setUint32(4, 36 + dataSize, true);
	writeString(8, 'WAVE');
	// fmt chunk
	writeString(12, 'fmt ');
	view.setUint32(16, 16, true); // chunk size
	view.setUint16(20, 1, true); // PCM
	view.setUint16(22, 1, true); // mono
	view.setUint32(24, sampleRate, true);
	view.setUint32(28, byteRate, true);
	view.setUint16(32, blockAlign, true);
	view.setUint16(34, 8 * bytesPerSample, true); // bits per sample
	// data chunk
	writeString(36, 'data');
	view.setUint32(40, dataSize, true);

	// PCM samples (clamped float -> int16)
	let offset = 44;
	for (let i = 0; i < samples.length; i++) {
		const s = Math.max(-1, Math.min(1, samples[i]));
		view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
		offset += bytesPerSample;
	}

	return new Blob([buffer], { type: 'audio/wav' });
}
