package com.hobbycoders.aishuffle

import android.Manifest
import android.content.Intent
import android.content.pm.ActivityInfo
import android.content.pm.PackageManager
import android.view.WindowManager
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.*
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.*
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.rule.GrantPermissionRule
import androidx.test.runner.lifecycle.ActivityLifecycleCallback
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import com.hobbycoders.aishuffle.ui.AccountActivity
import com.hobbycoders.aishuffle.ui.AddServerActivity
import com.hobbycoders.aishuffle.ui.QrCaptureActivity
import com.journeyapps.barcodescanner.BarcodeView
import com.journeyapps.barcodescanner.CameraPreview
import com.journeyapps.barcodescanner.DecoratedBarcodeView
import com.journeyapps.barcodescanner.ScanOptions
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

@RunWith(AndroidJUnit4::class)
class QrScannerSmokeTest {
    @get:Rule val permission: GrantPermissionRule = GrantPermissionRule.grant(Manifest.permission.CAMERA)
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private fun resumedScanner(): QrCaptureActivity {
        val resumed = CountDownLatch(1)
        val result = AtomicReference<QrCaptureActivity>()
        val monitor = ActivityLifecycleMonitorRegistry.getInstance()
        val callback = ActivityLifecycleCallback { activity, stage ->
            if (activity is QrCaptureActivity && stage == Stage.RESUMED) {
                result.set(activity); resumed.countDown()
            }
        }
        instrumentation.runOnMainSync {
            monitor.addLifecycleCallback(callback)
            monitor.getActivitiesInStage(Stage.RESUMED).filterIsInstance<QrCaptureActivity>().singleOrNull()?.let {
                result.set(it); resumed.countDown()
            }
        }
        try {
            assertTrue("The single Scan tap did not resume QrCaptureActivity", resumed.await(8, TimeUnit.SECONDS))
            return requireNotNull(result.get())
        } finally { instrumentation.runOnMainSync { monitor.removeLifecycleCallback(callback) } }
    }
    private fun awaitCamera(camera: BarcodeView, preview: Boolean, action: () -> Unit = {}) {
        val changed = CountDownLatch(1)
        val error = AtomicReference<Exception>()
        var observing = true // ZXing listeners live with the camera view; there is no remove API.
        fun observe() {
            if (observing && (if (preview) camera.isPreviewActive else camera.isCameraClosed && !camera.isPreviewActive)) changed.countDown()
        }
        val listener = object : CameraPreview.StateListener {
            override fun previewSized() = observe()
            override fun previewStarted() = observe()
            override fun previewStopped() = observe()
            override fun cameraClosed() = observe()
            override fun cameraError(failure: Exception) {
                if (observing) { error.set(failure); changed.countDown() }
            }
        }
        instrumentation.runOnMainSync { camera.addStateListener(listener); observe() }
        try {
            action()
            assertTrue(if (preview) "Scanner did not start its real camera preview" else "Scanner camera remained open after Back",
                changed.await(8, TimeUnit.SECONDS))
            error.get()?.let { throw AssertionError("Scanner camera failed", it) }
            instrumentation.runOnMainSync {
                assertTrue(if (preview) camera.isPreviewActive else camera.isCameraClosed && !camera.isPreviewActive)
            }
        } finally { instrumentation.runOnMainSync { observing = false } }
    }
    private fun cancelScanner(returnHint: String) {
        val scanner = resumedScanner()
        awaitNativeWindow(scanner)
        lateinit var camera: BarcodeView
        instrumentation.runOnMainSync {
            assertEquals("Scanner must not lock orientation at runtime", ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED, scanner.requestedOrientation)
            assertEquals("QR scanner must allow user screenshots", 0, scanner.window.attributes.flags and WindowManager.LayoutParams.FLAG_SECURE)
            camera = scanner.findViewById<DecoratedBarcodeView>(R.id.zxing_barcode_scanner).barcodeView
        }
        awaitCamera(camera, preview = true)
        TestScreenshots.capture("qr-scanner")
        awaitCamera(camera, preview = false) {
            onView(withId(R.id.scannerBack)).check(matches(isDisplayed())).check(matches(withText("Back"))).perform(click())
            onView(withHint(returnHint)).check(matches(isDisplayed()))
        }
    }
    @Test fun mergedManifestHasNoOrientationRestrictions() {
        val activities = requireNotNull(context.packageManager.getPackageInfo(context.packageName, PackageManager.GET_ACTIVITIES).activities)
        assertTrue("Merged manifest must contain activities", activities.isNotEmpty())
        assertFalse(activities.any { it.name == "com.journeyapps.barcodescanner.CaptureActivity" })
        activities.forEach { activity ->
            assertEquals(activity.name, ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED, activity.screenOrientation)
        }
    }
    @Test fun scannerRecreationRestartsCameraWithoutLockingOrientation() {
        val intent = ScanOptions().setCaptureActivity(QrCaptureActivity::class.java)
            .setDesiredBarcodeFormats(ScanOptions.QR_CODE).setBeepEnabled(false).createScanIntent(context)
        ActivityScenario.launch<QrCaptureActivity>(intent).use { scenario ->
            fun awaitPreview() {
                val scanner = scenario.nativeActivity()
                awaitNativeWindow(scanner)
                lateinit var camera: BarcodeView
                scenario.onActivity {
                    assertEquals(ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED, it.requestedOrientation)
                    camera = it.findViewById<DecoratedBarcodeView>(R.id.zxing_barcode_scanner).barcodeView
                }
                awaitCamera(camera, preview = true)
            }
            awaitPreview()
            scenario.recreate()
            awaitPreview()
        }
    }
    @Test fun workerScannerBackReturnsToWorkerFormAndClosesCamera() {
        ActivityScenario.launch(AddServerActivity::class.java).use { scenario ->
            awaitNativeWindow(scenario.nativeActivity())
            tapNative(onView(withText("Scan QR code")).perform(scrollTo()))
            cancelScanner("Pairing code")
            onView(withText("Add worker")).check(matches(isDisplayed()))
        }
    }
    @Test fun accountScannerBackReturnsToAccountPairingAndClosesCamera() {
        ActivityScenario.launch<AccountActivity>(Intent(context, AccountActivity::class.java).putExtra("mode", "pair")).use { scenario ->
            awaitNativeWindow(scenario.nativeActivity())
            tapNative(onView(withText("Scan account QR")).perform(scrollTo()))
            cancelScanner("Account pairing code")
            onView(withText("Use another device")).check(matches(isDisplayed()))
        }
    }
}
