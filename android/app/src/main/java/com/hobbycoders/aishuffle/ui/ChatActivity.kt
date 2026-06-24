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
import kotlinx.coroutines.launch

/**
 * Hosts the server's web UI in a WebView. Before loading, it mints a web session
 * via the mobile API and injects the `session` cookie into the [CookieManager]
 * for the server origin.
 */
class ChatActivity : AppCompatActivity() {

    private lateinit var binding: ActivityChatBinding
    private lateinit var server: Server

    private var filePathCallback: ValueCallback<Array<Uri>>? = null

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
        setSupportActionBar(binding.toolbar)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        binding.toolbar.setNavigationOnClickListener { finishOrGoBack() }

        val serverId = intent.getStringExtra(EXTRA_SERVER_ID)
        val loaded = serverId?.let { ServerStore.get(this).getById(it) }
        if (loaded == null) {
            Snackbar.make(binding.root, R.string.error_server_missing, Snackbar.LENGTH_LONG).show()
            finish()
            return
        }
        server = loaded
        title = server.name

        configureWebView()
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

    private fun startSession() {
        setLoading(true)
        lifecycleScope.launch {
            when (val result = ApiClient.createWebSession(server)) {
                is ApiResult.Success -> {
                    injectSessionCookie(result.data)
                    binding.webView.loadUrl(server.baseUrl)
                    setLoading(false)
                }
                is ApiResult.Failure -> {
                    setLoading(false)
                    showError(result.message)
                }
            }
        }
    }

    private fun injectSessionCookie(token: String) {
        val cookieManager = CookieManager.getInstance()
        cookieManager.setCookie(server.baseUrl, "session=$token; Path=/")
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
    }
}
