package com.hobbycoders.aishuffle.ui

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.provider.DocumentsContract
import android.webkit.CookieManager
import android.webkit.URLUtil
import android.webkit.WebView
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.lifecycle.lifecycleScope
import androidx.webkit.JavaScriptReplyProxy
import androidx.webkit.ScriptHandler
import androidx.webkit.WebMessageCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import com.hobbycoders.aishuffle.account.PairingInput
import com.hobbycoders.aishuffle.model.Server
import kotlinx.coroutines.*
import okhttp3.Call
import org.json.JSONObject
import java.io.OutputStream
import java.util.Base64
import java.util.concurrent.atomic.AtomicReference

/** One user-selected file at a time. The origin-scoped bridge can only save bounded bytes. */
internal class ChatDownloads(
    private val activity: ComponentActivity,
    private val web: WebView,
    private val server: Server,
    private val authorized: () -> Boolean,
    private val status: (String) -> Unit,
) {
    private class Transfer(val id: String, val page: Int, val size: Long, val url: String?, val reply: ((Boolean) -> Unit)?) {
        val output = AtomicReference<OutputStream?>()
        var uri: Uri? = null
        val chunks = if (url == null) DownloadChunks(size) else null
        var job: Job? = null
        var timeout: Job? = null
        @Volatile var call: Call? = null
    }
    @Volatile private var active: Transfer? = null
    private var page = 0
    private var chooserOpen = false
    private var script: ScriptHandler? = null
    private var bridgeInstalled = false
    private val client = WorkerDownloadClient()
    private val chooser = activity.registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        chooserOpen = false
        val transfer = active
        val uri = result.data?.data?.takeIf { result.resultCode == Activity.RESULT_OK && it.scheme == "content" }
        if (transfer == null || !current(transfer) || uri == null) {
            if (uri != null) removePartial(uri)
            cancel(if (uri == null) "Download cancelled." else "Chat changed. Download the file again.")
        } else {
            transfer.uri = uri
            transfer.job = activity.lifecycleScope.launch {
                try {
                    withContext(Dispatchers.IO + NonCancellable) {
                        transfer.output.set(requireNotNull(activity.contentResolver.openOutputStream(uri, "wt")))
                    }
                    if (!current(transfer)) {
                        if (active === transfer) cancel("Chat changed. Download the file again.") else cleanup(transfer)
                        return@launch
                    }
                    if (transfer.url != null) saveUrl(transfer) else transfer.reply?.invoke(true)
                } catch (error: Exception) { failed(transfer, error) }
            }
        }
    }

    fun install() {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER) ||
            !WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) return
        val origins = setOf(PairingInput.origin(server.baseUrl))
        WebViewCompat.addWebMessageListener(web, "shuffleFileSave", origins) { _, message, origin, mainFrame, reply ->
            if (!ChatAccessPolicy.message(server.baseUrl, web.url, origin.toString(), mainFrame, authorized()) ||
                activity.isFinishing || activity.isDestroyed) return@addWebMessageListener
            if (message.type != WebMessageCompat.TYPE_STRING) return@addWebMessageListener
            val raw = message.data ?: return@addWebMessageListener
            if (raw.length > ChatDownloadPolicy.MAX_MESSAGE) { cancel("The file could not be saved. Try again."); return@addWebMessageListener }
            receive(raw, reply)
        }
        bridgeInstalled = true
        script = WebViewCompat.addDocumentStartJavaScript(web, activity.assets.open("chat-downloads.js").bufferedReader().use { it.readText() }, origins)
    }

    private fun current(transfer: Transfer) = active === transfer && transfer.page == page &&
        !activity.isFinishing && !activity.isDestroyed && ChatAccessPolicy.document(server.baseUrl, web.url, authorized())

    private fun receive(raw: String, proxy: JavaScriptReplyProxy) {
        try {
            val message = JSONObject(raw)
            val id = message.getString("id")
            require(id.matches(Regex("[a-zA-Z0-9-]{1,80}")))
            fun reply(ok: Boolean) { runCatching { proxy.postMessage(JSONObject().put("id", id).put("ok", ok).toString()) } }
            when (message.getString("type")) {
                "begin", "url" -> {
                    if (active != null || chooserOpen) { status("Finish the current download first."); reply(false); return }
                    val url = if (message.getString("type") == "url") message.getString("url") else null
                    if (url != null) require(ChatDownloadPolicy.url(server.baseUrl, url))
                    val size = if (url == null) message.getLong("size").also(ChatDownloadPolicy::length) else -1L
                    start(Transfer(id, page, size, url, ::reply), message.optString("name"), message.optString("mime"))
                }
                "chunk", "finish" -> {
                    val transfer = active
                    if (transfer == null || transfer.id != id || !current(transfer)) { reply(false); return }
                    require(transfer.url == null && transfer.output.get() != null && transfer.job?.isActive != true)
                    val sequence = message.getInt("sequence")
                    if (message.getString("type") == "finish") {
                        requireNotNull(transfer.chunks).finish(sequence)
                        transfer.job = activity.lifecycleScope.launch { reply(finish(transfer)) }
                    } else {
                        val data = Base64.getDecoder().decode(message.getString("data"))
                        transfer.job = activity.lifecycleScope.launch {
                            try {
                                withContext(Dispatchers.IO) {
                                    if (active !== transfer || !authorized()) throw CancellationException()
                                    requireNotNull(transfer.chunks).write(sequence, data, requireNotNull(transfer.output.get()))
                                }
                                if (!current(transfer)) { cancel("Chat changed. Download the file again."); reply(false) }
                                else reply(true)
                            } catch (error: Exception) { failed(transfer, error); reply(false) }
                        }
                    }
                }
                "abort" -> if (active?.id == id) cancel("The file could not be saved. Try again.")
                "error" -> { status(if (message.optString("reason") == "size") ChatDownloadPolicy.LIMIT_MESSAGE else "This file could not be saved."); reply(false) }
            }
        } catch (error: Exception) {
            val message = if (error.message == ChatDownloadPolicy.LIMIT_MESSAGE) ChatDownloadPolicy.LIMIT_MESSAGE else "The file could not be saved. Try again."
            cancel(message)
            status(message)
        }
    }

    fun fromUrl(url: String, @Suppress("UNUSED_PARAMETER") userAgent: String?, disposition: String?, mime: String?, length: Long) {
        if (!ChatAccessPolicy.document(server.baseUrl, web.url, authorized())) return
        if (!ChatDownloadPolicy.url(server.baseUrl, url)) {
            status(if (url.startsWith("blob:") || url.startsWith("data:"))
                "Update Android System WebView, then refresh chat and try downloading again." else "Only files from this worker can be saved.")
            return
        }
        if (length > ChatDownloadPolicy.MAX_BYTES) { status(ChatDownloadPolicy.LIMIT_MESSAGE); return }
        if (active != null || chooserOpen) { status("Finish the current download first."); return }
        start(Transfer(java.util.UUID.randomUUID().toString(), page, -1, url, null), URLUtil.guessFileName(url, disposition, mime), mime)
    }

    private fun start(transfer: Transfer, name: String?, mime: String?) {
        active = transfer
        transfer.timeout = activity.lifecycleScope.launch { delay(5 * 60_000L); if (active === transfer) cancel("Download expired. Try again.") }
        try {
            chooserOpen = true
            status("Choose where to save the file.")
            chooser.launch(Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                .setType(ChatDownloadPolicy.mime(mime)).putExtra(Intent.EXTRA_TITLE, ChatDownloadPolicy.filename(name)))
        } catch (_: Exception) { chooserOpen = false; cancel("No file picker is available to save this file.") }
    }

    private suspend fun saveUrl(transfer: Transfer) {
        val cookies = CookieManager.getInstance().getCookie(server.baseUrl)
        val userAgent = web.settings.userAgentString
        status("Saving file…")
        withContext(Dispatchers.IO) {
            client.save(server, requireNotNull(transfer.url), cookies, userAgent, requireNotNull(transfer.output.get()),
                { active === transfer && authorized() }, { transfer.call = it })
        }
        val saved = finish(transfer)
        transfer.reply?.invoke(saved)
    }

    private suspend fun finish(transfer: Transfer): Boolean {
        return try {
            withContext(Dispatchers.IO) { transfer.output.getAndSet(null)?.close() }
            if (!current(transfer)) {
                if (active === transfer) cancel("Chat changed. Download the file again.") else cleanup(transfer)
                return false
            }
            active = null
            transfer.timeout?.cancel()
            status("File saved.")
            true
        } catch (error: Exception) { failed(transfer, error); false }
    }

    private fun failed(transfer: Transfer, error: Exception) {
        if (active !== transfer) { cleanup(transfer); return }
        val message = error.message.takeIf { error is IllegalArgumentException && it != null &&
            (it == ChatDownloadPolicy.LIMIT_MESSAGE || it.startsWith("Your worker session") || it.startsWith("The download was incomplete") ||
                it.startsWith("The file redirects") || it.startsWith("The file redirected") || it.startsWith("The worker could not")) }
        cancel(message ?: "The file could not be saved. Check your connection and try again.")
    }

    fun cancel(message: String? = null) {
        val transfer = active ?: return
        active = null
        transfer.call?.cancel(); transfer.job?.cancel(); transfer.timeout?.cancel()
        transfer.reply?.invoke(false)
        cleanup(transfer)
        if (message != null) status(message)
    }
    private fun cleanup(transfer: Transfer) {
        activity.lifecycleScope.launch(Dispatchers.IO + NonCancellable) {
            runCatching { transfer.output.getAndSet(null)?.close() }
            transfer.uri?.let { runCatching { DocumentsContract.deleteDocument(activity.contentResolver, it) } }
        }
    }
    private fun removePartial(uri: Uri) { activity.lifecycleScope.launch(Dispatchers.IO + NonCancellable) { runCatching { DocumentsContract.deleteDocument(activity.contentResolver, uri) } } }
    fun navigating() { page++; cancel("Chat changed. Download the file again.") }
    fun background() { if (!chooserOpen) cancel("Download cancelled when chat closed. Try again.") }
    fun close() {
        cancel()
        script?.remove()
        if (bridgeInstalled) WebViewCompat.removeWebMessageListener(web, "shuffleFileSave")
    }
}
