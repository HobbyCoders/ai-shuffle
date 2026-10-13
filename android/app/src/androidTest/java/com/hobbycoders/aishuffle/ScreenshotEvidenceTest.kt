package com.hobbycoders.aishuffle

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class ScreenshotEvidenceTest {
    private fun image() = Bitmap.createBitmap(400, 800, Bitmap.Config.ARGB_8888).apply { eraseColor(Color.BLACK) }
    @Test fun blackAppWithBrightSystemBarsIsRejected() {
        val bitmap = image()
        try {
            val canvas = Canvas(bitmap)
            val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE; textSize = 26f }
            canvas.drawRect(0f, 0f, 400f, 30f, paint)
            canvas.drawText("12:00", 15f, 25f, paint)
            canvas.drawRect(0f, 775f, 400f, 800f, paint)
            assertFalse(TestScreenshots.hasAppContent(bitmap))
        } finally { bitmap.recycle() }
    }
    @Test fun darkAppWithTopAndBottomControlsIsAccepted() {
        val bitmap = image()
        try {
            bitmap.eraseColor(Color.rgb(13, 16, 23))
            val canvas = Canvas(bitmap)
            val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE; textSize = 24f }
            canvas.drawText("Microphone is off", 30f, 90f, paint)
            canvas.drawText("End call", 110f, 730f, paint)
            assertTrue(TestScreenshots.hasAppContent(bitmap))
        } finally { bitmap.recycle() }
    }
}
