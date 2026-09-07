package com.hobbycoders.aishuffle.tools

import android.app.Activity
import android.view.MotionEvent
import android.view.View
import androidx.appcompat.app.AlertDialog
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.lifecycleScope
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.lang.ref.WeakReference
import java.util.UUID

/** Authorization comes only from a visible local consent host, never from tool arguments. */
data class PendingToolApproval(val id: String, val title: String, val detail: String, val expiresAt: Long)

object ToolApprovals {
    private val mutable = MutableStateFlow<PendingToolApproval?>(null)
    val pending: StateFlow<PendingToolApproval?> = mutable
    private var host = WeakReference<Activity>(null)
    private var answer: CompletableDeferred<Boolean>? = null
    private var collector: Job? = null
    private var dialog: AlertDialog? = null

    internal fun attach(activity: Activity) {
        if (host.get() === activity) return
        cancelAll(); collector?.cancel(); host = WeakReference(activity)
        val owner = activity as? LifecycleOwner ?: return
        collector = owner.lifecycleScope.launch(Dispatchers.Main.immediate) {
            pending.collect { request ->
                dialog?.setOnDismissListener(null); dialog?.dismiss(); dialog = null
                if (request == null) return@collect
                val shown = MaterialAlertDialogBuilder(activity)
                    .setTitle(request.title).setMessage(request.detail)
                    .setPositiveButton("Allow once", null).setNegativeButton("Deny", null)
                    .create()
                dialog = shown
                shown.setCanceledOnTouchOutside(false)
                shown.setOnDismissListener { resolve(activity, request.id, false) }
                shown.setOnShowListener {
                    for ((which, approved) in listOf(AlertDialog.BUTTON_POSITIVE to true, AlertDialog.BUTTON_NEGATIVE to false)) {
                        shown.getButton(which).apply {
                            guardConsentTouch(this)
                            setOnClickListener { resolve(activity, request.id, approved); shown.dismiss() }
                        }
                    }
                }
                shown.show()
                launch {
                    while (pending.value?.id == request.id && shown.isShowing) {
                        val remaining = ((request.expiresAt - System.currentTimeMillis() + 999) / 1000).coerceAtLeast(0)
                        shown.setMessage("${request.detail}\n\nExpires in $remaining seconds. Allow authorizes this request only.")
                        delay(250)
                    }
                }
            }
        }
    }
    internal fun detach(activity: Activity) {
        if (host.get() === activity) {
            cancelAll(); collector?.cancel(); collector = null
            dialog?.setOnDismissListener(null); dialog?.dismiss(); dialog = null; host.clear()
        }
    }
    fun cancelAll() { answer?.complete(false); mutable.value = null }

    /** Call only from local button callbacks. Negative/dismissal does not require focus. */
    fun resolve(activity: Activity, requestId: String, approved: Boolean): Boolean {
        val request = mutable.value ?: return false
        if (host.get() !== activity || activity.isFinishing || activity.isDestroyed || request.id != requestId) return false
        if (approved && (dialog?.window?.decorView?.hasWindowFocus() != true || System.currentTimeMillis() >= request.expiresAt)) return false
        return answer?.complete(approved) == true
    }

    internal suspend fun request(title: String, detail: String, deadline: Long = System.currentTimeMillis() + 20_000): Boolean = withContext(Dispatchers.Main.immediate) {
        val activity = host.get()
        if (activity == null || activity.isFinishing || activity.isDestroyed || !activity.hasWindowFocus() || answer != null) return@withContext false
        val expires = minOf(deadline, System.currentTimeMillis() + 20_000)
        val duration = expires - System.currentTimeMillis()
        if (duration <= 0) return@withContext false
        val deferred = CompletableDeferred<Boolean>(); answer = deferred
        mutable.value = PendingToolApproval(UUID.randomUUID().toString(), title, detail.take(1500), expires)
        try { withTimeoutOrNull(duration) { deferred.await() } == true && System.currentTimeMillis() < expires }
        finally { answer = null; mutable.value = null }
    }
}

internal fun guardConsentTouch(view: View) {
    view.filterTouchesWhenObscured = true
    view.setOnTouchListener { _, event ->
        event.flags and (MotionEvent.FLAG_WINDOW_IS_OBSCURED or MotionEvent.FLAG_WINDOW_IS_PARTIALLY_OBSCURED) != 0
    }
}
