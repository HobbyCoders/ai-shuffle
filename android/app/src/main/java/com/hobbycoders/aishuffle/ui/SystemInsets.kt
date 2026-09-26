package com.hobbycoders.aishuffle.ui

import android.app.Activity
import android.content.res.Configuration
import android.os.Build
import android.view.View
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.doOnAttach

/** Android 15+ forces edge-to-edge at target 36. Keep native controls and the
 * WebView outside bars, cutouts and the IME instead of relying on decor fitting.
 * Install once on the stable content container: ChatActivity replaces its child.
 * Older Android releases retain their existing decor/adjustResize behavior.
 */
internal fun Activity.protectSystemInsets() {
    if (Build.VERSION.SDK_INT < 35) return
    WindowCompat.setDecorFitsSystemWindows(window, false)
    // Enforced transparent bars no longer inherit the themed bar background.
    fun updateBarContrast() {
        val light = resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK != Configuration.UI_MODE_NIGHT_YES
        WindowCompat.getInsetsController(window, window.decorView).apply {
            isAppearanceLightStatusBars = light
            isAppearanceLightNavigationBars = light
        }
    }
    updateBarContrast()
    val root = findViewById<View>(android.R.id.content)
    // Decor/theme initialization can replace the pre-attachment appearance.
    // Reapply to the attached controller after that initialization completes.
    root.doOnAttach { it.post { updateBarContrast() } }
    val left = root.paddingLeft
    val top = root.paddingTop
    val right = root.paddingRight
    val bottom = root.paddingBottom
    ViewCompat.setOnApplyWindowInsetsListener(root) { view, insets ->
        // getInsets unions the types (max per side), not IME + navigation height.
        val safe = insets.getInsets(WindowInsetsCompat.Type.systemBars() or
            WindowInsetsCompat.Type.displayCutout() or WindowInsetsCompat.Type.ime())
        view.setPadding(left + safe.left, top + safe.top, right + safe.right, bottom + safe.bottom)
        // Children, including the WebView, already occupy the safe viewport.
        WindowInsetsCompat.CONSUMED
    }
    ViewCompat.requestApplyInsets(root)
}
