package com.hobbycoders.aishuffle.push

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.net.NotificationStreamService
import com.hobbycoders.aishuffle.wake.WakeWordService

/**
 * Restarts the notification stream service (and, when enabled, the wake-word
 * listener) after a device reboot or app update, but only if the user has at
 * least one server configured.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED -> {
                if (ServerStore.get(context).getAll().isNotEmpty()) {
                    NotificationStreamService.start(context)
                }
                // Mic foreground services can't always start from a boot
                // broadcast (Android 14 limits); startIfEnabled swallows the
                // refusal and the listener comes back next app open instead.
                WakeWordService.startIfEnabled(context)
            }
        }
    }
}
