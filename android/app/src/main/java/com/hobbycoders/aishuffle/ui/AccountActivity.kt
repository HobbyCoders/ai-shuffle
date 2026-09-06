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
    private var mode = "login"
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
        navigation("Account")
        if (mode == "manage" && repo.store.session() != null) manage() else form(mode)
    }
    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString("account_mode", mode)
    }
    override fun onDestroy() { oauthService.dispose(); super.onDestroy() }
    private fun launchScanner() { scanner.launch(ScanOptions().setDesiredBarcodeFormats(ScanOptions.QR_CODE).setBeepEnabled(false).setPrompt("Scan a trusted device’s account QR code")) }
    private fun scan() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) launchScanner()
        else camera.launch(Manifest.permission.CAMERA)
    }
    private fun form(selected: String) {
        mode = selected
        navigation("Account")
        val pairing = mode == "pair" || mode == "qr"
        heading(when (mode) { "create" -> "Create your account"; "recover" -> "Recover your account"; "pair", "qr" -> "Use another device"; else -> "Welcome back" },
            when (mode) {
                "create" -> "One account for your workers on every device."
                "recover" -> "Use your recovery code to set a new password."
                "pair", "qr" -> "On a signed-in device, open Account and choose Add a trusted device."
                else -> "Sign in to see your workers."
            })
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
        if (mode == "login" || mode == "create") {
            text("Use Google, GitHub, Microsoft or email.", 16f).setTextColor(color(R.color.slate))
            gap(16)
            button(if (mode == "create") "Continue to create account" else "Continue to sign in", primary = true) {
                runWork {
                    status.text = "Opening secure sign-in…"
                    val request = AccountOAuth.begin(repo.store, mode == "create")
                    oauthLauncher.launch(oauthService.getAuthorizationRequestIntent(request))
                }
            }
            row("Use another device", "Sign in with an account QR code or pairing code.", R.drawable.ic_qr) { form("pair") }
            link("Use an existing Shuffle password") { form("password") }
            return
        }
        val username = input("Username").apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        }
        val password = input(if (mode == "recover") "New password" else "Password", true)
        if (mode != "login") text("Use at least 12 characters.", 13f).setTextColor(color(R.color.slate))
        val recovery = if (mode == "recover") input("Recovery code", true) else null
        button(when (mode) { "create" -> "Create account"; "recover" -> "Reset password"; else -> "Sign in" }, primary = true) {
            try {
                require(username.text.toString().trim().isNotEmpty()) { "Enter your username." }
                require(password.text.toString().length >= 12) { "Use a password with at least 12 characters." }
                val origin = AccountStore.DEFAULT_HUB
                val submit = {
                    runWork {
                        val body = HubClient.device(repo.store.beginSignIn()).put("username", username.text.toString().trim()).put("password", password.text.toString())
                        recovery?.let { body.put("recovery_code", it.text.toString().trim()) }
                        val path = when (mode) { "create" -> "/v1/accounts"; "recover" -> "/v1/accounts/recover"; else -> "/v1/sessions" }
                        val result = try { HubClient.call(origin, path, "POST", body) }
                            catch (e: HubFailure) {
                                if (e.status == 401 && mode == "password") throw IllegalArgumentException("Check your username and password, then try again.")
                                throw e
                            }
                        password.setText(""); recovery?.setText("")
                        signedIn(origin, result)
                    }
                }
                when {
                    mode == "recover" -> confirm("Reset password?", "This signs out your other devices and replaces your password and recovery code.", submit)
                    else -> submit()
                }
            } catch (e: Exception) { status.text = e.message }
        }
        if (mode == "password") {
            link("Forgot password?") { form("recover") }
            gap(8)
            row("Use another device", "Sign in with an account QR code or pairing code.", R.drawable.ic_qr) { form("pair") }
            link("New to Shuffle? Create account") { form("create") }
        } else link("Already have an account? Sign in") { form("login") }
    }
    private suspend fun signedIn(origin: String, result: JSONObject) {
        repo.accept(origin, result)
        val recovery = result.optString("recovery_code")
        status.text = "Signed in. Loading your workers…"
        if (recovery.isNotBlank()) {
            // Never persist, copy automatically or log the recovery code.
            MaterialAlertDialogBuilder(this).setTitle("Save your recovery code")
                .setMessage("$recovery\n\nKeep this somewhere private. You’ll need it if you forget your password. It’s shown only once.")
                .setCancelable(false).setPositiveButton("I saved my recovery code") { _, _ ->
                    runWork { syncAfterSignIn() }
                }.show()
        } else syncAfterSignIn()
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
        row("Trusted devices", "View devices and manage access", R.drawable.ic_phone) { runWork { devices(session) } }
        row("Add a trusted device", "Create a sign-in QR code or pairing code", R.drawable.ic_qr) { runWork { grant(session) } }
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
                            if (current) { repo.clearLocal(); form("login"); status.text = "This device was revoked." }
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
        heading("Add a trusted device", "Expires ${grant.getString("expires_at")}\nScan or enter this code on the new device. You must approve its name here.")
        text(code.chunked(6).joinToString("  "), 28f).typeface = android.graphics.Typeface.MONOSPACE
        // Product QR contains only the short-lived code. It cannot choose an account server.
        require(session.hub == AccountStore.DEFAULT_HUB) { "Sign in to Shuffle again to pair another device." }
        val bits = MultiFormatWriter().encode(PairingInput.accountQr(code), BarcodeFormat.QR_CODE, 480, 480)
        val bitmap = Bitmap.createBitmap(480, 480, Bitmap.Config.RGB_565)
        for (y in 0 until 480) for (x in 0 until 480) bitmap.setPixel(x, y, if (bits[x, y]) android.graphics.Color.BLACK else android.graphics.Color.WHITE)
        content.addView(ImageView(this).apply { setImageBitmap(bitmap); adjustViewBounds = true; contentDescription = "Account pairing QR. The same code is printed above." }, android.widget.LinearLayout.LayoutParams(-1, (260 * resources.displayMetrics.density).toInt()))
        button("Back to account") { manage() }
        status.text = "Waiting for a device request. Keep this screen open."
        while (Instant.now().isBefore(expiry)) {
            delay(2000)
            val requests = HubClient.call(session.hub, "/v1/pairing/${HubClient.segment(grant.getString("id"))}/requests", token = session.token).getJSONArray("requests")
            check(repo.store.matches(session))
            val pending = (0 until requests.length()).map { requests.getJSONObject(it) }.filter { it.optString("status") == "pending" }
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
