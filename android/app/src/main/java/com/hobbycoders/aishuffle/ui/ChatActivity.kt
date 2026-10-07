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
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.widget.PopupMenu
import androidx.core.content.ContextCompat
import androidx.core.widget.doAfterTextChanged
import androidx.lifecycle.lifecycleScope
import androidx.webkit.ScriptHandler
import androidx.webkit.WebMessageCompat
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
 * The chat screen: the paired worker's phone UI (`https://<worker>/mobile/`,
 * see MobileUi) in a WebView that may show no other document and may talk to
 * nothing but that worker. The UI ships with the worker, so UI changes never
 * need a new APK; this activity owns only the native side.
 *
 * Native capabilities are restricted to the top-level phone UI page on the
 * paired worker; no JavascriptInterface. The page may ask to connect to
 * another paired worker (`?connect=`, see WorkerSwitch) and tells the shell
 * when it draws its own "Connected to" bar (MobileUi.SHELL_BRIDGE), which
 * replaces the native worker-name bar. The paired session is handed to the
 * page as a Bearer token through a document-start script, never as a cookie.
 * Voice calls are the page's own (the same as the website); the shell only
 * grants the microphone to that page.
 */
class ChatActivity : NativeScreen() {
    private lateinit var server: Server
    private lateinit var web: WebView
    private var bootstrap: ScriptHandler? = null
    @Volatile private var sessionToken: String? = null
    @Volatile private var workerDocumentAllowed = false
    private val workerAssets = WorkerAssetLoader()
    /** A failed image/media/file load: clears itself (#1946). Created with the status overlay. */
    private lateinit var fileNotice: StatusNotice
    /** "File saved.": informational, so it does not stay over the page. */
    private lateinit var savedNotice: StatusNotice
    /**
     * Why the phone UI page itself failed to load (old worker, HTTP error), shown
     * instead of the page. Cleared only when this activity starts a new load:
     * WebView may report the error before or after onPageStarted.
     */
    private var pageFailure: String? = null
    private var pageLoads = 0
    private var sessionJob: Job? = null
    private var sessionAttempt = 0
    private lateinit var sessionOwner: ChatSessionOwner
    private var sharedAttempt: WebSessionBootstrap.Attempt? = null
    private var needsSession = true
    private var reloadOnSession = true
    private lateinit var repairButton: MaterialButton
    private lateinit var downloads: ChatDownloads
    private var switchJob: Job? = null
    /** The native worker-name bar (back, name, menu); hidden while the page draws its own. */
    private var nativeBar: View? = null
    private var shellBridgeInstalled = false
    /** Whether the loaded page said it draws its own "Connected to" bar. False until told. */
    @androidx.annotation.VisibleForTesting internal var pageRendersWorkerBar = false
        private set
    @Volatile private var document = 0
    private var fileDocument = -1
    private var fileChooserOpen = false
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private val fileChooser = registerForActivityResult(androidx.activity.result.contract.ActivityResultContracts.StartActivityForResult()) { result ->
        fileChooserOpen = false
        val callback = fileCallback
        fileCallback = null
        val uris = ChatFileChooser.parseResult(result.resultCode, result.data)
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
            // Diagnostics only: the worker serves /mobile/ by path, not by user agent.
            userAgentString = WebUserAgent.decorate(userAgentString, appVersionName())
        }
        chatLayout()
        downloads = ChatDownloads(this, web, server, ::authorized, { sessionToken }) {
            if (it == ChatDownloads.SAVED_MESSAGE) savedNotice.show(it) else { status.text = it }
        }
        downloads.install()
        installShellBridge()
        // The page is the worker's own origin and authenticates with the Bearer token;
        // no third-party cookies. A Cloudflare Access worker's CF cookie (installed below)
        // is first-party here and rides the page load and its API calls.
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false)
        web.webChromeClient = object : WebChromeClient() {
            override fun onConsoleMessage(message: ConsoleMessage?) = true
            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                if (!validDocument() || fileChooserOpen) { callback.onReceiveValue(null); return true }
                fileCallback = callback
                fileDocument = document
                return try { fileChooserOpen = true; fileChooser.launch(ChatFileChooser.intent(params)); true }
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
                // "Connected to" sheet: `/mobile/?connect=<origin>` asks to switch workers. Never loaded.
                if (WorkerSwitch.isRequest(server.baseUrl, request.url.toString())) {
                    if (request.isForMainFrame) switchWorker(request.url.toString())
                    return true
                }
                // The phone UI's own routes are the only navigation the page may perform.
                if (MobileUi.isUiUrl(server.baseUrl, request.url.toString())) return false
                // Other worker links (attachments, /api files) save through SAF, never
                // navigate the privileged document or open an unauthenticated browser tab.
                val asset = WorkerAssetLoader.workerUrl(server.baseUrl, request.url.toString())
                if (asset != null) {
                    when (val navigation = ChatDownloadPolicy.navigation(server.baseUrl, asset, request.isForMainFrame, request.hasGesture())) {
                        is ChatDownloadPolicy.Navigation.Session -> if (validDocument()) view.loadUrl(navigation.url)
                        ChatDownloadPolicy.Navigation.Download -> downloads.fromUrl(asset, null, null, null, -1)
                        ChatDownloadPolicy.Navigation.Ignore -> Unit
                    }
                    return true
                }
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
                    // The phone UI page and its hashed assets: public files the worker serves,
                    // loaded normally so they cache like any site's.
                    MobileUi.isUiUrl(server.baseUrl, url.toString()) ->
                        if (request.method == "GET" || request.method == "HEAD") uiFixture?.invoke(request) else blocked("Blocked")
                    WorkerAssetLoader.workerUrl(server.baseUrl, url.toString()) != null -> {
                        if (request.isForMainFrame) blocked("Chat opens the phone UI, not other worker pages.")
                        // The engine's automatic icon probe: not a page load, so never proxy it
                        // (a miss would surface as a file-load notice).
                        else if (url.path == "/favicon.ico") blocked("No icon")
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
                                { workerDocumentAllowed && generation == document && token == sessionToken && authorized() },
                                status = { message ->
                                    view.post {
                                        if (generation == document && workerDocumentAllowed)
                                            fileNotice.show(message, sticky = message == WorkerAssetLoader.AUTH_MESSAGE)
                                    }
                                },
                                loaded = { view.post { if (generation == document) fileNotice.clear() } })
                        }
                    }
                    else -> blocked("External content blocked")
                }
            }
            override fun onPageStarted(view: WebView, url: String, favicon: android.graphics.Bitmap?) {
                document++
                workerDocumentAllowed = MobileUi.isUiUrl(server.baseUrl, url)
                workerAssets.cancel()
                downloads.navigating()
                cancelPagePermissions()
                if (!workerDocumentAllowed) { view.stopLoading(); return }
                if (pageFailure != null) workerDocumentAllowed = false
            }
            override fun onPageFinished(view: WebView, url: String) {
                if (MobileUi.isUiUrl(server.baseUrl, url)) status.text = pageFailure ?: fileNotice.message.orEmpty()
            }
            override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
                // A worker from before the phone UI moved to /mobile/ answers 404 here.
                if (request.isForMainFrame && MobileUi.isUiUrl(server.baseUrl, request.url.toString()))
                    pageFailed(view, MobileUi.loadFailure(response.statusCode))
            }
            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                // ERROR_UNKNOWN covers a load we replaced ourselves (Refresh chat); not a failure.
                if (request.isForMainFrame && error.errorCode != ERROR_UNKNOWN && MobileUi.isUiUrl(server.baseUrl, request.url.toString()))
                    pageFailed(view, "Chat could not reach ${server.name}. Check the connection, then refresh chat.")
            }
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
    /** Replace a failed phone UI page with a blank document and say why; Refresh chat retries. */
    private fun pageFailed(view: WebView, message: String) {
        val load = pageLoads
        pageFailure = message
        workerDocumentAllowed = false
        status.text = message
        // No page bar on a failed page: bring back the native one so Refresh chat stays reachable.
        applyPageWorkerBar(false)
        // Only if Refresh chat has not started a newer load meanwhile.
        view.post { if (!isDestroyed && load == pageLoads) { view.stopLoading(); view.loadUrl("about:blank") } }
    }
    private fun loadUi() {
        pageLoads++
        pageFailure = null
        web.loadUrl(MobileUi.indexUrl(server.baseUrl))
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
        nativeBar = toolbar
        (toolbar.parent as ViewGroup).removeView(toolbar)
        (status.parent as ViewGroup).removeView(status)
        // Status messages float over the top of the chat viewport and take no layout space:
        // nothing in the page moves when one appears or goes (#1946).
        status.setPadding(dp(16), dp(10), dp(16), dp(10))
        status.setTextColor(color(R.color.ink))
        status.background = rounded(R.color.card, 12)
        status.elevation = dp(4).toFloat()
        status.visibility = if (status.text.isNullOrBlank()) View.GONE else View.VISIBLE
        status.doAfterTextChanged { status.visibility = if (it.isNullOrBlank()) View.GONE else View.VISIBLE }
        fileNotice = StatusNotice(status)
        savedNotice = StatusNotice(status)
        // Tap to dismiss. A failed page keeps its reason: the page under it is blank and
        // Refresh chat is what fixes it.
        status.setOnClickListener {
            if (pageFailure == null || status.text.toString() != pageFailure) { fileNotice.clear(); savedNotice.clear(); status.text = "" }
        }
        // Keep system bars and the keyboard outside the chat viewport. The WebView
        // gets all remaining height, with no surrounding ScrollView or status row.
        // NativeScreen applies insets to the content container, even after replacing its child.
        repairButton = MaterialButton(this@ChatActivity).apply {
            text = "Reconnect this worker"; isAllCaps = false; visibility = View.GONE
            setTextColor(color(R.color.on_primary))
            backgroundTintList = android.content.res.ColorStateList.valueOf(color(R.color.primary))
            com.hobbycoders.aishuffle.tools.guardConsentTouch(this)
            setOnClickListener {
                if (authorized()) startActivity(Intent(this@ChatActivity, WorkerRepairActivity::class.java).putExtra(EXTRA_SERVER_ID, server.id))
            }
        }
        setContentView(ChatScreenLayout.build(this, toolbar, status, repairButton, web, color(R.color.surface)))
    }
    private fun openWorkers(message: String? = null) {
        startActivity(Intent(this, ServerListActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .apply { if (message != null) putExtra("sync_message", message) })
    }
    /** Open chat on another paired worker; the same hand-off as the Workers screen and the offline offer. */
    private fun openChat(target: Server) {
        startActivity(Intent(this, ChatActivity::class.java).putExtra(EXTRA_SERVER_ID, target.id))
        finish()
    }
    /** Workers this install may open: local pairings and the signed-in account's (as on the Workers screen). */
    private fun pairedWorkers(): List<Server> {
        val store = ServerStore.get(this)
        val account = AccountStore(this).session()
        return store.orderedByRecentUse(store.getAll().filter {
            it.accountId == null || (it.accountId == account?.accountId && it.hubUrl == account?.hub)
        })
    }
    /** The page asked to connect to another worker (`?connect=`). Only an already paired one is opened. */
    private fun switchWorker(url: String) {
        if (!validDocument()) return
        when (val decision = WorkerSwitch.decide(server.baseUrl, url, pairedWorkers())) {
            is WorkerSwitch.Decision.Open -> openChat(decision.server)
            is WorkerSwitch.Decision.NeedsAuthorization ->
                openWorkers("Tap ${decision.server.name} to authorize it on this phone, then open chat.")
            is WorkerSwitch.Decision.Reject -> Toast.makeText(this, decision.message, Toast.LENGTH_LONG).show()
            is WorkerSwitch.Decision.AlreadyConnected, is WorkerSwitch.Decision.NotASwitch -> Unit
        }
    }
    /** `shuffleShell`: the page reports its own chrome (MobileUi.SHELL_BRIDGE). Same scoping as `shuffleFileSave`. */
    private fun installShellBridge() {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) return
        WebViewCompat.addWebMessageListener(web, MobileUi.SHELL_BRIDGE, setOf(PairingInput.origin(server.baseUrl))) { _, message, origin, mainFrame, _ ->
            if (isFinishing || isDestroyed || message.type != WebMessageCompat.TYPE_STRING ||
                !ChatAccessPolicy.message(server.baseUrl, web.url, origin.toString(), mainFrame, authorized())) return@addWebMessageListener
            when (val parsed = MobileUi.shellMessage(message.data)) {
                is MobileUi.ShellMessage.WorkerBar -> applyPageWorkerBar(parsed.shown)
                is MobileUi.ShellMessage.OpenWorkers -> openWorkers()
                null -> Unit
            }
        }
        shellBridgeInstalled = true
    }
    private fun applyPageWorkerBar(shown: Boolean) {
        pageRendersWorkerBar = shown
        nativeBar?.visibility = if (shown) View.GONE else View.VISIBLE
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
    /** Whether the phone UI is on screen (as opposed to nothing / about:blank / a failed load). */
    private fun pageLoaded() = MobileUi.isUiUrl(server.baseUrl, web.url) && pageFailure == null
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
        val capabilities = MobileUi.CAPABILITIES.filter { it != "file-save" || downloads.available }
        bootstrap = WebViewCompat.addDocumentStartJavaScript(web,
            MobileUi.bootstrapScript(server.baseUrl, token, appVersionName(), android.os.Build.MODEL, server.hasCfAccess, capabilities),
            setOf(PairingInput.origin(server.baseUrl)))
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
            fileNotice.clear()
            status.text = "Opening a secure chat…"
            if (server.hasCfAccess) ApiClient.fetchCfAuthorizationCookie(server)?.let { cookie ->
                // HttpOnly and worker-only; SameSite=None as Cloudflare Access issues it.
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
                    if (reloadOnSession || !pageLoaded()) { reloadOnSession = false; loadUi() }
                    else status.text = ""
                }
                is ApiResult.Failure -> {
                    status.text = result.message
                    // Keep Refresh chat and Workers reachable while chat cannot open.
                    applyPageWorkerBar(false)
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
                .setItems(names) { _, index -> openChat(online[index]) }
                .setNegativeButton("Stay", null)
                .show()
        }
    }
    override fun onDestroy() {
        workerDocumentAllowed = false
        workerAssets.cancel()
        if (::fileNotice.isInitialized) { fileNotice.clear(); savedNotice.clear() }
        WebSessionBootstrap.cancel(sharedAttempt)
        cancelPagePermissions()
        switchJob?.cancel()
        if (::downloads.isInitialized) downloads.close()
        bootstrap?.remove(); bootstrap = null
        if (shellBridgeInstalled && ::web.isInitialized) runCatching { WebViewCompat.removeWebMessageListener(web, MobileUi.SHELL_BRIDGE) }
        if (::web.isInitialized) { runCatching { unregisterReceiver(signedOut) }; web.stopLoading(); web.clearHistory(); web.destroy() }
        super.onDestroy()
    }
    companion object {
        const val EXTRA_SERVER_ID = "server_id"
        /** Instrumentation tests only: answers phone UI requests for a fixture worker that has no network. */
        @androidx.annotation.VisibleForTesting @Volatile internal var uiFixture: ((WebResourceRequest) -> WebResourceResponse?)? = null
    }
}
