package com.hobbycoders.aishuffle.ui

import android.content.Intent
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.account.AccountRepository

class SettingsActivity : NativeScreen() {
    override fun onResume() {
        super.onResume()
        navigation("Shuffle")
        heading("Settings", "")
        val session = AccountRepository(this).store.session()
        section("ACCOUNT")
        row(session?.username ?: "Sign in", if (session != null) "Account and trusted devices" else "Sync workers across your devices", R.drawable.ic_account) {
            startActivity(Intent(this, AccountActivity::class.java).putExtra("mode", if (session != null) "manage" else "login"))
        }
        if (session == null) link("Create account") { startActivity(Intent(this, AccountActivity::class.java).putExtra("mode", "create")) }
        section("THIS PHONE")
        row("Phone tools", "App control, timers and permissions", R.drawable.ic_phone) {
            startActivity(Intent(this, ToolSettingsActivity::class.java))
        }
    }
}