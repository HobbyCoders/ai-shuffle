package com.hobbycoders.aishuffle.wake

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import org.vosk.Model
import org.vosk.Recognizer
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/**
 * In-house replacement for org.vosk.android.SpeechService: a dedicated thread
 * reading the mic and feeding Vosk, plus two things SpeechService can't do —
 *
 *  - An energy gate: in a quiet room the recognizer is starved entirely (the
 *    mic keeps running, decoding doesn't), so idle CPU is near zero. A slow-
 *    adapting noise floor decides what counts as "quiet", and on wake-up the
 *    last half second of audio is replayed from the ring buffer so word
 *    onsets that opened the gate aren't clipped off.
 *
 *  - A rolling ring buffer of the last ~3 s of raw PCM, preallocated with no
 *    per-read allocations, so the service can run a second-pass verification
 *    decode over the exact audio that produced a wake candidate.
 *
 * Results are the recognizer's finalized JSON, delivered on the capture
 * thread. Partial hypotheses are never surfaced — they were the historical
 * source of false wakes.
 */
internal class WakeAudioPipeline(
    private val model: Model,
    private val grammar: String,
    private val listener: Listener,
) {

    interface Listener {
        /** A finalized Vosk result (JSON), invoked on the capture thread. */
        fun onResult(json: String)

        /** Capture or decode failure; the pipeline thread is exiting. */
        fun onError(e: Exception)
    }

    @Volatile
    private var running = false
    private var thread: Thread? = null
    private val lock = Any()

    private val ring = ShortArray(RING_SAMPLES)
    private var ringPos = 0
    private var ringFilled = 0
    private val ringLock = Any()

    /** Safe to call repeatedly; a running pipeline is left alone. */
    fun start() {
        synchronized(lock) {
            if (running) return
            running = true
            thread = Thread(::captureLoop, "WakeAudioPipeline").also { it.start() }
        }
    }

    /**
     * Safe to call repeatedly and from any thread — including the capture
     * thread itself (the service stops the pipeline from inside onResult when
     * the wake phrase fires); joining ourselves would deadlock, so in that
     * case we only drop the flag and let the loop unwind on its own.
     */
    fun stop() {
        val t: Thread?
        synchronized(lock) {
            running = false
            t = thread
            thread = null
        }
        if (t != null && t !== Thread.currentThread()) {
            runCatching { t.join(2000) }
        }
    }

    /** Chronological copy of the buffered audio, for verification decodes. */
    fun snapshot(): ShortArray {
        synchronized(ringLock) {
            val out = ShortArray(ringFilled)
            val start = (ringPos - ringFilled + RING_SAMPLES) % RING_SAMPLES
            val tail = min(ringFilled, RING_SAMPLES - start)
            System.arraycopy(ring, start, out, 0, tail)
            if (tail < ringFilled) System.arraycopy(ring, 0, out, tail, ringFilled - tail)
            return out
        }
    }

    private fun writeRing(src: ShortArray, n: Int) {
        synchronized(ringLock) {
            var offset = 0
            var remaining = n
            while (remaining > 0) {
                val chunk = min(remaining, RING_SAMPLES - ringPos)
                System.arraycopy(src, offset, ring, ringPos, chunk)
                ringPos = (ringPos + chunk) % RING_SAMPLES
                offset += chunk
                remaining -= chunk
            }
            ringFilled = min(ringFilled + n, RING_SAMPLES)
        }
    }

    /** Copy the most recent samples into [dst]; returns how many were valid. */
    private fun readRecent(dst: ShortArray): Int {
        synchronized(ringLock) {
            val n = min(ringFilled, dst.size)
            val start = (ringPos - n + RING_SAMPLES) % RING_SAMPLES
            val tail = min(n, RING_SAMPLES - start)
            System.arraycopy(ring, start, dst, 0, tail)
            if (tail < n) System.arraycopy(ring, 0, dst, tail, n - tail)
            return n
        }
    }

    // Permission is checked by the service before the pipeline ever starts.
    @SuppressLint("MissingPermission")
    private fun captureLoop() {
        var record: AudioRecord? = null
        var recognizer: Recognizer? = null
        try {
            recognizer = Recognizer(model, SAMPLE_RATE.toFloat(), grammar)
            // Word-level output so results carry per-word confidence and
            // start/end times — the gates in the service need both.
            recognizer.setWords(true)

            val minBuf = AudioRecord.getMinBufferSize(
                SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
            )
            record = AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION,
                SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                max(minBuf, CHUNK_SAMPLES * 2 * 4),
            )
            if (record.state != AudioRecord.STATE_INITIALIZED) {
                throw IllegalStateException("AudioRecord failed to initialize")
            }
            record.startRecording()
            if (record.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                throw IllegalStateException("Mic busy — recording did not start")
            }

            val buf = ShortArray(CHUNK_SAMPLES)
            val flush = ShortArray(FLUSH_SAMPLES)
            var feeding = true
            var quietMs = 0
            var noiseFloor = 0.0

            while (running) {
                val n = record.read(buf, 0, buf.size)
                if (n <= 0) throw IllegalStateException("AudioRecord read returned $n")
                writeRing(buf, n)

                var sumSq = 0.0
                for (i in 0 until n) {
                    val s = buf[i].toDouble()
                    sumSq += s * s
                }
                val rms = sqrt(sumSq / n)
                // The floor tracks quiet quickly but rises only slowly, so
                // sustained speech can't drag the threshold up after itself.
                noiseFloor = if (noiseFloor == 0.0) rms else {
                    val alpha = if (rms < noiseFloor) FLOOR_ALPHA_DOWN else FLOOR_ALPHA_UP
                    noiseFloor + alpha * (rms - noiseFloor)
                }
                // Fixed margin keeps headroom even in a dead-silent room
                // where the relative term collapses to nothing.
                val threshold = noiseFloor * FLOOR_RATIO + FLOOR_MARGIN
                val chunkMs = n * 1000 / SAMPLE_RATE
                quietMs = if (rms < threshold) quietMs + chunkMs else 0

                if (feeding) {
                    if (recognizer.acceptWaveForm(buf, n)) {
                        listener.onResult(recognizer.result)
                    }
                    if (quietMs >= GATE_QUIET_MS) {
                        // Finalize before going idle so an utterance that
                        // trailed into the quiet stretch isn't lost.
                        listener.onResult(recognizer.finalResult)
                        feeding = false
                    }
                } else if (rms >= threshold) {
                    // Replay the recent past (which already includes this
                    // chunk) so the sound that opened the gate is decoded
                    // from its onset, not from mid-word.
                    val m = readRecent(flush)
                    if (m > 0 && recognizer.acceptWaveForm(flush, m)) {
                        listener.onResult(recognizer.result)
                    }
                    feeding = true
                }
            }
        } catch (e: Exception) {
            // Only report if we weren't asked to stop — a mid-read teardown
            // is expected to fail and isn't an error.
            if (running) {
                running = false
                listener.onError(e)
            }
        } finally {
            runCatching { record?.stop() }
            runCatching { record?.release() }
            runCatching { recognizer?.close() }
        }
    }

    companion object {
        private const val SAMPLE_RATE = 16000

        /** 100 ms per read — small enough for a responsive gate. */
        private const val CHUNK_SAMPLES = SAMPLE_RATE / 10

        /** ~3 s of 16 kHz mono 16-bit PCM (~96 KB). */
        private const val RING_SAMPLES = SAMPLE_RATE * 3

        /** ~0.5 s replayed when the energy gate reopens. */
        private const val FLUSH_SAMPLES = SAMPLE_RATE / 2

        /** Sustained quiet before the recognizer is starved. */
        private const val GATE_QUIET_MS = 1000

        private const val FLOOR_ALPHA_DOWN = 0.05
        private const val FLOOR_ALPHA_UP = 0.005
        private const val FLOOR_RATIO = 2.0
        private const val FLOOR_MARGIN = 120.0
    }
}
