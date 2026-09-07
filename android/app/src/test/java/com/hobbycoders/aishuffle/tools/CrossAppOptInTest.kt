package com.hobbycoders.aishuffle.tools

import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class CrossAppOptInTest {
    @Test fun crossAppCannotBeArmedWithoutConnectedOsService() {
        val prefs = ToolsPreferences(RuntimeEnvironment.getApplication())
        prefs.setEnabled(true); prefs.setCrossAppEnabled(true)
        assertFalse(prefs.crossAppEnabled)
        prefs.setEnabled(false)
    }
    @Test fun stoppingToolsAlsoStopsCrossAppSession() {
        val prefs = ToolsPreferences(RuntimeEnvironment.getApplication())
        prefs.setEnabled(false)
        assertFalse(prefs.enabled); assertFalse(prefs.crossAppEnabled)
    }
    @Test fun userAllowlistIsPersistedWithoutReplacingSecurityPolicy() {
        val prefs = ToolsPreferences(RuntimeEnvironment.getApplication())
        prefs.setAllowedApps(setOf("com.example.reader", "com.example.bank"))
        assertTrue("com.example.reader" in prefs.allowedApps)
        assertFalse(AppControlPolicy.permitsName("com.example.bank", "Bank"))
        assertTrue(AppControlPolicy.permitsName("com.example.reader", "Reader"))
        prefs.setAllowedApps(emptySet())
    }
}
