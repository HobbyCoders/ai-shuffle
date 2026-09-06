package com.hobbycoders.aishuffle.ui

import android.annotation.SuppressLint
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.Uri
import android.os.Bundle
import android.webkit.*
import android.widget.LinearLayout
import androidx.activity.OnBackPressedCallback
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.hobbycoders.aishuffle.account.AccountRepository
import com.hobbycoders.aishuffle.account.AccountStore
import com.hobbycoders.aishuffle.account.PairingInput
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.net.ApiResult
import com.hobbycoders.aishuffle.tools.DeviceToolsRuntime
import com.hobbycoders.aishuffle.voice.VoiceCallActivity
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import java.io.ByteArrayInputStream

/** No JavascriptInterface: even a trusted page's frames cannot call native code. */
class ChatActivity : NativeScreen() {
    private lateinit var server: Server
    private lateinit var web: WebView
    private var sessionJob: Job? = null
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private val fileChooser = registerForActivityResult(androidx.activity.result.contract.ActivityResultContracts.StartActivityForResult()) { result ->
        fileCallback?.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data))
        fileCallback = null
    }
    private val signedOut = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) { web.stopLoading(); web.loadUrl("about:blank"); finish() }
    }
    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        server = ServerStore.get(this).getById(intent.getStringExtra(EXTRA_SERVER_ID).orEmpty()) ?: run { finish(); return }
        try { PairingInput.origin(server.baseUrl) } catch (_: Exception) { heading("HTTPS required", "Pair this server again using its HTTPS address. Existing insecure credentials will not be sent."); return }
        if (!authorized()) { finish(); return }
        heading(server.name, "${server.host} · ${if (server.accountId == null) "direct pairing" else "account server"}")
        button("Voice") { startActivity(Intent(this, VoiceCallActivity::class.java).putExtra("server_id", server.id)) }
        button("Refresh chat") { startSession() }
        web = WebView(this)
        // The WebView owns the available viewport rather than living in a ScrollView.
        val shell = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val parent = content.parent as android.view.ViewGroup
        parent.removeView(content); parent.removeView(status)
        shell.addView(content); shell.addView(status)
        shell.addView(web, LinearLayout.LayoutParams(-1, 0, 1f)); setContentView(shell)
        with(web.settings) {
            javaScriptEnabled = true; domStorageEnabled = true
            allowFileAccess = false; allowContentAccess = false
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            javaScriptCanOpenWindowsAutomatically = false; setSupportMultipleWindows(false)
            mediaPlaybackRequiresUserGesture = true
        }
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false)
        web.webChromeClient = object : WebChromeClient() {
            override fun onConsoleMessage(message: ConsoleMessage?) = true
            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                if (!PairingInput.sameOrigin(server.baseUrl, view.url.orEmpty()) || !authorized()) return false
                fileCallback?.onReceiveValue(null)
                fileCallback = callback
                return try { fileChooser.launch(params.createIntent()); true }
                catch (_: Exception) { fileCallback = null; callback.onReceiveValue(null); true }
            }
            override fun onPermissionRequest(request: PermissionRequest) { request.deny() }
        }
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                if (PairingInput.sameOrigin(server.baseUrl, request.url.toString())) return false
                if (request.isForMainFrame && request.hasGesture() && request.url.scheme == "https") {
                    confirm("Open outside Shuffle?", "${request.url.host} opens in your browser. No account or server credential will be added.") {
                        runCatching { startActivity(Intent(Intent.ACTION_VIEW, request.url)) }
                    }
                }
                return true
            }
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                if (!PairingInput.sameOrigin(server.baseUrl, request.url.toString()))
                    return WebResourceResponse("text/plain", "UTF-8", 403, "External content blocked", emptyMap(), ByteArrayInputStream(ByteArray(0)))
                if (request.isForMainFrame && request.method != "GET")
                    return WebResourceResponse("text/plain", "UTF-8", 403, "Blocked", emptyMap(), ByteArrayInputStream("Use the native sign-in screen to reconnect.".toByteArray()))
                return if (request.isForMainFrame) ContainedDocument.load(server, request.url.toString(), request.requestHeaders["User-Agent"].orEmpty()) else null
            }
            override fun onPageStarted(view: WebView, url: String, favicon: android.graphics.Bitmap?) {
                if (!PairingInput.sameOrigin(server.baseUrl, url)) { view.stopLoading(); return }
            }
            override fun onPageFinished(view: WebView, url: String) { if (PairingInput.sameOrigin(server.baseUrl, url)) status.text = "" }
            override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: android.net.http.SslError) {
                handler.cancel(); status.text = "HTTPS verification failed. Fix the server certificate before reconnecting."
            }
        }
        // Downloads never forward session cookies into Android's redirect-following DownloadManager.
        web.setDownloadListener { _, _, _, _, _ -> status.text = "For downloads, open this server in your browser and sign in there." }
        ContextCompat.registerReceiver(this, signedOut, IntentFilter(AccountRepository.ACTION_CLEARED), ContextCompat.RECEIVER_NOT_EXPORTED)
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() { if (web.canGoBack()) web.goBack() else finish() }
        })
        startSession()
    }
    private fun authorized(): Boolean = server.accountId == null || AccountStore(this).session()?.let { it.accountId == server.accountId && it.hub == server.hubUrl } == true
    override fun onPause() {
        if (::web.isInitialized) DeviceToolsRuntime.detach(this)
        super.onPause()
    }
    override fun onStop() {
        if (::web.isInitialized) { DeviceToolsRuntime.detach(this); web.onPause() }
        sessionJob?.cancel()
        super.onStop()
    }
    override fun onResume() { super.onResume(); if (::web.isInitialized) { if (!authorized()) finish() else { web.onResume(); DeviceToolsRuntime.attach(this, server) } } }
    private fun startSession() {
        if (sessionJob?.isActive == true) return
        sessionJob = lifecycleScope.launch {
            status.text = "Opening a secure chat…"
            if (server.hasCfAccess) ApiClient.fetchCfAuthorizationCookie(server)?.let { CookieManager.getInstance().setCookie(server.baseUrl, "$it; Path=/; Secure; HttpOnly; SameSite=Strict") }
            when (val result = ApiClient.createWebSession(server)) {
                is ApiResult.Success -> {
                    if (!authorized()) return@launch
                    require(!result.data.contains(';') && !result.data.contains('\n') && !result.data.contains('\r'))
                    CookieManager.getInstance().setCookie(server.baseUrl, "session=${result.data}; Path=/; Secure; HttpOnly; SameSite=Strict") {
                        if (!isFinishing && authorized()) { CookieManager.getInstance().flush(); web.loadUrl(server.baseUrl + "/") }
                    }
                }
                is ApiResult.Failure -> status.text = result.message
                is ApiResult.NeedsCfAccess -> status.text = "Cloudflare Access rejected this device. Pair the server again with a valid service token."
            }
        }
    }
    override fun onDestroy() {
        fileCallback?.onReceiveValue(null); fileCallback = null
        if (::web.isInitialized) { runCatching { unregisterReceiver(signedOut) }; web.stopLoading(); web.clearHistory(); web.destroy() }
        super.onDestroy()
    }
    companion object { const val EXTRA_SERVER_ID = "server_id" }
}
