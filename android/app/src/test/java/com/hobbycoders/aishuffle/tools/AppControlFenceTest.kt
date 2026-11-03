package com.hobbycoders.aishuffle.tools

import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test

class AppControlFenceTest {
    private val approved = ScreenPrint("Play|[0,0][10,10]|visible;", "Play;")
    private var clock = 0L
    private val waits = mutableListOf<Long>()
    private var lateBy = 0L
    private val wait: suspend (Long) -> Unit = { waits += it; clock += it + lateBy }
    private fun settle(limit: Long = AppControlFence.SETTLE_MS, read: () -> FenceRead) =
        runBlocking { AppControlFence.settle(limit, { clock }, wait = wait) { read() } }
    private fun stable(limit: Long = 20_000, sample: () -> String?) =
        runBlocking { AppControlFence.stable(limit, { clock }, wait) { sample() } }

    @Test fun onlyAnExactMatchIsSame() = assertEquals(FenceRead.SAME, AppControlFence.compare(approved, approved.copy()))
    @Test fun geometryOnlyDifferenceIsUnconfirmedNotAccepted() =
        assertEquals(FenceRead.UNCONFIRMED, AppControlFence.compare(approved, ScreenPrint("Play|[0,128][10,138]|hidden;", "Play;")))
    @Test fun anyOtherDifferenceIsChangedEvenWithIdenticalGeometry() =
        assertEquals(FenceRead.CHANGED, AppControlFence.compare(approved, ScreenPrint("Pay|[0,0][10,10]|visible;", "Pay;")))

    @Test fun exactMatchProceedsWithoutWaiting() {
        assertTrue(settle { FenceRead.SAME })
        assertEquals(emptyList<Long>(), waits)
    }
    @Test fun transientBadReadRecoversOnAnExactReread() {
        val reads = ArrayDeque(listOf(FenceRead.UNCONFIRMED, FenceRead.UNCONFIRMED, FenceRead.SAME))
        assertTrue(settle { reads.removeFirst() })
        assertEquals(listOf(25L, 25L), waits)
    }
    @Test fun lastingGeometryChangeIsRefusedWhenTheWindowEnds() {
        var reads = 0
        assertFalse(settle(limit = 20_000) { reads++; FenceRead.UNCONFIRMED })
        assertEquals(AppControlFence.SETTLE_MS, clock)
        assertEquals(20, reads)
    }
    @Test fun realChangeDuringTheWindowIsRefusedAtOnce() {
        val reads = ArrayDeque(listOf(FenceRead.UNCONFIRMED, FenceRead.CHANGED, FenceRead.SAME))
        assertFalse(settle { reads.removeFirst() })
        assertEquals(1, waits.size)
    }
    @Test fun windowNeverOutlivesTheLimit() {
        assertFalse(settle(limit = 40) { FenceRead.UNCONFIRMED })
        assertEquals(40L, clock)
    }
    @Test fun spentLimitGetsNoRead() {
        var reads = 0
        assertFalse(settle(limit = 0) { reads++; FenceRead.SAME })
        assertEquals(0, reads)
    }
    @Test fun delayedResumeGetsNoFurtherRead() {
        lateBy = AppControlFence.SETTLE_MS
        var reads = 0
        assertFalse(settle(limit = 20_000) { reads++; if (reads == 1) FenceRead.UNCONFIRMED else FenceRead.SAME })
        assertEquals(1, reads)
    }
    @Test fun exactRetryFinishingPastTheWindowIsRefused() {
        var reads = 0
        assertFalse(settle(limit = 20_000) {
            reads++
            if (reads == 1) FenceRead.UNCONFIRMED else { clock += AppControlFence.SETTLE_MS; FenceRead.SAME }
        })
    }
    @Test fun exactFirstReadFinishingPastTheLimitIsRefused() {
        assertFalse(settle(limit = 1_000) { clock += 1_000; FenceRead.SAME })
    }
    @Test fun slowCleanFirstReadInsideTheLimitIsAccepted() {
        assertTrue(settle(limit = 20_000) { clock += AppControlFence.READ_MS - 1; FenceRead.SAME })
    }
    @Test fun windowOpensWhenTheFirstInexactReadFinishes() {
        val reads = ArrayDeque(listOf(1_000L to FenceRead.UNCONFIRMED, 100L to FenceRead.SAME))
        assertTrue(settle(limit = 20_000) { val (took, verdict) = reads.removeFirst(); clock += took; verdict })
    }

    @Test fun baselineIsTwoIdenticalSamples() {
        val samples = ArrayDeque(listOf("bad", "good", "good"))
        assertEquals("good", stable { samples.removeFirst() })
    }
    @Test fun baselineThatNeverRepeatsIsNull() {
        var n = 0
        assertNull(stable { "sample ${n++}" })
        assertEquals(AppControlFence.READ_MS + AppControlFence.SETTLE_MS, clock)
    }
    @Test fun rootFailureAfterOneSampleIsNull() {
        val samples = ArrayDeque(listOf("good", null))
        assertNull(stable { samples.removeFirst() })
    }
    @Test fun baselineRepeatPastTheLimitIsNull() {
        assertNull(stable(limit = 1_000) { clock += 600; "good" })
    }
    @Test fun twoSlowSamplesOfALargeScreenAreAStableBaseline() {
        assertEquals("good", stable { clock += AppControlFence.READ_MS - 1; "good" })
    }

    @Test fun readLimitAdmitsUpToTheNodeCount() {
        val limit = ReadLimit(Long.MAX_VALUE, { clock })
        repeat(AppControlFence.MAX_NODES) { assertTrue(limit.admit(0)) }
        assertFalse(limit.admit(0))
    }
    @Test fun readLimitRefusesPastTheDepth() {
        val limit = ReadLimit(Long.MAX_VALUE, { clock })
        assertTrue(limit.admit(AppControlFence.MAX_DEPTH))
        assertFalse(limit.admit(AppControlFence.MAX_DEPTH + 1))
    }
    @Test fun readLimitRefusesAtItsTime() {
        val limit = ReadLimit(100, { clock })
        clock = 99; assertTrue(limit.admit(0))
        clock = 100; assertFalse(limit.admit(0))
    }
    @Test fun readLimitRefusesOnceStopped() {
        var stopped = false
        val limit = ReadLimit(Long.MAX_VALUE, { clock }, { stopped })
        assertTrue(limit.admit(0))
        stopped = true
        assertFalse(limit.admit(0))
    }
}
