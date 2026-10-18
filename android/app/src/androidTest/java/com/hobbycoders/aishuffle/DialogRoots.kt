package com.hobbycoders.aishuffle

import android.view.View
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.ViewInteraction
import androidx.test.espresso.matcher.RootMatchers.isDialog
import org.hamcrest.Matcher

/**
 * A view inside the dialog that is opening or open.
 *
 * Espresso's default root accepts a dialog only once the dialog's window already
 * reports focus. The window manager hands focus to a new dialog after it is
 * drawn: about 150 ms later on the Android 16 emulator. A check in that gap
 * picks the activity window, which has just lost focus, and waits 10 s for it
 * (RootViewWithoutFocusException, NativeSmokeTest on API 36). Naming the dialog
 * root makes Espresso wait for the dialog itself to take focus.
 */
internal fun onDialogView(matcher: Matcher<View>): ViewInteraction = onView(matcher).inRoot(isDialog())
