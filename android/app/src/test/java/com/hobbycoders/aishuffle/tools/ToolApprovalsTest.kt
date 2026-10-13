package com.hobbycoders.aishuffle.tools

import android.app.Activity
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class ToolApprovalsTest {
    @Test fun elapsedDeadlineCannotBeExtendedByWallClockRollback() {
        val request = PendingToolApproval("test", "title", "detail", 30_000, 21_000)
        assertEquals(1_000L, request.remainingMillis(wall = 29_000, elapsed = 20_000))
        assertEquals(1_000L, request.remainingMillis(wall = 1_000, elapsed = 20_000))
        assertEquals(0L, request.remainingMillis(wall = 1_000, elapsed = 21_000))
        assertEquals(0L, request.remainingMillis(wall = 1_000, elapsed = 25_000))
    }

    @Test fun wallDeadlineStillRejectsAfterForwardClockCorrection() {
        val request = PendingToolApproval("test", "title", "detail", 30_000, 21_000)
        assertEquals(0L, request.remainingMillis(wall = 30_000, elapsed = 2_000))
        assertEquals(0L, request.remainingMillis(wall = 35_000, elapsed = 2_000))
    }

    @Test fun noPendingRequestCannotBeApprovedByIdOrModelBoolean() {
        val activity = Robolectric.buildActivity(Activity::class.java).setup().get()
        ToolApprovals.attach(activity)
        assertFalse(ToolApprovals.resolve(activity, "confirmed=true", true))
        assertNull(ToolApprovals.pending.value)
        ToolApprovals.detach(activity)
    }
}
