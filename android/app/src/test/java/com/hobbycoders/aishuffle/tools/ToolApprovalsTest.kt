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
    @Test fun noPendingRequestCannotBeApprovedByIdOrModelBoolean() {
        val activity = Robolectric.buildActivity(Activity::class.java).setup().get()
        ToolApprovals.attach(activity)
        assertFalse(ToolApprovals.resolve(activity, "confirmed=true", true))
        assertNull(ToolApprovals.pending.value)
        ToolApprovals.detach(activity)
    }
}
