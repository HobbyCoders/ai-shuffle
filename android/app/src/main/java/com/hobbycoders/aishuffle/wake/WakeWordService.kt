package com.hobbycoders.aishuffle.wake

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioManager
import android.media.ToneGenerator
import android.os.Build
import android.os.IBinder
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
import org.json.JSONObject
import org.vosk.Model
import org.vosk.Recognizer
import org.vosk.android.RecognitionListener
import org.vosk.android.SpeechService

/**
 * The always-on wake-word listener: a foreground service running Vosk's small
 * offline model, constrained to a grammar of just the wake phrase, so the only
 * thing it can "hear" is whether the phrase was said. Nothing is recorded or
 * sent anywhere — the realtime AI conversation only starts AFTER the phrase.
 *
 * While a voice call is live the recognizer is stopped (the call owns the mic)
 * and resumes when the call ends.
 */
class WakeWordService : Service(), RecognitionListener {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    private var model: Model? = null
    private var speechService: SpeechService? = null
    private var phrase: String = AssistPrefs.DEFAULT_PHRASE

    @Volatile
    private var triggered = false

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
     * phrase's words or `[unk]`, which makes spotting cheap and reliable.
     */
    private fun startRecognizer(m: Model) {
        stopRecognizer()
        val grammar = """["$phrase", "[unk]"]"""
        val recognizer = Recognizer(m, SAMPLE_RATE, grammar)
        val service = SpeechService(recognizer, SAMPLE_RATE)
        speechService = service
        service.startListening(this)
        Log.i(TAG, "Wake listening for \"$phrase\"")
    }

    private fun stopRecognizer() {
        runCatching { speechService?.stop() }
        runCatching { speechService?.shutdown() }
        speechService = null
    }

    private fun resumeListening() {
        val m = model ?: return
        if (speechService == null && !VoiceCallState.isActive) {
            scope.launch(Dispatchers.IO) { runCatching { startRecognizer(m) } }
        }
    }

    /** Stop the mic while a voice call owns it; resume when the call ends. */
    private fun watchCallState() {
        scope.launch {
            VoiceCallState.state.collect { state ->
                if (VoiceCallState.isActive) {
                    stopRecognizer()
                } else if (model != null && speechService == null) {
                    triggered = false
                    resumeListening()
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // RecognitionListener
    // ------------------------------------------------------------------

    override fun onPartialResult(hypothesis: String?) {
        checkForPhrase(hypothesis, "partial")
    }

    override fun onResult(hypothesis: String?) {
        checkForPhrase(hypothesis, "text")
    }

    override fun onFinalResult(hypothesis: String?) {
        checkForPhrase(hypothesis, "text")
    }

    override fun onError(exception: Exception?) {
        Log.w(TAG, "Wake recognizer error", exception)
        // Try to recover after a beat — mic contention is usually transient.
        scope.launch(Dispatchers.IO) {
            kotlinx.coroutines.delay(3000)
            resumeListening()
        }
    }

    override fun onTimeout() {
        resumeListening()
    }

    private fun checkForPhrase(hypothesis: String?, field: String) {
        if (triggered || hypothesis.isNullOrBlank()) return
        val heard = runCatching { JSONObject(hypothesis).optString(field) }
            .getOrDefault("")
            .lowercase()
        if (heard.contains(phrase)) {
            triggered = true
            onWakeWord()
        }
    }

    private fun onWakeWord() {
        Log.i(TAG, "Wake phrase detected")
        stopRecognizer() // free the mic for the call
        runCatching {
            ToneGenerator(AudioManager.STREAM_NOTIFICATION, 80)
                .startTone(ToneGenerator.TONE_PROP_ACK, 150)
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
