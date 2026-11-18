package com.hobbycoders.aishuffle.ui

import kotlinx.coroutines.*
import okhttp3.Call
import org.json.JSONObject
import java.io.OutputStream
import java.util.Base64
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * The one-at-a-time save lifecycle behind [ChatDownloads]: page requests, the destination,
 * deadlines and teardown. It holds no Android types, so its timers run under a test clock;
 * [ChatDownloads] supplies the activity, WebView, picker and document provider.
 */
internal class ChatTransfers(
    private val baseUrl: String,
    /** Runs every step and timer; ChatDownloads passes the activity's lifecycle scope (main thread). */
    private val scope: CoroutineScope,
    private val io: CoroutineDispatcher,
    private val clock: () -> Long,
    private val documents: Documents,
    /** The authorized phone UI page is showing and the activity is alive. */
    private val pageReady: () -> Boolean,
    private val authorized: () -> Boolean,
    /** Called on the scope's thread when a worker URL starts streaming. */
    private val streamer: () -> Streamer,
    /** Opens the system picker; the result comes back through [chosen]. Throws if none is available. */
    private val pickDestination: (name: String?, mime: String?) -> Unit,
    private val status: (String) -> Unit,
) {
    interface Documents {
        fun open(uri: String): OutputStream
        fun delete(uri: String)
    }
    fun interface Streamer {
        fun save(url: String, output: OutputStream, onChunk: () -> Boolean, onCall: (Call) -> Unit)
    }

    companion object {
        const val SAVED_MESSAGE = "File saved."
        const val CHOOSE_MS = 5 * 60_000L
        const val STALL_MS = 2 * 60_000L
        const val STALL_CHECK_MS = 10_000L
        // chat-downloads.js serializes `type` then `id` first, so the ID is read from a
        // bounded prefix and even a message too large to parse gets its own rejection.
        private val PREFIX = Regex("^\\{\"type\":\"[a-z]{1,16}\",\"id\":\"([a-zA-Z0-9-]{1,80})\"")
        fun messageId(raw: String): String? = PREFIX.find(raw.take(128))?.groupValues?.get(1)
    }

    private class Transfer(val id: String, val page: Int, val size: Long, val url: String?, val reply: ((Boolean) -> Unit)?) {
        val output = AtomicReference<OutputStream?>()
        /** This save's turn on its chosen document; set once, on the main thread, by [chosen]. */
        @Volatile var claim: Claim? = null
        val chunks = if (url == null) DownloadChunks(size) else null
        var job: Job? = null
        var timeout: Job? = null
        @Volatile var call: Call? = null
        /** [clock] time of the last chunk a streamed save read or wrote. */
        @Volatile var progressAt = 0L
        /** Set by a successful save or the first [cleanup]; only that first cleanup releases the claim. */
        val settled = AtomicBoolean(false)
        /** The latest cleanup; a second cleanup runs after it. */
        var teardown: Job? = null
    }

    /**
     * One turn on a chosen document. Every save of a document, and every late picker result
     * for it, takes a claim in the order the picker returned it. A claim may open, write,
     * close or delete the document only after the claim before it has been released, and is
     * released only after its own IO (close and any delete) has returned, so a stopped save
     * stays in line even if it is cancelled while it waits. A claim deletes its partial only
     * if no later claim exists; a later claim truncates and overwrites the document instead.
     */
    private inner class Claim(val uri: String) {
        private val held = Job()
        /** Released when the claim before this one has finished with the document. */
        val prior: Job? = lanes.put(uri, held)
        val latest get() = lanes[uri] === held
        fun release() { lanes.remove(uri, held); held.complete() }
    }
    @Volatile private var active: Transfer? = null
    private var page = 0
    private var chooserOpen = false
    /** The newest claim on each chosen document. Main thread only. */
    private val lanes = HashMap<String, Job>()

    private fun current(transfer: Transfer) = active === transfer && transfer.page == page && pageReady()

    /** A message from the authorized page. Every request with a readable ID gets an answer for that ID only. */
    fun receive(raw: String, post: (String) -> Unit) {
        val id = messageId(raw)
        fun reply(ok: Boolean) { if (id != null) runCatching { post(JSONObject().put("id", id).put("ok", ok).toString()) } }
        try {
            require(id != null && raw.length <= ChatDownloadPolicy.MAX_MESSAGE)
            val message = JSONObject(raw)
            require(message.getString("id") == id)
            when (message.getString("type")) {
                "begin", "url" -> {
                    if (active != null || chooserOpen) { status("Finish the current download first."); reply(false); return }
                    val url = if (message.getString("type") == "url") message.getString("url") else null
                    if (url != null) require(ChatDownloadPolicy.url(baseUrl, url))
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
                        transfer.job = scope.launch { reply(finish(transfer)) }
                    } else {
                        val data = Base64.getDecoder().decode(message.getString("data"))
                        transfer.job = scope.launch {
                            try {
                                withContext(io) {
                                    if (active !== transfer || !authorized()) throw CancellationException()
                                    requireNotNull(transfer.chunks).write(sequence, data, requireNotNull(transfer.output.get()))
                                }
                                if (!current(transfer)) { end("Chat changed. Download the file again."); reply(false) }
                                else reply(true)
                            } catch (error: Exception) { failed(transfer, error); reply(false) }
                        }
                    }
                }
                "abort" -> if (active?.id == id) end("The file could not be saved. Try again.")
                // The page reports size only for a Blob or data URL it would have to buffer.
                "error" -> { status(if (message.optString("reason") == "size") ChatDownloadPolicy.BUFFERED_LIMIT_MESSAGE else "This file could not be saved."); reply(false) }
            }
        } catch (error: Exception) {
            val message = error.message.takeIf(ChatDownloadPolicy::limitMessage) ?: "The file could not be saved. Try again."
            // A bad message ends only the save it belongs to; another save keeps going.
            if (id != null && active?.id == id) end(message) else if (active == null) status(message)
            reply(false)
        }
    }

    /** A worker URL the WebView's download listener already admitted. */
    fun fromUrl(url: String, name: String?, mime: String?) {
        if (active != null || chooserOpen) { status("Finish the current download first."); return }
        start(Transfer(UUID.randomUUID().toString(), page, -1, url, null), name, mime)
    }

    private fun start(transfer: Transfer, name: String?, mime: String?) {
        active = transfer
        transfer.timeout = scope.launch { delay(CHOOSE_MS); if (active === transfer) end("Download expired. Try again.") }
        try {
            chooserOpen = true
            status("Choose where to save the file.")
            pickDestination(name, mime)
        } catch (_: Exception) { chooserOpen = false; end("No file picker is available to save this file.") }
    }

    /** The picker's result: a content URI, or null when the user cancelled. */
    fun chosen(uri: String?) {
        chooserOpen = false
        val transfer = active
        if (uri == null) { end("Download cancelled."); return }
        val claim = Claim(uri)
        if (transfer == null || !current(transfer)) {
            // A late result: its document waits its turn like any save, then is removed unless a later save claimed it.
            scope.launch(NonCancellable) { retire(claim) }
            end("Chat changed. Download the file again.")
            return
        }
        transfer.claim = claim
        transfer.job = scope.launch {
            try {
                claim.prior?.join()
                withContext(io + NonCancellable) { transfer.output.set(documents.open(uri)) }
                if (!current(transfer)) {
                    if (active === transfer) end("Chat changed. Download the file again.") else cleanup(transfer)
                    return@launch
                }
                if (transfer.url != null) saveUrl(transfer) else transfer.reply?.invoke(true)
            } catch (error: Exception) { failed(transfer, error) }
        }
    }

    private suspend fun saveUrl(transfer: Transfer) {
        val streamer = streamer()
        status("Saving file…")
        // A large worker file can outlast the window for choosing a destination. Once
        // streaming starts, the save ends on cancellation (navigation, closing chat,
        // account change), on OkHttp's read timeout, or when no chunk has moved for
        // STALL_MS (a stuck document provider), never on total elapsed time.
        transfer.progressAt = clock()
        transfer.timeout?.cancel()
        transfer.timeout = scope.launch {
            while (active === transfer) {
                delay(STALL_CHECK_MS)
                if (active === transfer && clock() - transfer.progressAt > STALL_MS)
                    end("The download stopped. Check your connection and try again.")
            }
        }
        withContext(io) {
            streamer.save(requireNotNull(transfer.url), requireNotNull(transfer.output.get()),
                { transfer.progressAt = clock(); active === transfer && authorized() }, { transfer.call = it })
        }
        // Finish first: a download-listener save has no reply, and `reply?.invoke(finish(…))` would skip it.
        val saved = finish(transfer)
        transfer.reply?.invoke(saved)
    }

    private suspend fun finish(transfer: Transfer): Boolean {
        return try {
            withContext(io) { transfer.output.getAndSet(null)?.close() }
            if (!current(transfer)) {
                if (active === transfer) end("Chat changed. Download the file again.") else cleanup(transfer)
                return false
            }
            active = null
            transfer.timeout?.cancel()
            transfer.settled.set(true)
            transfer.claim?.release()
            status(SAVED_MESSAGE)
            true
        } catch (error: Exception) { failed(transfer, error); false }
    }

    private fun failed(transfer: Transfer, error: Exception) {
        if (active !== transfer) { cleanup(transfer); return }
        val message = error.message.takeIf { error is IllegalArgumentException && it != null &&
            (ChatDownloadPolicy.limitMessage(it) || it.startsWith("Your worker session") || it.startsWith("The download was incomplete") ||
                it.startsWith("The file redirects") || it.startsWith("The file redirected") || it.startsWith("The worker could not")) }
        end(message ?: "The file could not be saved. Check your connection and try again.")
    }

    fun cancel(message: String? = null) = end(message)
    // Not named cancel: inside scope.launch { } a bare cancel("…") resolves to the
    // coroutine's own CoroutineScope.cancel(String), which ended only the timer or job
    // and left the transfer active.
    private fun end(message: String?) {
        val transfer = active ?: return
        active = null
        transfer.call?.cancel(); transfer.job?.cancel(); transfer.timeout?.cancel()
        transfer.reply?.invoke(false)
        cleanup(transfer)
        if (message != null) status(message)
    }

    /**
     * Teardown. It waits for the transfer's in-flight IO (a write or close a stuck provider
     * holds) and closes the transfer's own stream. Only the first call retires the claim, so
     * a later call (the stalled IO failing after the user retried) can only close that
     * transfer's own stream.
     */
    private fun cleanup(transfer: Transfer) {
        val first = transfer.settled.compareAndSet(false, true)
        val work = transfer.job
        val earlier = transfer.teardown
        transfer.teardown = scope.launch(NonCancellable) {
            work?.join()
            earlier?.join()
            withContext(io) { runCatching { transfer.output.getAndSet(null)?.close() } }
            val claim = transfer.claim
            if (first && claim != null) retire(claim)
        }
    }

    /** Ends a claim that did not save: after the claim before it, delete unless a later claim exists, then release. */
    private suspend fun retire(claim: Claim) {
        try {
            claim.prior?.join()
            if (claim.latest) withContext(io) { runCatching { documents.delete(claim.uri) } }
        } finally { claim.release() }
    }

    fun navigating() { page++; end("Chat changed. Download the file again.") }
    fun background() { if (!chooserOpen) end("Download cancelled when chat closed. Try again.") }
}
