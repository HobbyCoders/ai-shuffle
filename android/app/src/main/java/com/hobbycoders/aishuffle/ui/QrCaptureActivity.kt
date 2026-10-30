package com.hobbycoders.aishuffle.ui

import android.os.Bundle
import android.view.KeyEvent
import android.widget.Button
import androidx.appcompat.app.AppCompatActivity
import com.google.zxing.client.android.Intents
import com.hobbycoders.aishuffle.R
import com.journeyapps.barcodescanner.CaptureManager
import com.journeyapps.barcodescanner.DecoratedBarcodeView

/** AndroidX host for ZXing's camera lifecycle, shared by both pairing flows. */
class QrCaptureActivity : AppCompatActivity() {
    private lateinit var capture: CaptureManager
    private lateinit var barcodeScanner: DecoratedBarcodeView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_qr_capture)
        protectSystemInsets()
        findViewById<Button>(R.id.scannerBack).apply {
            com.hobbycoders.aishuffle.tools.guardConsentTouch(this)
            setOnClickListener {
                setResult(RESULT_CANCELED)
                finish()
            }
        }
        barcodeScanner = findViewById(R.id.zxing_barcode_scanner)
        capture = CaptureManager(this, barcodeScanner)
        // ScanOptions defaults to a runtime orientation lock, even when the
        // manifest is unrestricted. Allow rotation and large-screen resizing.
        intent.putExtra(Intents.Scan.ORIENTATION_LOCKED, false)
        capture.initializeFromIntent(intent, savedInstanceState)
        capture.decode()
    }

    override fun onResume() {
        super.onResume()
        capture.onResume()
    }

    override fun onPause() {
        super.onPause()
        capture.onPause()
    }

    override fun onDestroy() {
        super.onDestroy()
        capture.onDestroy()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        capture.onSaveInstanceState(outState)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        capture.onRequestPermissionsResult(requestCode, permissions, grantResults)
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean =
        barcodeScanner.onKeyDown(keyCode, event) || super.onKeyDown(keyCode, event)
}
