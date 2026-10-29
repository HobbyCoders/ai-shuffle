package com.hobbycoders.aishuffle.push

import android.Manifest
import android.os.Build
import androidx.appcompat.app.AlertDialog
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
        prompt(activity, dialogOpen, request)
    }

    internal fun prompt(activity: AppCompatActivity, dialogOpen: (Boolean) -> Unit, request: (String) -> Unit): AlertDialog {
        var requesting = false
        dialogOpen(true)
        return AlertDialog.Builder(activity).setTitle("Only when a chat needs you")
            .setMessage("Allow AI Shuffle to notify you for questions, approvals, private-access requests, or an agent explicitly asking for you. No completion, parking or progress alerts. You can change this in Android Settings.")
            .setPositiveButton("Allow notifications") { _, _ ->
                requesting = true
                request(Manifest.permission.POST_NOTIFICATIONS)
            }
            .setNegativeButton("Not now", null)
            .setOnDismissListener { if (!requesting) dialogOpen(false) }.show()
    }
}
