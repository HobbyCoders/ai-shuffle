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
    private fun settleBy(limit: Long, read: (Long) -> FenceRead) =
        runBlocking { AppControlFence.settle(limit, { clock }, wait = wait) { read(it) } }
    private fun openGate() = DispatchGate(Long.MAX_VALUE) { clock }
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
    @Test fun exactReadWithAnEventInItIsReadAgain() {
        val reads = ArrayDeque(listOf(FenceRead.PENDING, FenceRead.SAME))
        assertTrue(settle { reads.removeFirst() })
        assertEquals(listOf(25L), waits)
    }
    @Test fun eventThenARealChangeIsRefused() {
        val reads = ArrayDeque(listOf(FenceRead.PENDING, FenceRead.CHANGED))
        assertFalse(settle { reads.removeFirst() })
    }
    @Test fun screenNeverQuietIsRefusedWhenItsWindowEnds() {
        var reads = 0
        assertFalse(settle(limit = 20_000) { reads++; FenceRead.PENDING })
        assertEquals(AppControlFence.QUIET_MS, clock)
        assertEquals(100, reads)
    }
    @Test fun quietWindowNeverOutlivesTheLimit() {
        assertFalse(settle(limit = 40) { FenceRead.PENDING })
        assertEquals(40L, clock)
    }
    @Test fun quietReadFinishingPastItsWindowIsRefused() {
        var reads = 0
        assertFalse(settle(limit = 20_000) {
            reads++
            if (reads == 1) FenceRead.PENDING else { clock += AppControlFence.QUIET_MS; FenceRead.SAME }
        })
    }
    @Test fun exactReadInTimeLetsALaterQuietReadPassTheBadReadWindow() {
        val reads = ArrayDeque(listOf(300L to FenceRead.UNCONFIRMED, 300L to FenceRead.PENDING, 300L to FenceRead.SAME))
        assertTrue(settle(limit = 20_000) { val (took, verdict) = reads.removeFirst(); clock += took; verdict })
    }
    @Test fun exactReadWithAnEventPastTheBadReadWindowIsRefused() {
        val reads = ArrayDeque(listOf(1L to FenceRead.UNCONFIRMED, AppControlFence.SETTLE_MS to FenceRead.PENDING, 1L to FenceRead.SAME))
        assertFalse(settle(limit = 20_000) { val (took, verdict) = reads.removeFirst(); clock += took; verdict })
    }

    @Test fun readIsToldTheBadReadWindowItMustFinishBy() {
        val seen = mutableListOf<Long>()
        assertFalse(settleBy(limit = 20_000) { until -> seen += until; clock += 100; if (seen.size == 1) FenceRead.UNCONFIRMED else { clock += 700; FenceRead.SAME } })
        assertEquals(listOf(20_000L, 100 + AppControlFence.SETTLE_MS), seen)
    }
    @Test fun readIsToldTheQuietWindowItMustFinishBy() {
        val seen = mutableListOf<Long>()
        assertFalse(settleBy(limit = 20_000) { until -> seen += until; clock += 100; if (seen.size == 1) FenceRead.PENDING else { clock += AppControlFence.QUIET_MS; FenceRead.SAME } })
        assertEquals(listOf(20_000L, 100 + AppControlFence.QUIET_MS), seen)
    }
    @Test fun readIsToldTheLimitWhenItComesBeforeTheWindow() {
        val seen = mutableListOf<Long>()
        assertFalse(settleBy(limit = 200) { until -> seen += until; clock += 100; if (seen.size == 1) FenceRead.UNCONFIRMED else { clock += 100; FenceRead.SAME } })
        assertEquals(listOf(200L, 200L), seen)
    }
    @Test fun nothingIsSentByAnExactReadThatCrossedItsWindow() {
        var gate: DispatchGate? = null
        assertFalse(settleBy(limit = 20_000) { until ->
            gate = DispatchGate(until) { clock }
            clock += 100
            if (clock == 100L) FenceRead.UNCONFIRMED else { clock += 700; assertFalse(gate!!.send()); FenceRead.SAME }
        })
        assertFalse(gate!!.sent)
    }
    @Test fun gateRefusesAtItsDeadline() {
        val gate = DispatchGate(sendBy = 500) { clock }
        clock = 500
        assertFalse(gate.send()); assertFalse(gate.sent)
        assertTrue(gate.abandon())
    }
    @Test fun gateSendsJustBeforeItsDeadline() {
        val gate = DispatchGate(sendBy = 500) { clock }
        clock = 499
        assertTrue(gate.send()); assertTrue(gate.sent)
    }
    @Test fun gateSentCanNotBeAbandoned() {
        val gate = openGate()
        assertTrue(gate.send()); assertFalse(gate.abandon()); assertTrue(gate.sent)
    }
    @Test fun gateAbandonedCanNotSend() {
        val gate = openGate()
        assertTrue(gate.abandon()); assertFalse(gate.send()); assertFalse(gate.sent)
    }
    @Test fun gateSendsOnce() {
        val gate = openGate()
        assertTrue(gate.send()); assertFalse(gate.send())
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
