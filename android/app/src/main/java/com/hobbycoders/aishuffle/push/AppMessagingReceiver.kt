package com.hobbycoders.aishuffle.push

import android.content.Context
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import org.json.JSONObject
import org.unifiedpush.android.connector.MessagingReceiver

/**
 * Receives UnifiedPush events from the distributor.
 *
 * The 2.5.0 connector delivers raw bytes to [onMessage]; the server posts a JSON
 * body of the form:
 * `{"title":..,"body":..,"priority":..,"data":{..},"timestamp":..}`.
 */
class AppMessagingReceiver : MessagingReceiver() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onNewEndpoint(context: Context, endpoint: String, instance: String) {
        Log.i(TAG, "New UnifiedPush endpoint received")
        // Network work — keep the receiver alive while we register with servers.
        val pending = goAsync()
        scope.launch {
            try {
                PushManager.onEndpointReceived(context.applicationContext, endpoint)
            } catch (e: Exception) {
                Log.w(TAG, "Failed to propagate endpoint", e)
            } finally {
                pending.finish()
            }
        }
    }

    override fun onMessage(context: Context, message: ByteArray, instance: String) {
        val text = message.toString(Charsets.UTF_8)
        Log.d(TAG, "Push message received (${message.size} bytes)")
        val payload = runCatching { JSONObject(text) }.getOrNull()
        if (payload == null) {
            Log.w(TAG, "Push payload was not valid JSON")
            return
        }
        NotificationHelper.show(
            context = context.applicationContext,
            title = payload.optString("title").takeIf { it.isNotBlank() },
            body = payload.optString("body").takeIf { it.isNotBlank() },
            priority = payload.optString("priority").takeIf { it.isNotBlank() },
            data = payload.optJSONObject("data"),
        )
    }

    override fun onRegistrationFailed(context: Context, instance: String) {
        Log.w(TAG, "UnifiedPush registration failed for instance $instance")
    }

    override fun onUnregistered(context: Context, instance: String) {
        Log.i(TAG, "UnifiedPush unregistered for instance $instance")
    }

    companion object {
        private const val TAG = "AppMessagingReceiver"
    }
}
