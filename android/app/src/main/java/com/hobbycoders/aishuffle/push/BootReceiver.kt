package com.hobbycoders.aishuffle.push

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.net.NotificationStreamService

/**
 * Restarts the notification stream service after a device reboot (or app
 * update), but only if the user has at least one server configured.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED -> {
                if (ServerStore.get(context).getAll().isNotEmpty()) {
                    NotificationStreamService.start(context)
                }
            }
        }
    }
}
