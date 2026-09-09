package com.hobbycoders.aishuffle.ui

import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import androidx.lifecycle.Lifecycle
import kotlinx.coroutines.launch
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.tools.*
import com.google.android.material.dialog.MaterialAlertDialogBuilder

class ToolSettingsActivity : NativeScreen() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        lifecycleScope.launch { repeatOnLifecycle(Lifecycle.State.RESUMED) {
            DeviceToolsRuntime.availability.collect { value ->
                status.text = when (value) {
                    "offline" -> "Tools server is offline. Check its connection; no command will be replayed."
                    "unsupported_server" -> "This server does not support Android tools. App control stopped; update the server or choose another."
                    "authorization_revoked", "signed_out" -> "Tools stopped. Sign in again or choose an authorized server."
                    "cross_app_consent_required" -> "App control is available. Each request still needs local consent."
                    "foreground_only" -> "Standard tools available while Shuffle is visible."
                    "secure_storage_unavailable" -> "Secure storage is unavailable. Unlock this phone and reopen Shuffle."
                    else -> ""
                }
            }
        } }
        intent.getStringExtra("server_id")?.let { id ->
            ServerStore.get(this).getById(id)?.let { DeviceToolsRuntime.attach(this, it) }
        }
    }
    override fun onResume() { super.onResume(); render(); attachSelected() }
    private fun attachSelected() { ServerStore.get(this).selectedToolsServer()?.let { DeviceToolsRuntime.attach(this, it) } }
    private fun render() {
        val prefs = ToolsPreferences(this)
        val store = ServerStore.get(this)
        val selected = store.selectedToolsServer()
        heading("Phone tools", "Choose a server before enabling tools. Each screen action needs a new local approval. Voice starts separately and stops when you leave the voice screen.")
        text(selected?.let { "Selected server · ${it.name}\n${it.host}" } ?: "No server selected", 20f)
        button("Choose tools server") {
            val servers = store.getAll().filterNot { it.needsAccountAuthorization }
            if (servers.isEmpty()) { status.text = "Pair a server or sync your account servers first."; return@button }
            MaterialAlertDialogBuilder(this).setTitle("Choose tools server")
                .setItems(servers.map { "${it.name} · ${it.host}" }.toTypedArray()) { _, which ->
                    prefs.setCrossAppEnabled(false)
                    DeviceToolsRuntime.attach(this, servers[which]); render()
                }.setNegativeButton("Cancel", null).show()
        }
        text(if (prefs.enabled) "Standard tools are on" else "Standard tools are off", 23f)
        text("Standard tools work while Shuffle is visible: open apps, maps and web links, prepare alarms, timers and calendar events. Android confirmation screens control calls, messages and saving events. Location requires permission and separate consent.")
        button(if (prefs.enabled) "Turn off standard tools" else "Turn on standard tools") {
            if (prefs.enabled) { prefs.setEnabled(false); render() }
            else if (selected == null) status.text = "Choose a paired server first."
            else confirm("Allow phone tools?", "${selected.name} can request phone actions while Shuffle is visible. You can turn this off at any time.") { prefs.setEnabled(true); attachSelected(); render() }
        }
        text("App control", 23f)
        text("${if (ShuffleAccessibilityService.current != null && prefs.accessibilityEnabled) "Accessibility connected" else "Accessibility not connected"}. Optional app control reads or changes only apps you select below. Every request, including reading the screen, shows the named app, exact action and Allow once / Deny. Password, payment, permission and security screens are blocked.")
        button("Open Android Accessibility settings") {
            confirm("Open Accessibility settings?", "Review the AI Shuffle app-control service in Android. Enabling that service does not start app control; return here to opt in.") { startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) }
        }
        button(if (prefs.crossAppEnabled) "Stop using other apps" else "Allow while using other apps") {
            if (prefs.crossAppEnabled) { prefs.setCrossAppEnabled(false); render() }
            else if (!prefs.enabled || selected == null) status.text = "Choose a server and turn on standard tools first."
            else if (ShuffleAccessibilityService.current == null || !prefs.accessibilityEnabled) status.text = "Enable AI Shuffle in Android Accessibility settings, then return here."
            else if (prefs.allowedApps.isEmpty()) status.text = "Choose at least one allowed app first."
            else confirm("Allow while using other apps?", "${selected.name} may request screen actions while Shuffle is not visible. A persistent Shuffle privacy strip has a Stop button. No microphone is started. This opt-in ends when you stop it, sign out, change server or the service disconnects; it does not restart after boot.") {
                prefs.setCrossAppEnabled(true); render()
            }
        }
        text("Allowed apps · ${prefs.allowedApps.size}", 20f)
        text("Only installed non-system launcher apps are listed. Browsers and known banking, payment, password and security apps are excluded. Select only ordinary content apps you trust; screen text is checked again before every action.")
        button("Choose allowed apps") {
            val apps = AppControlPolicy.candidates(this).entries.toList()
            if (apps.isEmpty()) { status.text = "No eligible apps installed. Install a non-system content app, then return here."; return@button }
            val checked = apps.map { it.key in prefs.allowedApps }.toBooleanArray()
            MaterialAlertDialogBuilder(this).setTitle("Allowed apps")
                .setMultiChoiceItems(apps.map { "${it.value} · ${it.key}" }.toTypedArray(), checked) { _, which, value -> checked[which] = value }
                .setNegativeButton("Cancel", null).setPositiveButton("Save apps") { _, _ ->
                    prefs.setAllowedApps(apps.filterIndexed { i, _ -> checked[i] }.map { it.key }.toSet()); render()
                }.show()
        }
    }
}
