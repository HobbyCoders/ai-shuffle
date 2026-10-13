package com.hobbycoders.aishuffle.ui

import android.os.Looper
import android.widget.TextView
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import java.time.Duration

/** #1946: one failed file load must not leave a notice over chat until Refresh chat. */
@RunWith(RobolectricTestRunner::class)
class StatusNoticeTest {
    private val status by lazy { TextView(RuntimeEnvironment.getApplication()) }
    private val notice by lazy { StatusNotice(status) }
    private fun advance(ms: Long) = shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(ms))

    @Test fun clearsByItselfAfterAShortTime() {
        notice.show(WorkerAssetLoader.RESTARTING_MESSAGE)
        advance(StatusNotice.DURATION_MS - 1)
        assertEquals(WorkerAssetLoader.RESTARTING_MESSAGE, status.text.toString())
        advance(1)
        assertEquals("", status.text.toString())
        assertNull(notice.message)
    }

    @Test fun aLaterSuccessfulLoadClearsItAtOnce() {
        notice.show(WorkerAssetLoader.RESTARTING_MESSAGE)
        notice.clear()
        assertEquals("", status.text.toString())
        assertNull(notice.message)
    }

    @Test fun anotherFailureRestartsTheTimer() {
        notice.show(WorkerAssetLoader.RESTARTING_MESSAGE)
        advance(StatusNotice.DURATION_MS - 1_000)
        notice.show(WorkerAssetLoader.RESTARTING_MESSAGE)
        advance(StatusNotice.DURATION_MS - 1)
        assertEquals(WorkerAssetLoader.RESTARTING_MESSAGE, status.text.toString())
        advance(1)
        assertEquals("", status.text.toString())
    }

    @Test fun stickyNoticeWaitsForASuccessfulLoad() {
        notice.show(WorkerAssetLoader.AUTH_MESSAGE, sticky = true)
        advance(StatusNotice.DURATION_MS * 10)
        assertEquals(WorkerAssetLoader.AUTH_MESSAGE, status.text.toString())
        notice.clear()
        assertEquals("", status.text.toString())
    }

    @Test fun neverClearsANewerStatusThatReplacedIt() {
        notice.show(WorkerAssetLoader.RESTARTING_MESSAGE)
        status.text = "Chat could not reach Unraid. Check the connection, then refresh chat."
        advance(StatusNotice.DURATION_MS)
        assertEquals("Chat could not reach Unraid. Check the connection, then refresh chat.", status.text.toString())
        notice.clear()
        assertEquals("Chat could not reach Unraid. Check the connection, then refresh chat.", status.text.toString())
    }
}
