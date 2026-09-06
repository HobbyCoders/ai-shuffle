package com.hobbycoders.aishuffle

import android.Manifest
import android.content.Intent
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
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import com.hobbycoders.aishuffle.ui.AccountActivity
import com.hobbycoders.aishuffle.ui.AddServerActivity
import com.hobbycoders.aishuffle.ui.QrCaptureActivity
import com.journeyapps.barcodescanner.BarcodeView
import com.journeyapps.barcodescanner.DecoratedBarcodeView
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class QrScannerSmokeTest {
    @get:Rule val permission: GrantPermissionRule = GrantPermissionRule.grant(Manifest.permission.CAMERA)
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private fun awaitCondition(message: String, condition: () -> Boolean) = kotlinx.coroutines.runBlocking {
        try { kotlinx.coroutines.withTimeout(8_000) { while (!condition()) kotlinx.coroutines.delay(50) } }
        catch (e: kotlinx.coroutines.TimeoutCancellationException) { throw AssertionError(message, e) }
    }
    private fun cancelScanner(returnHint: String) {
        var camera: BarcodeView? = null
        awaitCondition("Scanner did not show a live camera preview") {
            var ready = false
            instrumentation.runOnMainSync {
                val scanner = ActivityLifecycleMonitorRegistry.getInstance().getActivitiesInStage(Stage.RESUMED)
                    .filterIsInstance<QrCaptureActivity>().singleOrNull()
                if (scanner != null) {
                    assertTrue(scanner.window.attributes.flags and WindowManager.LayoutParams.FLAG_SECURE != 0)
                    camera = scanner.findViewById<DecoratedBarcodeView>(R.id.zxing_barcode_scanner).barcodeView
                    ready = scanner.hasWindowFocus() && camera!!.isPreviewActive
                }
            }
            ready
        }
        onView(withId(R.id.scannerBack)).check(matches(isDisplayed())).check(matches(withText("Back"))).perform(click())
        onView(withHint(returnHint)).check(matches(isDisplayed()))
        awaitCondition("Scanner camera remained open after Back") {
            var closed = false
            instrumentation.runOnMainSync { closed = camera!!.isCameraClosed && !camera!!.isPreviewActive }
            closed
        }
    }
    @Test fun workerScannerBackReturnsToWorkerFormAndClosesCamera() {
        ActivityScenario.launch(AddServerActivity::class.java).use {
            onView(withText("Scan QR code")).perform(scrollTo(), click())
            cancelScanner("Pairing code")
            onView(withText("Add worker")).check(matches(isDisplayed()))
        }
    }
    @Test fun accountScannerBackReturnsToAccountPairingAndClosesCamera() {
        ActivityScenario.launch<AccountActivity>(Intent(context, AccountActivity::class.java).putExtra("mode", "pair")).use {
            onView(withText("Scan account QR")).perform(scrollTo(), click())
            cancelScanner("Account pairing code")
            onView(withText("Use another device")).check(matches(isDisplayed()))
        }
    }
}
