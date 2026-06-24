package com.hobbycoders.aishuffle.ui

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.LinearLayoutManager
import com.google.android.material.snackbar.Snackbar
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.databinding.ActivityServerListBinding
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.NotificationStreamService
import com.hobbycoders.aishuffle.push.NotificationHelper

/**
 * Launcher screen: a list of saved servers. FAB adds one; tap opens chat;
 * long-press deletes.
 *
 * Notifications are delivered by [NotificationStreamService], an embedded
 * foreground service that holds an HTTPS Server-Sent-Events connection to each
 * server — no Firebase and no separate UnifiedPush distributor app required.
 * This screen starts that service whenever at least one server is configured
 * and stops it when the last one is removed.
 */
class ServerListActivity : AppCompatActivity() {

    private lateinit var binding: ActivityServerListBinding
    private lateinit var store: ServerStore
    private lateinit var adapter: ServerAdapter

    private val notificationPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* result ignored */ }

    private val addServerLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            if (result.resultCode == RESULT_OK) refresh()
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityServerListBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)

        store = ServerStore.get(this)
        NotificationHelper.ensureChannels(this)

        adapter = ServerAdapter(
            onClick = { openChat(it) },
            onLongClick = { confirmDelete(it) },
        )
        binding.serverList.layoutManager = LinearLayoutManager(this)
        binding.serverList.adapter = adapter

        binding.addServerFab.setOnClickListener {
            addServerLauncher.launch(Intent(this, AddServerActivity::class.java))
        }

        maybeRequestNotificationPermission()
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    private fun refresh() {
        val servers = store.getAll()
        adapter.submit(servers)
        binding.emptyState.visibility = if (servers.isEmpty()) android.view.View.VISIBLE else android.view.View.GONE
        binding.serverList.visibility = if (servers.isEmpty()) android.view.View.GONE else android.view.View.VISIBLE

        if (servers.isNotEmpty()) {
            // Bring the notification stream online (idempotent — safe each resume).
            NotificationStreamService.start(this)
            maybeHintBatteryOptimization()
        } else {
            // No servers left to watch: drop the connection and its notice.
            NotificationStreamService.stop(this)
        }
    }

    private fun openChat(server: Server) {
        startActivity(
            Intent(this, ChatActivity::class.java)
                .putExtra(ChatActivity.EXTRA_SERVER_ID, server.id),
        )
    }

    private fun confirmDelete(server: Server) {
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.delete_server_title))
            .setMessage(getString(R.string.delete_server_message, server.name))
            .setPositiveButton(R.string.delete) { _, _ ->
                store.remove(server.id)
                refresh()
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    /**
     * Android aggressively dozes background apps, which can silently kill the
     * SSE connection. The persistent connection is far more reliable once the
     * app is exempt from battery optimization, so prompt once.
     */
    private fun maybeHintBatteryOptimization() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return

        val prefs = getSharedPreferences("ai_shuffle_ui", MODE_PRIVATE)
        if (prefs.getBoolean(KEY_BATTERY_HINT_SHOWN, false)) return

        val power = getSystemService(Context.POWER_SERVICE) as? PowerManager ?: return
        if (power.isIgnoringBatteryOptimizations(packageName)) return

        prefs.edit().putBoolean(KEY_BATTERY_HINT_SHOWN, true).apply()

        Snackbar.make(binding.root, R.string.battery_hint_message, Snackbar.LENGTH_LONG)
            .setAction(R.string.battery_hint_action) { openBatteryOptimizationSettings() }
            .show()
    }

    private fun openBatteryOptimizationSettings() {
        // Opens the global battery-optimization list (no special permission
        // required). The user can mark AI Shuffle as "not optimized" there.
        runCatching {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }.onFailure {
            runCatching { startActivity(Intent(Settings.ACTION_SETTINGS)) }
        }
    }

    private fun maybeRequestNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.POST_NOTIFICATIONS,
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    companion object {
        private const val KEY_BATTERY_HINT_SHOWN = "battery_hint_shown"
    }
}
