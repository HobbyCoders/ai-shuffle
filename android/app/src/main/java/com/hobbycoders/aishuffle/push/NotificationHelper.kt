package com.hobbycoders.aishuffle.push

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context

/** Only deliberate ongoing voice calls need a foreground-service notification. */
object NotificationHelper {
    const val CHANNEL_SERVICE = "ai_shuffle_voice"
    fun ensureChannels(context: Context) {
        context.getSystemService(NotificationManager::class.java)?.createNotificationChannel(
            NotificationChannel(CHANNEL_SERVICE, "Voice conversation", NotificationManager.IMPORTANCE_LOW)
                .apply { description = "Visible only during a voice conversation you started." },
        )
    }
}
