package com.hobbycoders.aishuffle.ci

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.widget.Button
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.ui.ChatActivity

/** Only in releaseSmoke, never release. Use the real model/store writer rather
 * than reflecting into production fields or weakening the release keep rules. */
class ReleaseSmokeFixtureActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val server = Server.create("Minified worker", "https://release-smoke.invalid", "smoke-only-key")
        ServerStore.get(this).add(server)
        setContentView(Button(this).apply {
            text = "Open minified chat"
            setOnClickListener {
                startActivity(Intent(this@ReleaseSmokeFixtureActivity, ChatActivity::class.java)
                    .putExtra(ChatActivity.EXTRA_SERVER_ID, server.id))
            }
        })
    }
}
