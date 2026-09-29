package com.hobbycoders.aishuffle.ui

import android.app.Activity
import android.content.ClipData
import android.content.Intent
import android.net.Uri
import android.webkit.WebChromeClient
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class ChatFileChooserTest {
    private val first = Uri.parse("content://documents/first")
    private val second = Uri.parse("content://documents/second")
    private fun selection(vararg uris: Uri) = Intent().apply {
        clipData = ClipData.newRawUri("files", uris.first()).apply {
            uris.drop(1).forEach { addItem(ClipData.Item(it)) }
        }
    }
    private fun params(multiple: Boolean) = object : WebChromeClient.FileChooserParams() {
        override fun getMode() = if (multiple) MODE_OPEN_MULTIPLE else MODE_OPEN
        override fun getAcceptTypes() = arrayOf("text/plain")
        override fun isCaptureEnabled() = false
        override fun getTitle(): CharSequence? = null
        override fun getFilenameHint(): String? = null
        override fun createIntent() = Intent(Intent.ACTION_GET_CONTENT).setType("text/plain")
    }

    @Test fun explicitlyRequestsMultipleOnlyForMultipleInputs() {
        for (multiple in listOf(false, true)) {
            val intent = ChatFileChooser.intent(params(multiple))
            assertEquals(multiple, intent.getBooleanExtra(Intent.EXTRA_ALLOW_MULTIPLE, false))
            assertEquals(Intent.ACTION_GET_CONTENT, intent.action)
            assertEquals("text/plain", intent.type)
        }
    }
    @Test fun returnsEveryClipDataUriInPickerOrderEvenWithoutData() {
        assertArrayEquals(arrayOf(first, second), ChatFileChooser.parseResult(Activity.RESULT_OK, selection(first, second)))
        assertArrayEquals(arrayOf(first, second), ChatFileChooser.parseResult(Activity.RESULT_OK, selection(first, second).setData(first)))
    }
    @Test fun keepsSingleFileAndCancellationBehavior() {
        assertArrayEquals(arrayOf(first), ChatFileChooser.parseResult(Activity.RESULT_OK, Intent().setData(first)))
        assertNull(ChatFileChooser.parseResult(Activity.RESULT_CANCELED, selection(first, second)))
        assertNull(ChatFileChooser.parseResult(Activity.RESULT_OK, null))
        assertNull(ChatFileChooser.parseResult(Activity.RESULT_OK, Intent()))
    }
    @Test fun rejectsEntireInvalidSelectionWithoutFallingBackToData() {
        for (bad in listOf("file:///private/file", "https://example.test/file")) {
            assertNull(ChatFileChooser.parseResult(Activity.RESULT_OK, selection(first, Uri.parse(bad)).setData(first)))
            assertNull(ChatFileChooser.parseResult(Activity.RESULT_OK, Intent().setData(Uri.parse(bad))))
        }
        val text = selection(first).apply { clipData!!.addItem(ClipData.Item("not a uri")) }
        assertNull(ChatFileChooser.parseResult(Activity.RESULT_OK, text))
    }
    @Test fun enforcesSelectionLimitBeforeDelivery() {
        assertEquals(32, ChatFileChooser.parseResult(Activity.RESULT_OK, selection(*Array(32) { first }))!!.size)
        assertNull(ChatFileChooser.parseResult(Activity.RESULT_OK, selection(*Array(33) { first })))
    }
}
