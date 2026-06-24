package com.hobbycoders.aishuffle.net

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.push.NotificationHelper
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener
import okhttp3.sse.EventSources
import org.json.JSONObject
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

/**
 * Foreground service that keeps one Server-Sent-Events connection open to each
 * saved server's `/api/v1/mobile/notifications/stream` and raises a local
 * notification for every event.
 *
 * This is how the app receives push **without Firebase and without a separate
 * UnifiedPush distributor app**. The cost is a persistent (minimum-importance)
 * "connected" notification, which Android requires for a long-running
 * foreground service.
 *
 * Connections auto-reconnect with capped exponential backoff. The service is
 * START_STICKY so the OS restarts it after being killed, and [reconcile] makes
 * repeated [start] calls idempotent (it only opens/closes what changed).
 */
class NotificationStreamService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    // serverId -> live connection state. ConcurrentHashMap because EventSource
    // callbacks arrive on OkHttp threads while reconcile runs on the main thread.
    private val connections = ConcurrentHashMap<String, Connection>()

    private val client: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(20, TimeUnit.SECONDS)
            // No write timeout matters; read timeout must exceed the server's
            // 25s keepalive so a *silent* drop is detected and triggers reconnect.
            .readTimeout(60, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build()
    }

    private inner class Connection(val server: Server) {
        @Volatile var source: EventSource? = null
        @Volatile var attempt: Int = 0
        @Volatile var reconnectJob: Job? = null
        @Volatile var closed: Boolean = false
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        startInForeground(getString(R.string.service_connecting))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        val servers = ServerStore.get(this).getAll()
        if (servers.isEmpty()) {
            // Nothing to watch — drop the foreground notification and stop.
            stopSelf()
            return START_NOT_STICKY
        }
        reconcile(servers)
        return START_STICKY
    }

    /** Open connections for new servers, close them for removed ones. */
    private fun reconcile(servers: List<Server>) {
        val wanted = servers.associateBy { it.id }

        // Close connections whose server was removed.
        connections.keys.filterNot { wanted.containsKey(it) }.forEach { id ->
            connections.remove(id)?.let { teardown(it) }
        }

        // Open connections for servers we are not already watching.
        for (server in servers) {
            if (connections.containsKey(server.id)) continue
            val conn = Connection(server)
            connections[server.id] = conn
            connect(conn)
        }

        updateForegroundNotification()
    }

    private fun connect(conn: Connection) {
        if (conn.closed) return
        val server = conn.server
        val request = Request.Builder()
            .url("${server.baseUrl}/api/v1/mobile/notifications/stream")
            .header("Authorization", "Bearer ${server.apiKey}")
            .header("Accept", "text/event-stream")
            .build()
        conn.source = EventSources.createFactory(client)
            .newEventSource(request, StreamListener(conn))
    }

    private inner class StreamListener(private val conn: Connection) : EventSourceListener() {

        override fun onOpen(eventSource: EventSource, response: Response) {
            conn.attempt = 0 // reset backoff on a healthy connection
            Log.i(TAG, "Stream open for ${conn.server.name}")
            updateForegroundNotification()
        }

        override fun onEvent(
            eventSource: EventSource,
            id: String?,
            type: String?,
            data: String,
        ) {
            val payload = runCatching { JSONObject(data) }.getOrNull() ?: return
            val inner = payload.optJSONObject("data")
            // Questions / permission prompts are time-sensitive -> high channel.
            val eventType = payload.optString("type")
            val priority = if (eventType == "user_question" || eventType == "permission_request") {
                "high"
            } else {
                "default"
            }
            NotificationHelper.show(
                context = applicationContext,
                title = payload.optString("title").takeIf { it.isNotBlank() },
                body = payload.optString("body").takeIf { it.isNotBlank() },
                priority = priority,
                data = inner,
            )
        }

        override fun onClosed(eventSource: EventSource) {
            scheduleReconnect(conn, "closed by server")
        }

        override fun onFailure(eventSource: EventSource, t: Throwable?, response: Response?) {
            scheduleReconnect(conn, t?.message ?: "connection failed")
        }
    }

    private fun scheduleReconnect(conn: Connection, reason: String) {
        if (conn.closed || connections[conn.server.id] !== conn) return
        conn.source = null
        val attempt = conn.attempt.coerceAtMost(MAX_BACKOFF_STEPS)
        conn.attempt = attempt + 1
        val delayMs = (BASE_BACKOFF_MS * (1L shl attempt)).coerceAtMost(MAX_BACKOFF_MS)
        Log.w(TAG, "Reconnecting ${conn.server.name} in ${delayMs}ms ($reason)")
        updateForegroundNotification()
        conn.reconnectJob = scope.launch {
            delay(delayMs)
            if (!conn.closed && connections[conn.server.id] === conn) connect(conn)
        }
    }

    private fun teardown(conn: Connection) {
        conn.closed = true
        conn.reconnectJob?.cancel()
        conn.source?.cancel()
        conn.source = null
    }

    private fun startInForeground(text: String) {
        val notification = NotificationHelper.buildServiceNotification(this, text)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NotificationHelper.FOREGROUND_NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE,
            )
        } else {
            startForeground(NotificationHelper.FOREGROUND_NOTIFICATION_ID, notification)
        }
    }

    private fun updateForegroundNotification() {
        val count = connections.size
        val live = connections.values.count { it.source != null }
        val text = when {
            count == 0 -> getString(R.string.service_connecting)
            live == count -> resources.getQuantityString(R.plurals.service_connected, count, count)
            else -> getString(R.string.service_reconnecting)
        }
        val notification = NotificationHelper.buildServiceNotification(this, text)
        val mgr = getSystemService(NOTIFICATION_SERVICE) as android.app.NotificationManager
        mgr.notify(NotificationHelper.FOREGROUND_NOTIFICATION_ID, notification)
    }

    override fun onDestroy() {
        connections.values.forEach { teardown(it) }
        connections.clear()
        scope.cancel()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "NotifStreamService"
        private const val ACTION_STOP = "com.hobbycoders.aishuffle.STOP_STREAM"

        private const val BASE_BACKOFF_MS = 2_000L
        private const val MAX_BACKOFF_MS = 60_000L
        private const val MAX_BACKOFF_STEPS = 5 // 2s,4s,8s,16s,32s -> capped 60s

        /** Start (or refresh) the stream service. Safe to call repeatedly. */
        fun start(context: Context) {
            val intent = Intent(context, NotificationStreamService::class.java)
            context.startForegroundService(intent)
        }

        /** Stop the stream service and drop the persistent notification. */
        fun stop(context: Context) {
            val intent = Intent(context, NotificationStreamService::class.java)
                .setAction(ACTION_STOP)
            context.startService(intent)
        }
    }
}
