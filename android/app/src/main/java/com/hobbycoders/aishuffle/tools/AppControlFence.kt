package com.hobbycoders.aishuffle.tools

import kotlinx.coroutines.delay

/** One full read of the target's accessibility tree: every field, and the same rows without bounds or visibility. */
internal data class ScreenPrint(val exact: String, val identity: String)

/** PENDING: the read matched exactly, but an event arrived during it, so it does not prove the screen as it is now. */
internal enum class FenceRead { SAME, UNCONFIRMED, PENDING, CHANGED }

/**
 * Settles who decides an attempt whose wait can end while it is still running: it either sends its
 * action or is abandoned first, never both, so the waiter knows whether an action may have gone out.
 * It also refuses to send at or past [sendBy], even if the waiter's timer has not fired yet.
 */
internal class DispatchGate(private val sendBy: Long, private val now: () -> Long) {
    private val state = java.util.concurrent.atomic.AtomicInteger(OPEN)
    val sent get() = state.get() == SENT
    fun send() = now() < sendBy && state.compareAndSet(OPEN, SENT)
    fun abandon() = state.compareAndSet(OPEN, ABANDONED)
    private companion object { const val OPEN = 0; const val SENT = 1; const val ABANDONED = 2 }
}

/**
 * Bounds one read of the target's tree. Once [admit] has refused, the read is over and must count
 * as a refusal: a read that stopped early never proved the whole screen.
 */
internal class ReadLimit(private val until: Long, private val now: () -> Long, private val stop: () -> Boolean = { false }) {
    var nodes = 0
        private set
    fun admit(depth: Int): Boolean = !(++nodes > AppControlFence.MAX_NODES || depth > AppControlFence.MAX_DEPTH || now() >= until || stop())
}

/**
 * The cross-app fence only ever proceeds on an exact match with what was approved.
 *
 * Android's accessibility server can return one bad read while its window list is
 * updating, which adding or removing our panel triggers: it hands the node query the
 * pooled WindowInfo matrix by reference, outside its lock, so a node can come back
 * offset by another window's position, and bounds-derived visibility can change with it.
 * So a read differing only in bounds or visibility is re-read for a moment instead of
 * being refused on the spot. A lasting difference is still refused when the moment ends,
 * and any other difference is refused at once.
 */
internal object AppControlFence {
    /** Far longer than the transient (about 25 ms on API 34 and 36), short enough to refuse a real move promptly. */
    const val SETTLE_MS = 500L
    /** One fresh read of a tree, which costs a call into the target app per node. */
    const val READ_MS = 2_000L
    /** Room for several reads to find a gap between events, the same time a baseline gets for two slow reads. */
    const val QUIET_MS = READ_MS + SETTLE_MS
    const val MAX_NODES = 300
    const val MAX_DEPTH = 20
    private const val STEP_MS = 25L

    /**
     * How long a screen gets to read exact again after a read that was not. The proving read has to
     * fit inside, or a screen slower to read than [SETTLE_MS] could never recover from one bad read.
     * So it is the moment itself plus two reads at the pace just measured, since reads of one screen
     * vary by about that much, and never more than [QUIET_MS]. A retry far slower than the screen's
     * own pace still misses it, and it is enforced up to the dispatch gate like any other deadline.
     */
    fun retryWindow(windowMs: Long, readMs: Long): Long = maxOf(windowMs, SETTLE_MS + minOf(READ_MS, 2 * readMs))

    fun compare(expected: ScreenPrint, actual: ScreenPrint): FenceRead = when {
        expected.exact == actual.exact -> FenceRead.SAME
        expected.identity == actual.identity -> FenceRead.UNCONFIRMED
        else -> FenceRead.CHANGED
    }

    /**
     * True only if a read returned SAME and finished in time.
     *
     * [limitMs] is the caller's hard limit, the command deadline or consent expiry: nothing
     * finishing past it counts. The first read that is not exact opens a retry window, see
     * [retryWindow]. A clean first read has tolerated nothing and needs only the hard limit, so a
     * large screen is not refused for being large; each read is bounded by [ReadLimit].
     * [lastReadMs] is how long the read just finished spent reading, if the caller measured it.
     *
     * The first PENDING read opens a separate window of [QUIET_MS] to find a read no event landed
     * in. Its exact match, in time, ends the [windowMs] window, since whatever opened that has reverted.
     *
     * [read] is given the time on [now]'s clock it must finish by, the earliest of all of these, so
     * anything it does that can't be undone is held to the same deadline as its verdict.
     */
    suspend fun settle(limitMs: Long, now: () -> Long, windowMs: Long = SETTLE_MS, wait: suspend (Long) -> Unit = { delay(it) }, lastReadMs: () -> Long? = { null }, read: suspend (until: Long) -> FenceRead): Boolean {
        if (limitMs <= 0) return false
        val end = now() + limitMs
        var retryEnd = Long.MAX_VALUE
        var quietEnd = Long.MAX_VALUE
        while (true) {
            val started = now()
            val verdict = read(minOf(end, retryEnd, quietEnd))
            val finished = now()
            if (verdict == FenceRead.CHANGED || finished >= minOf(end, retryEnd, quietEnd)) return false
            if (verdict == FenceRead.SAME) return true
            if (verdict == FenceRead.PENDING) {
                retryEnd = Long.MAX_VALUE
                if (quietEnd == Long.MAX_VALUE) quietEnd = finished + QUIET_MS
            } else if (retryEnd == Long.MAX_VALUE) retryEnd = finished + retryWindow(windowMs, lastReadMs() ?: (finished - started))
            val until = minOf(end, retryEnd, quietEnd)
            wait(minOf(STEP_MS, until - finished))
            if (now() >= until) return false // A late wake-up does not earn another read.
        }
    }

    /**
     * Two identical samples in a row, or null: the last sample of a failed attempt is unconfirmed.
     * Taking a baseline authorizes nothing, so its window allows for two slow reads of a large screen.
     */
    suspend fun <T : Any> stable(limitMs: Long, now: () -> Long, wait: suspend (Long) -> Unit = { delay(it) }, sample: suspend () -> T?): T? {
        var last: T? = null
        val settled = settle(limitMs, now, QUIET_MS, wait) {
            val next = sample()
            val repeated = next != null && next == last
            last = next
            when { next == null -> FenceRead.CHANGED; repeated -> FenceRead.SAME; else -> FenceRead.UNCONFIRMED }
        }
        return last.takeIf { settled }
    }
}
