package com.hobbycoders.aishuffle.push

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.account.AccountStore
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.ui.ChatActivity
import org.json.JSONObject

/** The single Needs you channel. Receipts are committed BEFORE display. */
internal object NeedsYouNotifications {
    const val CHANNEL = "needs-you"
    private const val PREFS = "needs_you_push"
    private const val TAG_PREFIX = "needs-you:"
    private fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    fun channel(context: Context) {
        context.getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, "Needs you", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Only chats waiting for your input or action"
                lockscreenVisibility = android.app.Notification.VISIBILITY_PRIVATE
            })
    }
    fun authorized(context: Context, server: Server): Boolean {
        if (server.needsAccountAuthorization || server.apiKey.isBlank()) return false
        if (server.accountId == null) return true
        val account = AccountStore(context).session() ?: return false
        return server.accountId == account.accountId && server.hubUrl == account.hub
    }
    fun bind(context: Context, worker: String, server: Server) {
        if (!AttentionCommand.validId(worker)) return
        prefs(context).edit().putString("worker:$worker", JSONObject().put("server", server.id)
            .put("device", server.deviceId).put("origin", server.baseUrl).toString()).commit()
    }
    fun server(context: Context, worker: String): Server? {
        if (!AttentionCommand.validId(worker)) return null
        val mapping = runCatching { JSONObject(prefs(context).getString("worker:$worker", "")!!) }.getOrNull() ?: return null
        return ServerStore.get(context).getById(mapping.optString("server"))?.takeIf {
            it.deviceId == mapping.optString("device") && it.baseUrl == mapping.optString("origin") && authorized(context, it)
        }
    }
    @Synchronized
    fun receive(context: Context, data: Map<String, String>) {
        val command = AttentionCommand.parse(data, System.currentTimeMillis() / 1000) ?: return
        val server = server(context, command.worker) ?: return
        display(context, command, server)
    }
    @Synchronized
    internal fun display(context: Context, command: AttentionCommand, server: Server) {
        val prefs = prefs(context)
        val key = "receipt:${command.session}"
        val old = runCatching { JSONObject(prefs.getString(key, "{}")!!) }.getOrDefault(JSONObject())
        val decision = AttentionPolicy.apply(AttentionReceipt(old.optLong("version"), old.optString("episode")), command) ?: return
        // Disk failure must not allow a display whose receipt will be lost on restart.
        if (!prefs.edit().putString(key, JSONObject().put("version", decision.receipt.version)
                .put("episode", decision.receipt.episode).put("worker", command.worker).toString()).commit()) return
        val manager = NotificationManagerCompat.from(context)
        val tag = TAG_PREFIX + command.session
        if (!decision.show) { manager.cancel(tag, 0); return }
        channel(context)
        if (!manager.areNotificationsEnabled()) return
        val tap = Intent(context, ChatActivity::class.java).putExtra(ChatActivity.EXTRA_SERVER_ID, server.id)
            .putExtra("push_worker_id", command.worker).putExtra("push_session_id", command.session)
            .setData(android.net.Uri.parse("shuffle-attention:${command.session}"))
        val intent = PendingIntent.getActivity(context, 0, tap, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        // For a NEW episode, remove the previous notification so onlyAlertOnce
        // doesn't suppress its first alert. All updates/retries remain silent.
        if (decision.alert) manager.cancel(tag, 0)
        try {
            manager.notify(tag, 0, NotificationCompat.Builder(context, CHANNEL)
                .setSmallIcon(R.drawable.ic_shuffle).setContentTitle("AI Shuffle").setContentText(command.body)
                .setContentIntent(intent).setAutoCancel(true).setOnlyAlertOnce(true)
                .setSilent(!decision.alert).setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
                .build())
        } catch (_: SecurityException) { /* Permission can be revoked between check and display. */ }
    }
    fun cancelMissing(context: Context, worker: String, sessions: Set<String>) {
        val manager = context.getSystemService(NotificationManager::class.java)
        val prefs = prefs(context)
        manager.activeNotifications.filter { it.tag?.startsWith(TAG_PREFIX) == true }.forEach {
            val session = it.tag.removePrefix(TAG_PREFIX)
            val receipt = runCatching { JSONObject(prefs.getString("receipt:$session", "{}")!!) }.getOrNull()
            if (receipt?.optString("worker") == worker && session !in sessions) manager.cancel(it.tag, it.id)
        }
    }
    fun prune(context: Context) {
        val prefs = prefs(context)
        prefs.all.keys.filter { it.startsWith("worker:") }.forEach {
            val worker = it.removePrefix("worker:")
            if (server(context, worker) == null) cancelMissing(context, worker, emptySet())
        }
    }
}
