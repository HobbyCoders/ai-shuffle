package com.hobbycoders.aishuffle

import android.view.View
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.closeSoftKeyboard
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.action.ViewActions.scrollTo
import androidx.test.espresso.action.ViewActions.typeText
import androidx.test.espresso.matcher.ViewMatchers.withHint
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.hobbycoders.aishuffle.ui.AddServerActivity
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class SystemInsetsSmokeTest {
    @Test fun actualKeyboardResizesTheNativeSafeViewportAndRestoresIt() {
        ActivityScenario.launch(AddServerActivity::class.java).use { scenario ->
            fun awaitKeyboard(visible: Boolean) = kotlinx.coroutines.runBlocking {
                kotlinx.coroutines.withTimeout(8_000) {
                    while (true) {
                        var ready = false
                        scenario.onActivity { activity ->
                            val root = activity.findViewById<View>(android.R.id.content)
                            val insets = ViewCompat.getRootWindowInsets(root)
                            ready = activity.hasWindowFocus() && insets != null &&
                                insets.isVisible(WindowInsetsCompat.Type.ime()) == visible
                        }
                        if (ready) break
                        kotlinx.coroutines.delay(50)
                    }
                }
            }
            onView(withHint("Pairing code")).perform(scrollTo(), click(), typeText("test-only"))
            awaitKeyboard(true)
            TestScreenshots.capture("native-form-keyboard-visible")
            closeSoftKeyboard()
            awaitKeyboard(false)
            TestScreenshots.capture("native-form-keyboard-hidden")
        }
    }
}
