package com.hobbycoders.aishuffle.tools

import android.app.Activity
import androidx.appcompat.app.AlertDialog
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.ui.NativeScreen
import kotlinx.coroutines.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowDialog
import org.robolectric.shadows.ShadowLooper
import org.robolectric.util.ReflectionHelpers

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

    class Host : NativeScreen()
    @Test fun unfocusedAllowStaysPendingUntilAFocusedTap() = checkTap(false, false)
    @Test fun unfocusedDenyStillResolves() = checkTap(true, false)
    @Test fun expiredUnfocusedAllowClosesWithoutApproval() = checkTap(false, true)

    private fun checkTap(deny: Boolean, expire: Boolean) = runBlocking {
        Robolectric.buildActivity(Host::class.java).also { it.get().setTheme(R.style.Theme_AIShuffle) }
            .setup().visible().use { controller ->
                controller.windowFocusChanged(true)
                val answer = CoroutineScope(Dispatchers.Main.immediate).async { ToolApprovals.request("Approve", "Local action") }
                try {
                    ShadowLooper.idleMainLooper()
                    assertNotNull(ToolApprovals.pending.value)
                    val dialog = ShadowDialog.getLatestDialog() as AlertDialog
                    val info = ReflectionHelpers.getField<Any>(dialog.window!!.decorView, "mAttachInfo")
                    ReflectionHelpers.setField(info, "mHasWindowFocus", false)
                    if (expire) org.robolectric.shadows.ShadowSystemClock.advanceBy(java.time.Duration.ofSeconds(21))
                    dialog.getButton(if (deny) AlertDialog.BUTTON_NEGATIVE else AlertDialog.BUTTON_POSITIVE).performClick()
                    ShadowLooper.idleMainLooper()
                    if (deny || expire) {
                        assertTrue(answer.isCompleted)
                        assertFalse(answer.await())
                        assertNull(ToolApprovals.pending.value)
                        assertFalse(dialog.isShowing)
                    } else {
                        assertTrue("Early Allow must not dismiss the approval", dialog.isShowing)
                        assertNotNull(ToolApprovals.pending.value)
                        assertFalse("An unfocused tap is neither approval nor denial", answer.isCompleted)
                        ReflectionHelpers.setField(info, "mHasWindowFocus", true)
                        dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick()
                        ShadowLooper.idleMainLooper()
                        assertTrue(answer.isCompleted)
                        assertTrue(answer.await())
                    }
                } finally { ToolApprovals.detach(controller.get()); answer.cancel() }
            }
    }
}
