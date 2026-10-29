package com.hobbycoders.aishuffle.push

import android.Manifest
import android.os.Build
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.NotificationManagerCompat
import com.hobbycoders.aishuffle.BuildConfig
import com.hobbycoders.aishuffle.data.ServerStore

object PushPermission {
    fun ask(activity: AppCompatActivity, dialogOpen: (Boolean) -> Unit = {}, request: (String) -> Unit) {
        if (!BuildConfig.PUSH_ENABLED || Build.VERSION.SDK_INT < 33 ||
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
