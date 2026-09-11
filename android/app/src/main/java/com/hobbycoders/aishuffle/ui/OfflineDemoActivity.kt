package com.hobbycoders.aishuffle.ui

import android.annotation.SuppressLint
import android.os.Bundle
import android.view.ViewGroup
import android.webkit.*
import android.widget.LinearLayout
import com.google.android.material.button.MaterialButton
import androidx.appcompat.app.AppCompatActivity
import androidx.webkit.WebViewAssetLoader
import java.io.ByteArrayInputStream

/** Public offline sample. No ServerStore, account tokens, native bridge or network client. */
class OfflineDemoActivity : AppCompatActivity() {
    private lateinit var web: WebView
    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(MaterialButton(this).apply {
            text = "Exit offline demo"
            setOnClickListener { finish() }
        })
        web = WebView(this)
        root.addView(web, LinearLayout.LayoutParams(-1, 0, 1f))
        setContentView(root)
        protectSystemInsets()
        with(web.settings) {
            javaScriptEnabled = true
            // Shared renderers read theme preferences; this origin has no account storage.
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            blockNetworkLoads = true
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            javaScriptCanOpenWindowsAutomatically = false
            setSupportMultipleWindows(false)
        }
        val loader = WebViewAssetLoader.Builder().setDomain(DOMAIN)
            .addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(this)).build()
        web.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest) { request.deny() }
            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<android.net.Uri>>, params: FileChooserParams): Boolean {
                callback.onReceiveValue(null); return true
            }
        }
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest) = true
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse {
                val uri = request.url
                if (uri.scheme != "https" || uri.host != DOMAIN || uri.port != -1 || request.method != "GET") return blocked()
                val response = if (uri.path == MobileUi.PATH + "demo") {
                    WebResourceResponse("text/html", "UTF-8", assets.open(MobileUi.INDEX_ASSET))
                } else loader.shouldInterceptRequest(uri) ?: return blocked()
                response.responseHeaders = mapOf("Content-Security-Policy" to
                    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; " +
                    "img-src 'self' data:; font-src 'self'; connect-src 'none'; media-src 'none'; " +
                    "frame-src 'none'; worker-src 'none'; object-src 'none'; form-action 'none'; base-uri 'self'")
                return response
            }
        }
        web.loadUrl(URL)
    }
    override fun onDestroy() {
        if (::web.isInitialized) {
            (web.parent as? ViewGroup)?.removeView(web)
            web.stopLoading(); web.destroy()
        }
        super.onDestroy()
    }
    companion object {
        // Separate from the authenticated chat origin: no account cookies/storage.
        const val DOMAIN = "offline-demo.shuffle.invalid"
        const val URL = "https://$DOMAIN/assets/mobile-ui/demo"
        private fun blocked() = WebResourceResponse("text/plain", "UTF-8", 403, "Offline demo", emptyMap(),
            ByteArrayInputStream("Network access is disabled in the offline demo.".toByteArray()))
    }
}
