package com.hobbycoders.aishuffle.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.os.Bundle
import android.text.InputType
import android.widget.ImageView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.textfield.TextInputEditText
import com.google.zxing.BarcodeFormat
import com.google.zxing.MultiFormatWriter
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.account.*
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import kotlinx.coroutines.delay
import org.json.JSONObject
import java.time.Instant
import net.openid.appauth.AuthorizationService

class AccountActivity : NativeScreen() {
    private lateinit var repo: AccountRepository
    private lateinit var pairCode: TextInputEditText
    private var profileUsername: TextInputEditText? = null
    private var profileDraft: String? = null
    private var mode = "login"
    /** Set once the deletion page was opened, so returning to the app checks the result. */
    private var deletionOpened = false
    private lateinit var oauthService: AuthorizationService
    private val oauthLauncher = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        if (result.resultCode != RESULT_OK || result.data == null) {
            repo.store.pendingOAuth = null
            status.text = "Sign-in cancelled. You can try again when you’re ready."
        } else runWork { signedIn(AccountStore.DEFAULT_HUB, AccountOAuth.finish(repo.store, result.data!!)) }
    }
    private val scanner = registerForActivityResult(ScanContract()) { result ->
        result.contents?.let {
            try {
                val pair = PairingInput.parseAccount(it, AccountStore.DEFAULT_HUB)
                pairCode.setText(pair.code)
                status.text = "QR scanned. Request approval to sign in."
            } catch (e: Exception) { status.text = e.message }
        }
    }
    private val camera = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) launchScanner() else status.text = "Camera is off. Enter the pairing code instead."
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        repo = AccountRepository(this)
        oauthService = AuthorizationService(this)
        mode = savedInstanceState?.getString("account_mode") ?: intent.getStringExtra("mode") ?: "login"
        profileDraft = savedInstanceState?.getString("profile_username")
        deletionOpened = savedInstanceState?.getBoolean("deletion_opened") ?: false
        navigation("Account")
        when {
            mode == "manage" && repo.store.session() != null -> manage()
            mode == "delete" && repo.store.session() != null -> deleteAccount()
            mode == "delete" -> deletionFinished()
            mode == "profile_sync" || (mode == "profile_setup" && repo.store.session()?.profileComplete == true) -> {
                profileSyncScreen()
                runWork { syncAfterSignIn() }
            }
            mode == "profile_setup" || mode == "profile_edit" -> profileForm(mode == "profile_setup")
            mode == "profile_loading_setup" || mode == "profile_loading_edit" -> loadProfile(mode == "profile_loading_setup")
            else -> form(mode, launchOAuth = savedInstanceState == null)
        }
    }
    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString("account_mode", mode)
        outState.putBoolean("deletion_opened", deletionOpened)
        if (mode == "profile_setup" || mode == "profile_edit") outState.putString("profile_username", profileUsername?.text?.toString())
    }
    override fun onResume() {
        super.onResume()
        // Back from the deletion page: ask the hub whether this account still exists.
        if (mode == "delete" && deletionOpened) runWork { checkDeletion() }
    }
    override fun onDestroy() { oauthService.dispose(); super.onDestroy() }
    private fun launchScanner() { scanner.launch(ScanOptions().setCaptureActivity(QrCaptureActivity::class.java).setDesiredBarcodeFormats(ScanOptions.QR_CODE).setBeepEnabled(false).setPrompt("Scan a trusted device’s account QR code")) }
    private fun scan() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) launchScanner()
        else camera.launch(Manifest.permission.CAMERA)
    }
    private fun form(selected: String, launchOAuth: Boolean = true) {
        mode = selected.takeIf { it in setOf("login", "create", "pair", "qr") } ?: "login"
        navigation("Account")
        if (mode == "login" || mode == "create") {
            heading(if (mode == "create") "Create account" else "Sign in", "Finish in your browser, then return to Shuffle.")
            button("Try again", primary = true) { openOAuth() }
            row("Use another device", "Sign in with an account QR code or pairing code.", R.drawable.ic_qr) { form("pair") }
            // Recreating this screen while the browser is open must not replace its pending proof.
            if (launchOAuth) openOAuth()
            return
        }
        val pairing = mode == "pair" || mode == "qr"
        heading("Use another device", "On a signed-in device, open Account and choose Add a trusted device.")
        if (pairing) {
            navigationAction(R.drawable.ic_qr, "Scan account QR code") { scan() }
            button("Scan account QR") { scan() }
            pairCode = input("Account pairing code", true)
            button("Request approval", primary = true) {
                try {
                    val origin = AccountStore.DEFAULT_HUB
                    val code = PairingInput.code(pairCode.text.toString())
                    confirm("Request sign-in?", "Approve this phone on your trusted device to finish signing in.") {
                        runWork {
                            val request = HubClient.call(origin, "/v1/pairing/exchange", "POST", HubClient.device(repo.store.beginSignIn()).put("code", code))
                            pairCode.setText("")
                            status.text = "Waiting for approval on your trusted device. Keep this screen open."
                            val deadline = Instant.now().plusSeconds(300)
                            while (Instant.now().isBefore(deadline)) {
                                delay(2000)
                                val answer = HubClient.call(origin, "/v1/pairing/poll", "POST", JSONObject().put("request_id", request.getString("request_id")).put("poll_token", request.getString("poll_token")))
                                if (answer.has("access_token")) { signedIn(origin, answer); return@runWork }
                                check(answer.optString("status") == "pending") { "This request has expired. Create a new code on your trusted device." }
                            }
                            error("Pairing expired. Create a new code on your trusted device.")
                        }
                    }
                } catch (e: Exception) { status.text = e.message }
            }
            return
        }
    }
    private fun openOAuth() {
        runWork {
            status.text = if (mode == "create") "Opening account creation…" else "Opening sign-in…"
            val request = AccountOAuth.begin(repo.store, mode == "create")
            oauthLauncher.launch(oauthService.getAuthorizationRequestIntent(request))
        }
    }
    private suspend fun signedIn(origin: String, result: JSONObject) {
        repo.accept(origin, result)
        status.text = "Signed in. Loading your workers…"
        finishAccountSetup()
    }
    private suspend fun finishAccountSetup() {
        val session = repo.store.session() ?: error("Sign in again to finish setting up your account.")
        when (session.profileComplete) {
            true -> { profileSyncScreen(); syncAfterSignIn() }
            false -> profileForm(onboarding = true)
            null -> {
                profileLoading(onboarding = true)
                if (AccountProfile(repo.store).refresh().profileComplete == true) { profileSyncScreen(); syncAfterSignIn() }
                else profileForm(onboarding = true)
            }
        }
    }
    private fun profileSyncScreen() {
        mode = "profile_sync"
        navigation("Account")
        heading("Loading your workers", "")
        button("Try again", primary = true) { runWork { syncAfterSignIn() } }
    }
    private fun profileLoading(onboarding: Boolean) {
        mode = if (onboarding) "profile_loading_setup" else "profile_loading_edit"
        navigation("Account")
        heading("Loading your account", "")
        button("Try again", primary = true) { loadProfile(onboarding) }
    }
    private fun loadProfile(onboarding: Boolean) {
        profileLoading(onboarding)
        runWork {
            val session = AccountProfile(repo.store).refresh()
            if (onboarding && session.profileComplete == true) { profileSyncScreen(); syncAfterSignIn() }
            else profileForm(onboarding)
        }
    }
    private fun profileForm(onboarding: Boolean) {
        val session = repo.store.session() ?: return form("login")
        if (session.profileComplete == null) { loadProfile(onboarding); return }
        mode = if (onboarding) "profile_setup" else "profile_edit"
        navigation("Account")
        heading(if (onboarding) "Choose your username" else "Edit username", "This is how your account appears in Shuffle.")
        val username = input("Username").apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            setText(profileDraft ?: if (session.profileComplete == true) session.username else "")
        }
        profileDraft = null
        profileUsername = username
        text(AccountProfile.USERNAME_HINT, 13f).setTextColor(color(R.color.slate))
        button(if (onboarding) "Save and continue" else "Save username", primary = true) {
            runWork {
                AccountProfile(repo.store).saveUsername(username.text.toString())
                if (onboarding) { profileSyncScreen(); syncAfterSignIn() }
                else { manage(); status.text = "Username updated." }
            }
        }
        if (!onboarding) link("Cancel") { manage() }
    }
    private suspend fun syncAfterSignIn() {
        intent.getStringExtra("worker_link_code")?.let { code ->
            startActivity(Intent(this, AddServerActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra("worker_link_code", code))
            finish()
            return
        }
        var message = ""
        try { repo.sync() }
        catch (e: kotlinx.coroutines.CancellationException) { throw e }
        catch (e: Exception) {
            if (e is HubFailure && e.status == 401) throw e
            message = "Signed in. Some workers could not load. Tap Refresh to retry."
        }
        startActivity(Intent(this, ServerListActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .putExtra("sync_message", message))
        finish()
    }
    private fun manage() {
        mode = "manage"
        val session = repo.store.session() ?: return form("login")
        navigation("Settings")
        heading(session.username, "Your Shuffle account")
        row("Edit username", "Choose how your account appears in Shuffle", R.drawable.ic_account) { loadProfile(onboarding = false) }
        row("Trusted devices", "View devices and manage access", R.drawable.ic_phone) { runWork { devices(session) } }
        row("Add a trusted device", "Create a sign-in QR code or pairing code", R.drawable.ic_qr) { runWork { grant(session) } }
        row("Delete account", "Permanently delete your Shuffle account and its data", R.drawable.ic_account) { deleteAccount() }
        gap(16)
        button("Sign out") {
            confirm("Sign out?", "This removes your account connections from this phone. Direct worker connections stay saved. If you’re offline, revoke this device from another trusted device too.") {
                runWork {
                    repo.clearLocal()
                    try { HubClient.call(session.hub, "/v1/sessions/current", "DELETE", token = session.token) }
                    catch (e: kotlinx.coroutines.CancellationException) { throw e }
                    catch (_: Exception) { /* Local sign-out already completed. */ }
                    startActivity(Intent(this@AccountActivity, ServerListActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
                    finish()
                }
            }
        }
    }
    /**
     * Google Play account deletion, started in the app. The hub's deletion
     * page does the deleting: it asks for a fresh sign-in on the hub itself,
     * so a stolen phone token cannot delete an account. See AccountDeletion.
     */
    private fun deleteAccount() {
        val session = repo.store.session() ?: return deletionFinished()
        mode = "delete"
        navigation("Delete account")
        heading("Delete ${session.username}", "Permanently delete your Shuffle account.")
        section("WHAT IS DELETED")
        text("Your Shuffle profile and sign-in identity, trusted devices, pairing codes and worker registrations.", 14f)
        text("Hosted network membership, recovery information and sync records held by the account service.", 14f)
        text("Remote-access tunnels and addresses created for your workers.", 14f)
        section("WHAT STAYS")
        text("Files, conversations and credentials on your own workers and devices, and anything your AI providers hold. Deletion is not a remote wipe. Your Google or other sign-in provider account is not deleted.", 14f)
        section("HOW IT WORKS")
        text("You sign in again on the account service's deletion page and type DELETE to confirm. It cannot be undone. When you come back, this phone checks the result and signs out.", 14f)
        gap(8)
        button(if (deletionOpened) "Open the deletion page again" else "Continue to delete", primary = true) {
            confirm("Delete your account?", "You will sign in again in your browser and confirm there. Deletion cannot be undone.") { openDeletion(session) }
        }
        if (deletionOpened) button("Check again") { runWork { checkDeletion() } }
        link("Cancel") { deletionOpened = false; manage() }
    }
    private fun openDeletion(session: AccountSession) {
        val url = try { AccountDeletion.pageFor(session.hub) } catch (e: IllegalArgumentException) { status.text = e.message; return }
        try {
            androidx.browser.customtabs.CustomTabsIntent.Builder().build().launchUrl(this, android.net.Uri.parse(url))
            deletionOpened = true
        } catch (_: android.content.ActivityNotFoundException) {
            status.text = "Install a browser to delete your account, or open $url on any device."
        }
    }
    private suspend fun checkDeletion() {
        val session = repo.store.session() ?: return deletionFinished()
        status.text = "Checking your account…"
        when (AccountDeletion.check { HubClient.call(session.hub, "/v1/devices", token = session.token) }) {
            AccountDeletion.Result.ACCESS_ENDED -> { repo.clearLocal(); deletionFinished() }
            AccountDeletion.Result.STILL_ACTIVE -> {
                deleteAccount()
                status.text = "Your account is still active. Finish deletion in the browser, then come back here."
            }
        }
    }
    /** The hub no longer accepts this phone and its account data here is gone. */
    private fun deletionFinished() {
        mode = "delete"
        deletionOpened = false
        navigation("Account")
        heading("Account deleted", "The account service no longer accepts this phone, and your account connections on this phone were removed. Direct worker connections stay saved. If cleanup is still finishing, the deletion page in your browser shows its status.")
        button("Done", primary = true) {
            startActivity(Intent(this, ServerListActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
            finish()
        }
    }
    private suspend fun devices(session: AccountSession) {
        val devices = HubClient.call(session.hub, "/v1/devices", token = session.token).getJSONArray("devices")
        check(repo.store.matches(session))
        heading("Trusted devices", "Devices signed in to ${session.username}")
        for (i in 0 until devices.length()) {
            val d = devices.getJSONObject(i)
            val current = d.getString("id") == session.deviceId
            val revoked = !d.isNull("revoked_at") && d.optString("revoked_at").isNotBlank()
            group {
                text(d.getString("name"), 18f)
                text(if (current) "This phone" else if (revoked) "Access revoked" else d.optString("platform"), 13f).setTextColor(color(R.color.slate))
                if (!revoked) button("Revoke ${d.getString("name")}") {
                    confirm("Revoke this device?", "${d.getString("name")} will lose access to your account and workers.") {
                        runWork {
                            HubClient.call(session.hub, "/v1/devices/${HubClient.segment(d.getString("id"))}", "DELETE", token = session.token)
                            if (current) { repo.clearLocal(); form("login", launchOAuth = false); status.text = "This device was revoked." }
                            else { devices(session); status.text = "Device revoked." }
                        }
                    }
                }
            }
        }
        link("Back to account") { manage() }
    }
    private suspend fun grant(session: AccountSession) {
        val grant = HubClient.call(session.hub, "/v1/pairing", "POST", token = session.token)
        val expiry = Instant.parse(grant.getString("expires_at"))
        val code = PairingInput.code(grant.getString("code"))
        val pairing = PairingInput.parse(grant.getString("pairing_url"))
        require(pairing.origin == session.hub && pairing.code == code) { "Hub returned an invalid pairing link." }
        heading("Add a trusted device", "Expires ${grant.getString("expires_at")}\nScan or enter this code on the new device. The first device to use it is trusted right away; the code then stops working.")
        text(code.chunked(6).joinToString("  "), 28f).typeface = android.graphics.Typeface.MONOSPACE
        // Product QR contains only the short-lived code. It cannot choose an account server.
        require(session.hub == AccountStore.DEFAULT_HUB) { "Sign in to Shuffle again to pair another device." }
        val bits = MultiFormatWriter().encode(PairingInput.accountQr(code), BarcodeFormat.QR_CODE, 480, 480)
        val bitmap = Bitmap.createBitmap(480, 480, Bitmap.Config.RGB_565)
        for (y in 0 until 480) for (x in 0 until 480) bitmap.setPixel(x, y, if (bits[x, y]) android.graphics.Color.BLACK else android.graphics.Color.WHITE)
        content.addView(ImageView(this).apply { setImageBitmap(bitmap); adjustViewBounds = true; contentDescription = "Account pairing QR. The same code is printed above." }, android.widget.LinearLayout.LayoutParams(-1, (260 * resources.displayMetrics.density).toInt()))
        button("Back to account") { manage() }
        status.text = "Waiting for the new device. Keep this screen open."
        while (Instant.now().isBefore(expiry)) {
            delay(2000)
            val requests = HubClient.call(session.hub, "/v1/pairing/${HubClient.segment(grant.getString("id"))}/requests", token = session.token).getJSONArray("requests")
            check(repo.store.matches(session))
            val all = (0 until requests.length()).map { requests.getJSONObject(it) }
            // The hub trusts the first device that presents the code; show who it was.
            val trusted = all.firstOrNull { it.optString("status") == "approved" }
            if (trusted != null) {
                manage(); status.text = "${trusted.getString("device_name")} is now a trusted device. The pairing code stopped working."
                return
            }
            val pending = all.filter { it.optString("status") == "pending" }
            if (pending.isNotEmpty()) {
                heading("Review device request", "Code expires ${grant.getString("expires_at")}. Approve only a phone you are pairing now. Approval invalidates this code and other requests.")
                for (request in pending) {
                    val deviceName = request.getString("device_name")
                    text("$deviceName · ${request.optString("platform")}", 22f)
                    button("Approve $deviceName") {
                        confirm("Trust $deviceName?", "This device will receive account and shared-server access. Request expires ${grant.getString("expires_at")}. Approve only if this name matches your device.") {
                            runWork {
                                check(Instant.now().isBefore(expiry)) { "Pairing expired. Create a new code." }
                                HubClient.call(session.hub, "/v1/pairing/${HubClient.segment(grant.getString("id"))}/approve", "POST", JSONObject().put("request_id", request.getString("id")), session.token)
                                manage(); status.text = "$deviceName approved. Pairing code invalidated."
                            }
                        }
                    }
                }
                button("Do not approve · back to account") { manage() }
                status.text = "No access is granted until you approve."
                return
            }
        }
        manage(); status.text = "Pairing expired. Create a new code to try again."
    }
}
