package com.hobbycoders.aishuffle.ui

import android.content.Intent
import android.content.ActivityNotFoundException
import android.net.Uri
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.account.AccountDeletion
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
        // Play account deletion: in the app when signed in, the public page otherwise.
        link("Delete account") {
            if (session != null) startActivity(Intent(this, AccountActivity::class.java).putExtra("mode", "delete"))
            else openLink(AccountDeletion.PUBLIC_URL, "Install a browser to open the account deletion page.")
        }
        section("THIS PHONE")
        row("Phone tools", "App control, timers and permissions", R.drawable.ic_phone) {
            startActivity(Intent(this, ToolSettingsActivity::class.java))
        }
        row("Explore offline demo", "Try sample conversations without an account or worker", R.drawable.ic_shuffle) {
            startActivity(Intent(this, OfflineDemoActivity::class.java))
        }
        link("Version and release notes") {
            openLink("https://github.com/HobbyCoders/ai-shuffle/releases/tag/android-latest", "Install a browser to open the release notes.")
        }
    }

    private fun openLink(url: String, noBrowser: String) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        } catch (_: ActivityNotFoundException) {
            status.text = noBrowser
        }
    }
}
