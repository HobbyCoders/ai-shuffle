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
import androidx.core.widget.doAfterTextChanged
import androidx.lifecycle.lifecycleScope
import androidx.webkit.ScriptHandler
import androidx.webkit.WebViewAssetLoader
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import com.google.android.material.dialog.MaterialAlertDialogBuilder
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
import com.hobbycoders.aishuffle.presence.ProbeResult
import com.hobbycoders.aishuffle.presence.WorkerPresenceProbe
import com.hobbycoders.aishuffle.tools.DeviceToolsRuntime
import kotlinx.coroutines.Job
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import com.google.android.material.button.MaterialButton
import java.io.ByteArrayInputStream

/**
 * The chat screen: the app's own build of the mobile UI (see MobileUi) in a
 * WebView that may load nothing but that bundle and may talk to nothing but
 * the paired worker's HTTP/WebSocket API. The worker never serves a document
 * here — a worker on any version cannot change what this phone shows.
 *
 * Native capabilities are restricted to the bundle's asset origin; no
 * JavascriptInterface. The paired session is handed to the page as a Bearer
 * token through a document-start script, never as a cookie. Voice calls are
 * the page's own (the same as the website); the shell only grants the
 * microphone to that origin.
 */
class ChatActivity : NativeScreen() {
    private lateinit var server: Server
    private lateinit var web: WebView
    private lateinit var assetLoader: WebViewAssetLoader
    private var bootstrap: ScriptHandler? = null
    @Volatile private var sessionToken: String? = null
    @Volatile private var workerDocumentAllowed = false
    private val workerAssets = WorkerAssetLoader()
    private var assetFailure: String? = null
    private var sessionJob: Job? = null
    private var sessionAttempt = 0
    private lateinit var sessionOwner: ChatSessionOwner
    private var sharedAttempt: WebSessionBootstrap.Attempt? = null
    private var needsSession = true
    private var reloadOnSession = true
    private lateinit var repairButton: MaterialButton
    private lateinit var downloads: ChatDownloads
    private var switchJob: Job? = null
    @Volatile private var document = 0
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
        ServerStore.get(this).markUsed(server.id)
        web = WebView(this)
        // getSettings starts Chromium synchronously. On a cold WebView 151,
        // registering a document-start script before this throws
        // "Must be started before we block!". Configure the view before any bridge
        // is installed, without loading a document or weakening origin checks.
        with(web.settings) {
            javaScriptEnabled = true; domStorageEnabled = true
            allowFileAccess = false; allowContentAccess = false
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            javaScriptCanOpenWindowsAutomatically = false; setSupportMultipleWindows(false)
            mediaPlaybackRequiresUserGesture = true
            // Diagnostics only: the shell is the bundle's, not chosen from the user agent.
            userAgentString = WebUserAgent.decorate(userAgentString, appVersionName())
        }
        chatLayout()
        // The bundled UI: assets/mobile-ui/ at https://appassets.androidplatform.net/assets/mobile-ui/.
        assetLoader = WebViewAssetLoader.Builder()
            .addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(this))
            .build()
        downloads = ChatDownloads(this, web, server, ::authorized, { sessionToken }) { status.text = it }
        downloads.install()
        // The page's own origin never gets a cookie. A Cloudflare Access worker is
        // the one exception: its CF cookie (installed below with SameSite=None) must
        // ride the page's cross-origin API calls, which the WebView counts as third-party.
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, server.hasCfAccess)
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
                // Worker links (including old /api attachment links) save through SAF, never
                // navigate the privileged document or open an unauthenticated browser tab.
                val asset = WorkerAssetLoader.workerUrl(server.baseUrl, request.url.toString())
                if (asset != null) {
                    if (request.isForMainFrame && request.hasGesture()) downloads.fromUrl(asset, null, null, null, -1)
                    return true
                }
                // The bundle is a single page; its own in-app routes are the only navigation it may perform.
                if (MobileUi.isAssetOrigin(request.url.toString())) return false
                if (request.isForMainFrame && request.hasGesture() && request.url.scheme == "https") {
                    confirm("Open outside Shuffle?", "${request.url.host} opens in your browser. No account or server credential will be added.") {
                        runCatching { startActivity(Intent(Intent.ACTION_VIEW, request.url)) }
                    }
                }
                return true
            }
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                val url = request.url
                return when {
                    WorkerAssetLoader.workerUrl(server.baseUrl, url.toString()) != null -> {
                        if (request.isForMainFrame) blocked("Chat opens from the app, not from the worker.")
                        // Explicitly authenticated fetch/SSE keep their existing API/CORS path.
                        // Element loads have no Authorization header and need the native stream.
                        else if (PairingInput.sameOrigin(server.baseUrl, url.toString()) &&
                            (request.method !in listOf("GET", "HEAD") || request.requestHeaders.keys.any { it.equals("Authorization", true) })) null
                        else {
                            val generation = document
                            val token = sessionToken
                            workerAssets.load(server, url.toString(), request.method, request.requestHeaders, token,
                                CookieManager.getInstance().getCookie(server.baseUrl),
                                request.requestHeaders.entries.firstOrNull { it.key.equals("User-Agent", true) }?.value ?: "AI-Shuffle-Android",
                                { workerDocumentAllowed && generation == document && token == sessionToken && authorized() }) { message ->
                                view.post { if (generation == document && workerDocumentAllowed) { assetFailure = message; status.text = message } }
                            }
                        }
                    }
                    MobileUi.isAssetOrigin(url.toString()) -> {
                        if (request.method != "GET") blocked("Blocked")
                        else if (MobileUi.isIndexPath(url.path)) index()
                        else assetLoader.shouldInterceptRequest(url) ?: blocked("This content is blocked.")
                    }
                    else -> blocked("External content blocked")
                }
            }
            override fun onPageStarted(view: WebView, url: String, favicon: android.graphics.Bitmap?) {
                document++
                workerDocumentAllowed = MobileUi.isAssetOrigin(url) && WorkerAssetLoader.workerUrl(server.baseUrl, url) == null
                workerAssets.cancel()
                downloads.navigating()
                cancelPagePermissions()
                if (!MobileUi.isAssetOrigin(url)) { view.stopLoading(); return }
            }
            override fun onPageFinished(view: WebView, url: String) { if (MobileUi.isAssetOrigin(url)) status.text = assetFailure.orEmpty() }
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
    /** The bundle root as index.html: the SPA's router treats the base path as `/`. */
    private fun index(): WebResourceResponse = try {
        WebResourceResponse("text/html", "UTF-8", 200, "OK", mapOf("Cache-Control" to "no-store"), getAssets().open(MobileUi.INDEX_ASSET))
    } catch (_: Exception) {
        blocked("The chat UI is missing from this build of the app. Reinstall Shuffle.")
    }
    private fun blocked(message: String) = WebResourceResponse("text/plain", "UTF-8", 403, "Blocked", mapOf("Cache-Control" to "no-store"), ByteArrayInputStream(message.toByteArray()))
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
        val more = iconButton(R.drawable.ic_more, "More chat options") { }
        more.setOnClickListener {
            PopupMenu(this, more).apply {
                menu.add("Refresh chat").setOnMenuItemClickListener { startSession(reload = true); true }
                menu.add("Workers").setOnMenuItemClickListener { openWorkers(); true }
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
        // NativeScreen applies insets to the content container, even after replacing its child.
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
    private fun openWorkers() {
        startActivity(Intent(this, ServerListActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP))
    }
    private fun authorized() = ::sessionOwner.isInitialized && sessionOwner.current()
    private fun validDocument() = ::web.isInitialized && !isFinishing && !isDestroyed &&
        ChatAccessPolicy.document(MobileUi.ORIGIN, web.url, authorized())
    private fun validMicrophone(request: PermissionRequest) = validDocument() &&
        ChatAccessPolicy.microphone(MobileUi.ORIGIN, web.url, request.origin.toString(), request.resources, authorized())
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
        switchJob?.cancel()
        super.onStop()
    }
    override fun onResume() {
        super.onResume()
        if (::web.isInitialized) {
            if (!authorized()) finish()
            else {
                web.onResume(); web.post { completeMicrophonePermission() }
                DeviceToolsRuntime.attach(this, server)
                web.post { if (needsSession && !isFinishing && authorized()) startSession() }
            }
        }
    }
    override fun onWindowFocusChanged(hasFocus: Boolean) { super.onWindowFocusChanged(hasFocus); if (hasFocus && ::web.isInitialized) completeMicrophonePermission() }
    /** Whether the bundle is on screen (as opposed to nothing / about:blank). */
    private fun pageLoaded() = MobileUi.isAssetOrigin(web.url)
    /**
     * Hand the page its runtime: worker origin + Bearer token, installed before
     * any page script runs (document start) and pushed into an already-loaded
     * page without a reload.
     */
    private fun installRuntime(token: String): Boolean {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
            status.text = "Update Android System WebView, then refresh chat."
            return false
        }
        workerAssets.cancel()
        bootstrap?.remove()
        bootstrap = WebViewCompat.addDocumentStartJavaScript(web,
            MobileUi.bootstrapScript(server.baseUrl, token, appVersionName(), android.os.Build.MODEL, server.hasCfAccess), setOf(MobileUi.ORIGIN))
        sessionToken = token
        if (pageLoaded()) web.evaluateJavascript(MobileUi.tokenUpdateScript(token), null)
        return true
    }
    private fun startSession(reload: Boolean = false) {
        if (sessionJob?.isActive == true || !lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.RESUMED)) return
        needsSession = true
        if (reload) reloadOnSession = true
        val attempt = ++sessionAttempt
        val shared = WebSessionBootstrap.begin(server.baseUrl).also { sharedAttempt = it }
        fun owns() = !isFinishing && !isDestroyed && authorized() && attempt == sessionAttempt &&
            lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.RESUMED)
        sessionJob = lifecycleScope.launch {
            repairButton.visibility = View.GONE
            assetFailure = null
            status.text = "Opening a secure chat…"
            if (server.hasCfAccess) ApiClient.fetchCfAuthorizationCookie(server)?.let { cookie ->
                // Cross-site from the bundle's origin, so SameSite=None; still HttpOnly and worker-only.
                val saved = WebSessionBootstrap.installCookie(shared, ::owns) { done ->
                    CookieManager.getInstance().setCookie(server.baseUrl, "$cookie; Path=/; Secure; HttpOnly; SameSite=None", done)
                }
                if (!saved) { status.text = "The Cloudflare Access cookie could not be saved. Refresh chat to try again."; return@launch }
            }
            when (val result = WebSessionBootstrap.mint(server, shared, ::owns)) {
                is ApiResult.Success -> {
                    if (!result.data.matches(Regex("[A-Za-z0-9_-]{1,4096}"))) {
                        status.text = "The worker returned an invalid chat authorization. Update the worker and retry."
                        return@launch
                    }
                    if (!owns() || !WebSessionBootstrap.current(shared)) return@launch
                    if (!installRuntime(result.data)) return@launch
                    needsSession = false
                    if (reloadOnSession || !pageLoaded()) { reloadOnSession = false; web.loadUrl(MobileUi.INDEX_URL) }
                    else status.text = ""
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
                    } else if (result.statusCode == null && owns() && WebSessionBootstrap.current(shared)) {
                        // No HTTP answer at all: the worker is unreachable. Another one may be online.
                        offerOnlineWorker()
                    }
                }
                is ApiResult.NeedsCfAccess -> status.text = "Cloudflare Access rejected this device. Pair the server again with a valid service token."
            }
        }
    }
    /** The worker did not answer: probe the others and offer to switch to one that is online. */
    private fun offerOnlineWorker() {
        if (switchJob?.isActive == true) return
        val store = ServerStore.get(this)
        val account = AccountStore(this).session()
        val others = store.orderedByRecentUse(store.getAll().filter { it.id != server.id && !it.needsAccountAuthorization &&
            (it.accountId == null || (it.accountId == account?.accountId && it.hubUrl == account?.hub)) })
        if (others.isEmpty()) return
        switchJob = lifecycleScope.launch {
            val online = withTimeoutOrNull(8_000) {
                others.map { candidate -> async { candidate to WorkerPresenceProbe.check(candidate) } }
                    .map { it.await() }.filter { it.second == ProbeResult.ONLINE }.map { it.first }
            }.orEmpty()
            if (online.isEmpty() || isFinishing || isDestroyed || !authorized() ||
                !lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.RESUMED)) return@launch
            val names = online.map { "${it.name} · ${it.host}" }.toTypedArray()
            MaterialAlertDialogBuilder(this@ChatActivity)
                .setTitle("${server.name} is not reachable")
                .setMessage("Switch to another device that is online?")
                .setItems(names) { _, index ->
                    val target = online[index]
                    startActivity(Intent(this@ChatActivity, ChatActivity::class.java).putExtra(EXTRA_SERVER_ID, target.id))
                    finish()
                }
                .setNegativeButton("Stay", null)
                .show()
        }
    }
    override fun onDestroy() {
        workerDocumentAllowed = false
        workerAssets.cancel()
        WebSessionBootstrap.cancel(sharedAttempt)
        cancelPagePermissions()
        switchJob?.cancel()
        if (::downloads.isInitialized) downloads.close()
        bootstrap?.remove(); bootstrap = null
        if (::web.isInitialized) { runCatching { unregisterReceiver(signedOut) }; web.stopLoading(); web.clearHistory(); web.destroy() }
        super.onDestroy()
    }
    companion object { const val EXTRA_SERVER_ID = "server_id" }
}
