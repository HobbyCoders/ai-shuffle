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
import com.hobbycoders.aishuffle.data.AssistPrefs
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
 * [VoiceCallState]; killing the activity does not end the call.
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
        // Claim the call SYNCHRONOUSLY so a near-simultaneous second starter
        // (e.g. the call screen opening right after the wake word) sees it
        // as active and can't restart it in a different mode.
        VoiceCallState.reset()
        VoiceCallState.update { it.copy(status = CallStatus.CONNECTING) }
        val serverId = intent?.getStringExtra(EXTRA_SERVER_ID)
            ?: AssistPrefs.get(this).voiceServerId
        scope.launch { connect(serverId) }
        return START_NOT_STICKY
    }

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
        val openIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, VoiceCallActivity::class.java)
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
        val srv = serverId?.let { store.getById(it) } ?: store.getAll().firstOrNull()
        if (srv == null) {
            failCall(getString(R.string.voice_error_no_server))
            return
        }
        server = srv
        VoiceCallState.update { it.copy(serverName = srv.name) }

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

        // 2. Voice mode must be enabled and funded server-side.
        when (val enabled = VoiceApi.isVoiceModeEnabled(srv, cookie)) {
            is ApiResult.Success -> if (!enabled.data) {
                failCall(getString(R.string.voice_error_disabled)); return
            }
            else -> { /* can't read settings — try the session anyway */ }
        }

        // 3. Create the voice session (server mints the ephemeral ek_ secret).
        val session = when (val result = VoiceApi.createSession(srv, cookie)) {
            is ApiResult.Success -> result.data
            is ApiResult.NeedsCfAccess -> {
                failCall(getString(R.string.cf_access_reauth)); return
            }
            is ApiResult.Failure -> {
                failCall(result.message); return
            }
        }
        voiceSessionId = session.sessionId

        // 4. WebRTC leg to OpenAI.
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

            // 5. Attach the server's sideband, declaring OUR phone tools so the
            // model sees them and the sideband leaves their servicing to us.
            if (callId != null) {
                val attach = VoiceApi.attachCall(
                    srv, cookie, session.sessionId, callId,
                    clientTools = tools.declarations(),
                    clientInstructions = PhoneTools.instructionsFor(mode),
                )
                if (attach is ApiResult.Failure) {
                    Log.w(TAG, "Sideband attach failed: ${attach.message}")
                }
            } else {
                Log.w(TAG, "No call_id from OpenAI — server sideband cannot join")
            }
        } catch (e: Exception) {
            failCall("Call setup failed: ${e.message}")
            return
        }

        VoiceCallState.update { it.copy(status = CallStatus.LIVE) }
        lastActivityMs = System.currentTimeMillis()

        if (mode == SessionMode.COMMAND) {
            // The "speak now" cue: the wake chime only meant "phrase heard" —
            // this one means the agent is actually on the line.
            runCatching {
                ToneGenerator(AudioManager.STREAM_NOTIFICATION, 85)
                    .startTone(ToneGenerator.TONE_PROP_BEEP2, 200)
            }
            startIdleWatchdog()
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

        scope.launch(Dispatchers.IO) {
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
                // Let the model speak its short wrap-up, then hang up. This is
                // what makes hands-free "hey shuffle → call my love" one-shot.
                delay(outcome.endDelayMs)
                endCall(userInitiated = true)
            }
        }
    }

    // ------------------------------------------------------------------
    // Teardown
    // ------------------------------------------------------------------

    private fun failCall(message: String) {
        Log.w(TAG, "Voice call failed: $message")
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
    }

    override fun onDestroy() {
        realtime?.close()
        realtime = null
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
         * Start a call (no-op if one is already active). COMMAND mode = wake
         * word: do one task, then the agent hangs itself up. CHAT mode =
         * button/tile/assist: stays connected until the user ends it.
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
