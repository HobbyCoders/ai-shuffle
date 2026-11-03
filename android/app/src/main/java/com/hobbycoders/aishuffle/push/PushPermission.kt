package com.hobbycoders.aishuffle.push

import android.Manifest
import android.os.Build
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.NotificationManagerCompat
import com.hobbycoders.aishuffle.BuildConfig
import com.hobbycoders.aishuffle.data.ServerStore

object PushPermission {
    /** The build's push setting. Unit tests turn it on to drive the real startup request without Firebase config. */
    @androidx.annotation.VisibleForTesting internal var enabled = BuildConfig.PUSH_ENABLED

    fun ask(activity: AppCompatActivity, dialogOpen: (Boolean) -> Unit = {}, request: (String) -> Unit) {
        if (!enabled || Build.VERSION.SDK_INT < 33 ||
            NotificationManagerCompat.from(activity).areNotificationsEnabled() ||
            ServerStore.get(activity).getAll().none { NeedsYouNotifications.authorized(activity, it) }) return
        val prefs = activity.getSharedPreferences("needs_you_permission", 0)
        if (prefs.getBoolean("asked", false)) return
        prefs.edit().putBoolean("asked", true).commit()
        prompt(dialogOpen, request)
    }

    internal fun prompt(dialogOpen: (Boolean) -> Unit, request: (String) -> Unit) {
        dialogOpen(true)
        request(Manifest.permission.POST_NOTIFICATIONS)
    }
}
