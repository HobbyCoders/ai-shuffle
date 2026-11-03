package com.hobbycoders.aishuffle.tools

import kotlinx.coroutines.delay

/** One full read of the target's accessibility tree: every field, and the same rows without bounds or visibility. */
internal data class ScreenPrint(val exact: String, val identity: String)

internal enum class FenceRead { SAME, UNCONFIRMED, CHANGED }

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
    /** A cached read on the main thread for an accessibility event. Past this it is redone off the main thread. */
    const val EVENT_READ_MS = 50L
    const val MAX_NODES = 300
    const val MAX_DEPTH = 20
    private const val STEP_MS = 25L

    fun compare(expected: ScreenPrint, actual: ScreenPrint): FenceRead = when {
        expected.exact == actual.exact -> FenceRead.SAME
        expected.identity == actual.identity -> FenceRead.UNCONFIRMED
        else -> FenceRead.CHANGED
    }

    /**
     * True only if a read returned SAME and finished in time.
     *
     * [limitMs] is the caller's hard limit, the command deadline or consent expiry: nothing
     * finishing past it counts. The first read that is not exact opens a retry window of
     * [windowMs]. A clean first read has tolerated nothing and needs only the hard limit, so a
     * large screen is not refused for being large; each read is bounded by [ReadLimit].
     */
    suspend fun settle(limitMs: Long, now: () -> Long, windowMs: Long = SETTLE_MS, wait: suspend (Long) -> Unit = { delay(it) }, read: suspend () -> FenceRead): Boolean {
        if (limitMs <= 0) return false
        val end = now() + limitMs
        var retryEnd = Long.MAX_VALUE
        while (true) {
            val verdict = read()
            val finished = now()
            if (verdict == FenceRead.CHANGED || finished >= end || finished >= retryEnd) return false
            if (verdict == FenceRead.SAME) return true
            if (retryEnd == Long.MAX_VALUE) retryEnd = finished + windowMs
            val until = minOf(end, retryEnd)
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
        val settled = settle(limitMs, now, READ_MS + SETTLE_MS, wait) {
            val next = sample()
            val repeated = next != null && next == last
            last = next
            when { next == null -> FenceRead.CHANGED; repeated -> FenceRead.SAME; else -> FenceRead.UNCONFIRMED }
        }
        return last.takeIf { settled }
    }
}
