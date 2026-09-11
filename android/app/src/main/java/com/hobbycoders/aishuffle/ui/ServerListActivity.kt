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
import com.hobbycoders.aishuffle.presence.WorkerPresence
import com.hobbycoders.aishuffle.presence.WorkerPresenceMonitor

class ServerListActivity : NativeScreen() {
    private class StatusRow(val server: Server, val label: TextView, val dot: View)
    private val statusRows = mutableMapOf<String, StatusRow>()
    private val presence by lazy { WorkerPresenceMonitor(this, lifecycleScope, ::updatePresence) }
    private fun updatePresence(server: Server, observed: WorkerPresence) {
        val row = statusRows[server.id]?.takeIf { it.server == server } ?: return
        val affiliation = if (server.accountId == null) "This phone" else "Your account"
        val authorization = if (server.needsAccountAuthorization) " Authorization needed. Tap to retry." else ""
        val description = "$affiliation. ${observed.description(server.name)}$authorization"
        if (row.label.contentDescription == description) return
        row.label.text = "$affiliation · ${if (server.needsAccountAuthorization) "Authorization needed" else observed.state.label}"
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
    override fun onStop() { presence.stop(); super.onStop() }
    override fun onNewIntent(intent: Intent) { super.onNewIntent(intent); setIntent(intent) }
    override fun onResume() {
        super.onResume()
        render()
        if (intent.getBooleanExtra("auth_expired", false)) {
            status.text = "Your sign-in expired. Sign in again to restore your workers."
            intent.removeExtra("auth_expired")
        }
        intent.getStringExtra("sync_message")?.let { status.text = it; intent.removeExtra("sync_message") }
        ServerStore.get(this).selectedToolsServer()?.let { com.hobbycoders.aishuffle.tools.DeviceToolsRuntime.attach(this, it) }
    }
    private fun openAccount(mode: String) { startActivity(Intent(this, AccountActivity::class.java).putExtra("mode", mode)) }
    private fun openWorker(server: Server) {
        if (server.needsAccountAuthorization) {
            runWork {
                try { AccountRepository(this@ServerListActivity).sync(server.hubServerId) }
                finally { render() }
                val updated = ServerStore.get(this@ServerListActivity).getAll().firstOrNull { it.id == server.id }
                if (updated != null && !updated.needsAccountAuthorization) openWorker(updated)
            }
        } else startActivity(Intent(this, ChatActivity::class.java).putExtra("server_id", server.id))
    }
    private fun addWorker() { startActivity(Intent(this, AddServerActivity::class.java)) }
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
            button("Explore offline demo") { startActivity(Intent(this, OfflineDemoActivity::class.java)) }
            gap(24)
            row("Connect a worker", "Use a URL and pairing code, or scan a QR code.", R.drawable.ic_worker) { addWorker() }
            text("You can connect directly without an account.", 13f).apply {
                gravity = android.view.Gravity.CENTER; setTextColor(color(R.color.slate))
            }
            return
        }
        navigation("Workers", back = false)
        navigationAction(R.drawable.ic_settings, "Settings") { startActivity(Intent(this, SettingsActivity::class.java)) }
        navigationAction(R.drawable.ic_refresh, "Refresh workers") {
            refreshPresence()
            if (account != null) runWork {
                try { repo.sync() } finally { render() }
                status.text = "Workers are up to date."
            }
        }
        navigationAction(R.drawable.ic_add, "Add worker") { addWorker() }
        content.removeAllViews(); status.text = ""
        button("Explore offline demo") { startActivity(Intent(this, OfflineDemoActivity::class.java)) }
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
                openWorker(server)
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
                    if (server.needsAccountAuthorization) {
                        menu.add("Retry authorization").setOnMenuItemClickListener { openWorker(server); true }
                    } else {
                        menu.add("Start voice chat").setOnMenuItemClickListener {
                            startActivity(Intent(this@ServerListActivity, VoiceCallActivity::class.java).putExtra("server_id", server.id)); true
                        }
                        menu.add("Phone tools").setOnMenuItemClickListener {
                            startActivity(Intent(this@ServerListActivity, ToolSettingsActivity::class.java).putExtra("server_id", server.id)); true
                        }
                    }
                    if (account != null && server.accountId == null) menu.add("Share with account").setOnMenuItemClickListener {
                        confirm("Share ${server.name}?", "Devices signed in as ${account.username} will be able to connect to ${server.host}.") {
                            runWork { repo.link(server); repo.sync(); render(); status.text = "Worker linked to your account." }
                        }; true
                    }
                    val accountWorker = server.accountId != null
                    menu.add(if (accountWorker) "Remove from account" else "Remove from this phone").setOnMenuItemClickListener {
                        val message = if (accountWorker)
                            "This removes ${server.name} from your account on all devices and revokes its account access and managed remote connection. Direct pairings stay saved. To restore it, sign in again on that worker."
                        else "This removes its saved connection from this phone. The worker keeps running."
                        confirm("Remove ${server.name}?", message) {
                            runWork {
                                val cleanupPending = repo.remove(server)
                                com.hobbycoders.aishuffle.tools.DeviceToolsRuntime.stop()
                                render()
                                status.text = when {
                                    cleanupPending -> "Worker removed from your account. Remote connection cleanup is still pending."
                                    accountWorker -> "Worker removed from your account."
                                    else -> "Worker removed from this phone."
                                }
                            }
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
}
