package com.hobbycoders.aishuffle.ui

import android.content.Intent
import android.content.res.ColorStateList
import android.os.Bundle
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.view.View
import android.graphics.drawable.GradientDrawable
import androidx.appcompat.widget.PopupMenu
import androidx.lifecycle.lifecycleScope
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.account.AccountRepository
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.voice.VoiceCallActivity
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.presence.PresenceState
import com.hobbycoders.aishuffle.presence.ProbeResult
import com.hobbycoders.aishuffle.presence.WorkerPresence
import com.hobbycoders.aishuffle.presence.WorkerPresenceMonitor
import com.hobbycoders.aishuffle.presence.WorkerPresenceProbe
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Launch screen. With workers paired it first looks for one that is online and
 * opens chat there (the most recently used first); the full list lives under
 * "Devices" and is the landing screen only when nothing is online, when the
 * user asks for it, or when there is nothing to open yet.
 */
class ServerListActivity : NativeScreen() {
    private class StatusRow(val server: Server, val label: TextView, val dot: View)
    private val statusRows = mutableMapOf<String, StatusRow>()
    private val presence by lazy { WorkerPresenceMonitor(this, lifecycleScope, ::updatePresence) }
    /** One home-worker auto-pick per launch; Devices shows the list afterwards. */
    private var autoPickDone = false
    private var autoPick: Job? = null
    private fun updatePresence(server: Server, observed: WorkerPresence) {
        val row = statusRows[server.id]?.takeIf { it.server == server } ?: return
        val affiliation = if (server.accountId == null) "This phone" else "Your account"
        val description = "$affiliation. ${observed.description(server.name)}"
        if (row.label.contentDescription == description) return
        row.label.text = "$affiliation · ${observed.state.label}"
        row.label.contentDescription = description
        row.dot.background = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(color(when (observed.state) {
                PresenceState.ONLINE -> R.color.worker_online
                PresenceState.OFFLINE -> R.color.worker_offline
                else -> R.color.slate
            }))
        }
    }
    private fun refreshPresence() { presence.start(statusRows.values.map { it.server }, AccountRepository(this).store.session()) }
    override fun onStop() { presence.stop(); autoPick?.cancel(); autoPick = null; super.onStop() }
    override fun onNewIntent(intent: Intent) { super.onNewIntent(intent); setIntent(intent) }
    override fun onResume() {
        super.onResume()
        if (intent.getBooleanExtra(EXTRA_SHOW_DEVICES, false)) { autoPickDone = true; intent.removeExtra(EXTRA_SHOW_DEVICES) }
        render()
        if (intent.getBooleanExtra("auth_expired", false)) {
            status.text = "Your sign-in expired. Sign in again to restore your workers."
            intent.removeExtra("auth_expired")
        }
        intent.getStringExtra("sync_message")?.let { status.text = it; intent.removeExtra("sync_message") }
        ServerStore.get(this).selectedToolsServer()?.let { com.hobbycoders.aishuffle.tools.DeviceToolsRuntime.attach(this, it) }
    }
    private fun openAccount(mode: String) { startActivity(Intent(this, AccountActivity::class.java).putExtra("mode", mode)) }
    private fun addWorker() { startActivity(Intent(this, AddServerActivity::class.java)) }
    /**
     * Probe every worker at once and open the first online one in most-recently-
     * used order. Falls through to the Devices list when none answers in time.
     */
    private fun renderAutoPick(servers: List<Server>) {
        navigation("Shuffle", back = false)
        navigationAction(R.drawable.ic_settings, "Settings") { startActivity(Intent(this, SettingsActivity::class.java)) }
        content.removeAllViews(); status.text = ""
        heading("Connecting", "Looking for a worker that is online…")
        link("Devices") { autoPickDone = true; autoPick?.cancel(); render() }
        val candidates = ServerStore.get(this).orderedByRecentUse(servers)
        autoPick?.cancel()
        autoPick = lifecycleScope.launch {
            val chosen = withTimeoutOrNull(8_000) {
                val probes = candidates.map { candidate -> async { candidate to WorkerPresenceProbe.check(candidate) } }
                // Await in preference order: the most recently used worker wins when it is online.
                probes.firstNotNullOfOrNull { probe -> probe.await().let { (candidate, result) -> candidate.takeIf { result == ProbeResult.ONLINE } } }
            }
            autoPickDone = true
            if (isFinishing || isDestroyed || !lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.RESUMED)) return@launch
            if (chosen != null && ServerStore.get(this@ServerListActivity).getById(chosen.id) == chosen) {
                startActivity(Intent(this@ServerListActivity, ChatActivity::class.java).putExtra(ChatActivity.EXTRA_SERVER_ID, chosen.id))
            } else {
                render()
                status.text = "No worker is online right now. Pick one below to try anyway."
            }
        }
    }
    private fun render() {
        presence.stop()
        statusRows.clear()
        val repo = AccountRepository(this)
        val account = repo.store.session()
        if (account == null && repo.store.hasStoredSession()) {
            runWork { repo.clearLocal(); render(); status.text = "Your sign-in expired. Sign in again to restore your workers." }
            return
        }
        navigation("Shuffle", back = false)
        navigationAction(R.drawable.ic_settings, "Settings") { startActivity(Intent(this, SettingsActivity::class.java)) }
        val servers = ServerStore.get(this).getAll().filter { it.accountId == null || (it.accountId == account?.accountId && it.hubUrl == account?.hub) }
        if (account == null && servers.isEmpty()) {
            content.removeAllViews()
            gap(24)
            content.addView(ImageView(this).apply {
                setImageResource(R.drawable.ic_shuffle)
                imageTintList = ColorStateList.valueOf(color(R.color.ink))
                background = rounded(R.color.container, 20)
                setPadding(dp(15), dp(15), dp(15), dp(15))
                importantForAccessibility = android.view.View.IMPORTANT_FOR_ACCESSIBILITY_NO
            }, LinearLayout.LayoutParams(dp(64), dp(64)))
            gap(28)
            text("Welcome to Shuffle", 32f).typeface = android.graphics.Typeface.create("sans-serif-medium", android.graphics.Typeface.NORMAL)
            text("Your workers, wherever you are. Sign in to bring them to this phone.", 16f).setTextColor(color(R.color.slate))
            gap(28)
            button("Sign in", primary = true) { openAccount("login") }
            button("Create account") { openAccount("create") }
            link("Use another device") { openAccount("pair") }
            gap(24)
            row("Connect a worker", "Use a URL and pairing code, or scan a QR code.", R.drawable.ic_worker) { addWorker() }
            text("You can connect directly without an account.", 13f).apply {
                gravity = android.view.Gravity.CENTER; setTextColor(color(R.color.slate))
            }
            return
        }
        if (!autoPickDone && servers.isNotEmpty()) { renderAutoPick(servers); return }
        navigation("Devices", back = false)
        navigationAction(R.drawable.ic_settings, "Settings") { startActivity(Intent(this, SettingsActivity::class.java)) }
        navigationAction(R.drawable.ic_refresh, "Refresh workers") {
            refreshPresence()
            if (account != null) runWork { repo.sync(); render(); status.text = "Workers are up to date." }
        }
        navigationAction(R.drawable.ic_add, "Add worker") { addWorker() }
        content.removeAllViews(); status.text = ""
        text(if (account != null) "Signed in as ${account.username}" else "Connected to this phone", 14f).setTextColor(color(R.color.slate))
        gap(12)
        if (account != null && account.profileComplete != true) {
            row("Choose your username", "Finish setting up your Shuffle account.", R.drawable.ic_account) { openAccount("profile_setup") }
        }
        if (servers.isEmpty()) {
            group {
                text("No workers yet", 20f)
                text("Connect your first worker with its pairing code or QR code. Workers linked to your account appear here.", 15f).setTextColor(color(R.color.slate))
                button("Add worker", primary = true) { addWorker() }
            }
        }
        for (server in servers) {
            val workerRow = row(server.name, server.host, R.drawable.ic_worker) {
                startActivity(Intent(this, ChatActivity::class.java).putExtra("server_id", server.id))
            }
            val labels = workerRow.getChildAt(1) as LinearLayout
            // Keep the existing row and its options; status has text as well as a color indicator.
            val stateLine = LinearLayout(this).apply { gravity = android.view.Gravity.CENTER_VERTICAL; setPadding(0, dp(4), 0, 0) }
            val dot = View(this).apply { importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO }
            val label = TextView(this).apply { textSize = 12f; setTextColor(color(R.color.slate)) }
            stateLine.addView(dot, LinearLayout.LayoutParams(dp(7), dp(7)).apply { marginEnd = dp(6) })
            stateLine.addView(label, LinearLayout.LayoutParams(0, -2, 1f))
            labels.addView(stateLine)
            statusRows[server.id] = StatusRow(server, label, dot)
            updatePresence(server, WorkerPresence())
            workerRow.removeViewAt(workerRow.childCount - 1)
            val more = iconButton(R.drawable.ic_more, "Options for ${server.name}") {}
            more.background = rounded(R.color.card)
            workerRow.addView(more)
            more.setOnClickListener {
                if (!busy) PopupMenu(this, more).apply {
                    menu.add("Start voice chat").setOnMenuItemClickListener {
                        startActivity(Intent(this@ServerListActivity, VoiceCallActivity::class.java).putExtra("server_id", server.id)); true
                    }
                    menu.add("Phone tools").setOnMenuItemClickListener {
                        startActivity(Intent(this@ServerListActivity, ToolSettingsActivity::class.java).putExtra("server_id", server.id)); true
                    }
                    if (account != null && server.accountId == null) menu.add("Share with account").setOnMenuItemClickListener {
                        confirm("Share ${server.name}?", "Devices signed in as ${account.username} will be able to connect to ${server.host}.") {
                            runWork { repo.link(server); repo.sync(); render(); status.text = "Worker linked to your account." }
                        }; true
                    }
                    menu.add("Remove from this phone").setOnMenuItemClickListener {
                        confirm("Remove ${server.name}?", "This removes its saved connection from this phone. The worker keeps running.") {
                            com.hobbycoders.aishuffle.tools.DeviceToolsRuntime.stop()
                            ServerStore.get(this@ServerListActivity).remove(server.id); render()
                        }; true
                    }
                    show()
                }
            }
        }
        if (account == null) {
            section("YOUR ACCOUNT")
            row("Sign in to Shuffle", "Bring your workers to every device.", R.drawable.ic_account) { openAccount("login") }
            link("Use another device") { openAccount("pair") }
        }
        refreshPresence()
    }
    companion object {
        /** Open the Devices list directly instead of auto-picking an online worker. */
        const val EXTRA_SHOW_DEVICES = "show_devices"
    }
}
