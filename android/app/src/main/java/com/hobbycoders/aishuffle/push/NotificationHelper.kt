package com.hobbycoders.aishuffle.push

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.TaskStackBuilder
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.ui.ChatActivity
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

    /** Min-importance channel for the persistent foreground-service notice. */
    const val CHANNEL_SERVICE = "ai_shuffle_service"

    /** Fixed id for the ongoing foreground-service notification. */
    const val FOREGROUND_NOTIFICATION_ID = 1001

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

        // IMPORTANCE_MIN keeps the ongoing "connected" notice silent and collapsed
        // at the bottom of the shade, with no status-bar icon where possible.
        val service = NotificationChannel(
            CHANNEL_SERVICE,
            context.getString(R.string.channel_service_name),
            NotificationManager.IMPORTANCE_MIN,
        ).apply {
            description = context.getString(R.string.channel_service_desc)
            setShowBadge(false)
        }

        manager.createNotificationChannel(high)
        manager.createNotificationChannel(default)
        manager.createNotificationChannel(service)
    }

    /**
     * Builds the persistent, low-key notification shown while the push stream
     * service is running. Tapping it opens the server list.
     */
    fun buildServiceNotification(context: Context, contentText: String): android.app.Notification {
        ensureChannels(context)
        val tapIntent = Intent(context, ServerListActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pendingIntent = PendingIntent.getActivity(
            context,
            0,
            tapIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(context, CHANNEL_SERVICE)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(context.getString(R.string.app_name))
            .setContentText(contentText)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setOngoing(true)
            .setShowWhen(false)
            .setContentIntent(pendingIntent)
            .build()
    }

    /**
     * Posts a notification built from a decoded payload.
     *
     * @param title     notification title (falls back to the app name)
     * @param body      notification text
     * @param priority  "high" routes to the high-importance channel
     * @param data      the payload's `data` object, copied into the tap intent
     * @param serverId  when known, tapping deep-links into this server's chat
     *                  (with the server list as the Back target); null falls
     *                  back to opening the server list.
     */
    fun show(
        context: Context,
        title: String?,
        body: String?,
        priority: String?,
        data: JSONObject?,
        serverId: String? = null,
    ) {
        ensureChannels(context)

        val isHigh = priority.equals("high", ignoreCase = true)
        val channelId = if (isHigh) CHANNEL_HIGH else CHANNEL_DEFAULT

        val pendingFlags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        val pendingIntent = buildTapIntent(
            context = context,
            serverId = serverId,
            data = data,
            requestCode = (System.currentTimeMillis() % Int.MAX_VALUE).toInt(),
            pendingFlags = pendingFlags,
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

    /**
     * Tapping a notification should open the chat for the server that produced
     * it. When that server is known, deep-link straight into [ChatActivity] with
     * the server list synthesised as the Back target; otherwise (e.g. the legacy
     * UnifiedPush path, which can't attribute a server) open the server list.
     */
    private fun buildTapIntent(
        context: Context,
        serverId: String?,
        data: JSONObject?,
        requestCode: Int,
        pendingFlags: Int,
    ): PendingIntent {
        if (serverId != null) {
            val chatIntent = Intent(context, ChatActivity::class.java)
                .putExtra(ChatActivity.EXTRA_SERVER_ID, serverId)
            applyPayloadExtras(chatIntent, data)
            // ChatActivity declares ServerListActivity as its parent, so this
            // builds a [ServerList, Chat] back-stack in a fresh task.
            return TaskStackBuilder.create(context)
                .addNextIntentWithParentStack(chatIntent)
                .getPendingIntent(requestCode, pendingFlags)!!
        }
        val listIntent = Intent(context, ServerListActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        applyPayloadExtras(listIntent, data)
        return PendingIntent.getActivity(context, requestCode, listIntent, pendingFlags)
    }

    /** Copy the payload's type/session_id/request_id onto a tap intent. */
    private fun applyPayloadExtras(intent: Intent, data: JSONObject?) {
        val obj = data ?: return
        obj.optString("type").takeIf { it.isNotBlank() }?.let { intent.putExtra(EXTRA_TYPE, it) }
        obj.optString("session_id").takeIf { it.isNotBlank() }?.let { intent.putExtra(EXTRA_SESSION_ID, it) }
        obj.optString("request_id").takeIf { it.isNotBlank() }?.let { intent.putExtra(EXTRA_REQUEST_ID, it) }
    }

    const val EXTRA_TYPE = "push_type"
    const val EXTRA_SESSION_ID = "push_session_id"
    const val EXTRA_REQUEST_ID = "push_request_id"
}
