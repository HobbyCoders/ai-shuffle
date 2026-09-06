package com.hobbycoders.aishuffle.tools

import android.content.ComponentName
import android.content.Context
import android.provider.Settings

class ToolsPreferences(context: Context) {
    private val app = context.applicationContext
    private val prefs = app.getSharedPreferences("device_tools", Context.MODE_PRIVATE)
    val enabled: Boolean get() = prefs.getBoolean("enabled", false)
    val accessibilityEnabled: Boolean get() {
        if (Settings.Secure.getInt(app.contentResolver, Settings.Secure.ACCESSIBILITY_ENABLED, 0) != 1) return false
        val expected = ComponentName(app, ShuffleAccessibilityService::class.java)
        return Settings.Secure.getString(app.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
            .orEmpty().split(':').any { ComponentName.unflattenFromString(it) == expected }
    }
    companion object { @Volatile private var crossAppSession = false }
    val crossAppEnabled: Boolean get() = enabled && crossAppSession
    val allowedApps: Set<String> get() = prefs.getStringSet("allowed_apps", emptySet()).orEmpty().toSet()
    fun setAllowedApps(packages: Set<String>) {
        prefs.edit().putStringSet("allowed_apps", packages).commit()
        ShuffleAccessibilityService.current?.cancelConsent()
        DeviceToolsRuntime.preferencesChanged()
    }
    fun setCrossAppEnabled(value: Boolean) {
        crossAppSession = value && enabled && ShuffleAccessibilityService.current != null
        ShuffleAccessibilityService.current?.cancelConsent()
        DeviceToolsRuntime.preferencesChanged()
    }
    fun setEnabled(value: Boolean) {
        prefs.edit().putBoolean("enabled", value).commit()
        if (!value) { crossAppSession = false; ToolApprovals.cancelAll(); ShuffleAccessibilityService.current?.cancelConsent() }
        DeviceToolsRuntime.preferencesChanged()
    }
}
