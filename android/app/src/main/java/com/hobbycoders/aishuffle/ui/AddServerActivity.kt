package com.hobbycoders.aishuffle.ui

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.databinding.ActivityAddServerBinding
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.net.ApiResult
import com.hobbycoders.aishuffle.push.PushManager
import kotlinx.coroutines.launch

/**
 * Collects a display name + server URL, registers this device with the server,
 * persists the returned key, and (if a push endpoint is already known) registers
 * it with the new server. Shows progress and inline errors.
 */
class AddServerActivity : AppCompatActivity() {

    private lateinit var binding: ActivityAddServerBinding
    private lateinit var store: ServerStore

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityAddServerBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        binding.toolbar.setNavigationOnClickListener { finish() }

        store = ServerStore.get(this)

        binding.connectButton.setOnClickListener { submit() }
    }

    private fun submit() {
        binding.errorText.visibility = android.view.View.GONE
        val name = binding.nameInput.text?.toString().orEmpty()
        val url = binding.urlInput.text?.toString().orEmpty()

        // Build the Server up-front to validate name/URL before any network call.
        val server = try {
            Server.create(name = name, rawUrl = url, apiKey = "")
        } catch (e: IllegalArgumentException) {
            showError(e.message ?: getString(R.string.error_invalid_input))
            return
        }

        setLoading(true)
        lifecycleScope.launch {
            when (val result = ApiClient.registerDevice(server.baseUrl, server.name, server.deviceId)) {
                is ApiResult.Success -> {
                    val saved = server.copy(apiKey = result.data.apiKey)
                    store.add(saved)
                    // Best-effort: register an existing push endpoint with this server.
                    PushManager.registerKnownEndpointWith(applicationContext, saved.id)
                    setResult(RESULT_OK)
                    finish()
                }
                is ApiResult.Failure -> {
                    setLoading(false)
                    showError(result.message)
                }
            }
        }
    }

    private fun setLoading(loading: Boolean) {
        binding.progress.visibility = if (loading) android.view.View.VISIBLE else android.view.View.GONE
        binding.connectButton.isEnabled = !loading
        binding.nameInput.isEnabled = !loading
        binding.urlInput.isEnabled = !loading
    }

    private fun showError(message: String) {
        binding.errorText.text = message
        binding.errorText.visibility = android.view.View.VISIBLE
    }
}
