package com.hobbycoders.aishuffle.ui

import android.content.Intent
import android.os.Bundle
import com.google.android.material.textfield.TextInputEditText
import com.hobbycoders.aishuffle.account.ManagedWorkerRepair
import com.hobbycoders.aishuffle.account.PairingInput
import com.hobbycoders.aishuffle.account.WorkerRepairSnapshot
import com.hobbycoders.aishuffle.data.ServerStore

/** An explicit one-worker action; this screen never clears account or worker storage. */
class WorkerRepairActivity : NativeScreen() {
    private lateinit var snapshot: WorkerRepairSnapshot
    private var code: TextInputEditText? = null
    private var ready = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val server = ServerStore.get(this).getById(intent.getStringExtra(ChatActivity.EXTRA_SERVER_ID).orEmpty()) ?: run { finish(); return }
        snapshot = runCatching { WorkerRepairSnapshot(this, server) }.getOrElse { finish(); return }
        navigation("Reconnect worker")
        heading(server.name, "Checking this worker's repair support…")
    }

    override fun onResume() {
        super.onResume()
        if (!::snapshot.isInitialized) return
        if (!snapshot.current()) { finish(); return }
        if (!ready) runWork {
            ManagedWorkerRepair.verify(snapshot.server)
            snapshot.check()
            showForm()
        }
    }

    private fun showForm() {
        ready = true
        heading(snapshot.server.name, "Reconnect only this worker to this phone. Your Shuffle account and other workers stay connected.")
        text(snapshot.server.host, 14f)
        text("On this worker, ask its administrator to prepare a repair for this phone's revoked authorization, then create a new one-time pairing code in Settings → Providers → Devices.", 15f)
        code = input("One-time repair code").apply { isSaveEnabled = false }
        button("Reconnect this worker", primary = true) {
            val value = try { PairingInput.code(code?.text.toString()) } catch (error: IllegalArgumentException) {
                status.text = error.message; return@button
            }
            runWork {
                val key = ManagedWorkerRepair.repair(snapshot, value)
                snapshot.save(key)
                code?.setText("")
                startActivity(Intent(this, ChatActivity::class.java).putExtra(ChatActivity.EXTRA_SERVER_ID, snapshot.server.id)
                    .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP))
                finish()
            }
        }
        text("If a code was already used or the connection was interrupted, ask the administrator to prepare another repair and code.", 13f)
    }

    override fun onStop() { code?.setText(""); super.onStop() }
}
