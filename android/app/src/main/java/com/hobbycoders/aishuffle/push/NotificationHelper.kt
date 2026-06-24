package com.hobbycoders.aishuffle.push

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.ui.ServerListActivity
import org.json.JSONObject

/**
 * Creates the notification channels and turns a decoded UnifiedPush payload into
 * a user-visible notification that deep-links back into the app.
 */
object NotificationHelper {

    /** High-importance channel for questions / permission requests. */
    const val CHANNEL_HIGH = "ai_shuffle_actions"

    /** Default channel for informational messages (e.g. session done). */
    const val CHANNEL_DEFAULT = "ai_shuffle_updates"

    private const val GROUP = "ai_shuffle"

    fun ensureChannels(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java) ?: return

        val high = NotificationChannel(
            CHANNEL_HIGH,
            context.getString(R.string.channel_actions_name),
            NotificationManager.IMPORTANCE_HIGH,
        ).apply { description = context.getString(R.string.channel_actions_desc) }

        val default = NotificationChannel(
            CHANNEL_DEFAULT,
            context.getString(R.string.channel_updates_name),
            NotificationManager.IMPORTANCE_DEFAULT,
        ).apply { description = context.getString(R.string.channel_updates_desc) }

        manager.createNotificationChannel(high)
        manager.createNotificationChannel(default)
    }

    /**
     * Posts a notification built from a decoded payload.
     *
     * @param title     notification title (falls back to the app name)
     * @param body      notification text
     * @param priority  "high" routes to the high-importance channel
     * @param data      the payload's `data` object, copied into the tap intent
     */
    fun show(
        context: Context,
        title: String?,
        body: String?,
        priority: String?,
        data: JSONObject?,
    ) {
        ensureChannels(context)

        val isHigh = priority.equals("high", ignoreCase = true)
        val channelId = if (isHigh) CHANNEL_HIGH else CHANNEL_DEFAULT

        val tapIntent = Intent(context, ServerListActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            data?.let { obj ->
                obj.optString("type").takeIf { it.isNotBlank() }
                    ?.let { putExtra(EXTRA_TYPE, it) }
                obj.optString("session_id").takeIf { it.isNotBlank() }
                    ?.let { putExtra(EXTRA_SESSION_ID, it) }
                obj.optString("request_id").takeIf { it.isNotBlank() }
                    ?.let { putExtra(EXTRA_REQUEST_ID, it) }
            }
        }

        val pendingFlags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        val pendingIntent = PendingIntent.getActivity(
            context,
            (System.currentTimeMillis() % Int.MAX_VALUE).toInt(),
            tapIntent,
            pendingFlags,
        )

        val notification = NotificationCompat.Builder(context, channelId)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title?.takeIf { it.isNotBlank() } ?: context.getString(R.string.app_name))
            .setContentText(body.orEmpty())
            .setStyle(NotificationCompat.BigTextStyle().bigText(body.orEmpty()))
            .setPriority(if (isHigh) NotificationCompat.PRIORITY_HIGH else NotificationCompat.PRIORITY_DEFAULT)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setGroup(GROUP)
            .setAutoCancel(true)
            .setContentIntent(pendingIntent)
            .build()

        // POST_NOTIFICATIONS may be denied on API 33+; NotificationManagerCompat
        // silently no-ops in that case, so this is safe to call unconditionally.
        NotificationManagerCompat.from(context)
            .notify((System.currentTimeMillis() % Int.MAX_VALUE).toInt(), notification)
    }

    const val EXTRA_TYPE = "push_type"
    const val EXTRA_SESSION_ID = "push_session_id"
    const val EXTRA_REQUEST_ID = "push_request_id"
}
