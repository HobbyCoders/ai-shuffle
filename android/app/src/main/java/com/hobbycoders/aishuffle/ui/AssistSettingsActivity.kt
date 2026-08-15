package com.hobbycoders.aishuffle.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.MenuItem
import android.widget.ArrayAdapter
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.google.android.material.snackbar.Snackbar
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.data.AssistPrefs
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.databinding.ActivityAssistSettingsBinding
import com.hobbycoders.aishuffle.voice.AssistNotificationListener
import com.hobbycoders.aishuffle.voice.VoiceCallActivity
import com.hobbycoders.aishuffle.wake.VoskModelManager
import com.hobbycoders.aishuffle.wake.WakeWordService
import kotlinx.coroutines.launch

/**
 * Voice assistant setup: wake word on/off + phrase, which server funds the
 * call, the offline model download, and the permission checklist the phone
 * tools need. Everything is optional — each tool degrades gracefully when its
 * permission is missing.
 */
class AssistSettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivityAssistSettingsBinding
    private lateinit var prefs: AssistPrefs

    private val phonePermissions = arrayOf(
        Manifest.permission.RECORD_AUDIO,
        Manifest.permission.CALL_PHONE,
        Manifest.permission.READ_CONTACTS,
        Manifest.permission.SEND_SMS,
        Manifest.permission.READ_SMS,
        Manifest.permission.READ_CALENDAR,
        Manifest.permission.WRITE_CALENDAR,
        Manifest.permission.ACCESS_FINE_LOCATION,
    )

    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
            refreshPermissionSummary()
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityAssistSettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        prefs = AssistPrefs.get(this)

        bindWakeWord()
        bindServerPicker()
        bindPermissions()
        bindModelState()

        binding.talkNowButton.setOnClickListener {
            startActivity(Intent(this, VoiceCallActivity::class.java))
        }
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        if (item.itemId == android.R.id.home) {
            finish(); return true
        }
        return super.onOptionsItemSelected(item)
    }

    override fun onResume() {
        super.onResume()
        refreshPermissionSummary()
    }

    // -- wake word -----------------------------------------------------

    private fun bindWakeWord() {
        binding.wakePhraseInput.setText(prefs.wakePhrase)
        binding.wakeSwitch.isChecked = prefs.wakeWordEnabled

        binding.wakeSwitch.setOnCheckedChangeListener { _, checked ->
            prefs.wakeWordEnabled = checked
            if (checked) {
                savePhrase()
                if (!VoskModelManager.isReady(this)) {
                    downloadModelThenStart()
                } else {
                    WakeWordService.startIfEnabled(this)
                }
            } else {
                WakeWordService.stop(this)
            }
        }

        binding.savePhraseButton.setOnClickListener {
            savePhrase()
            if (prefs.wakeWordEnabled) {
                // Restart so the recognizer picks up the new grammar.
                WakeWordService.stop(this)
                WakeWordService.startIfEnabled(this)
            }
            Snackbar.make(binding.root, R.string.assist_phrase_saved, Snackbar.LENGTH_SHORT).show()
        }
    }

    private fun savePhrase() {
        prefs.wakePhrase = binding.wakePhraseInput.text?.toString() ?: AssistPrefs.DEFAULT_PHRASE
        binding.wakePhraseInput.setText(prefs.wakePhrase)
    }

    // -- server picker -------------------------------------------------

    private fun bindServerPicker() {
        val servers = ServerStore.get(this).getAll()
        if (servers.isEmpty()) {
            binding.serverPicker.isEnabled = false
            return
        }
        val adapter = ArrayAdapter(
            this, android.R.layout.simple_spinner_dropdown_item,
            servers.map { it.name },
        )
        binding.serverPicker.adapter = adapter
        val selected = servers.indexOfFirst { it.id == prefs.voiceServerId }
        if (selected >= 0) binding.serverPicker.setSelection(selected)
        binding.serverPicker.onItemSelectedListener =
            object : android.widget.AdapterView.OnItemSelectedListener {
                override fun onItemSelected(
                    parent: android.widget.AdapterView<*>?, view: android.view.View?,
                    position: Int, id: Long,
                ) {
                    prefs.voiceServerId = servers.getOrNull(position)?.id
                }

                override fun onNothingSelected(parent: android.widget.AdapterView<*>?) {}
            }
    }

    // -- permissions ---------------------------------------------------

    private fun bindPermissions() {
        binding.grantPermissionsButton.setOnClickListener {
            permissionLauncher.launch(phonePermissions)
        }
        binding.notificationAccessButton.setOnClickListener {
            runCatching {
                startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
            }
        }
        binding.overlayButton.setOnClickListener {
            runCatching {
                startActivity(
                    Intent(
                        Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:$packageName"),
                    ),
                )
            }
        }
    }

    private fun refreshPermissionSummary() {
        val missing = phonePermissions.count {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        binding.permissionSummary.text = if (missing == 0) {
            getString(R.string.assist_permissions_all_granted)
        } else {
            getString(R.string.assist_permissions_missing, missing)
        }
        binding.notificationAccessButton.text = getString(
            if (AssistNotificationListener.isEnabled(this)) {
                R.string.assist_notification_access_granted
            } else {
                R.string.assist_notification_access
            },
        )
        binding.overlayButton.text = getString(
            if (Settings.canDrawOverlays(this)) {
                R.string.assist_overlay_granted
            } else {
                R.string.assist_overlay
            },
        )
    }

    // -- wake model ----------------------------------------------------

    private fun bindModelState() {
        binding.downloadModelButton.setOnClickListener { downloadModelThenStart() }
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                VoskModelManager.state.collect { state ->
                    when (state) {
                        is VoskModelManager.State.Idle -> {
                            binding.modelStatus.text = getString(R.string.assist_model_needed)
                            binding.downloadModelButton.isEnabled = true
                            binding.modelProgress.visibility = android.view.View.GONE
                        }
                        is VoskModelManager.State.Downloading -> {
                            binding.modelStatus.text =
                                getString(R.string.assist_model_downloading, state.percent)
                            binding.downloadModelButton.isEnabled = false
                            binding.modelProgress.visibility = android.view.View.VISIBLE
                            binding.modelProgress.progress = state.percent
                        }
                        VoskModelManager.State.Unpacking -> {
                            binding.modelStatus.text = getString(R.string.assist_model_unpacking)
                            binding.modelProgress.visibility = android.view.View.VISIBLE
                        }
                        VoskModelManager.State.Ready -> {
                            binding.modelStatus.text = getString(R.string.assist_model_ready)
                            binding.downloadModelButton.isEnabled = false
                            binding.modelProgress.visibility = android.view.View.GONE
                        }
                        is VoskModelManager.State.Error -> {
                            binding.modelStatus.text = state.message
                            binding.downloadModelButton.isEnabled = true
                            binding.modelProgress.visibility = android.view.View.GONE
                        }
                    }
                }
            }
        }
        VoskModelManager.isReady(this) // refresh state from disk
    }

    private fun downloadModelThenStart() {
        lifecycleScope.launch {
            val ok = VoskModelManager.ensureModel(this@AssistSettingsActivity)
            if (ok) WakeWordService.startIfEnabled(this@AssistSettingsActivity)
        }
    }
}
