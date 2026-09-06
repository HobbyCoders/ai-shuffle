package com.hobbycoders.aishuffle.ui

import android.content.Intent
import android.content.res.ColorStateList
import android.os.Bundle
import android.widget.ImageView
import android.widget.LinearLayout
import androidx.appcompat.widget.PopupMenu
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.account.AccountRepository
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.voice.VoiceCallActivity

class ServerListActivity : NativeScreen() {
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
    private fun addWorker() { startActivity(Intent(this, AddServerActivity::class.java)) }
    private fun render() {
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
            gap(24)
            row("Connect a worker", "Use a URL and pairing code, or scan a QR code.", R.drawable.ic_worker) { addWorker() }
            text("You can connect directly without an account.", 13f).apply {
                gravity = android.view.Gravity.CENTER; setTextColor(color(R.color.slate))
            }
            return
        }
        navigation("Workers", back = false)
        navigationAction(R.drawable.ic_settings, "Settings") { startActivity(Intent(this, SettingsActivity::class.java)) }
        if (account != null) navigationAction(R.drawable.ic_refresh, "Refresh workers") {
            runWork { repo.sync(); render(); status.text = "Workers are up to date." }
        }
        navigationAction(R.drawable.ic_add, "Add worker") { addWorker() }
        content.removeAllViews(); status.text = ""
        text(if (account != null) "Signed in as ${account.username}" else "Connected to this phone", 14f).setTextColor(color(R.color.slate))
        gap(12)
        if (servers.isEmpty()) {
            group {
                text("No workers yet", 20f)
                text("Connect your first worker with its pairing code or QR code. Workers linked to your account appear here.", 15f).setTextColor(color(R.color.slate))
                button("Add worker", primary = true) { addWorker() }
            }
        }
        for (server in servers) {
            val workerRow = row(server.name, "${server.host}\n${if (server.accountId == null) "This phone" else "Your account"}", R.drawable.ic_worker) {
                startActivity(Intent(this, ChatActivity::class.java).putExtra("server_id", server.id))
            }
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
        }
    }
}
