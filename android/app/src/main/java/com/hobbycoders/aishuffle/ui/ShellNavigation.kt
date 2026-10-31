package com.hobbycoders.aishuffle.ui

import android.content.Context
import android.content.Intent

/**
 * The native screens chat opens for the page (MobileUi.SHELL_BRIDGE). While the
 * page draws its own "Connected to" bar the app's bar and its menu are hidden,
 * so these are the page's only ways to the Workers screen and to the app's own
 * Settings: account, sign out, trusted devices, phone tools, delete account
 * (issue #951).
 */
internal object ShellNavigation {
    /** The screen a `shuffleShell` message opens, or null when it is not a navigation. */
    fun intent(context: Context, message: MobileUi.ShellMessage?): Intent? = when (message) {
        is MobileUi.ShellMessage.OpenWorkers -> workers(context)
        is MobileUi.ShellMessage.OpenAppSettings -> appSettings(context)
        else -> null
    }

    /** The Workers screen (pair, rename, remove; first setup and recovery), above chat. */
    fun workers(context: Context, message: String? = null): Intent = Intent(context, ServerListActivity::class.java)
        .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        .apply { if (message != null) putExtra("sync_message", message) }

    /** The app's Settings screen; Back returns to chat. */
    fun appSettings(context: Context): Intent = Intent(context, SettingsActivity::class.java)
}
