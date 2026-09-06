package com.hobbycoders.aishuffle

import android.graphics.Bitmap
import android.view.View
import android.view.WindowManager
import android.view.inspector.WindowInspector
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertTrue
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Instrumentation-only capture of synthetic, credential-free fixtures. No production flag bypass. */
internal object TestScreenshots {
    fun capture(name: String) {
        check(android.os.Build.VERSION.SDK_INT >= 29) { "Frame-committed capture requires API 29+" }
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val windows = mutableListOf<Pair<View, Boolean>>()
        try {
            instrumentation.runOnMainSync {
                WindowInspector.getGlobalWindowViews().filter { it.isAttachedToWindow && it.isShown && it.windowVisibility == View.VISIBLE }.forEach { root ->
                    val params = root.layoutParams as WindowManager.LayoutParams
                    windows += root to (params.flags and WindowManager.LayoutParams.FLAG_SECURE != 0)
                    val copy = WindowManager.LayoutParams().apply { copyFrom(params); flags = flags and WindowManager.LayoutParams.FLAG_SECURE.inv() }
                    (root.context.getSystemService(android.content.Context.WINDOW_SERVICE) as WindowManager).updateViewLayout(root, copy)
                }
            }
            // Two committed hardware frames: first relayouts the secure surface, second renders its contents.
            repeat(2) {
                val rendered = CountDownLatch(windows.size)
                instrumentation.runOnMainSync {
                    windows.forEach { (root, _) ->
                        check(root.isHardwareAccelerated) { "Screenshot window must render a hardware frame" }
                        root.viewTreeObserver.registerFrameCommitCallback { rendered.countDown() }
                        root.invalidate()
                    }
                }
                assertTrue("Windows did not commit a rendered frame for $name", rendered.await(5, TimeUnit.SECONDS))
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
        } finally {
            instrumentation.runOnMainSync {
                windows.forEach { (root, secure) ->
                    if (root.isAttachedToWindow) {
                        val copy = WindowManager.LayoutParams().apply {
                            copyFrom(root.layoutParams as WindowManager.LayoutParams)
                            flags = if (secure) flags or WindowManager.LayoutParams.FLAG_SECURE else flags and WindowManager.LayoutParams.FLAG_SECURE.inv()
                        }
                        (root.context.getSystemService(android.content.Context.WINDOW_SERVICE) as WindowManager).updateViewLayout(root, copy)
                    }
                }
            }
        }
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
