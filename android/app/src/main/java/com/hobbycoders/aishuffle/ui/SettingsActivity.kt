package com.hobbycoders.aishuffle.ui

import android.content.Intent
import android.content.ActivityNotFoundException
import android.net.Uri
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
        section("ANDROID APK")
        row("Download latest APK", "Install over this app to keep your accounts and workers", R.drawable.ic_refresh) {
            openDownloadPage("https://github.com/HobbyCoders/ai-shuffle/releases/download/android-latest/AI-Shuffle-Android.apk")
        }
        link("Version and release notes") {
            openDownloadPage("https://github.com/HobbyCoders/ai-shuffle/releases/tag/android-latest")
        }
    }

    private fun openDownloadPage(url: String) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        } catch (_: ActivityNotFoundException) {
            status.text = "Install a browser to open the Android download."
        }
    }
}
