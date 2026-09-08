package com.hobbycoders.aishuffle.ui

import android.widget.Button
import com.hobbycoders.aishuffle.R
import com.journeyapps.barcodescanner.CaptureActivity
import com.journeyapps.barcodescanner.DecoratedBarcodeView

/** All pairing scanners share visible navigation and the library's camera lifecycle. */
class QrCaptureActivity : CaptureActivity() {
    override fun initializeContent(): DecoratedBarcodeView {
        setContentView(R.layout.activity_qr_capture)
        findViewById<Button>(R.id.scannerBack).apply {
            com.hobbycoders.aishuffle.tools.guardConsentTouch(this)
            setOnClickListener {
                setResult(RESULT_CANCELED)
                finish()
            }
        }
        return findViewById(R.id.zxing_barcode_scanner)
    }
}
