package com.hobbycoders.aishuffle.ui

import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.webkit.CookieManager
import android.webkit.ConsoleMessage
import android.webkit.DownloadListener
import android.webkit.JavascriptInterface
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.net.toUri
import androidx.lifecycle.lifecycleScope
import com.google.android.material.snackbar.Snackbar
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.databinding.ActivityChatBinding
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.net.ApiResult
import com.hobbycoders.aishuffle.push.NotificationHelper
import kotlinx.coroutines.launch

/**
 * Hosts the server's web UI in a WebView. Before loading, it mints a web session
 * via the mobile API and injects the `session` cookie into the [CookieManager]
 * for the server origin.
 */
class ChatActivity : AppCompatActivity() {

    private lateinit var binding: ActivityChatBinding
    private lateinit var server: Server

    /** Optional session id from a notification deep-link; opens that chat. */
    private var deepLinkSessionId: String? = null

    private var filePathCallback: ValueCallback<Array<Uri>>? = null

    /**
     * Whether the current touch gesture began on the hosted web UI's main app
     * header (the `.topbar`). Pull-to-refresh is reserved exclusively for that
     * header so it never competes with normal scrolling anywhere else in the
     * page — including at the top of the chat or side panels. Injected JS reports
     * this on every touchstart via [RefreshBridge]. Written from the WebView's JS
     * thread, read on the UI thread, hence @Volatile.
     */
    @Volatile
    private var pullFromHeader = false

    /** Bridge the injected JS calls on touchstart to report where the touch began. */
    private inner class RefreshBridge {
        @JavascriptInterface
        fun setFromHeader(fromHeader: Boolean) {
            pullFromHeader = fromHeader
        }
    }

    private val fileChooser =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            val callback = filePathCallback ?: return@registerForActivityResult
            val uris = WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data)
            callback.onReceiveValue(uris)
            filePathCallback = null
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityChatBinding.inflate(layoutInflater)
        setContentView(binding.root)
        // No toolbar: the WebView is fullscreen. Back is handled by the
        // OnBackPressedCallback registered in registerBackHandler().

        val serverId = intent.getStringExtra(EXTRA_SERVER_ID)
        // A notification tap may target a specific session; deep-link into it.
        deepLinkSessionId = intent.getStringExtra(NotificationHelper.EXTRA_SESSION_ID)
            ?.takeIf { it.isNotBlank() }
        val loaded = serverId?.let { ServerStore.get(this).getById(it) }
        if (loaded == null) {
            Snackbar.make(binding.root, R.string.error_server_missing, Snackbar.LENGTH_LONG).show()
            finish()
            return
        }
        server = loaded
        title = server.name

        configureWebView()
        configureSwipeRefresh()
        registerBackHandler()
        startSession()
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView() {
        with(binding.webView.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            loadWithOverviewMode = true
            useWideViewPort = true
            mediaPlaybackRequiresUserGesture = false
        }

        CookieManager.getInstance().apply {
            setAcceptCookie(true)
            setAcceptThirdPartyCookies(binding.webView, true)
        }

        binding.webView.addJavascriptInterface(RefreshBridge(), "AndroidRefresh")

        binding.webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView,
                request: WebResourceRequest,
            ): Boolean {
                val url = request.url.toString()
                return if (isSameOrigin(url)) {
                    false // let the WebView handle it
                } else {
                    openExternally(request.url)
                    true
                }
            }

            override fun onPageFinished(view: WebView, url: String) {
                super.onPageFinished(view, url)
                binding.swipeRefresh.isRefreshing = false
                view.evaluateJavascript(REFRESH_GATE_JS, null)
            }
        }

        binding.webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                webView: WebView,
                callback: ValueCallback<Array<Uri>>,
                params: FileChooserParams,
            ): Boolean {
                filePathCallback?.onReceiveValue(null)
                filePathCallback = callback
                return try {
                    fileChooser.launch(params.createIntent())
                    true
                } catch (e: Exception) {
                    filePathCallback = null
                    false
                }
            }

            override fun onConsoleMessage(message: ConsoleMessage): Boolean {
                android.util.Log.d(TAG, "console: ${message.message()}")
                return true
            }
        }

        binding.webView.setDownloadListener(DownloadListener { url, _, contentDisposition, mimeType, _ ->
            routeDownload(url, contentDisposition, mimeType)
        })
    }

    /**
     * Swipe down from the web UI's main app header to reload — the only way to
     * pick up a freshly deployed frontend inside the chrome-less WebView.
     * [pullFromHeader] (fed by injected JS) ensures the gesture only engages when
     * the touch began on the header, so it never steals scrolls anywhere else in
     * the page. Reporting "the child can scroll up" for every non-header touch
     * tells SwipeRefreshLayout to leave that gesture to the WebView.
     */
    private fun configureSwipeRefresh() {
        with(binding.swipeRefresh) {
            // Match the dark web UI so the spinner is visible against it.
            setProgressBackgroundColorSchemeColor(0xFF1E1F22.toInt())
            setColorSchemeColors(0xFFFFFFFF.toInt())
            setOnChildScrollUpCallback { _, _ -> !pullFromHeader }
            setOnRefreshListener { binding.webView.reload() }
        }
    }

    private fun startSession() {
        setLoading(true)
        lifecycleScope.launch {
            // When the server is behind Cloudflare Access, exchange the service
            // token for a CF_Authorization cookie first, so the WebView (and the
            // chat WebSocket) are authorized; otherwise Access redirects to login.
            if (server.hasCfAccess) {
                ApiClient.fetchCfAuthorizationCookie(server)?.let { injectRawCookie(it) }
            }
            when (val result = ApiClient.createWebSession(server)) {
                is ApiResult.Success -> {
                    injectSessionCookie(result.data)
                    binding.webView.loadUrl(buildStartUrl())
                    setLoading(false)
                }
                is ApiResult.NeedsCfAccess -> {
                    setLoading(false)
                    showError(getString(R.string.cf_access_reauth))
                }
                is ApiResult.Failure -> {
                    setLoading(false)
                    showError(result.message)
                }
            }
        }
    }

    /**
     * The URL to open: the server root, or — when a notification targeted a
     * specific session — the web UI with a `?session=<id>` hint it reads on
     * boot. Auth cookies are set on the origin with `Path=/`, so the deeper URL
     * stays authenticated. Remote (cluster) sessions use this same URL; the
     * hosting install relays them to the owning device.
     */
    private fun buildStartUrl(): String {
        val sid = deepLinkSessionId ?: return server.baseUrl
        return "${server.baseUrl}/?session=${Uri.encode(sid)}"
    }

    private fun injectSessionCookie(token: String) {
        val cookieManager = CookieManager.getInstance()
        cookieManager.setCookie(server.baseUrl, "session=$token; Path=/")
        cookieManager.flush()
    }

    /** Seed a raw `name=value` cookie (e.g. CF_Authorization) for the origin. */
    private fun injectRawCookie(cookiePair: String) {
        val cookieManager = CookieManager.getInstance()
        cookieManager.setCookie(server.baseUrl, "$cookiePair; Path=/")
        cookieManager.flush()
    }

    private fun registerBackHandler() {
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                finishOrGoBack()
            }
        })
    }

    /** Walk back through web history first; only leave the screen when there's none. */
    private fun finishOrGoBack() {
        if (binding.webView.canGoBack()) {
            binding.webView.goBack()
        } else {
            finish()
        }
    }

    /** True when [url] shares scheme + host + port with the server origin. */
    private fun isSameOrigin(url: String): Boolean = runCatching {
        val u = url.toUri()
        val b = server.baseUrl.toUri()
        u.scheme.equals(b.scheme, ignoreCase = true) &&
            u.host.equals(b.host, ignoreCase = true) &&
            u.port == b.port
    }.getOrDefault(false)

    private fun openExternally(uri: Uri) {
        runCatching {
            startActivity(Intent(Intent.ACTION_VIEW, uri))
        }.onFailure {
            Snackbar.make(binding.root, R.string.error_open_external, Snackbar.LENGTH_SHORT).show()
        }
    }

    private fun routeDownload(url: String, contentDisposition: String?, mimeType: String?) {
        runCatching {
            val uri = url.toUri()
            val request = DownloadManager.Request(uri).apply {
                setMimeType(mimeType)
                addRequestHeader("User-Agent", binding.webView.settings.userAgentString)
                // Forward the session cookie so authenticated downloads succeed.
                CookieManager.getInstance().getCookie(url)?.let { addRequestHeader("Cookie", it) }
                setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                val name = android.webkit.URLUtil.guessFileName(url, contentDisposition, mimeType)
                setDestinationInExternalPublicDir(android.os.Environment.DIRECTORY_DOWNLOADS, name)
            }
            (getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager).enqueue(request)
            Snackbar.make(binding.root, R.string.download_started, Snackbar.LENGTH_SHORT).show()
        }.onFailure {
            Snackbar.make(binding.root, R.string.error_download, Snackbar.LENGTH_SHORT).show()
        }
    }

    private fun setLoading(loading: Boolean) {
        binding.progress.visibility = if (loading) android.view.View.VISIBLE else android.view.View.GONE
    }

    private fun showError(message: String) {
        binding.errorText.text = message
        binding.errorText.visibility = android.view.View.VISIBLE
        binding.webView.visibility = android.view.View.GONE
    }

    override fun onDestroy() {
        binding.webView.destroy()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "ChatActivity"
        const val EXTRA_SERVER_ID = "server_id"

        /**
         * Installed once per full page load. On every touchstart it reports
         * whether the touch began inside the web UI's main app header
         * (`.topbar`), so native pull-to-refresh only arms for a pull that starts
         * on the header and never interferes with scrolling elsewhere. Survives
         * SvelteKit's client-side route changes (same document); the guard avoids
         * stacking duplicate listeners across reloads.
         */
        private val REFRESH_GATE_JS = """
            (function () {
              if (window.__aishRefreshGate) return;
              window.__aishRefreshGate = true;
              document.addEventListener('touchstart', function (e) {
                try {
                  var t = e.target;
                  var onHeader = !!(t && t.closest && t.closest('.topbar'));
                  AndroidRefresh.setFromHeader(onHeader);
                } catch (_) {}
              }, { passive: true, capture: true });
            })();
        """.trimIndent()
    }
}
