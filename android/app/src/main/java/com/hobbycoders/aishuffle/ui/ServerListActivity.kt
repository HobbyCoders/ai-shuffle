package com.hobbycoders.aishuffle.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
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
import com.hobbycoders.aishuffle.push.NotificationHelper
import com.hobbycoders.aishuffle.push.PushManager

/**
 * Launcher screen: a list of saved servers. FAB adds one; tap opens chat;
 * long-press deletes. Also drives UnifiedPush distributor registration and the
 * Android 13+ notification-permission prompt.
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
            ensurePushRegistration()
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

    /** Try to register with a distributor; show a one-time hint if none exists. */
    private fun ensurePushRegistration() {
        when (PushManager.ensureRegistered(this)) {
            is PushManager.RegisterOutcome.NoDistributor -> showNoDistributorHintOnce()
            is PushManager.RegisterOutcome.Requested -> { /* nothing to do */ }
        }
    }

    private fun showNoDistributorHintOnce() {
        val prefs = getSharedPreferences("ai_shuffle_ui", MODE_PRIVATE)
        if (prefs.getBoolean(KEY_NO_DISTRIBUTOR_SHOWN, false)) return
        prefs.edit().putBoolean(KEY_NO_DISTRIBUTOR_SHOWN, true).apply()

        Snackbar.make(binding.root, R.string.no_distributor_hint, Snackbar.LENGTH_INDEFINITE)
            .setAction(R.string.dismiss) { /* dismiss */ }
            .show()
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
        private const val KEY_NO_DISTRIBUTOR_SHOWN = "no_distributor_hint_shown"
    }
}
