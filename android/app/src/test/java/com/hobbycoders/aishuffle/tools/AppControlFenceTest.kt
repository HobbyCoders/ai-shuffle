package com.hobbycoders.aishuffle.tools

import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test

class AppControlFenceTest {
    private val approved = ScreenPrint("Play|[0,0][10,10]|visible;", "Play;")
    private var clock = 0L
    private val waits = mutableListOf<Long>()
    private fun settle(budget: Long = AppControlFence.SETTLE_MS, read: () -> FenceRead) =
        runBlocking { AppControlFence.settle(budget, { clock }, { waits += it; clock += it }, read) }

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
        assertFalse(settle { reads++; FenceRead.UNCONFIRMED })
        assertEquals(AppControlFence.SETTLE_MS, clock)
        assertEquals(21, reads)
    }
    @Test fun realChangeDuringTheWindowIsRefusedAtOnce() {
        val reads = ArrayDeque(listOf(FenceRead.UNCONFIRMED, FenceRead.CHANGED, FenceRead.SAME))
        assertFalse(settle { reads.removeFirst() })
        assertEquals(1, waits.size)
    }
    @Test fun windowNeverOutlivesTheBudget() {
        assertFalse(settle(budget = 40) { FenceRead.UNCONFIRMED })
        assertEquals(40L, clock)
    }
    @Test fun spentBudgetGetsOneReadAndNoWait() {
        var reads = 0
        assertFalse(settle(budget = 0) { reads++; FenceRead.UNCONFIRMED })
        assertEquals(1, reads)
        assertEquals(emptyList<Long>(), waits)
    }
}
