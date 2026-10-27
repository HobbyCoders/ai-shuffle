package com.hobbycoders.aishuffle.ui

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class MobileUiTest {
    @Test fun theWorkerServesThePhoneUiAtMobile() {
        assertEquals("https://worker.example/mobile/", MobileUi.indexUrl("https://Worker.Example/"))
        assertEquals("https://worker.example:8443/mobile/", MobileUi.indexUrl("https://worker.example:8443"))
    }

    @Test fun onlyThePairedWorkersPhoneUiCounts() {
        val base = "https://worker.example"
        for (url in listOf("https://worker.example/mobile/", "https://worker.example/mobile", "https://worker.example/mobile/chat/abc?x=1",
                "https://worker.example:443/mobile/_app/immutable/entry/start.js")) assertTrue(url, MobileUi.isUiUrl(base, url))
        for (url in listOf("https://worker.example/", "https://worker.example/chat", "https://worker.example/mobilex",
                "https://worker.example/api/v1/auth/status", "https://worker.example/mobile/../api/v1/x",
                "https://worker.example/mobile/%2E%2E/api", "http://worker.example/mobile/", "https://worker.example:8443/mobile/",
                "https://worker.example.evil/mobile/", "https://user@worker.example/mobile/",
                "https://appassets.androidplatform.net/assets/mobile-ui/", "about:blank", null)) assertFalse(url.toString(), MobileUi.isUiUrl(base, url))
    }

    @Test fun anOldWorkerGetsAnUpdateMessage() {
        assertTrue(MobileUi.loadFailure(404).contains("Update the worker"))
        assertTrue(MobileUi.loadFailure(502).contains("HTTP 502"))
    }

    @Test fun bootstrapScriptCarriesTheRuntimeContractAsJson() {
        val script = MobileUi.bootstrapScript("https://Worker.Example:8443/", "shc_abc\"</script>", " 0.8.6 ", "Pixel 9", false)
        // Only the top-level phone UI page receives the session.
        assertTrue(script.startsWith("if (window.top === window && location.pathname.indexOf(\"/mobile/\") === 0) { window.__SHUFFLE_MOBILE__ = {"))
        assertTrue(script.endsWith("}; }"))
        val json = runtime(script)
        assertEquals("https://worker.example:8443", json.getString("apiOrigin"))
        assertEquals("shc_abc\"</script>", json.getString("token"))
        assertEquals("0.8.6", json.getString("version"))
        assertEquals("Pixel 9", json.getString("deviceName"))
        assertFalse(json.getBoolean("sendCookies"))
        // The raw closing tag never appears: org.json escapes the slash.
        assertFalse(script.contains("</script>"))
        val native = json.getJSONObject("native")
        assertEquals("android", native.getString("platform"))
        assertEquals(MobileUi.BRIDGE_VERSION, native.getInt("bridge"))
        assertEquals(MobileUi.CAPABILITIES, (0 until native.getJSONArray("capabilities").length()).map { native.getJSONArray("capabilities").getString(it) })
    }

    @Test fun capabilitiesTheDeviceLacksAreNotAdvertised() {
        val json = runtime(MobileUi.bootstrapScript("https://worker.example", "shc_x", null, null, false, listOf("microphone")))
        assertEquals(1, json.getJSONObject("native").getJSONArray("capabilities").length())
    }

    private fun runtime(script: String) =
        JSONObject(script.substringAfter("window.__SHUFFLE_MOBILE__ = ").removeSuffix(" }").removeSuffix(";"))

    @Test fun optionalFieldsAreOmittedWhenBlank() {
        val json = runtime(MobileUi.bootstrapScript("https://worker.example", "shc_x", "", null, true))
        assertFalse(json.has("version"))
        assertFalse(json.has("deviceName"))
        assertTrue(json.getBoolean("sendCookies"))
    }

    @Test fun voiceRoutingMessagesRequireBooleanCallState() {
        assertTrue(MobileUi.CAPABILITIES.contains("voice-routing"))
        assertEquals(MobileUi.ShellMessage.VoiceCall(true), MobileUi.shellMessage("{\"type\":\"voice-call\",\"active\":true}"))
        assertEquals(MobileUi.ShellMessage.VoiceCall(false), MobileUi.shellMessage("{\"type\":\"voice-call\",\"active\":false}"))
        assertNull(MobileUi.shellMessage("{\"type\":\"voice-call\",\"active\":\"true\"}"))
        assertNull(MobileUi.shellMessage("{\"type\":\"voice-call\"}"))
    }

    @Test fun voiceCallStateIsParsedStrictlyAndTitlesAreBounded() {
        assertTrue(MobileUi.CAPABILITIES.contains(MobileUi.VOICE_CALL_CONTROLS))
        assertEquals(MobileUi.ShellMessage.VoiceCallState("live", true, 1700000000000, "Planning"),
            MobileUi.shellMessage("{\"type\":\"voice-call-state\",\"status\":\"live\",\"muted\":true,\"since\":1700000000000,\"title\":\" Planning\\n\"}"))
        assertEquals(MobileUi.ShellMessage.VoiceCallState("connecting", false, null, null),
            MobileUi.shellMessage("{\"type\":\"voice-call-state\",\"status\":\"connecting\",\"muted\":false,\"since\":-1,\"title\":\"  \"}"))
        val long = MobileUi.shellMessage("{\"type\":\"voice-call-state\",\"status\":\"live\",\"muted\":false,\"title\":\"" + "x".repeat(100) + "\"}")
        assertEquals(MobileUi.MAX_CALL_TITLE, (long as MobileUi.ShellMessage.VoiceCallState).title!!.length)
        for (raw in listOf("{\"type\":\"voice-call-state\",\"status\":\"ended\",\"muted\":false}",
                "{\"type\":\"voice-call-state\",\"status\":\"live\"}",
                "{\"type\":\"voice-call-state\",\"status\":\"live\",\"muted\":\"false\"}",
                "{\"type\":\"voice-call-state\",\"muted\":true}")) assertNull(raw, MobileUi.shellMessage(raw))
    }

    @Test fun voiceCallCommandsDispatchAQuotedPageEvent() {
        assertEquals("window.dispatchEvent(new CustomEvent('shufflevoicecall', { detail: { command: \"hangup\" } }))",
            MobileUi.voiceCallEventScript(MobileUi.VoiceCallCommand.HANGUP))
        assertTrue(MobileUi.voiceCallEventScript(MobileUi.VoiceCallCommand.NOTIFICATIONS_OFF).contains("\"notifications-off\""))
    }

    @Test fun shellMessagesAreParsedStrictly() {
        assertEquals(MobileUi.ShellMessage.WorkerBar(true), MobileUi.shellMessage("{\"type\":\"worker-bar\",\"shown\":true}"))
        assertEquals(MobileUi.ShellMessage.WorkerBar(false), MobileUi.shellMessage("{\"type\":\"worker-bar\",\"shown\":false}"))
        assertEquals(MobileUi.ShellMessage.OpenWorkers, MobileUi.shellMessage("{\"type\":\"workers\"}"))
        for (raw in listOf(null, "", "not json", "[]", "{}", "{\"type\":\"worker-bar\"}", "{\"type\":\"worker-bar\",\"shown\":\"true\"}",
                "{\"type\":\"worker-bar\",\"shown\":1}", "{\"type\":\"connect\",\"url\":\"https://evil.example\"}",
                "{\"type\":\"workers\",\"pad\":\"" + "x".repeat(300) + "\"}")) assertNull(raw.toString(), MobileUi.shellMessage(raw))
    }

    @Test fun tokenUpdateQuotesTheToken() {
        val script = MobileUi.tokenUpdateScript("shc_'quoted'\\")
        assertTrue(script.contains("window.shuffleMobileSetToken(\"shc_'quoted'\\\\\")"))
        assertTrue(script.contains("window.__SHUFFLE_MOBILE__.token = \"shc_'quoted'\\\\\""))
    }
}
