package com.hobbycoders.aishuffle.ui

import android.content.Context
import android.view.Gravity
import android.view.View
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView

/**
 * The chat screen's view tree: toolbar, repair button and WebView in a column, with the
 * status floating over the top of the chat viewport (#1946). Status messages take no layout
 * space, so nothing in the page moves when one appears or goes. Every view passed in must be
 * detached; each gets exactly one parent here.
 */
internal object ChatScreenLayout {
    fun build(context: Context, toolbar: View, status: TextView, repairButton: View, web: View, background: Int): FrameLayout {
        val density = context.resources.displayMetrics.density
        fun dp(value: Int) = (value * density).toInt()
        val shell = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL; setBackgroundColor(background)
            addView(toolbar, LinearLayout.LayoutParams(-1, dp(56)))
            addView(repairButton, LinearLayout.LayoutParams(-1, -2).apply { marginStart = dp(16); marginEnd = dp(16) })
            addView(web, LinearLayout.LayoutParams(-1, 0, 1f))
        }
        val root = FrameLayout(context).apply {
            addView(shell, FrameLayout.LayoutParams(-1, -1))
            addView(status, FrameLayout.LayoutParams(-1, -2, Gravity.TOP).apply { setMargins(dp(8), dp(8), dp(8), 0) })
        }
        // Pin the overlay to the top of the WebView, below whichever bars are shown. A
        // translation, so it never relayouts the shell.
        shell.addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ -> status.translationY = web.top.toFloat() }
        return root
    }
}
