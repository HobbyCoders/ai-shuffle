package com.hobbycoders.aishuffle.tools

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test

class AppControlFenceTest {
    private val row = ScreenRow(7, 1, "id/play", "android.widget.Button", "Play", null, listOf(0, 0, 10, 10), true, false, false, true, 0)
    private val approved = ScreenPrint(listOf(row))
    private fun printOf(changed: ScreenRow) = ScreenPrint(listOf(changed))
    private var clock = 0L
    private val waits = mutableListOf<Long>()
    private var lateBy = 0L
    private val wait: suspend (Long) -> Unit = { waits += it; clock += it + lateBy }
    private fun settle(limit: Long = AppControlFence.SETTLE_MS, read: () -> FenceRead) =
        runBlocking { AppControlFence.settle(limit, { clock }, wait = wait) { Reading(read()) } }
    private fun settleBy(limit: Long, read: (Long) -> FenceRead) =
        runBlocking { AppControlFence.settle(limit, { clock }, wait = wait) { Reading(read(it)) } }
    private fun openGate() = DispatchGate(Long.MAX_VALUE) { clock }
    private fun stable(limit: Long = 20_000, sample: () -> String?) =
        runBlocking { AppControlFence.stable(limit, { clock }, wait) { sample() } }

    @Test fun onlyAnExactMatchIsSame() = assertEquals(FenceRead.SAME, AppControlFence.compare(approved, ScreenPrint(listOf(row.copy()))))
    @Test fun geometryOnlyDifferenceIsUnconfirmedNotAccepted() =
        assertEquals(FenceRead.UNCONFIRMED, AppControlFence.compare(approved, printOf(row.copy(bounds = listOf(0, 128, 10, 138), visible = false))))
    @Test fun anyOtherDifferenceIsChangedEvenWithIdenticalGeometry() =
        assertEquals(FenceRead.CHANGED, AppControlFence.compare(approved, printOf(row.copy(text = "Pay"))))
    @Test fun rowsWhoseValuesWouldJoinAlikeAreDifferent() { // Same node, place, bounds and traits: only the split between fields moved.
        val before = row.copy(text = "A|B", description = "C")
        val after = row.copy(text = "A", description = "B|C")
        assertEquals(FenceRead.CHANGED, AppControlFence.compare(before, after))
        assertEquals(FenceRead.CHANGED, AppControlFence.compare(printOf(before), printOf(after)))
        assertEquals(listOf("text", "description"), before.changedFields(after))
    }
    @Test fun rowsSplitAcrossAnyOtherPairOfFieldsAreDifferent() {
        val joined = listOf(row.copy(viewId = "a;b", className = "c") to row.copy(viewId = "a", className = "b;c"),
            row.copy(description = "x;", text = null) to row.copy(description = null, text = "x;"),
            row.copy(text = "null", description = null) to row.copy(text = null, description = "null"))
        joined.forEach { (before, after) -> assertEquals(FenceRead.CHANGED, AppControlFence.compare(before, after)) }
    }
    @Test fun rowsSpilledAcrossNodesAreDifferent() { // A row boundary inside a value is not a row.
        val one = printOf(row.copy(text = "A;B"))
        val two = ScreenPrint(listOf(row.copy(text = "A"), row.copy(text = "B")))
        assertEquals(FenceRead.CHANGED, AppControlFence.compare(one, two))
    }
    @Test fun anotherSourceNodeWithIdenticalContentsIsChanged() =
        assertEquals(FenceRead.CHANGED, AppControlFence.compare(approved, printOf(row.copy(source = 8))))

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
    @Test fun retryWindowIsTheMomentPlusTwoReadsAtTheMeasuredPace() {
        assertEquals(AppControlFence.SETTLE_MS, AppControlFence.retryWindow(AppControlFence.SETTLE_MS, 0))
        assertEquals(AppControlFence.SETTLE_MS + 1_200, AppControlFence.retryWindow(AppControlFence.SETTLE_MS, 600))
    }
    @Test fun retryWindowIsCapped() =
        assertEquals(AppControlFence.QUIET_MS, AppControlFence.retryWindow(AppControlFence.SETTLE_MS, 60_000))
    @Test fun slowScreenRecoversFromOneBadRead() { // The 1150 ms bad read, then a retry of the same pace, from the failing run.
        val reads = ArrayDeque(listOf(1_150L to FenceRead.UNCONFIRMED, 1_183L to FenceRead.SAME))
        assertTrue(settle(limit = 20_000) { val (took, verdict) = reads.removeFirst(); clock += took; verdict })
    }
    @Test fun slowScreenLastingDifferenceIsStillRefusedAtItsWindow() {
        var reads = 0
        assertFalse(settle(limit = 20_000) { reads++; clock += 1_000; FenceRead.UNCONFIRMED })
        assertTrue("Refused at $clock", clock <= 1_000 + AppControlFence.QUIET_MS + 1_000)
    }
    @Test fun slowScreenRetryFarSlowerThanItsPaceIsRefused() {
        val reads = ArrayDeque(listOf(300L to FenceRead.UNCONFIRMED, 1_200L to FenceRead.SAME))
        assertFalse(settle(limit = 20_000) { val (took, verdict) = reads.removeFirst(); clock += took; verdict })
    }
    @Test fun slowScreenRetryNeverOutlivesTheLimit() {
        val reads = ArrayDeque(listOf(1_000L to FenceRead.UNCONFIRMED, 1_000L to FenceRead.SAME))
        assertFalse(settle(limit = 1_900) { val (took, verdict) = reads.removeFirst(); clock += took; verdict })
    }
    @Test fun measuredReadTimeSizesTheWindowNotTimeAroundIt() { // Time outside the read earns no extra window.
        val seen = mutableListOf<Long>()
        runBlocking { AppControlFence.settle(20_000, { clock }, wait = wait) { until ->
            seen += until; clock += 1_000; Reading(if (seen.size == 1) FenceRead.UNCONFIRMED else FenceRead.SAME, 100)
        } }
        assertEquals(1_000 + AppControlFence.retryWindow(AppControlFence.SETTLE_MS, 100), seen[1])
    }
    @Test fun fastCheckFinishingBetweenASlowReadAndItsWindowDoesNotShrinkIt() = runBlocking {
        val seen = mutableListOf<Long>()
        val fastCheckDone = CompletableDeferred<Unit>()
        val slow = async {
            AppControlFence.settle(20_000, { clock }, wait = wait) { until ->
                seen += until
                if (seen.size == 1) { clock += 1_000; fastCheckDone.await(); Reading(FenceRead.UNCONFIRMED, 1_000) }
                else { clock += 1_183; Reading(FenceRead.SAME, 1_183) }
            }
        }
        launch {
            assertTrue(AppControlFence.settle(20_000, { clock }, wait = wait) { Reading(FenceRead.SAME, 1) })
            fastCheckDone.complete(Unit)
        }
        assertTrue("The unchanged slow screen was refused", slow.await())
        assertEquals(1_000 + AppControlFence.READ_MS + AppControlFence.SETTLE_MS, seen[1])
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
        assertEquals(listOf(20_000L, 100 + AppControlFence.retryWindow(AppControlFence.SETTLE_MS, 100)), seen)
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

    /** Runs [limit] over [count] children, each answered by [answer], and says what was asked of the target. */
    private class Children(val answer: (Int) -> String? = { "child $it" }) {
        val asked = mutableListOf<Int>()
        fun walk(limit: ReadLimit, count: Int, visit: (String) -> Boolean = { true }) =
            limit.eachChild(count, { asked += it; answer(it) }, visit)
    }
    private fun openLimit() = ReadLimit(Long.MAX_VALUE, { clock })

    @Test fun everyAdvertisedChildPresentIsProved() {
        val children = Children()
        assertTrue(children.walk(openLimit(), 3))
        assertEquals(listOf(0, 1, 2), children.asked)
    }
    @Test fun missingChildFailsTheReadAndAsksForNothingMore() {
        val children = Children { if (it == 1) null else "child $it" }
        assertFalse(children.walk(openLimit(), 5))
        assertEquals(listOf(0, 1), children.asked)
    }
    @Test fun parentAdvertisingThousandsOfMissingChildrenCostsOneQuery() {
        val children = Children { null }
        assertFalse(children.walk(openLimit(), 1_000))
        assertEquals(listOf(0), children.asked)
    }
    @Test fun readStoppedDuringAChildAsksForNoFurtherChild() {
        var stopped = false
        val children = Children()
        assertFalse(children.walk(ReadLimit(Long.MAX_VALUE, { clock }, { stopped }), 5) { stopped = true; true })
        assertEquals(listOf(0), children.asked)
    }
    @Test fun readStoppedBeforeItsChildrenAsksForNone() {
        val children = Children()
        assertFalse(children.walk(ReadLimit(Long.MAX_VALUE, { clock }, { true }), 5))
        assertEquals(emptyList<Int>(), children.asked)
    }
    @Test fun readPastItsTimeAsksForNoFurtherChild() {
        val children = Children()
        assertFalse(children.walk(ReadLimit(100, { clock }), 5) { clock = 100; true })
        assertEquals(listOf(0), children.asked)
    }
    @Test fun readWithItsNodesSpentAsksForNoFurtherChild() {
        val limit = openLimit()
        repeat(AppControlFence.MAX_NODES) { assertTrue(limit.admit(0)) }
        val children = Children()
        assertFalse(children.walk(limit, 1))
        assertEquals(emptyList<Int>(), children.asked)
    }
    @Test fun treeOfExactlyTheNodeLimitIsProved() {
        val limit = openLimit()
        assertTrue(limit.admit(0))
        assertTrue(Children().walk(limit, AppControlFence.MAX_NODES - 1) { limit.admit(1) })
        assertEquals(AppControlFence.MAX_NODES, limit.nodes)
    }
    @Test fun childThatFailsItsOwnReadEndsTheWalk() {
        val children = Children()
        assertFalse(children.walk(openLimit(), 5) { false })
        assertEquals(listOf(0), children.asked)
    }
}
