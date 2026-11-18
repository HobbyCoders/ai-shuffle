package com.hobbycoders.aishuffle.ui

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.SystemClock
import android.provider.DocumentsContract
import android.webkit.CookieManager
import android.webkit.URLUtil
import android.webkit.WebView
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.lifecycle.lifecycleScope
import androidx.webkit.ScriptHandler
import androidx.webkit.WebMessageCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import com.hobbycoders.aishuffle.model.Server
import kotlinx.coroutines.Dispatchers

/** One user-selected file at a time. The bridge is scoped to the paired worker's phone UI page (MobileUi.isUiUrl) and can only save bounded bytes. */
internal class ChatDownloads(
    private val activity: ComponentActivity,
    private val web: WebView,
    private val server: Server,
    private val authorized: () -> Boolean,
    /** The paired-session token the page uses; worker downloads carry it as a Bearer. */
    private val sessionToken: () -> String?,
    private val status: (String) -> Unit,
) {
    companion object {
        const val SAVED_MESSAGE = ChatTransfers.SAVED_MESSAGE
    }
    private var script: ScriptHandler? = null
    private var bridgeInstalled = false
    /** Whether the page has the `shuffleFileSave` bridge (advertised as the "file-save" capability). */
    val available get() = bridgeInstalled
    private val client = WorkerDownloadClient()
    private val transfers = ChatTransfers(
        baseUrl = server.baseUrl,
        scope = activity.lifecycleScope,
        io = Dispatchers.IO,
        clock = SystemClock::elapsedRealtime,
        documents = object : ChatTransfers.Documents {
            override fun open(uri: String) = requireNotNull(activity.contentResolver.openOutputStream(Uri.parse(uri), "wt"))
            override fun delete(uri: String) { DocumentsContract.deleteDocument(activity.contentResolver, Uri.parse(uri)) }
        },
        pageReady = { !activity.isFinishing && !activity.isDestroyed && ChatAccessPolicy.document(server.baseUrl, web.url, authorized()) },
        authorized = authorized,
        streamer = {
            val cookies = CookieManager.getInstance().getCookie(server.baseUrl)
            val userAgent = web.settings.userAgentString
            val bearer = sessionToken()?.let { "Bearer $it" }
            ChatTransfers.Streamer { url, output, onChunk, onCall -> client.save(server, url, cookies, userAgent, output, onChunk, onCall, bearer) }
        },
        pickDestination = { name, mime ->
            chooser.launch(Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                .setType(ChatDownloadPolicy.mime(mime)).putExtra(Intent.EXTRA_TITLE, ChatDownloadPolicy.filename(name)))
        },
        status = status,
    )
    private val chooser = activity.registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val uri = result.data?.data?.takeIf { result.resultCode == Activity.RESULT_OK && it.scheme == "content" }
        transfers.chosen(uri?.toString())
    }

    fun install() {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER) ||
            !WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) return
        // The worker's origin; ChatAccessPolicy then requires the top-level phone UI page.
        val origins = setOf(com.hobbycoders.aishuffle.account.PairingInput.origin(server.baseUrl))
        WebViewCompat.addWebMessageListener(web, "shuffleFileSave", origins) { _, message, origin, mainFrame, reply ->
            if (!ChatAccessPolicy.message(server.baseUrl, web.url, origin.toString(), mainFrame, authorized()) ||
                activity.isFinishing || activity.isDestroyed) return@addWebMessageListener
            if (message.type != WebMessageCompat.TYPE_STRING) return@addWebMessageListener
            val raw = message.data ?: return@addWebMessageListener
            transfers.receive(raw) { reply.postMessage(it) }
        }
        bridgeInstalled = true
        script = WebViewCompat.addDocumentStartJavaScript(web, activity.assets.open("chat-downloads.js").bufferedReader().use { it.readText() }, origins)
    }

    fun fromUrl(url: String, @Suppress("UNUSED_PARAMETER") userAgent: String?, disposition: String?, mime: String?, length: Long) {
        if (!ChatAccessPolicy.document(server.baseUrl, web.url, authorized())) return
        if (!ChatDownloadPolicy.url(server.baseUrl, url)) {
            status(if (url.startsWith("blob:") || url.startsWith("data:"))
                "Update Android System WebView, then refresh chat and try downloading again." else "Only files from this worker can be saved.")
            return
        }
        // Worker URLs are streamed (saveUrl), never buffered: only the streamed cap applies.
        if (length > ChatDownloadPolicy.STREAMED_MAX_BYTES) { status(ChatDownloadPolicy.STREAMED_LIMIT_MESSAGE); return }
        transfers.fromUrl(url, URLUtil.guessFileName(url, disposition, mime), mime)
    }

    fun cancel(message: String? = null) = transfers.cancel(message)
    fun navigating() = transfers.navigating()
    fun background() = transfers.background()
    fun close() {
        transfers.cancel()
        script?.remove()
        if (bridgeInstalled) WebViewCompat.removeWebMessageListener(web, "shuffleFileSave")
    }
}
