package com.hobbycoders.aishuffle.ui

import android.app.Activity
import android.content.ComponentName
import android.content.pm.ActivityInfo
import android.graphics.Color
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import com.hobbycoders.aishuffle.R
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment

/** The chat view tree must build on create (a double-parented view crashed chat) and status must take no layout space (#1946). */
@RunWith(RobolectricTestRunner::class)
class ChatScreenLayoutTest {
    @Test fun unlockConfigurationChangesDoNotRecreateTheChatDocument() {
        val app = RuntimeEnvironment.getApplication()
        val info = app.packageManager.getActivityInfo(ComponentName(app, ChatActivity::class.java), 0)
        val changes = ActivityInfo.CONFIG_ORIENTATION or ActivityInfo.CONFIG_SCREEN_SIZE or
            ActivityInfo.CONFIG_SMALLEST_SCREEN_SIZE or ActivityInfo.CONFIG_SCREEN_LAYOUT or
            ActivityInfo.CONFIG_KEYBOARD or ActivityInfo.CONFIG_KEYBOARD_HIDDEN or ActivityInfo.CONFIG_UI_MODE
        assertEquals(changes, info.configChanges and changes)
    }

    private class Screen(val root: FrameLayout, val toolbar: View, val status: TextView, val web: View)

    /** As ChatActivity does: start from NativeScreen's layout, detach its toolbar and status, build the chat tree. */
    private fun chat(activity: Activity): Screen {
        activity.setContentView(R.layout.activity_native_screen)
        val toolbar = activity.findViewById<LinearLayout>(R.id.screenToolbar)
        val status = activity.findViewById<TextView>(R.id.screenStatus)
        (toolbar.parent as ViewGroup).removeView(toolbar)
        (status.parent as ViewGroup).removeView(status)
        // Robolectric's WebView stub does not measure like the real one; the chat viewport
        // is any view to this layout.
        val web = View(activity)
        val repair = Button(activity).apply { visibility = View.GONE }
        val root = ChatScreenLayout.build(activity, toolbar, status, repair, web, Color.WHITE)
        activity.setContentView(root)
        return Screen(root, toolbar, status, web)
    }
    private fun layout(root: View) {
        root.measure(View.MeasureSpec.makeMeasureSpec(1080, View.MeasureSpec.EXACTLY), View.MeasureSpec.makeMeasureSpec(2000, View.MeasureSpec.EXACTLY))
        root.layout(0, 0, 1080, 2000)
    }

    @Test fun buildsTheChatTreeWithStatusOnlyAsAnOverlay() {
        Robolectric.buildActivity(Activity::class.java).setup().use { controller ->
            val screen = chat(controller.get())
            assertSame(screen.root, screen.status.parent)
            val shell = screen.web.parent as LinearLayout
            assertSame(screen.root, shell.parent)
            assertSame(shell, screen.toolbar.parent)
            for (index in 0 until shell.childCount) assertNotEquals(screen.status, shell.getChildAt(index))
        }
    }

    @Test fun statusFloatsOverTheTopOfTheChatWithoutMovingIt() {
        Robolectric.buildActivity(Activity::class.java).setup().use { controller ->
            val screen = chat(controller.get())
            screen.status.visibility = View.GONE
            layout(screen.root)
            val top = screen.web.top
            val height = screen.web.height
            assertTrue("web top " + top, top > 0)
            assertTrue("web height " + height, height > 0)

            screen.status.text = WorkerAssetLoader.RESTARTING_MESSAGE
            screen.status.visibility = View.VISIBLE
            layout(screen.root)
            assertEquals(top, screen.web.top)
            assertEquals(height, screen.web.height)
            assertTrue(screen.status.height > 0)
            // Pinned below the toolbar, over the chat viewport.
            assertEquals(top.toFloat(), screen.status.translationY)
        }
    }
}
