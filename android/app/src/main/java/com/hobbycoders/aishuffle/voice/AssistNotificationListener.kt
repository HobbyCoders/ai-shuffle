package com.hobbycoders.aishuffle.voice

import android.app.Notification
import android.content.ComponentName
import android.content.Context
import android.provider.Settings
import android.service.notification.NotificationListenerService
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * Grants the voice agent read access to the status bar: the `read_notifications`
 * tool serves a snapshot of what is currently posted (message previews, emails,
 * app alerts). The user enables this in system settings (Notification access);
 * until then [snapshot] returns null and the tool reports it gracefully.
 */
class AssistNotificationListener : NotificationListenerService() {

    override fun onListenerConnected() {
        instance = this
    }

    override fun onListenerDisconnected() {
        if (instance === this) instance = null
    }

    companion object {
        @Volatile
        private var instance: AssistNotificationListener? = null

        /** Whether the user has granted notification access to this app. */
        fun isEnabled(context: Context): Boolean {
            val component = ComponentName(context, AssistNotificationListener::class.java)
            val enabled = Settings.Secure.getString(
                context.contentResolver, "enabled_notification_listeners",
            ) ?: return false
            return enabled.split(':').any {
                it == component.flattenToString() || it == component.flattenToShortString()
            }
        }

        /**
         * The current status-bar notifications as a JSON array, newest first,
         * or null when access is not granted / the listener is not connected.
         * Ongoing/service notices (like our own connection notice) are skipped.
         */
        fun snapshot(): JSONArray? {
            val listener = instance ?: return null
            val active = runCatching { listener.activeNotifications }.getOrNull() ?: return null
            val formatter = DateTimeFormatter.ofPattern("EEE h:mm a")
            val arr = JSONArray()
            active
                .filter { it.isClearable } // skip ongoing/foreground-service notices
                .sortedByDescending { it.postTime }
                .take(15)
                .forEach { sbn ->
                    val extras = sbn.notification.extras
                    val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
                    val text = extras.getCharSequence(Notification.EXTRA_TEXT)?.toString()
                        ?: extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()
                        ?: ""
                    if (title.isBlank() && text.isBlank()) return@forEach
                    val appLabel = runCatching {
                        val pm = listener.packageManager
                        pm.getApplicationLabel(pm.getApplicationInfo(sbn.packageName, 0)).toString()
                    }.getOrDefault(sbn.packageName)
                    arr.put(JSONObject().apply {
                        put("app", appLabel)
                        put("title", title.take(120))
                        put("text", text.take(300))
                        put(
                            "when",
                            Instant.ofEpochMilli(sbn.postTime)
                                .atZone(ZoneId.systemDefault())
                                .format(formatter),
                        )
                    })
                }
            return arr
        }
    }
}
