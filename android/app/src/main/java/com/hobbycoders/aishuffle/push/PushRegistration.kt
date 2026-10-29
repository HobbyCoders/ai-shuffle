package com.hobbycoders.aishuffle.push

import android.content.Context
import androidx.core.app.NotificationManagerCompat
import androidx.work.*
import com.google.android.gms.tasks.Tasks
import com.google.firebase.messaging.FirebaseMessaging
import com.hobbycoders.aishuffle.BuildConfig
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.net.ApiResult
import java.util.concurrent.TimeUnit

/** Short, retryable registration work, never a foreground service or poller. */
object PushRegistration {
    fun schedule(context: Context) {
        if (!BuildConfig.PUSH_ENABLED) return
        NeedsYouNotifications.channel(context)
        NeedsYouNotifications.prune(context)
        // Serial jobs: token rotation queued during an in-flight old registration
        // must run afterwards, not be lost or overwrite the newer token later.
        WorkManager.getInstance(context).enqueueUniqueWork("needs-you-registration", ExistingWorkPolicy.APPEND_OR_REPLACE,
            OneTimeWorkRequestBuilder<PushRegistrationWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build())
    }
}

class PushRegistrationWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        if (!BuildConfig.PUSH_ENABLED) return Result.success()
        val context = applicationContext
        val token = if (NotificationManagerCompat.from(context).areNotificationsEnabled()) {
            try { kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) {
                Tasks.await(FirebaseMessaging.getInstance().token, 20, TimeUnit.SECONDS)
            } } catch (_: Exception) { return if (runAttemptCount < 5) Result.retry() else Result.failure() }
        } else null
        val store = ServerStore.get(context)
        var retry = false
        for (server in store.getAll().filter { NeedsYouNotifications.authorized(context, it) }) {
            val registered = ApiClient.pushToken(server, token)
            if (registered !is ApiResult.Success) {
                if (registered is ApiResult.Failure && (registered.statusCode == null || registered.statusCode >= 500)) retry = true
                continue
            }
            val result = ApiClient.pushState(server)
            if (result !is ApiResult.Success) { retry = true; continue }
            // Re-check the saved device after network I/O: never bind a removed,
            // re-paired, or signed-out connection from an old response.
            val current = store.getById(server.id) ?: continue
            if (current.deviceId != server.deviceId || current.baseUrl != server.baseUrl ||
                current.accountId != server.accountId || !NeedsYouNotifications.authorized(context, current)) continue
            val worker = result.data.optString("worker_id")
            if (!AttentionCommand.validId(worker)) continue
            NeedsYouNotifications.bind(context, worker, current)
            val items = result.data.optJSONArray("notifications") ?: continue
            val active = mutableSetOf<String>()
            for (i in 0 until items.length()) {
                val item = items.optJSONObject(i) ?: continue
                val data = item.keys().asSequence().associateWith { item.optString(it) }
                val command = AttentionCommand.parse(data, System.currentTimeMillis() / 1000) ?: continue
                if (command.worker != worker) continue
                if (command.action != "clear" && token != null) active.add(command.session)
                if (token != null) NeedsYouNotifications.receive(context, data + ("silent" to "true"))
            }
            NeedsYouNotifications.cancelMissing(context, worker, active)
        }
        return if (retry && runAttemptCount < 5) Result.retry() else Result.success()
    }
}
