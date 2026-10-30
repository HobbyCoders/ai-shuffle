package com.hobbycoders.aishuffle

import android.app.Activity
import android.view.View
import android.view.ViewTreeObserver
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso
import androidx.test.espresso.IdlingRegistry
import androidx.test.espresso.IdlingResource
import androidx.test.platform.app.InstrumentationRegistry

/** Wait for rendered state, not a guessed delay or repeated Espresso failures/screenshots. */
internal class NativeStateIdlingResource(
    private val description: String,
    private val root: View,
    private val ready: () -> Boolean,
    private val requireFocusedFrame: Boolean = false,
) : IdlingResource, ViewTreeObserver.OnGlobalLayoutListener,
    ViewTreeObserver.OnWindowFocusChangeListener, ViewTreeObserver.OnDrawListener, AutoCloseable {
    private var callback: IdlingResource.ResourceCallback? = null
    private var focusedFrame = !requireFocusedFrame
    private var framePending = false
    private var closed = false

    init {
        root.viewTreeObserver.addOnGlobalLayoutListener(this)
        root.viewTreeObserver.addOnWindowFocusChangeListener(this)
        root.viewTreeObserver.addOnDrawListener(this)
        requestFrame()
    }
    override fun getName() = description
    override fun isIdleNow() = ready() && focusedFrame && (!requireFocusedFrame ||
        (root.hasWindowFocus() && root.isLaidOut && !root.isLayoutRequested))
    override fun registerIdleTransitionCallback(callback: IdlingResource.ResourceCallback) {
        this.callback = callback
    }
    private fun changed() {
        if (closed) return
        if (isIdleNow()) callback?.onTransitionToIdle() else requestFrame()
    }
    override fun onGlobalLayout() = changed()
    // Same-sized TextView changes invalidate a frame without requesting layout.
    override fun onDraw() = changed()
    override fun onWindowFocusChanged(hasFocus: Boolean) {
        if (requireFocusedFrame) focusedFrame = false
        changed()
    }
    private fun requestFrame() {
        if (!requireFocusedFrame || framePending || closed || !root.hasWindowFocus()) return
        check(android.os.Build.VERSION.SDK_INT >= 29) { "Focused-frame idling requires API 29+" }
        check(root.isHardwareAccelerated) { "The native test window must render a hardware frame" }
        framePending = true
        root.viewTreeObserver.registerFrameCommitCallback {
            framePending = false
            if (!closed) {
                focusedFrame = root.hasWindowFocus() && root.isLaidOut && !root.isLayoutRequested
                changed()
            }
        }
        root.invalidate()
    }
    override fun close() {
        closed = true
        callback = null
        root.viewTreeObserver.takeIf { it.isAlive }?.apply {
            removeOnGlobalLayoutListener(this@NativeStateIdlingResource)
            removeOnWindowFocusChangeListener(this@NativeStateIdlingResource)
            removeOnDrawListener(this@NativeStateIdlingResource)
        }
    }
}

internal fun awaitNativeState(activity: Activity, description: String, ready: () -> Boolean) {
    val instrumentation = InstrumentationRegistry.getInstrumentation()
    lateinit var resource: NativeStateIdlingResource
    instrumentation.runOnMainSync {
        resource = NativeStateIdlingResource(description, activity.window.decorView, ready)
    }
    IdlingRegistry.getInstance().register(resource)
    try { Espresso.onIdle() }
    finally {
        IdlingRegistry.getInstance().unregister(resource)
        instrumentation.runOnMainSync { resource.close() }
    }
}

/** ActivityScenario RESUMED does not mean its first focused frame has reached the renderer. */
internal fun awaitNativeWindow(activity: Activity) {
    val instrumentation = InstrumentationRegistry.getInstrumentation()
    lateinit var resource: NativeStateIdlingResource
    instrumentation.runOnMainSync {
        resource = NativeStateIdlingResource("Focused frame: ${activity.javaClass.simpleName}",
            activity.window.decorView, { !activity.isFinishing }, requireFocusedFrame = true)
    }
    IdlingRegistry.getInstance().register(resource)
    try { Espresso.onIdle() }
    finally {
        IdlingRegistry.getInstance().unregister(resource)
        instrumentation.runOnMainSync { resource.close() }
    }
    instrumentation.runOnMainSync {
        val callback = activity.window.callback
        activity.window.callback = object : android.view.Window.Callback by callback {
            override fun dispatchTouchEvent(event: android.view.MotionEvent): Boolean {
                android.util.Log.i("NativeInputTiming", "${activity.javaClass.simpleName} action=${event.actionMasked} " +
                    "flags=${event.flags} x=${event.x} y=${event.y} eventTime=${event.eventTime} " +
                    "uptime=${android.os.SystemClock.uptimeMillis()} focused=${activity.hasWindowFocus()}")
                return callback.dispatchTouchEvent(event)
            }
        }
    }
}

internal fun <A : Activity> ActivityScenario<A>.nativeActivity(): A {
    lateinit var activity: A
    onActivity { activity = it }
    return activity
}
