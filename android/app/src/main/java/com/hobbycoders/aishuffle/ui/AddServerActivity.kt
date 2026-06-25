package com.hobbycoders.aishuffle.ui

import android.os.Bundle
import android.view.View
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
 *
 * If the server is fronted by Cloudflare Access, the first attempt detects it and
 * reveals a service-token section; the user supplies a token and retries. Plain
 * servers never see those fields.
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
        binding.errorText.visibility = View.GONE
        val name = binding.nameInput.text?.toString().orEmpty()
        val url = binding.urlInput.text?.toString().orEmpty()

        // Build the Server up-front to validate name/URL before any network call.
        val server = try {
            Server.create(name = name, rawUrl = url, apiKey = "")
        } catch (e: IllegalArgumentException) {
            showError(e.message ?: getString(R.string.error_invalid_input))
            return
        }

        // Only read the Cloudflare Access token once that section is showing.
        val cfVisible = binding.cfAccessSection.visibility == View.VISIBLE
        val cfId = if (cfVisible) binding.cfIdInput.text?.toString()?.trim().orEmpty() else ""
        val cfSecret = if (cfVisible) binding.cfSecretInput.text?.toString()?.trim().orEmpty() else ""
        if (cfVisible && (cfId.isBlank() || cfSecret.isBlank())) {
            showError(getString(R.string.cf_access_missing))
            return
        }

        setLoading(true)
        lifecycleScope.launch {
            val result = ApiClient.registerDevice(
                baseUrl = server.baseUrl,
                deviceName = server.name,
                deviceId = server.deviceId,
                cfAccessClientId = cfId.takeIf { it.isNotBlank() },
                cfAccessClientSecret = cfSecret.takeIf { it.isNotBlank() },
            )
            when (result) {
                is ApiResult.Success -> {
                    val saved = server.copy(
                        apiKey = result.data.apiKey,
                        cfAccessClientId = cfId.takeIf { it.isNotBlank() },
                        cfAccessClientSecret = cfSecret.takeIf { it.isNotBlank() },
                    )
                    store.add(saved)
                    // Best-effort: register an existing push endpoint with this server.
                    PushManager.registerKnownEndpointWith(applicationContext, saved.id)
                    setResult(RESULT_OK)
                    finish()
                }
                is ApiResult.NeedsCfAccess -> {
                    setLoading(false)
                    revealCfAccessSection()
                }
                is ApiResult.Failure -> {
                    setLoading(false)
                    showError(result.message)
                }
            }
        }
    }

    /** Show the Cloudflare Access token fields and explain why. */
    private fun revealCfAccessSection() {
        binding.cfAccessSection.visibility = View.VISIBLE
        binding.connectButton.setText(R.string.cf_access_connect)
        showError(getString(R.string.cf_access_detected))
        binding.cfIdInput.requestFocus()
    }

    private fun setLoading(loading: Boolean) {
        binding.progress.visibility = if (loading) View.VISIBLE else View.GONE
        binding.connectButton.isEnabled = !loading
        binding.nameInput.isEnabled = !loading
        binding.urlInput.isEnabled = !loading
        binding.cfIdInput.isEnabled = !loading
        binding.cfSecretInput.isEnabled = !loading
    }

    private fun showError(message: String) {
        binding.errorText.text = message
        binding.errorText.visibility = View.VISIBLE
    }
}
