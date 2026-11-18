package com.hobbycoders.aishuffle.ui

import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.cancel
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestCoroutineScheduler
import okhttp3.Call
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.OutputStream
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.SynchronousQueue
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.CancellationException

/**
 * Drives the production save lifecycle with a virtual clock for its timers and real
 * threads for document IO, so a provider can stall while minutes pass.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class ChatTransfersTest {
    private val base = "https://worker.example"
    private val doc = "content://docs/document/big.zip"
    private val scheduler = TestCoroutineScheduler()
    private val errors = CopyOnWriteArrayList<Throwable>()
    private val scope = CoroutineScope(SupervisorJob() + StandardTestDispatcher(scheduler) +
        CoroutineExceptionHandler { _, error -> errors += error })
    private val pool = ThreadPoolExecutor(0, 16, 1, TimeUnit.SECONDS, SynchronousQueue())
    private val docs = Docs()
    private val worker = FakeStreamer()
    private val statuses = CopyOnWriteArrayList<String>()
    private val replies = CopyOnWriteArrayList<String>()
    private var picks = 0
    private val transfers = ChatTransfers(base, scope, pool.asCoroutineDispatcher(), { scheduler.currentTime }, docs,
        { true }, { true }, { worker }, { _, _ -> picks++ }, { statuses += it })

    @After fun tearDown() {
        worker.steps.put(Step.Fail)
        docs.gates.forEach { it.countDown() }
        scope.cancel()
        pool.shutdownNow()
        assertTrue("uncaught: $errors", errors.isEmpty())
    }

    private class Stream(private val gate: CountDownLatch?) : ByteArrayOutputStream() {
        @Volatile var closing = false
        @Volatile var closed = false
        override fun close() { closing = true; gate?.await(); closed = true }
    }
    private inner class Docs : ChatTransfers.Documents {
        val events = CopyOnWriteArrayList<String>()
        val streams = CopyOnWriteArrayList<Stream>()
        /** Gates handed to the next opened streams' close(); empty means close returns at once. */
        val gates = CopyOnWriteArrayList<CountDownLatch>()
        val nextGates = LinkedBlockingQueue<CountDownLatch>()
        override fun open(uri: String): OutputStream {
            events += "open $uri"
            return Stream(nextGates.poll()).also { streams += it }
        }
        /** Gates for the next deletes; the delete returns once its gate opens. */
        val deleteGates = LinkedBlockingQueue<CountDownLatch>()
        override fun delete(uri: String) {
            events += "delete $uri"
            deleteGates.poll()?.await()
            events += "deleted $uri"
        }
    }
    private sealed class Step {
        class Chunk(val bytes: ByteArray) : Step()
        object End : Step()
        object Fail : Step()
    }
    /** Stands in for WorkerDownloadClient: each step is one network read the test releases. */
    private class FakeStreamer : ChatTransfers.Streamer {
        val steps = LinkedBlockingQueue<Step>()
        val started = AtomicInteger()
        val written = AtomicInteger()
        @Volatile var call: Call? = null
        override fun save(url: String, output: OutputStream, onChunk: () -> Boolean, onCall: (Call) -> Unit) {
            call = OkHttpClient().newCall(Request.Builder().url(url).build()).also(onCall)
            started.incrementAndGet()
            while (true) {
                when (val step = steps.take()) {
                    is Step.Chunk -> {
                        if (!onChunk()) throw CancellationException("Download cancelled")
                        output.write(step.bytes)
                        written.incrementAndGet()
                    }
                    Step.End -> return
                    Step.Fail -> throw IOException("Canceled")
                }
            }
        }
    }

    private fun url(id: String, target: String = "$base/api/files/by-path?path=big.zip") =
        "{\"type\":\"url\",\"id\":\"$id\",\"url\":\"$target\",\"name\":\"big.zip\"}"
    private fun send(raw: String) = transfers.receive(raw) { replies += it }
    private fun replies(id: String) = replies.map(::JSONObject).filter { it.getString("id") == id }.map { it.getBoolean("ok") }

    /** Runs main-thread work until [condition] holds; IO runs on real threads meanwhile. */
    private fun settle(what: String, condition: () -> Boolean) {
        val end = System.nanoTime() + TimeUnit.SECONDS.toNanos(10)
        while (true) {
            scheduler.runCurrent()
            if (condition()) return
            check(System.nanoTime() < end) { "Timed out waiting for $what" }
            Thread.sleep(2)
        }
    }
    /** Waits until the main queue is drained and only [stuck] IO threads (held by a gate) are busy. */
    private fun quiesce(stuck: Int = 0) {
        var calm = 0
        settle("quiet") {
            calm = if (pool.activeCount == stuck) calm + 1 else 0
            calm >= 25
        }
    }
    private fun advance(ms: Long) { scheduler.advanceTimeBy(ms); scheduler.runCurrent() }
    private fun chunk(vararg bytes: Byte) {
        val before = worker.written.get()
        worker.steps.put(Step.Chunk(bytes))
        settle("chunk written") { worker.written.get() > before }
    }
    private fun startStream(id: String) {
        val starts = worker.started.get()
        send(url(id))
        transfers.chosen(doc)
        settle("stream started") { worker.started.get() > starts }
    }

    @Test fun movingStreamOutlivesTheFiveMinuteChooserWindow() {
        startStream("a1")
        repeat(8) { advance(60_000); chunk(it.toByte()) }
        assertTrue(scheduler.currentTime > ChatTransfers.CHOOSE_MS)
        assertTrue(replies.isEmpty())
        assertEquals("Saving file…", statuses.last())

        worker.steps.put(Step.End)
        settle("reply") { replies.isNotEmpty() }
        assertEquals(listOf(true), replies("a1"))
        assertEquals(ChatTransfers.SAVED_MESSAGE, statuses.last())
        assertArrayEquals(ByteArray(8) { it.toByte() }, docs.streams.single().toByteArray())
        assertTrue(docs.streams.single().closed)
        quiesce()
        assertEquals(listOf("open $doc"), docs.events)
    }

    /**
     * A finished stream is closed, reported saved, survives the stall window and frees the
     * slot and the document, whether it came from the page bridge (with a reply) or the
     * WebView download listener (no reply).
     */
    private fun finishedSaveIsFinal(start: (String) -> Unit) {
        start("a1")
        transfers.chosen(doc)
        settle("stream started") { worker.started.get() == 1 }
        chunk(4); chunk(2)
        worker.steps.put(Step.End)
        settle("saved") { statuses.last() == ChatTransfers.SAVED_MESSAGE }
        quiesce()
        assertTrue(docs.streams.single().closed)
        assertArrayEquals(byteArrayOf(4, 2), docs.streams.single().toByteArray())
        advance(ChatTransfers.STALL_MS + 2 * ChatTransfers.STALL_CHECK_MS)
        quiesce()
        assertEquals(ChatTransfers.SAVED_MESSAGE, statuses.last())
        assertEquals(listOf("open $doc"), docs.events)

        // The same entry point can save again, to the same document, at once.
        start("a2")
        assertEquals(2, picks)
        transfers.chosen(doc)
        settle("second stream started") { worker.started.get() == 2 }
        chunk(8)
        worker.steps.put(Step.End)
        settle("saved again") { docs.streams.size == 2 && docs.streams[1].closed }
        quiesce()
        assertEquals(ChatTransfers.SAVED_MESSAGE, statuses.last())
        assertEquals(listOf("open $doc", "open $doc"), docs.events)
        assertArrayEquals(byteArrayOf(8), docs.streams[1].toByteArray())
    }

    @Test fun bridgeUrlSaveFinishesAndFreesTheSlot() {
        finishedSaveIsFinal { send(url(it)) }
        assertEquals(listOf(true), replies("a1"))
        assertEquals(listOf(true), replies("a2"))
    }

    @Test fun downloadListenerSaveFinishesAndFreesTheSlot() {
        finishedSaveIsFinal { transfers.fromUrl("$base/api/files/by-path?path=big.zip", "big.zip", "application/zip") }
        assertTrue(replies.isEmpty())
    }

    @Test fun stalledStreamRetiresRepliesAndRemovesThePartial() {
        startStream("a1")
        chunk(1)
        advance(ChatTransfers.STALL_MS)
        assertTrue(replies.isEmpty())
        advance(ChatTransfers.STALL_CHECK_MS)
        assertEquals(listOf(false), replies("a1"))
        assertEquals("The download stopped. Check your connection and try again.", statuses.last())
        assertTrue(worker.call!!.isCanceled())
        // Teardown waits for the stuck read to give up before touching the document.
        assertEquals(listOf("open $doc"), docs.events)

        worker.steps.put(Step.Fail)
        settle("partial removed") { "delete $doc" in docs.events }
        quiesce()
        assertEquals(listOf("open $doc", "delete $doc", "deleted $doc"), docs.events)
        assertTrue(docs.streams.single().closed)
        assertTrue(replies("a1").none { it })
    }

    @Test fun stalledCloseCannotTouchARetryOfTheSameDocument() {
        val gate = closeGate()
        stuckClose(gate)

        // The user retries and picks the same document while the old close is still stuck.
        send(url("a2"))
        assertEquals(2, picks)
        transfers.chosen(doc)
        scheduler.runCurrent()
        assertEquals(listOf("open $doc"), docs.events)

        gate.countDown()
        settle("retry streaming") { worker.started.get() == 2 }
        chunk(9)
        worker.steps.put(Step.End)
        settle("retry reply") { replies("a2").isNotEmpty() }
        quiesce()
        assertEquals(listOf(true), replies("a2"))
        assertEquals(ChatTransfers.SAVED_MESSAGE, statuses.last())
        assertEquals(listOf("open $doc", "open $doc"), docs.events)
        assertArrayEquals(byteArrayOf(9), docs.streams[1].toByteArray())
        assertTrue(replies("a1").none { it })
    }

    @Test fun lateFailureAfterCleanupCannotDeleteALaterSave() {
        val gate = CountDownLatch(1).also { docs.gates += it; docs.nextGates += it }
        startStream("a1")
        chunk(1)
        worker.steps.put(Step.End)
        settle("old close stuck") { docs.streams.single().closing }
        advance(ChatTransfers.STALL_MS + ChatTransfers.STALL_CHECK_MS)
        gate.countDown()
        settle("partial removed") { "delete $doc" in docs.events }
        quiesce()

        startStream("a2")
        chunk(9)
        worker.steps.put(Step.End)
        settle("retry reply") { replies("a2").isNotEmpty() }
        quiesce()
        assertEquals(listOf(true), replies("a2"))
        assertEquals(listOf("open $doc", "delete $doc", "deleted $doc", "open $doc"), docs.events)
        assertArrayEquals(byteArrayOf(9), docs.streams[1].toByteArray())
    }

    @Test fun badRequestsAreRejectedByTheirOwnIdOnly() {
        send(url("r1", "$base/" + "a".repeat(8192)))
        assertEquals(listOf(false), replies("r1"))
        assertEquals(0, picks)
        assertEquals("The file could not be saved. Try again.", statuses.last())

        startStream("a1")
        send("{\"type\":\"url\",\"id\":\"r2\",\"url\":\"$base/" + "x".repeat(ChatDownloadPolicy.MAX_MESSAGE) + "\"}")
        assertEquals(listOf(false), replies("r2"))
        send("{not json")
        send("{\"type\":\"chunk\",\"id\":\"r3\",\"sequence\":0,\"data\":\"\"}")
        assertEquals(listOf(false), replies("r3"))
        assertEquals(3, replies.size)
        assertFalse(worker.call!!.isCanceled())
        assertEquals("Saving file…", statuses.last())

        chunk(5)
        worker.steps.put(Step.End)
        settle("reply") { replies("a1").isNotEmpty() }
        assertEquals(listOf(true), replies("a1"))
    }

    @Test fun oversizedMessageForTheActiveSaveEndsIt() {
        startStream("a1")
        send("{\"type\":\"chunk\",\"id\":\"a1\",\"data\":\"" + "x".repeat(ChatDownloadPolicy.MAX_MESSAGE) + "\"}")
        assertEquals(listOf(false, false), replies("a1"))
        assertTrue(worker.call!!.isCanceled())
        worker.steps.put(Step.Fail)
        settle("partial removed") { "delete $doc" in docs.events }
    }

    @Test fun destinationNotChosenInFiveMinutesExpires() {
        send(url("a1"))
        advance(ChatTransfers.CHOOSE_MS - 1)
        assertTrue(replies.isEmpty())
        advance(1)
        assertEquals(listOf(false), replies("a1"))
        assertEquals("Download expired. Try again.", statuses.last())
        transfers.chosen(doc)
        settle("late pick removed") { "delete $doc" in docs.events }
        assertEquals(1, picks)
    }

    private fun closeGate() = CountDownLatch(1).also { docs.gates += it; docs.nextGates += it }
    private fun deleteGate() = CountDownLatch(1).also { docs.gates += it; docs.deleteGates += it }
    /** Save "a1" writes a byte, then its close sticks on [gate] until the stall watchdog retires it. */
    private fun stuckClose(gate: CountDownLatch) {
        startStream("a1")
        chunk(1)
        worker.steps.put(Step.End)
        settle("old close stuck") { docs.streams.single().closing }
        advance(ChatTransfers.STALL_MS + ChatTransfers.STALL_CHECK_MS)
        assertEquals(listOf(false), replies("a1"))
        assertFalse(gate.count == 0L)
    }

    @Test fun retryCancelledWhileWaitingKeepsTheNextRetryInLine() {
        val gate = closeGate()
        stuckClose(gate)
        // B picks the same document and waits behind A's stuck close; then B is cancelled.
        send(url("b1"))
        transfers.chosen(doc)
        scheduler.runCurrent()
        transfers.navigating()
        assertEquals(listOf(false), replies("b1"))
        // C picks the same document while A's close is still stuck.
        send(url("c1"))
        transfers.chosen(doc)
        quiesce(stuck = 1)
        assertEquals(listOf("open $doc"), docs.events)
        assertEquals(1, worker.started.get())

        gate.countDown()
        settle("C streaming") { worker.started.get() == 2 }
        chunk(7)
        worker.steps.put(Step.End)
        settle("C reply") { replies("c1").isNotEmpty() }
        quiesce()
        assertEquals(listOf(true), replies("c1"))
        // Neither A nor B deleted the document C claimed; C truncated and wrote it.
        assertEquals(listOf("open $doc", "open $doc"), docs.events)
        assertTrue(docs.streams[0].closed)
        assertArrayEquals(byteArrayOf(7), docs.streams[1].toByteArray())
    }

    @Test fun retryWaitsUntilAnEarlierDeleteReturns() {
        val gate = deleteGate()
        startStream("a1")
        chunk(1)
        advance(ChatTransfers.STALL_MS + ChatTransfers.STALL_CHECK_MS)
        worker.steps.put(Step.Fail)
        settle("delete started") { "delete $doc" in docs.events }
        // The user retries and picks the same document while the provider is still deleting it.
        send(url("a2"))
        transfers.chosen(doc)
        quiesce(stuck = 1)
        assertEquals(listOf("open $doc", "delete $doc"), docs.events)
        assertTrue(replies("a2").isEmpty())

        gate.countDown()
        settle("retry streaming") { worker.started.get() == 2 }
        assertEquals(listOf("open $doc", "delete $doc", "deleted $doc", "open $doc"), docs.events)
        chunk(9)
        worker.steps.put(Step.End)
        settle("retry reply") { replies("a2").isNotEmpty() }
        quiesce()
        assertEquals(listOf(true), replies("a2"))
        assertEquals(4, docs.events.size)
    }

    @Test fun latePickerResultWaitsItsTurnAndSparesANewerSave() {
        val gate = closeGate()
        stuckClose(gate)
        // X opens the picker; the chat changes before the user picks the same document.
        send(url("x1"))
        transfers.navigating()
        assertEquals(listOf(false), replies("x1"))
        transfers.chosen(doc)
        scheduler.runCurrent()
        // C claims the same document after the late result.
        send(url("c1"))
        transfers.chosen(doc)
        quiesce(stuck = 1)
        assertEquals(listOf("open $doc"), docs.events)

        gate.countDown()
        settle("C streaming") { worker.started.get() == 2 }
        chunk(3)
        worker.steps.put(Step.End)
        settle("C reply") { replies("c1").isNotEmpty() }
        quiesce()
        assertEquals(listOf(true), replies("c1"))
        assertEquals(listOf("open $doc", "open $doc"), docs.events)
        assertArrayEquals(byteArrayOf(3), docs.streams[1].toByteArray())
    }

    @Test fun latePickerResultDeletesOnlyAfterAnEarlierCloseReturns() {
        val gate = closeGate()
        stuckClose(gate)
        send(url("x1"))
        transfers.navigating()
        transfers.chosen(doc)
        quiesce(stuck = 1)
        assertEquals(listOf("open $doc"), docs.events)

        gate.countDown()
        settle("late pick removed") { "deleted $doc" in docs.events }
        quiesce()
        // A saw the later claim and left the delete to it: one delete, after A's close.
        assertEquals(listOf("open $doc", "delete $doc", "deleted $doc"), docs.events)
        assertTrue(docs.streams.single().closed)
    }
}
