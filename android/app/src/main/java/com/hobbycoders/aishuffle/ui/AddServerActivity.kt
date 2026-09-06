package com.hobbycoders.aishuffle.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.text.InputType
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.account.AccountRepository
import com.hobbycoders.aishuffle.account.PairingInput
import com.hobbycoders.aishuffle.account.WorkerEnrollment
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.net.ApiResult
import com.google.android.material.textfield.TextInputEditText
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull

class AddServerActivity : NativeScreen() {
    private var url: TextInputEditText? = null
    private lateinit var code: TextInputEditText
    private var direct = false
    private var approvedId: String? = null
    private val scan = registerForActivityResult(ScanContract()) { result ->
        result.contents?.let { raw ->
            try {
                if (raw.startsWith("aishuffle://worker/")) {
                    val value = PairingInput.workerCode(raw)
                    render(false); code.setText(value)
                    status.text = "QR scanned. Continue to review this worker."
                } else {
                    val pair = PairingInput.parseDirect(raw)
                    render(true); url?.setText(pair.origin); code.setText(pair.code)
                    status.text = "QR scanned. Review the worker address, then connect."
                }
            } catch (e: Exception) { status.text = e.message }
        }
    }
    private val camera = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) launchScanner() else status.text = "Camera is off. You can still enter the pairing code."
    }
    private fun launchScanner() {
        scan.launch(ScanOptions().setCaptureActivity(QrCaptureActivity::class.java).setDesiredBarcodeFormats(ScanOptions.QR_CODE).setBeepEnabled(false).setPrompt("Scan your worker’s pairing QR code"))
    }
    private fun scanWorker() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) launchScanner()
        else camera.launch(Manifest.permission.CAMERA)
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        render(savedInstanceState?.getBoolean("direct") ?: false)
        url?.setText(savedInstanceState?.getString("worker_url").orEmpty())
        approvedId = savedInstanceState?.getString("approved_worker_id")
        if (approvedId != null) status.text = "Worker approved. Tap Continue to reconnect, or return to Workers and refresh. No new code is needed."
        receiveWorkerCode()
    }
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent); setIntent(intent); receiveWorkerCode()
    }
    private fun receiveWorkerCode() {
        intent.getStringExtra("worker_link_code")?.let {
            intent.removeExtra("worker_link_code")
            runCatching { PairingInput.workerCode(it) }.onSuccess { value ->
                render(false); code.setText(value)
                status.text = "Signed in. Continue to review this worker."
            }
        }
    }
    private fun render(useUrl: Boolean) {
        direct = useUrl; url = null; approvedId = null
        navigation("Workers")
        navigationAction(R.drawable.ic_qr, "Scan worker QR code") { scanWorker() }
        heading("Add worker", if (direct) "Use a worker’s HTTPS address and pairing code."
            else "On your worker, open Settings → Devices and choose Connect to my account.")
        button("Scan QR code") { scanWorker() }
        gap(12)
        if (direct) {
            url = input("Worker URL").apply {
                inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
                isSaveEnabled = false
            }
        }
        code = input("Pairing code", true)
        text(if (direct) "You can also paste a direct pairing link in Worker URL."
            else "Enter its 12-character code. Shuffle sets up remote access automatically.", 13f).setTextColor(color(R.color.slate))
        button(if (direct) "Connect worker" else "Continue", primary = true) {
            if (direct) submitDirect() else submitAccount()
        }
        link(if (direct) "Use an account connection code" else "Use a worker URL") { render(!direct) }
    }
    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putBoolean("direct", direct)
        outState.putString("approved_worker_id", approvedId)
        outState.putString("worker_url", runCatching { PairingInput.origin(url?.text.toString()) }.getOrDefault(""))
    }
    private fun submitAccount() {
        approvedId?.let { id -> runWork { awaitWorker(id) }; return }
        try {
            val value = PairingInput.workerCode(code.text.toString())
            val repo = AccountRepository(this)
            if (repo.store.session() == null) {
                startActivity(Intent(this, AccountActivity::class.java).putExtra("worker_link_code", value))
                return
            }
            runWork {
                val worker = repo.lookupWorker(value)
                confirm("Add ${worker.getString("name")}?", "This worker will join your account and become reachable remotely. Only approve a worker you own. Phone tools stay off until you enable them.") {
                    runWork {
                        val id = repo.approveWorker(value)
                        approvedId = id; code.setText("")
                        awaitWorker(id)
                    }
                }
                status.text = "Review the worker to continue."
            }
        } catch (e: Exception) { status.text = e.message }
    }
    private suspend fun awaitWorker(id: String) {
        val repo = AccountRepository(this)
        var lastFailure: String? = null
        status.text = "Worker approved. Setting up its remote connection…"
        val connected = withTimeoutOrNull(60_000) {
            repeat(24) {
                try {
                    if (repo.workerAvailable(id)) {
                        status.text = "Connecting your phone to the worker…"
                        repo.sync()
                        if (ServerStore.get(this@AddServerActivity).getAll().any { it.hubServerId == id }) {
                            return@withTimeoutOrNull true
                        }
                    }
                } catch (e: CancellationException) { throw e }
                catch (e: Exception) {
                    if (!WorkerEnrollment.retryable(e)) throw e
                    lastFailure = e.message
                    status.text = "${e.message.orEmpty()}\n\nYour approval is saved. Retrying the connection…"
                }
                delay(2500)
            }
            false
        }
        if (connected == true) { setResult(RESULT_OK); finish(); return }
        status.text = listOfNotNull(lastFailure,
            "Worker approved. Keep Shuffle running on it. Tap Continue to retry, or go back and refresh Workers. No new code is needed.").joinToString("\n\n")
    }
    private fun submitDirect() {
        try {
            val rawUrl = url?.text.toString().trim()
            val pair = if (rawUrl.contains('#')) PairingInput.parseDirect(rawUrl)
                else PairingInput.Pair(PairingInput.origin(rawUrl), PairingInput.code(code.text.toString()))
            url?.setText(pair.origin); code.setText(pair.code)
            val server = Server.create(java.net.URI(pair.origin).host, pair.origin, "")
            confirm("Connect to ${server.host}?", "This worker will receive the pairing code and this phone’s device information.") {
                runWork {
                    when (val result = ApiClient.registerDevice(server.baseUrl, "${Build.MANUFACTURER} ${Build.MODEL}", server.deviceId, null, null, pair.code)) {
                        is ApiResult.Success -> {
                            ServerStore.get(this@AddServerActivity).add(server.copy(apiKey = result.data.apiKey, deviceId = result.data.deviceId))
                            code.setText(""); setResult(RESULT_OK); finish()
                        }
                        is ApiResult.NeedsCfAccess -> status.text = "This address has a separate Cloudflare login gate. Use the worker’s Shuffle account connection."
                        is ApiResult.Failure -> status.text = result.message
                    }
                }
            }
        } catch (e: Exception) { status.text = e.message }
    }
}
