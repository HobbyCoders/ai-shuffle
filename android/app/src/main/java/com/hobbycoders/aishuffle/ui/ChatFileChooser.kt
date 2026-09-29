package com.hobbycoders.aishuffle.ui

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.webkit.WebChromeClient

/** Android multi-select pickers return ClipData, not Intent.data. */
internal object ChatFileChooser {
    fun intent(params: WebChromeClient.FileChooserParams): Intent = params.createIntent().apply {
        putExtra(Intent.EXTRA_ALLOW_MULTIPLE, params.mode == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE)
    }

    fun parseResult(resultCode: Int, data: Intent?): Array<Uri>? {
        if (resultCode != Activity.RESULT_OK || data == null) return null
        val clip = data.clipData
        val uris = if (clip != null) {
            if (clip.itemCount !in 1..32) return null
            Array(clip.itemCount) { index -> clip.getItemAt(index).uri ?: return null }
        } else {
            arrayOf(data.data ?: return null)
        }
        // Keep the existing content-provider-only boundary; never return a partial
        // selection or let an invalid item fall back to Intent.data.
        return uris.takeIf { it.all { uri -> uri.scheme == "content" } }
    }
}
