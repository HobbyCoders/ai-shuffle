package com.hobbycoders.aishuffle

import android.graphics.Bitmap
import android.view.View
import android.view.WindowManager
import android.view.inspector.WindowInspector
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Capture synthetic, credential-free fixtures without changing production window flags. */
internal object TestScreenshots {
    fun capture(name: String) {
        check(android.os.Build.VERSION.SDK_INT >= 29) { "Frame-committed capture requires API 29+" }
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val windows = mutableListOf<View>()
        instrumentation.runOnMainSync {
            WindowInspector.getGlobalWindowViews().filter { it.isAttachedToWindow && it.isShown && it.windowVisibility == View.VISIBLE }.forEach { root ->
                val params = root.layoutParams as WindowManager.LayoutParams
                assertEquals("$name must allow user screenshots", 0, params.flags and WindowManager.LayoutParams.FLAG_SECURE)
                windows += root
            }
        }
        // Wait for committed hardware frames before capturing the unchanged app surfaces.
        repeat(2) {
            val rendered = CountDownLatch(windows.size)
            instrumentation.runOnMainSync {
                windows.forEach { root ->
                    check(root.isHardwareAccelerated) { "Screenshot window must render a hardware frame" }
                    root.viewTreeObserver.registerFrameCommitCallback { rendered.countDown() }
                    root.invalidate()
                }
            }
            assertTrue("Windows did not commit a rendered frame for $name", rendered.await(5, TimeUnit.SECONDS))
        }
        // Verify the actual window-produced insets on every captured Shuffle surface
        // (native forms, chat and QR), including rotation and keyboard captures.
        if (android.os.Build.VERSION.SDK_INT >= 35) instrumentation.runOnMainSync {
            val activities = androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry.getInstance()
                .getActivitiesInStage(androidx.test.runner.lifecycle.Stage.RESUMED)
            activities.filter { it is com.hobbycoders.aishuffle.ui.NativeScreen ||
                it is com.hobbycoders.aishuffle.ui.QrCaptureActivity }.forEach { activity ->
                val content = activity.findViewById<android.view.ViewGroup>(android.R.id.content)
                val child = content.getChildAt(0)
                val insets = requireNotNull(androidx.core.view.ViewCompat.getRootWindowInsets(content))
                val safe = insets.getInsets(androidx.core.view.WindowInsetsCompat.Type.systemBars() or
                    androidx.core.view.WindowInsetsCompat.Type.displayCutout() or
                    androidx.core.view.WindowInsetsCompat.Type.ime())
                val position = IntArray(2).also { child.getLocationInWindow(it) }
                val decor = activity.window.decorView
                val light = activity.resources.configuration.uiMode and
                    android.content.res.Configuration.UI_MODE_NIGHT_MASK != android.content.res.Configuration.UI_MODE_NIGHT_YES
                val appearance = requireNotNull(activity.window.insetsController).systemBarsAppearance
                assertEquals("$name status icon contrast", light,
                    appearance and android.view.WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS != 0)
                assertEquals("$name navigation icon contrast", light,
                    appearance and android.view.WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS != 0)
                assertTrue("$name overlaps left system inset", position[0] >= safe.left)
                assertTrue("$name overlaps status bar/cutout", position[1] >= safe.top)
                assertTrue("$name overlaps right system inset", position[0] + child.width <= decor.width - safe.right)
                assertTrue("$name overlaps navigation bar/keyboard", position[1] + child.height <= decor.height - safe.bottom)
                assertTrue("$name lost its safe viewport", child.width > 0 && child.height > 0)
            }
        }
        val automation = instrumentation.uiAutomation
        var image = automation.takeScreenshot()
        repeat(2) {
            if (image == null) {
                automation.waitForIdle(100, 2_000)
                image = automation.takeScreenshot()
            }
        }
        val captured = requireNotNull(image) { "Android compositor returned no image for $name after rendered frames and bounded idle waits" }
        try { save(captured, name) } finally { captured.recycle() }
    }

    internal fun hasAppContent(image: Bitmap): Boolean {
        // Ignore system bars, but include controls near the top and bottom of the app.
        val pixels = mutableSetOf<Int>()
        var nonBlack = 0
        var samples = 0
        for (y in image.height / 20 until image.height * 19 / 20 step 8) {
            for (x in image.width / 20 until image.width * 19 / 20 step 8) {
                val rgb = image.getPixel(x, y) and 0x00ffffff
                pixels += rgb
                if (rgb != 0) nonBlack++
                samples++
            }
        }
        return nonBlack > samples / 100 && pixels.size > 8
    }
    fun save(image: Bitmap, name: String) {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val output = InstrumentationRegistry.getArguments().getString("additionalTestOutputDir")
            ?: File(instrumentation.targetContext.getExternalFilesDir(null), "ui-smoke").absolutePath
        val file = File(output, "$name.png")
        require(file.parentFile!!.mkdirs() || file.parentFile!!.isDirectory)
        file.outputStream().use { assertTrue(image.compress(Bitmap.CompressFormat.PNG, 100, it)) }
        assertTrue(file.length() > 0)
        assertTrue("$name has a black/blank app surface; saved for diagnosis only", hasAppContent(image))
    }
}
