package com.hobbycoders.aishuffle.ui

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class MobileUiTest {
    @Test fun bundleRootIsServedAsTheIndexAndNothingElseIs() {
        assertEquals("https://appassets.androidplatform.net/assets/mobile-ui/", MobileUi.INDEX_URL)
        assertTrue(MobileUi.isIndexPath("/assets/mobile-ui/"))
        assertTrue(MobileUi.isIndexPath("/assets/mobile-ui"))
        assertFalse(MobileUi.isIndexPath("/assets/mobile-ui/index.html"))
        assertFalse(MobileUi.isIndexPath("/assets/mobile-ui/_app/immutable/entry/start.js"))
        assertFalse(MobileUi.isIndexPath(null))
    }

    @Test fun onlyTheAssetOriginCounts() {
        assertTrue(MobileUi.isAssetOrigin("https://appassets.androidplatform.net/assets/mobile-ui/"))
        assertFalse(MobileUi.isAssetOrigin("http://appassets.androidplatform.net/assets/mobile-ui/"))
        assertFalse(MobileUi.isAssetOrigin("https://appassets.androidplatform.net.evil/assets/"))
        assertFalse(MobileUi.isAssetOrigin("https://worker.example/"))
        assertFalse(MobileUi.isAssetOrigin(null))
    }

    @Test fun bootstrapScriptCarriesTheRuntimeContractAsJson() {
        val script = MobileUi.bootstrapScript("https://Worker.Example:8443/", "shc_abc\"</script>", " 0.8.6 ", "Pixel 9", false)
        assertTrue(script.startsWith("window.__SHUFFLE_MOBILE__ = {"))
        assertTrue(script.endsWith("};"))
        val json = JSONObject(script.removePrefix("window.__SHUFFLE_MOBILE__ = ").removeSuffix(";"))
        assertEquals("https://worker.example:8443", json.getString("apiOrigin"))
        assertEquals("shc_abc\"</script>", json.getString("token"))
        assertEquals("0.8.6", json.getString("version"))
        assertEquals("Pixel 9", json.getString("deviceName"))
        assertFalse(json.getBoolean("sendCookies"))
        // The raw closing tag never appears: org.json escapes the slash.
        assertFalse(script.contains("</script>"))
    }

    @Test fun optionalFieldsAreOmittedWhenBlank() {
        val json = JSONObject(MobileUi.bootstrapScript("https://worker.example", "shc_x", "", null, true)
            .removePrefix("window.__SHUFFLE_MOBILE__ = ").removeSuffix(";"))
        assertFalse(json.has("version"))
        assertFalse(json.has("deviceName"))
        assertTrue(json.getBoolean("sendCookies"))
    }

    @Test fun tokenUpdateQuotesTheToken() {
        val script = MobileUi.tokenUpdateScript("shc_'quoted'\\")
        assertTrue(script.contains("window.shuffleMobileSetToken(\"shc_'quoted'\\\\\")"))
        assertTrue(script.contains("window.__SHUFFLE_MOBILE__.token = \"shc_'quoted'\\\\\""))
    }
}
