package com.hobbycoders.aishuffle.tools

import kotlinx.coroutines.delay

/** One full read of the target's accessibility tree: every field, and the same rows without bounds or visibility. */
internal data class ScreenPrint(val exact: String, val identity: String)

internal enum class FenceRead { SAME, UNCONFIRMED, CHANGED }

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
    private const val STEP_MS = 25L

    fun compare(expected: ScreenPrint, actual: ScreenPrint): FenceRead = when {
        expected.exact == actual.exact -> FenceRead.SAME
        expected.identity == actual.identity -> FenceRead.UNCONFIRMED
        else -> FenceRead.CHANGED
    }

    /**
     * True only if a read returned SAME within [budgetMs]. There is no suspension after that
     * read, so a caller on the main thread acts on the state that read just proved.
     */
    suspend fun settle(budgetMs: Long, now: () -> Long, wait: suspend (Long) -> Unit = { delay(it) }, read: () -> FenceRead): Boolean {
        val end = now() + budgetMs
        while (true) {
            when (read()) {
                FenceRead.SAME -> return true
                FenceRead.CHANGED -> return false
                FenceRead.UNCONFIRMED -> Unit
            }
            val left = end - now()
            if (left <= 0) return false
            wait(minOf(STEP_MS, left))
        }
    }
}
