package com.hobbycoders.aishuffle.ui

import android.Manifest
import android.annotation.SuppressLint
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.Uri
import android.content.pm.PackageManager
import android.os.Bundle
import android.text.TextUtils
import android.view.View
import android.view.ViewGroup
import android.webkit.*
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.widget.PopupMenu
import androidx.core.content.ContextCompat
import androidx.core.view.WindowCompat
import androidx.core.widget.doAfterTextChanged
import androidx.lifecycle.lifecycleScope
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.account.AccountRepository
import com.hobbycoders.aishuffle.account.AccountStore
import com.hobbycoders.aishuffle.account.PairingInput
import com.hobbycoders.aishuffle.account.ManagedWorkerRepair
import com.hobbycoders.aishuffle.account.WorkerRepairFailure
import com.hobbycoders.aishuffle.account.WorkerRepairSnapshot
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.net.ApiResult
import com.hobbycoders.aishuffle.tools.DeviceToolsRuntime
import com.hobbycoders.aishuffle.voice.VoiceCallActivity
import com.hobbycoders.aishuffle.voice.VoiceCallService
import kotlinx.coroutines.Job
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import com.google.android.material.button.MaterialButton
import java.io.ByteArrayInputStream

/** Native capabilities are restricted to the connected HTTPS document; no JavascriptInterface. */
class ChatActivity : NativeScreen() {
    private lateinit var server: Server
    private lateinit var web: WebView
    private var sessionJob: Job? = null
    private var sessionAttempt = 0
    private lateinit var sessionOwner: ChatSessionOwner
    private var sharedAttempt: WebSessionBootstrap.Attempt? = null
    private var needsSession = true
    private lateinit var repairButton: MaterialButton
    private lateinit var downloads: ChatDownloads
    private lateinit var nativeVoice: NativeVoiceBridge
    private var document = 0
    private var fileDocument = -1
    private var fileChooserOpen = false
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private val fileChooser = registerForActivityResult(androidx.activity.result.contract.ActivityResultContracts.StartActivityForResult()) { result ->
        fileChooserOpen = false
        val callback = fileCallback
        fileCallback = null
        val uris = WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data)
        callback?.onReceiveValue(uris?.takeIf { fileDocument == document && validDocument() &&
            it.size in 1..32 && it.all { uri -> uri.scheme == "content" } })
    }
    private var microphone: PermissionRequest? = null
    private var microphoneDocument = -1
    private var microphoneDialogOpen = false
    private var microphoneReady = false
    private val microphonePermission = registerForActivityResult(androidx.activity.result.contract.ActivityResultContracts.RequestPermission()) { granted ->
        microphoneDialogOpen = false
        microphoneReady = granted
        if (!granted) { microphone?.deny(); microphone = null }
        else completeMicrophonePermission()
    }
    private val signedOut = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) { web.stopLoading(); web.loadUrl("about:blank"); finish() }
    }
    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        server = ServerStore.get(this).getById(intent.getStringExtra(EXTRA_SERVER_ID).orEmpty()) ?: run { finish(); return }
        try { PairingInput.origin(server.baseUrl) } catch (_: Exception) { heading("HTTPS required", "Pair this server again using its HTTPS address. Existing insecure credentials will not be sent."); return }
        sessionOwner = ChatSessionOwner(this, server)
        if (!authorized()) { finish(); return }
        web = WebView(this)
        chatLayout()
        downloads = ChatDownloads(this, web, server, ::authorized) { status.text = it }
        downloads.install()
        nativeVoice = NativeVoiceBridge(this, web, server.baseUrl, ::authorized, refusal = { sessionId ->
            val call = com.hobbycoders.aishuffle.voice.VoiceCallState.state.value
            if (com.hobbycoders.aishuffle.voice.VoiceCallState.isActive && (call.serverId != server.id || call.sessionId != sessionId))
                "End the current voice call before opening another chat." else null
        }) { sessionId ->
            startActivity(Intent(this, VoiceCallActivity::class.java)
                .putExtra(VoiceCallService.EXTRA_SERVER_ID, server.id).putExtra(VoiceCallService.EXTRA_SESSION_ID, sessionId))
        }
        nativeVoice.install()
        with(web.settings) {
            javaScriptEnabled = true; domStorageEnabled = true
            allowFileAccess = false; allowContentAccess = false
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            javaScriptCanOpenWindowsAutomatically = false; setSupportMultipleWindows(false)
            mediaPlaybackRequiresUserGesture = true
            // Tells the frontend to render its mobile shell; see WebUserAgent.
            userAgentString = WebUserAgent.decorate(userAgentString, appVersionName())
        }
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false)
        web.webChromeClient = object : WebChromeClient() {
            override fun onConsoleMessage(message: ConsoleMessage?) = true
            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                if (!validDocument() || fileChooserOpen) { callback.onReceiveValue(null); return true }
                fileCallback = callback
                fileDocument = document
                return try { fileChooserOpen = true; fileChooser.launch(params.createIntent()); true }
                catch (_: Exception) { fileChooserOpen = false; fileCallback = null; callback.onReceiveValue(null); true }
            }
            override fun onPermissionRequest(request: PermissionRequest) {
                if (!validMicrophone(request) || microphoneDialogOpen || !microphoneForeground()) { request.deny(); return }
                if (ContextCompat.checkSelfPermission(this@ChatActivity, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                    request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                } else {
                    microphone?.deny()
                    microphone = request
                    microphoneDocument = document
                    microphoneReady = false
                    microphoneDialogOpen = true
                    microphonePermission.launch(Manifest.permission.RECORD_AUDIO)
                }
            }
            override fun onPermissionRequestCanceled(request: PermissionRequest) {
                if (microphone === request) microphone = null
            }
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
                document++
                downloads.navigating()
                nativeVoice.navigating()
                cancelPagePermissions()
                if (!PairingInput.sameOrigin(server.baseUrl, url)) { view.stopLoading(); return }
            }
            override fun onPageFinished(view: WebView, url: String) { if (PairingInput.sameOrigin(server.baseUrl, url)) status.text = "" }
            override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: android.net.http.SslError) {
                handler.cancel(); status.text = "HTTPS verification failed. Fix the server certificate before reconnecting."
            }
        }
        web.setDownloadListener(downloads::fromUrl)
        ContextCompat.registerReceiver(this, signedOut, IntentFilter(AccountRepository.ACTION_CLEARED), ContextCompat.RECEIVER_NOT_EXPORTED)
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() { if (web.canGoBack()) web.goBack() else finish() }
        })
    }
    private fun appVersionName(): String? =
        runCatching { packageManager.getPackageInfo(packageName, 0).versionName }.getOrNull()
    private fun chatLayout() {
        navigation(server.name)
        val toolbar = findViewById<LinearLayout>(R.id.screenToolbar)
        toolbar.setPadding(dp(4), 0, dp(4), 0)
        for (index in 0 until toolbar.childCount) {
            (toolbar.getChildAt(index) as? TextView)?.apply {
                maxLines = 1; ellipsize = TextUtils.TruncateAt.END
                setPadding(dp(4), 0, dp(8), 0)
            }
        }
        navigationAction(R.drawable.ic_mic, "Start voice call") {
            startActivity(Intent(this, VoiceCallActivity::class.java).putExtra("server_id", server.id))
        }
        val more = iconButton(R.drawable.ic_more, "More chat options") { }
        more.setOnClickListener {
            PopupMenu(this, more).apply {
                menu.add("Refresh chat").setOnMenuItemClickListener { startSession(); true }
                show()
            }
        }
        toolbar.addView(more)
        (toolbar.parent as ViewGroup).removeView(toolbar)
        (status.parent as ViewGroup).removeView(status)
        status.setPadding(dp(16), dp(4), dp(16), dp(8))
        status.visibility = if (status.text.isNullOrBlank()) View.GONE else View.VISIBLE
        status.doAfterTextChanged { status.visibility = if (it.isNullOrBlank()) View.GONE else View.VISIBLE }
        // Keep system bars and the keyboard outside the chat viewport. The WebView
        // gets all remaining height, with no surrounding ScrollView or blank status row.
        WindowCompat.setDecorFitsSystemWindows(window, true)
        val shell = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; setBackgroundColor(color(R.color.surface))
            addView(toolbar, LinearLayout.LayoutParams(-1, dp(56)))
            addView(status, LinearLayout.LayoutParams(-1, -2))
            repairButton = MaterialButton(this@ChatActivity).apply {
                text = "Reconnect this worker"; isAllCaps = false; visibility = View.GONE
                setTextColor(color(R.color.on_primary))
                backgroundTintList = android.content.res.ColorStateList.valueOf(color(R.color.primary))
                com.hobbycoders.aishuffle.tools.guardConsentTouch(this)
                setOnClickListener {
                    if (authorized()) startActivity(Intent(this@ChatActivity, WorkerRepairActivity::class.java).putExtra(EXTRA_SERVER_ID, server.id))
                }
            }
            addView(repairButton, LinearLayout.LayoutParams(-1, -2).apply { marginStart = dp(16); marginEnd = dp(16) })
            addView(web, LinearLayout.LayoutParams(-1, 0, 1f))
        }
        setContentView(shell)
    }
    private fun authorized() = ::sessionOwner.isInitialized && sessionOwner.current()
    private fun validDocument() = ::web.isInitialized && !isFinishing && !isDestroyed &&
        ChatAccessPolicy.document(server.baseUrl, web.url, authorized())
    private fun validMicrophone(request: PermissionRequest) = validDocument() &&
        ChatAccessPolicy.microphone(server.baseUrl, web.url, request.origin.toString(), request.resources, authorized())
    private fun microphoneForeground() = hasWindowFocus() && lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.RESUMED)
    private fun completeMicrophonePermission() {
        val request = microphone ?: return
        if (microphoneDocument != document || !validMicrophone(request)) {
            request.deny(); microphone = null; microphoneReady = false
        } else if (microphoneReady && microphoneForeground()) {
            microphone = null; microphoneReady = false
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED)
                request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
            else request.deny()
        }
    }
    private fun cancelPagePermissions() {
        microphone?.deny(); microphone = null; microphoneReady = false
        fileCallback?.onReceiveValue(null); fileCallback = null
    }
    override fun onPause() {
        if (::web.isInitialized) DeviceToolsRuntime.detach(this)
        super.onPause()
    }
    override fun onStop() {
        if (::downloads.isInitialized) downloads.background()
        // The Android permission UI can stop this activity. Keep its request until
        // the result, then recheck both the document generation and account.
        if (!microphoneDialogOpen) { microphone?.deny(); microphone = null; microphoneReady = false }
        if (::web.isInitialized) { DeviceToolsRuntime.detach(this); web.onPause() }
        sessionAttempt++
        WebSessionBootstrap.cancel(sharedAttempt)
        sessionJob?.cancel()
        super.onStop()
    }
    override fun onResume() {
        super.onResume()
        if (::web.isInitialized) {
            if (!authorized()) finish()
            else {
                web.onResume(); nativeVoice.resumed(); web.post { completeMicrophonePermission() }
                DeviceToolsRuntime.attach(this, server)
                web.post { if (needsSession && !isFinishing && authorized()) startSession() }
            }
        }
    }
    override fun onWindowFocusChanged(hasFocus: Boolean) { super.onWindowFocusChanged(hasFocus); if (hasFocus && ::web.isInitialized) completeMicrophonePermission() }
    private fun startSession() {
        if (sessionJob?.isActive == true || !lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.RESUMED)) return
        needsSession = true
        val attempt = ++sessionAttempt
        val shared = WebSessionBootstrap.begin(server.baseUrl).also { sharedAttempt = it }
        fun owns() = !isFinishing && !isDestroyed && authorized() && attempt == sessionAttempt &&
            lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.RESUMED)
        sessionJob = lifecycleScope.launch {
            repairButton.visibility = View.GONE
            status.text = "Opening a secure chat…"
            if (server.hasCfAccess) ApiClient.fetchCfAuthorizationCookie(server)?.let { cookie ->
                val saved = WebSessionBootstrap.installCookie(shared, ::owns) { done ->
                    CookieManager.getInstance().setCookie(server.baseUrl, "$cookie; Path=/; Secure; HttpOnly; SameSite=Strict", done)
                }
                if (!saved) { status.text = "The secure chat cookie could not be saved. Refresh chat to try again."; return@launch }
            }
            when (val result = WebSessionBootstrap.mint(server, shared, ::owns)) {
                is ApiResult.Success -> {
                    if (!result.data.matches(Regex("[A-Za-z0-9_-]{1,4096}"))) {
                        status.text = "The worker returned an invalid chat authorization. Update the worker and retry."
                        return@launch
                    }
                    val saved = WebSessionBootstrap.installCookie(shared, ::owns) { done ->
                        CookieManager.getInstance().setCookie(server.baseUrl, "session=${result.data}; Path=/; Secure; HttpOnly; SameSite=Strict", done)
                    }
                    if (!saved) { status.text = "The secure chat cookie could not be saved. Refresh chat to try again."; return@launch }
                    needsSession = false
                    CookieManager.getInstance().flush(); web.loadUrl(server.baseUrl + "/")
                }
                is ApiResult.Failure -> {
                    status.text = result.message
                    if (result.statusCode == 401 && server.accountId != null) {
                        try {
                            val snapshot = WorkerRepairSnapshot(this@ChatActivity, server)
                            snapshot.check()
                            ManagedWorkerRepair.verify(server)
                            snapshot.check()
                            if (owns() && WebSessionBootstrap.current(shared)) repairButton.visibility = View.VISIBLE
                        } catch (error: CancellationException) { throw error }
                        catch (error: Exception) {
                            if (owns() && WebSessionBootstrap.current(shared)) status.text = result.message + "\n" +
                                ((error as? WorkerRepairFailure)?.message ?: "This worker could not be verified for repair. Refresh your workers and try again.")
                        }
                    }
                }
                is ApiResult.NeedsCfAccess -> status.text = "Cloudflare Access rejected this device. Pair the server again with a valid service token."
            }
        }
    }
    override fun onDestroy() {
        WebSessionBootstrap.cancel(sharedAttempt)
        cancelPagePermissions()
        if (::downloads.isInitialized) downloads.close()
        if (::nativeVoice.isInitialized) nativeVoice.close()
        if (::web.isInitialized) { runCatching { unregisterReceiver(signedOut) }; web.stopLoading(); web.clearHistory(); web.destroy() }
        super.onDestroy()
    }
    companion object { const val EXTRA_SERVER_ID = "server_id" }
}
