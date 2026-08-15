package com.hobbycoders.aishuffle.wake

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.media.ToneGenerator
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.provider.Settings
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.data.AssistPrefs
import com.hobbycoders.aishuffle.push.NotificationHelper
import com.hobbycoders.aishuffle.voice.VoiceCallActivity
import com.hobbycoders.aishuffle.voice.VoiceCallService
import com.hobbycoders.aishuffle.voice.VoiceCallState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import org.vosk.Model
import org.vosk.Recognizer

/**
 * The always-on wake-word listener: a foreground service running Vosk's small
 * offline model, constrained to a grammar of the wake phrase (plus decoys),
 * so the only thing it can "hear" is whether the phrase was said. Nothing is
 * recorded or sent anywhere — the realtime AI conversation only starts AFTER
 * the phrase.
 *
 * Audio capture and the energy gate live in WakeAudioPipeline; this service
 * owns the model, the trigger gates, and the call/device lifecycle. While a
 * voice call is live the recognizer is stopped (the call owns the mic) and
 * resumes when the call ends.
 */
class WakeWordService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    private var model: Model? = null
    private var pipeline: WakeAudioPipeline? = null
    private var phrase: String = AssistPrefs.DEFAULT_PHRASE

    @Volatile
    private var triggered = false

    /**
     * Holding the microphone while a Bluetooth headset connects can stop the
     * system from settling the headset into its media (A2DP) profile — on
     * some OEMs that silences EVERY app's audio, not just ours. When the
     * output topology changes and no call is live, release the mic briefly,
     * clear any stale communication-mode state, and resume listening once
     * the system has settled.
     */
    private var deviceCallbackRegistered = false
    private var sawInitialDeviceCallback = false
    private val deviceCallback = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(added: Array<out AudioDeviceInfo>) {
            onAudioTopologyChanged()
        }

        override fun onAudioDevicesRemoved(removed: Array<out AudioDeviceInfo>) {
            onAudioTopologyChanged()
        }
    }

    private fun onAudioTopologyChanged() {
        // Registration replays the current devices once — that's not a change.
        if (!sawInitialDeviceCallback) {
            sawInitialDeviceCallback = true
            return
        }
        if (VoiceCallState.isActive || model == null) return
        Log.i(TAG, "Audio devices changed — releasing the mic to let routing settle")
        stopRecognizer()
        resetStaleAudioMode()
        scope.launch(Dispatchers.IO) {
            kotlinx.coroutines.delay(1500)
            resumeListening()
        }
    }

    /** Clear leaked call-audio state; only ever outside a live call. */
    private fun resetStaleAudioMode() {
        runCatching {
            val am = getSystemService(AUDIO_SERVICE) as AudioManager
            if (am.mode != AudioManager.MODE_NORMAL) {
                Log.w(TAG, "Audio mode was ${am.mode} with no live call — resetting")
                am.mode = AudioManager.MODE_NORMAL
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                am.clearCommunicationDevice()
            }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val prefs = AssistPrefs.get(this)
        phrase = prefs.wakePhrase

        // Started via startForegroundService: startForeground() must run even
        // if we bail out right after, or the system kills the app. On Android
        // 14 a mic-type FGS from a background context (e.g. boot) can be
        // refused HERE, inside startForeground — treat that as "not now".
        try {
            startForegroundWithNotification()
        } catch (e: Exception) {
            Log.w(TAG, "Mic foreground start refused (will retry on next app open)", e)
            stopSelf()
            return START_NOT_STICKY
        }

        if (!prefs.wakeWordEnabled || !hasMicPermission() || !VoskModelManager.isReady(this)) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (!deviceCallbackRegistered) {
            deviceCallbackRegistered = true
            (getSystemService(AUDIO_SERVICE) as AudioManager)
                .registerAudioDeviceCallback(deviceCallback, Handler(Looper.getMainLooper()))
        }
        if (model == null) {
            scope.launch(Dispatchers.IO) { loadAndListen() }
            watchCallState()
        } else {
            resumeListening()
        }
        return START_STICKY
    }

    private fun hasMicPermission(): Boolean = ContextCompat.checkSelfPermission(
        this, android.Manifest.permission.RECORD_AUDIO,
    ) == PackageManager.PERMISSION_GRANTED

    private fun startForegroundWithNotification() {
        val notification = buildNotification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID, notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun buildNotification(): Notification {
        NotificationHelper.ensureChannels(this)
        val tap = PendingIntent.getActivity(
            this, 0,
            Intent(this, com.hobbycoders.aishuffle.ui.AssistSettingsActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, NotificationHelper.CHANNEL_SERVICE)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.wake_notification_title))
            .setContentText(getString(R.string.wake_notification_text, phrase))
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setOngoing(true)
            .setShowWhen(false)
            .setContentIntent(tap)
            .build()
    }

    private fun loadAndListen() {
        try {
            val m = Model(VoskModelManager.modelDir(this).absolutePath)
            model = m
            startRecognizer(m)
        } catch (e: Exception) {
            Log.e(TAG, "Wake model failed to load", e)
            stopSelf()
        }
    }

    /**
     * Grammar-constrained recognizer: the decoder can only output the wake
     * phrase, its individual words, or `[unk]`. The lone-word decoys matter —
     * with only the full phrase available, the decoder forces near-misses
     * ("hey" alone, background chatter) into the phrase; giving it cheaper
     * escapes makes a full-phrase result far more meaningful.
     */
    private fun startRecognizer(m: Model) {
        stopRecognizer()
        val entries = LinkedHashSet<String>()
        entries.add(phrase)
        phrase.split(' ').forEach { if (it.isNotBlank()) entries.add(it) }
        entries.add("[unk]")
        val grammar = JSONArray(entries.toList()).toString()
        val p = WakeAudioPipeline(
            m, grammar,
            object : WakeAudioPipeline.Listener {
                override fun onResult(json: String) = checkForPhrase(json)

                override fun onError(e: Exception) {
                    Log.w(TAG, "Wake recognizer error", e)
                    // Recover after a beat — mic contention is usually transient.
                    scope.launch(Dispatchers.IO) {
                        kotlinx.coroutines.delay(3000)
                        stopRecognizer()
                        resumeListening()
                    }
                }
            },
        )
        pipeline = p
        p.start()
        Log.i(TAG, "Wake listening for \"$phrase\"")
    }

    private fun stopRecognizer() {
        runCatching { pipeline?.stop() }
        pipeline = null
    }

    private fun resumeListening() {
        val m = model ?: return
        if (pipeline == null && !VoiceCallState.isActive) {
            scope.launch(Dispatchers.IO) { runCatching { startRecognizer(m) } }
        }
    }

    /** Stop the mic while a voice call owns it; resume when the call ends. */
    private fun watchCallState() {
        scope.launch {
            VoiceCallState.state.collect { state ->
                if (VoiceCallState.isActive) {
                    stopRecognizer()
                } else if (model != null && pipeline == null) {
                    triggered = false
                    resumeListening()
                }
            }
        }
    }

    /**
     * Trigger only on a finalized utterance that survives every gate: it must
     * contain the phrase, decode it with decent confidence, land in plausible
     * human timing, and — the last word — survive a grammar-free second-pass
     * decode of the raw audio. Even with the decoy grammar the decoder can be
     * eager to hear the phrase in noise; the layered gates are what separate
     * a real "hey shuffle" from a cough that vaguely rhymes with it.
     */
    private fun checkForPhrase(hypothesis: String?) {
        if (triggered || hypothesis.isNullOrBlank()) return
        val json = runCatching { JSONObject(hypothesis) }.getOrNull() ?: return
        val heard = json.optString("text").lowercase()
        if (!heard.contains(phrase)) return

        val words = json.optJSONArray("result")
        if (words != null && words.length() > 0 && !phrasePassesGates(words)) return
        if (!verifyCandidate()) return

        triggered = true
        onWakeWord()
    }

    /** Confidence + timing gates over the word-level result entries. */
    private fun phrasePassesGates(words: JSONArray): Boolean {
        val phraseWords = phrase.split(' ')
        // The phrase must appear as a contiguous run of result words — take
        // the most recent run if it somehow appears twice.
        var matchStart = -1
        outer@ for (i in (words.length() - phraseWords.size) downTo 0) {
            for (j in phraseWords.indices) {
                val w = words.optJSONObject(i + j) ?: continue@outer
                if (w.optString("word") != phraseWords[j]) continue@outer
            }
            matchStart = i
            break
        }
        if (matchStart < 0) {
            Log.d(TAG, "Wake candidate rejected (phrase words not contiguous)")
            return false
        }

        var sum = 0.0
        var prevEnd = -1.0
        for (j in phraseWords.indices) {
            val w = words.getJSONObject(matchStart + j)
            sum += w.optDouble("conf", 0.0)
            // A long pause mid-phrase means two unrelated sounds got stitched
            // together, not one deliberate utterance.
            val start = w.optDouble("start", 0.0)
            if (prevEnd >= 0 && start - prevEnd > MAX_WORD_GAP_SEC) {
                Log.d(TAG, "Wake candidate rejected (%.2fs gap between words)".format(start - prevEnd))
                return false
            }
            prevEnd = w.optDouble("end", 0.0)
        }
        val avg = sum / phraseWords.size
        if (avg < MIN_CONFIDENCE) {
            Log.d(TAG, "Wake candidate rejected (conf=%.2f)".format(avg))
            return false
        }
        // Humans can't say the phrase in a click, and don't stretch it over
        // several seconds — spans outside that window are noise artifacts.
        val span = words.getJSONObject(matchStart + phraseWords.size - 1).optDouble("end", 0.0) -
            words.getJSONObject(matchStart).optDouble("start", 0.0)
        if (span < MIN_PHRASE_SEC || span > MAX_PHRASE_SEC) {
            Log.d(TAG, "Wake candidate rejected (span %.2fs)".format(span))
            return false
        }
        return true
    }

    /**
     * Second pass: re-decode the ring buffer (which still holds the candidate
     * utterance) with NO grammar. The free decoder isn't biased toward the
     * phrase, so noise the grammar decoder forced into "hey shuffle" comes
     * out as whatever it really was. Fuzzy matching absorbs small model
     * misspellings. Runs only on candidates, so the cost is negligible.
     * Fails open — a broken verifier must not brick wake detection.
     */
    private fun verifyCandidate(): Boolean {
        val m = model ?: return true
        val audio = pipeline?.snapshot() ?: return true
        if (audio.isEmpty()) return true
        return try {
            val verifier = Recognizer(m, SAMPLE_RATE)
            try {
                verifier.acceptWaveForm(audio, audio.size)
                val text = JSONObject(verifier.finalResult).optString("text").lowercase()
                val tokens = text.split(' ').filter { it.isNotBlank() }
                val ok = phrase.split(' ').filter { it.isNotBlank() }.all { pw ->
                    val maxDist = maxOf(1, pw.length / 3)
                    tokens.any { levenshtein(pw, it) <= maxDist }
                }
                if (!ok) {
                    Log.i(TAG, "Wake candidate failed verification (grammar heard \"$phrase\", free decode heard \"$text\")")
                }
                ok
            } finally {
                runCatching { verifier.close() }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Wake verification pass errored — allowing trigger", e)
            true
        }
    }

    private fun levenshtein(a: String, b: String): Int {
        if (a == b) return 0
        val prev = IntArray(b.length + 1) { it }
        val cur = IntArray(b.length + 1)
        for (i in 1..a.length) {
            cur[0] = i
            for (j in 1..b.length) {
                val cost = if (a[i - 1] == b[j - 1]) 0 else 1
                cur[j] = minOf(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
            }
            System.arraycopy(cur, 0, prev, 0, cur.size)
        }
        return prev[b.length]
    }

    private fun onWakeWord() {
        Log.i(TAG, "Wake phrase detected")
        stopRecognizer() // free the mic for the call
        // Quick full-volume ack ("phrase heard") — the louder ready tone
        // follows from VoiceCallService once the agent is actually live.
        runCatching {
            val tone = ToneGenerator(AudioManager.STREAM_NOTIFICATION, ToneGenerator.MAX_VOLUME)
            tone.startTone(ToneGenerator.TONE_PROP_ACK, 150)
            scope.launch(Dispatchers.IO) {
                kotlinx.coroutines.delay(400)
                runCatching { tone.release() }
            }
        }

        // COMMAND mode: one task, agent hangs itself up — pure hands-free.
        VoiceCallService.start(this, mode = com.hobbycoders.aishuffle.voice.SessionMode.COMMAND)
        val callScreen = Intent(this, VoiceCallActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (Settings.canDrawOverlays(this)) {
            // Overlay permission also exempts us from background-start limits.
            runCatching { startActivity(callScreen) }
        } else {
            // Fall back to a full-screen, high-priority notification.
            val fullScreen = PendingIntent.getActivity(
                this, 2, callScreen,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
            val notification = NotificationCompat.Builder(this, NotificationHelper.CHANNEL_HIGH)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(getString(R.string.wake_detected_title))
                .setContentText(getString(R.string.wake_detected_text))
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setCategory(NotificationCompat.CATEGORY_CALL)
                .setAutoCancel(true)
                .setFullScreenIntent(fullScreen, true)
                .build()
            androidx.core.app.NotificationManagerCompat.from(this)
                .notify(WAKE_NOTIFICATION_ID, notification)
        }
    }

    override fun onDestroy() {
        if (deviceCallbackRegistered) {
            runCatching {
                (getSystemService(AUDIO_SERVICE) as AudioManager)
                    .unregisterAudioDeviceCallback(deviceCallback)
            }
            deviceCallbackRegistered = false
        }
        stopRecognizer()
        runCatching { model?.close() }
        model = null
        scope.cancel()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "WakeWordService"
        private const val NOTIFICATION_ID = 2002
        private const val WAKE_NOTIFICATION_ID = 2003
        private const val SAMPLE_RATE = 16000.0f

        /** Minimum average per-word confidence for a wake trigger. */
        private const val MIN_CONFIDENCE = 0.85

        /** Plausible-timing window for the whole phrase. */
        private const val MIN_PHRASE_SEC = 0.3
        private const val MAX_PHRASE_SEC = 2.5

        /** Longest allowed pause between consecutive phrase words. */
        private const val MAX_WORD_GAP_SEC = 0.6

        /** Start listening if enabled + ready; safe to call any time. */
        fun startIfEnabled(context: Context) {
            val prefs = AssistPrefs.get(context)
            if (!prefs.wakeWordEnabled) return
            if (!VoskModelManager.isReady(context)) return
            runCatching {
                context.startForegroundService(Intent(context, WakeWordService::class.java))
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, WakeWordService::class.java))
        }
    }
}
