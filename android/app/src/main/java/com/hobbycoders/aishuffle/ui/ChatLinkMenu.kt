package com.hobbycoders.aishuffle.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.DialogInterface
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.webkit.WebView
import android.widget.Toast
import androidx.activity.ComponentActivity
import com.google.android.material.dialog.MaterialAlertDialogBuilder

/**
 * Long-press a link in chat to copy its address. Unlike Chrome, Android's WebView has
 * no link menu of its own: without this, holding a link only selects its text.
 *
 * Only web links (http/https) are offered; the address goes to the clipboard and is
 * never loaded, so the WebView's navigation rules are unchanged.
 */
internal class ChatLinkMenu(
    private val activity: ComponentActivity,
    private val web: WebView,
    private val validDocument: () -> Boolean,
) {
    private var closed = false
    private var dialog: DialogInterface? = null
    /** Receives the link address WebView looks up for an image inside a link. */
    private val linkReply = Handler(Looper.getMainLooper()) { message ->
        if (!closed) copyableLink(message.data.getString("url"))?.let(::show)
        true
    }

    fun install() {
        web.setOnLongClickListener { longPressed() }
    }

    fun close() {
        closed = true
        linkReply.removeCallbacksAndMessages(null)
        dialog?.dismiss(); dialog = null
    }

    private fun longPressed(): Boolean {
        if (closed || !validDocument()) return false
        val hit = web.hitTestResult
        return when (hit.type) {
            WebView.HitTestResult.SRC_ANCHOR_TYPE -> copyableLink(hit.extra)?.let { show(it); true } ?: false
            // An image inside a link reports the image's address; ask WebView for the link's.
            WebView.HitTestResult.SRC_IMAGE_ANCHOR_TYPE -> { web.requestFocusNodeHref(linkReply.obtainMessage()); true }
            else -> false
        }
    }

    private fun show(url: String) {
        if (closed || !validDocument()) return
        dialog?.dismiss()
        dialog = MaterialAlertDialogBuilder(activity)
            .setTitle(url)
            .setItems(arrayOf(COPY_LABEL)) { _, _ -> copy(url) }
            .setOnDismissListener { if (dialog === it) dialog = null }
            .show()
    }

    private fun copy(url: String) {
        val clipboard = activity.getSystemService(ClipboardManager::class.java) ?: return
        clipboard.setPrimaryClip(ClipData.newPlainText("Link", url))
        // Android 13 and later confirm every copy themselves.
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            Toast.makeText(activity, COPIED_MESSAGE, Toast.LENGTH_SHORT).show()
        }
    }

    companion object {
        const val COPY_LABEL = "Copy link address"
        const val COPIED_MESSAGE = "Link copied"
        private const val MAX_LENGTH = 8192

        /** The address to offer for copying, or null: http(s) only, never script or data URLs. */
        fun copyableLink(url: String?): String? {
            if (url.isNullOrEmpty() || url.length > MAX_LENGTH) return null
            val web = url.startsWith("https://", ignoreCase = true) || url.startsWith("http://", ignoreCase = true)
            return if (web && url.length > url.indexOf("//") + 2) url else null
        }
    }
}
