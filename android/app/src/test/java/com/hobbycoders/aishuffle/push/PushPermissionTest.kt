package com.hobbycoders.aishuffle.push

import android.Manifest
import androidx.appcompat.app.AppCompatActivity
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.ui.CallNotificationAsk
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowAlertDialog

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class PushPermissionTest {
    @Test fun requestsTheSystemPermissionDirectlyWithoutAnAppDialog() {
        Robolectric.buildActivity(AppCompatActivity::class.java).also {
            it.get().setTheme(R.style.Theme_AIShuffle)
        }.setup().use {
            var open = false
            val requested = mutableListOf<String>()
            PushPermission.prompt({ open = it }) { permission ->
                assertTrue("claim the shared flag before the system request", open)
                requested.add(permission)
            }
            assertEquals(listOf(Manifest.permission.POST_NOTIFICATIONS), requested)
            assertTrue(open)
            assertNull("there is no custom rationale", ShadowAlertDialog.getLatestAlertDialog())
        }
    }

    @Test fun keepsCallPermissionBlockedUntilTheSystemPermissionResult() {
        Robolectric.buildActivity(AppCompatActivity::class.java).also {
            it.get().setTheme(R.style.Theme_AIShuffle)
        }.setup().use { controller ->
            var open = false
            val requested = mutableListOf<String>()
            val ask = CallNotificationAsk(controller.get().getSharedPreferences("call-permission-test", 0), sdk = 34)
            fun next() = ask.next(keptAlive = true, visible = true, dialogOpen = open, shown = false, granted = false)
            PushPermission.prompt({ open = it }, requested::add)
            assertEquals(listOf(Manifest.permission.POST_NOTIFICATIONS), requested)
            assertTrue("the platform permission owns the flag until its result callback", open)
            assertEquals(CallNotificationAsk.Next.NOTHING, next())
        }
    }
}
