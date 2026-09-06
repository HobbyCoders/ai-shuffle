package com.hobbycoders.aishuffle.voice

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioManager
import android.media.ToneGenerator
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.net.ApiResult
import com.hobbycoders.aishuffle.net.VoiceApi
import com.hobbycoders.aishuffle.push.NotificationHelper
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.util.Collections

/**
 * Owns one live voice call end-to-end: mints the web session, creates the voice
 * session on the server, runs the WebRTC leg against OpenAI Realtime, attaches
 * the server's sideband (declaring the phone tools), and services those phone
 * tool calls locally. The UI ([VoiceCallActivity]) is just a viewer over
 * [VoiceCallState]; leaving the activity ends the call and releases audio.
 */
class VoiceCallService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    private var server: Server? = null
    private var sessionCookie: String? = null
    private var voiceSessionId: String? = null
    private var realtime: RealtimeClient? = null
    private lateinit var tools: PhoneTools

    /** How this session was started; COMMAND sessions end themselves. */
    private var mode = SessionMode.CHAT

    /** Last time anything happened on the call (speech either way, a tool). */
    @Volatile
    private var lastActivityMs = 0L
    private var watchdog: Job? = null
    private var connectionJob: Job? = null

    /** Function calls already serviced (each arrives twice — see sideband notes). */
    private val servicedCalls = Collections.synchronizedSet(mutableSetOf<String>())

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        tools = PhoneTools(applicationContext)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_HANGUP -> {
                endCall(userInitiated = true)
                return START_NOT_STICKY
            }
            ACTION_TOGGLE_MUTE -> {
                val muted = !VoiceCallState.state.value.muted
                realtime?.setMicEnabled(!muted)
                VoiceCallState.update { it.copy(muted = muted) }
                return START_NOT_STICKY
            }
        }

        if (androidx.core.content.ContextCompat.checkSelfPermission(this, android.Manifest.permission.RECORD_AUDIO) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            VoiceCallState.update { it.copy(status = CallStatus.ERROR, error = "Microphone permission is required. Open the voice screen to grant it.") }
            stopSelf()
            return START_NOT_STICKY
        }

        // Started via startForegroundService: startForeground() must run even
        // if we bail out right after, or the system kills the app. On Android
        // 14 a mic-type FGS from a background context can be refused HERE,
        // inside startForeground — surface it instead of crashing.
        try {
            startForegroundWithNotification()
        } catch (e: Exception) {
            Log.w(TAG, "Mic foreground start refused", e)
            VoiceCallState.update {
                it.copy(status = CallStatus.ERROR, error = "Microphone access was blocked — open the app and try again.")
            }
            stopSelf()
            return START_NOT_STICKY
        }

        if (VoiceCallState.isActive) return START_NOT_STICKY // one call at a time

        mode = if (intent?.getStringExtra(EXTRA_MODE) == MODE_COMMAND) {
            SessionMode.COMMAND
        } else {
            SessionMode.CHAT
        }
        // Claim synchronously so repeated taps cannot create duplicate calls.
        VoiceCallState.reset()
        val serverId = intent?.getStringExtra(EXTRA_SERVER_ID)
        VoiceCallState.update { it.copy(status = CallStatus.CONNECTING, serverId = serverId) }
        connectionJob = scope.launch {
            try { connect(serverId) }
            catch (e: kotlinx.coroutines.CancellationException) { throw e }
            catch (_: Exception) { failCall("Cannot access the selected server or Android audio services. Reopen voice and retry.") }
        }
        return START_NOT_STICKY
    }

    private fun startForegroundWithNotification() {
        val notification = buildNotification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun buildNotification(): Notification {
        NotificationHelper.ensureChannels(this)
        val openIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, VoiceCallActivity::class.java)
                .putExtra(EXTRA_SERVER_ID, server?.id ?: VoiceCallState.state.value.serverId)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val hangupIntent = PendingIntent.getService(
            this, 1,
            Intent(this, VoiceCallService::class.java).setAction(ACTION_HANGUP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, NotificationHelper.CHANNEL_SERVICE)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.voice_call_notification_title))
            .setContentText(getString(R.string.voice_call_notification_text))
            .setOngoing(true)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setContentIntent(openIntent)
            .addAction(0, getString(R.string.voice_hang_up), hangupIntent)
            .build()
    }

    // ------------------------------------------------------------------
    // Call setup
    // ------------------------------------------------------------------

    private suspend fun connect(serverId: String?) {
        // State was already set to CONNECTING synchronously in onStartCommand.
        val store = ServerStore.get(this)
        val srv = serverId?.let { store.getById(it) }
        if (srv == null) {
            failCall(getString(R.string.voice_error_no_server))
            return
        }
        server = srv
        VoiceCallState.update { it.copy(serverName = srv.name, serverId = srv.id) }

        // 1. aim_ key -> web session cookie (voice endpoints don't take aim_).
        val cookie = when (val result = ApiClient.createWebSession(srv)) {
            is ApiResult.Success -> result.data
            is ApiResult.NeedsCfAccess -> {
                failCall(getString(R.string.cf_access_reauth)); return
            }
            is ApiResult.Failure -> {
                failCall(result.message); return
            }
        }
        sessionCookie = cookie

        // 2. Create the voice session (server mints the ephemeral ek_ secret).
        // Session creation returns a descriptive error when voice mode is off.
        val sessionMode = if (mode == SessionMode.COMMAND) "command" else "chat"
        val session = when (val result = VoiceApi.createSession(srv, cookie, sessionMode)) {
            is ApiResult.Success -> result.data
            is ApiResult.NeedsCfAccess -> {
                failCall(getString(R.string.cf_access_reauth)); return
            }
            is ApiResult.Failure -> {
                failCall(result.message); return
            }
        }
        voiceSessionId = session.sessionId

        // 3. WebRTC leg to OpenAI.
        val client = try {
            RealtimeClient(this, onEvent = ::handleRealtimeEvent, onDisconnected = {
                scope.launch { endCall(userInitiated = false) }
            })
        } catch (e: Exception) {
            failCall("WebRTC init failed: ${e.message}"); return
        }
        realtime = client

        try {
            val offer = client.createOffer()
            val (answer, callId) = when (
                val result = VoiceApi.postSdpOffer(session.clientSecret, session.model, offer)
            ) {
                is ApiResult.Success -> result.data
                is ApiResult.Failure -> {
                    failCall(result.message); return
                }
                else -> {
                    failCall("Unexpected OpenAI response"); return
                }
            }
            client.acceptAnswer(answer)

            // 4. Attach the server's sideband, declaring OUR phone tools so the
            // model sees them and the sideband leaves their servicing to us.
            if (callId != null) {
                val attach = VoiceApi.attachCall(
                    srv, cookie, session.sessionId, callId,
                    clientTools = tools.declarations(),
                    // The prompt text itself lives server-side (Settings →
                    // Voice) — the app only reports which kind of session
                    // this is.
                    sessionMode = sessionMode,
                )
                if (attach is ApiResult.Failure) {
                    Log.w(TAG, "Sideband attach failed")
                }
            } else {
                Log.w(TAG, "No call_id from OpenAI — server sideband cannot join")
            }
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            failCall("Call setup failed. Check the connection and retry.")
            return
        }

        VoiceCallState.update { it.copy(status = CallStatus.LIVE) }
        lastActivityMs = System.currentTimeMillis()

        playReadyTone()
        if (mode == SessionMode.COMMAND) {
            startIdleWatchdog()
        }
    }

    /**
     * The "speak now" cue means the agent is on the line. Full tone volume, a
     * distinctive rising triple beep, and played on the VOICE_CALL stream so
     * it comes out of whatever device the call itself uses (headset/car),
     * not the phone speaker.
     */
    private fun playReadyTone() {
        runCatching {
            val tone = ToneGenerator(AudioManager.STREAM_VOICE_CALL, ToneGenerator.MAX_VOLUME)
            tone.startTone(ToneGenerator.TONE_SUP_CONFIRM, 400)
            scope.launch {
                delay(600)
                runCatching { tone.release() }
            }
        }
    }

    /**
     * COMMAND sessions must never linger: if nothing has happened for
     * [IDLE_TIMEOUT_MS] (user silent, model idle), hang up. The model is also
     * told to call end_session on silence — this is the belt to that suspender.
     */
    private fun startIdleWatchdog() {
        watchdog?.cancel()
        watchdog = scope.launch {
            while (isActive && VoiceCallState.isActive) {
                delay(5_000)
                if (System.currentTimeMillis() - lastActivityMs > IDLE_TIMEOUT_MS) {
                    Log.i(TAG, "Command session idle — hanging up")
                    endCall(userInitiated = false)
                    break
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // Realtime events (data channel)
    // ------------------------------------------------------------------

    private fun handleRealtimeEvent(event: JSONObject) {
        lastActivityMs = System.currentTimeMillis()
        when (event.optString("type")) {
            "conversation.item.input_audio_transcription.completed" -> {
                val text = event.optString("transcript").trim()
                if (text.isNotEmpty()) {
                    VoiceCallState.update { it.copy(userCaption = text) }
                }
            }
            "response.output_audio_transcript.done" -> {
                val text = event.optString("transcript").trim()
                if (text.isNotEmpty()) {
                    VoiceCallState.update { it.copy(assistantCaption = text) }
                }
            }
            "response.function_call_arguments.done" -> {
                maybeServiceCall(
                    event.optString("call_id"),
                    event.optString("name"),
                    event.optString("arguments"),
                )
            }
            "response.done" -> {
                val output = event.optJSONObject("response")?.optJSONArray("output")
                if (output != null) {
                    for (i in 0 until output.length()) {
                        val item = output.optJSONObject(i) ?: continue
                        if (item.optString("type") == "function_call") {
                            maybeServiceCall(
                                item.optString("call_id"),
                                item.optString("name"),
                                item.optString("arguments"),
                            )
                        }
                    }
                }
            }
        }
    }

    /**
     * Service one of OUR phone tools. Mirrors the sideband's dedupe: the same
     * call arrives twice (arguments.done, then inside response.done). Names we
     * don't own are the sideband's business — never answer them.
     */
    private fun maybeServiceCall(callId: String, name: String, arguments: String) {
        if (callId.isBlank() || name.isBlank()) return
        if (!tools.owns(name)) return
        if (!servicedCalls.add(callId)) return

        scope.launch {
            val args = runCatching { JSONObject(arguments.ifBlank { "{}" }) }
                .getOrDefault(JSONObject())
            val outcome = tools.execute(name, args)
            realtime?.send(JSONObject().apply {
                put("type", "conversation.item.create")
                put("item", JSONObject().apply {
                    put("type", "function_call_output")
                    put("call_id", callId)
                    put("output", outcome.result.toString())
                })
            })
            realtime?.send(JSONObject().put("type", "response.create"))

            if (outcome.endSession) {
                // Let the model speak its short wrap-up before hanging up.
                delay(outcome.endDelayMs)
                endCall(userInitiated = true)
            }
        }
    }

    // ------------------------------------------------------------------
    // Teardown
    // ------------------------------------------------------------------

    private fun failCall(message: String) {
        Log.w(TAG, "Voice call failed")
        VoiceCallState.update { it.copy(status = CallStatus.ERROR, error = message) }
        cleanup()
        stopSelf()
    }

    private fun endCall(userInitiated: Boolean) {
        VoiceCallState.update { it.copy(status = CallStatus.ENDED) }
        cleanup()
        stopSelf()
        if (!userInitiated) {
            Log.i(TAG, "Voice call ended by transport")
        }
    }

    /** Close the WebRTC leg and tell the server to drop the sideband. */
    private fun cleanup() {
        scope.cancel()
        connectionJob?.cancel()
        connectionJob = null
        watchdog?.cancel()
        watchdog = null
        realtime?.close()
        realtime = null
        val srv = server
        val cookie = sessionCookie
        val sessionId = voiceSessionId
        if (srv != null && cookie != null && sessionId != null) {
            // Fire-and-forget on a background scope that survives stopSelf().
            CoroutineScope(Dispatchers.IO).launch {
                VoiceApi.hangup(srv, cookie, sessionId)
            }
        }
        voiceSessionId = null
        sessionCookie = null
        server = null
        stopForeground(STOP_FOREGROUND_REMOVE)
    }

    override fun onDestroy() {
        cleanup()
        scope.cancel()
        if (VoiceCallState.isActive) {
            VoiceCallState.update { it.copy(status = CallStatus.ENDED) }
        }
        super.onDestroy()
    }

    companion object {
        private const val TAG = "VoiceCallService"
        private const val NOTIFICATION_ID = 2001
        private const val IDLE_TIMEOUT_MS = 30_000L

        const val EXTRA_SERVER_ID = "server_id"
        const val EXTRA_MODE = "session_mode"
        const val MODE_COMMAND = "command"
        const val MODE_CHAT = "chat"
        const val ACTION_HANGUP = "com.hobbycoders.aishuffle.voice.HANGUP"
        const val ACTION_TOGGLE_MUTE = "com.hobbycoders.aishuffle.voice.TOGGLE_MUTE"

        /**
         * Start a deliberate continuous call. CHAT remains connected until ended.
         * A selected server and previously granted microphone access are required.
         */
        fun start(
            context: Context,
            serverId: String? = null,
            mode: SessionMode = SessionMode.CHAT,
        ) {
            val intent = Intent(context, VoiceCallService::class.java)
                .putExtra(
                    EXTRA_MODE,
                    if (mode == SessionMode.COMMAND) MODE_COMMAND else MODE_CHAT,
                )
            serverId?.let { intent.putExtra(EXTRA_SERVER_ID, it) }
            context.startForegroundService(intent)
        }

        fun hangup(context: Context) {
            context.startService(
                Intent(context, VoiceCallService::class.java).setAction(ACTION_HANGUP),
            )
        }

        fun toggleMute(context: Context) {
            context.startService(
                Intent(context, VoiceCallService::class.java).setAction(ACTION_TOGGLE_MUTE),
            )
        }
    }
}
