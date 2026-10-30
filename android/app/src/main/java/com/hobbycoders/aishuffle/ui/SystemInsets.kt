package com.hobbycoders.aishuffle.ui

import android.content.res.Configuration
import android.view.View
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.doOnAttach

/** Use AndroidX edge-to-edge on every supported Android version. Keep native
 * controls and the WebView outside bars, cutouts and the IME. Install once on
 * the stable content container: ChatActivity replaces its child.
 */
internal fun ComponentActivity.protectSystemInsets() {
    enableEdgeToEdge()
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
