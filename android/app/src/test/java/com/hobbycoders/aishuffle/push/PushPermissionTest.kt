package com.hobbycoders.aishuffle.push

import android.Manifest
import android.content.DialogInterface
import android.os.Looper
import androidx.appcompat.app.AppCompatActivity
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.ui.CallNotificationAsk
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class PushPermissionTest {
    @Test fun dismissingTheRationaleReleasesTheSharedDialogFlag() {
        for (cancel in listOf(false, true)) {
            Robolectric.buildActivity(AppCompatActivity::class.java).also {
                it.get().setTheme(R.style.Theme_AIShuffle)
            }.setup().use { controller ->
                var open = false
                val requested = mutableListOf<String>()
                val dialog = PushPermission.prompt(controller.get(), { open = it }, requested::add)
                assertTrue(open)
                if (cancel) dialog.cancel() else dialog.getButton(DialogInterface.BUTTON_NEGATIVE).performClick()
                shadowOf(Looper.getMainLooper()).idle()
                assertFalse(open)
                assertTrue(requested.isEmpty())
            }
        }
    }

    @Test fun allowingKeepsCallPermissionBlockedUntilTheSystemPermissionResult() {
        Robolectric.buildActivity(AppCompatActivity::class.java).also {
            it.get().setTheme(R.style.Theme_AIShuffle)
        }.setup().use { controller ->
            var open = false
            val requested = mutableListOf<String>()
            val ask = CallNotificationAsk(controller.get().getSharedPreferences("call-permission-test", 0), sdk = 34)
            fun next() = ask.next(keptAlive = true, visible = true, dialogOpen = open, shown = false, granted = false)
            val dialog = PushPermission.prompt(controller.get(), { open = it }, requested::add)
            assertEquals(CallNotificationAsk.Next.NOTHING, next())
            dialog.getButton(DialogInterface.BUTTON_POSITIVE).performClick()
            shadowOf(Looper.getMainLooper()).idle()
            assertEquals(listOf(Manifest.permission.POST_NOTIFICATIONS), requested)
            assertFalse(dialog.isShowing)
            assertTrue("the platform permission still owns the flag", open)
            assertEquals(CallNotificationAsk.Next.NOTHING, next())
        }
    }
}
