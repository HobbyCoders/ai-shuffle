package com.hobbycoders.aishuffle.ui

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import androidx.annotation.VisibleForTesting
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.hobbycoders.aishuffle.R

/**
 * Keeps the page's voice call running while chat is in the background: a
 * foreground service of type microphone with an ongoing "Voice call" notification
 * (Mute/Unmute, Hang up, tap to return). It holds no call itself; the call stays
 * the chat page's own. [VoiceCallKeeper] starts it while chat is on screen
 * (Android 14 allows a microphone service only then) and stops it when the call ends.
 *
 * Notification buttons come back here and go to the [Owner] (the chat that owns
 * the call), which hands them to its page. Main thread only.
 */
class VoiceCallService : Service() {
    /** What the notification shows. [muted] is null when the page cannot take commands (an older worker UI). */
    data class Call(val serverId: String, val workerName: String, val title: String? = null,
                    val status: String? = null, val muted: Boolean? = null, val since: Long? = null)

    interface Owner {
        fun onCommand(command: MobileUi.VoiceCallCommand)
        /** The service is in the foreground: the call now survives the lock screen. */
        fun onServiceForeground()
        /**
         * The service could not go foreground or stopped without the owner asking
         * (hang-up fallback, task removed, Android stopped it, another chat took over).
         * The call is no longer kept alive.
         */
        fun onServiceStopped()
    }

    /** The [start] this instance went foreground for; a later call is not this one's to release. */
    private var servedSession = -1

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val current = call
        if (current == null) {
            // Ended before the service came up. Still promote once: a service started
            // with startForegroundService must call startForeground before it stops.
            promote(Call("", getString(R.string.app_name)))
            stopSelf()
            return START_NOT_STICKY
        }
        when (intent?.action) {
            ACTION_MUTE -> owner?.onCommand(MobileUi.VoiceCallCommand.MUTE)
            ACTION_UNMUTE -> owner?.onCommand(MobileUi.VoiceCallCommand.UNMUTE)
            ACTION_HANGUP -> {
                owner?.onCommand(MobileUi.VoiceCallCommand.HANGUP)
                // The page ends the call and says so, which stops this service. A page that
                // cannot answer must not leave an undismissable call notification behind.
                handler.removeCallbacks(hangupFallback)
                handler.postDelayed(hangupFallback, HANGUP_FALLBACK_MS)
            }
            // Promoting again also refreshes the notification for a new owner.
            else -> if (promote(current)) owner?.onServiceForeground() else {
                // Not in the foreground, so nothing keeps the call alive: say so and stop.
                release(notify = true)
                if (!running) stopSelf()
            }
        }
        return START_NOT_STICKY
    }

    private fun promote(current: Call): Boolean = try {
        ensureChannel(this)
        goForeground(this, notification(this, current))
        running = true
        servedSession = session
        true
    } catch (error: Exception) {
        // Android refused the microphone service (permission revoked, app not visible).
        // The call still works while chat is open; it just cannot outlive the screen.
        Log.w(TAG, "Could not keep the voice call in the foreground", error)
        false
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        // The chat (and its call) goes with the task; nothing left to keep alive.
        stopSelf()
    }

    override fun onDestroy() {
        running = false
        // A stop the owner did not ask for (task removed, Android). Owner-requested stops
        // and the hang-up fallback have already released it.
        if (owner != null && servedSession == session) release(notify = true)
        servedSession = -1
        super.onDestroy()
    }

    companion object {
        private const val TAG = "VoiceCallService"
        const val CHANNEL = "voice_calls"
        const val NOTIFICATION_ID = 741
        internal const val ACTION_SHOW = "com.hobbycoders.aishuffle.voice.SHOW"
        internal const val ACTION_MUTE = "com.hobbycoders.aishuffle.voice.MUTE"
        internal const val ACTION_UNMUTE = "com.hobbycoders.aishuffle.voice.UNMUTE"
        internal const val ACTION_HANGUP = "com.hobbycoders.aishuffle.voice.HANGUP"
        @VisibleForTesting internal const val HANGUP_FALLBACK_MS = 5_000L

        private val handler by lazy { Handler(Looper.getMainLooper()) }
        @VisibleForTesting internal var call: Call? = null
            private set
        private var owner: Owner? = null
        @VisibleForTesting internal var running = false
            private set
        private var appContext: Context? = null
        /** Counts [start] calls, so a destroyed instance can tell its call from a newer one. */
        private var session = 0
        private val hangupFallback = Runnable {
            val context = appContext ?: return@Runnable
            release(notify = true)
            context.stopService(Intent(context, VoiceCallService::class.java))
        }

        /** How the service goes foreground; tests replace it to simulate Android refusing. */
        @VisibleForTesting internal var goForeground: (Service, Notification) -> Unit = { service, notification ->
            ServiceCompat.startForeground(service, NOTIFICATION_ID, notification,
                if (Build.VERSION.SDK_INT >= 30) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0)
        }

        /** Forgets the call and its owner (no Activity is held after this); [notify] tells the owner. */
        private fun release(notify: Boolean) {
            handler.removeCallbacks(hangupFallback)
            val previous = owner
            call = null; owner = null; appContext = null
            if (notify) previous?.onServiceStopped()
        }

        /**
         * Asks Android for the service for [owner]'s call. False when Android refuses
         * (chat not on screen). The call is kept alive only once [Owner.onServiceForeground] arrives.
         */
        fun start(context: Context, current: Call, owner: Owner): Boolean {
            if (this.owner !== owner) release(notify = true)
            call = current; this.owner = owner; appContext = context.applicationContext; session++
            return try {
                ContextCompat.startForegroundService(context, Intent(context, VoiceCallService::class.java).setAction(ACTION_SHOW))
                true
            } catch (error: Exception) {
                Log.w(TAG, "Could not start the voice call service", error)
                release(notify = false)
                false
            }
        }

        /** Shows [current] in the notification, if [owner] still owns the call. */
        fun update(context: Context, owner: Owner, current: Call) {
            if (owner !== this.owner) return
            call = current
            if (running && canNotify(context)) runCatching { post(context, current) }
                .onFailure { Log.w(TAG, "Could not update the call notification", it) }
        }

        @SuppressLint("MissingPermission") // only called after canNotify
        private fun post(context: Context, current: Call) =
            NotificationManagerCompat.from(context).notify(NOTIFICATION_ID, notification(context, current))

        /** Stops the service, if [owner] still owns the call. */
        fun stop(context: Context, owner: Owner) {
            if (owner !== this.owner) return
            release(notify = false)
            context.stopService(Intent(context, VoiceCallService::class.java))
        }

        private fun canNotify(context: Context) = Build.VERSION.SDK_INT < 33 ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

        /** Whether the call notification can appear at all (app notifications and this channel on). */
        fun notificationsShown(context: Context): Boolean {
            val manager = NotificationManagerCompat.from(context)
            if (!manager.areNotificationsEnabled()) return false
            return Build.VERSION.SDK_INT < 26 ||
                manager.getNotificationChannel(CHANNEL)?.importance != NotificationManager.IMPORTANCE_NONE
        }

        fun ensureChannel(context: Context) {
            if (Build.VERSION.SDK_INT < 26) return
            val manager = context.getSystemService(NotificationManager::class.java)
            if (manager.getNotificationChannel(CHANNEL) != null) return
            // Shown on the lock screen like a call, but never rings or buzzes.
            manager.createNotificationChannel(NotificationChannel(CHANNEL, "Voice calls", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "Shows a voice call while it runs with the screen off or in another app."
                setSound(null, null); enableVibration(false); setShowBadge(false)
            })
        }

        @VisibleForTesting internal fun title(current: Call) = "Voice call · " + (current.title ?: current.workerName)

        @VisibleForTesting internal fun text(current: Call): String {
            val status = when (current.status) {
                "connecting" -> "Connecting…"
                "reconnecting" -> "Reconnecting…"
                "live" -> "On call"
                else -> "Tap to return to the call"
            }
            return if (current.muted == true) "$status · Muted" else status
        }

        @VisibleForTesting internal fun notification(context: Context, current: Call): Notification {
            val open = PendingIntent.getActivity(context, 0,
                Intent(context, ChatActivity::class.java).putExtra(ChatActivity.EXTRA_SERVER_ID, current.serverId)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT or Intent.FLAG_ACTIVITY_SINGLE_TOP),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            fun build(title: String) = NotificationCompat.Builder(context, CHANNEL)
                .setSmallIcon(R.drawable.ic_shuffle)
                .setColor(ContextCompat.getColor(context, R.color.primary))
                .setContentTitle(title)
                .setContentText(text(current))
                .setContentIntent(open)
                .setOngoing(true).setSilent(true).setOnlyAlertOnce(true)
                .setCategory(NotificationCompat.CATEGORY_CALL)
                .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
                .apply {
                    current.since?.let { setWhen(it).setShowWhen(true).setUsesChronometer(true) } ?: setShowWhen(false)
                    current.muted?.let { muted ->
                        addAction(0, if (muted) "Unmute" else "Mute", command(context, if (muted) ACTION_UNMUTE else ACTION_MUTE))
                        addAction(0, "Hang up", command(context, ACTION_HANGUP))
                    }
                }
            // The lock screen shows the controls but not the chat's title.
            return build(title(current))
                .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
                .setPublicVersion(build("Voice call").build())
                .build()
        }

        private fun command(context: Context, action: String) = PendingIntent.getService(context, action.hashCode(),
            Intent(context, VoiceCallService::class.java).setAction(action), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    }
}
