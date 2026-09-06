package com.hobbycoders.aishuffle.tools

import android.app.Application
import android.content.Intent
import android.provider.AlarmClock
import android.provider.CalendarContract
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import kotlinx.coroutines.runBlocking

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class ToolSafetyTest {
    private val app: Application get() = RuntimeEnvironment.getApplication()
    @Test fun modelConfirmationCannotSendSmsOrPlaceCall() {
        val executor = DeviceToolExecutor(app)
        val args = JSONObject().put("number", "+1 212 555 0100").put("message", "draft").put("confirmed", true)
        assertEquals(Intent.ACTION_DIAL, executor.intentFor("place_call", args).action)
        assertEquals(Intent.ACTION_SENDTO, executor.intentFor("send_sms", args).action)
        assertEquals("smsto", executor.intentFor("send_sms", args).data?.scheme)
        assertEquals("draft", executor.intentFor("send_sms", args).getStringExtra("sms_body"))
    }
    @Test fun calendarRequiresSystemEditorAndClockDoesNotSkipUi() {
        val executor = DeviceToolExecutor(app)
        val event = executor.intentFor("create_reminder", JSONObject().put("title", "Dentist").put("start_time", "2026-09-07T12:00:00Z"))
        assertEquals(Intent.ACTION_INSERT, event.action)
        assertEquals(CalendarContract.Events.CONTENT_URI, event.data)
        val alarm = executor.intentFor("set_alarm", JSONObject().put("hour", 9).put("minute", 30))
        assertFalse(alarm.getBooleanExtra(AlarmClock.EXTRA_SKIP_UI, true))
    }
    @Test fun validationRejectsCoercionAndDangerousUris() {
        val executor = DeviceToolExecutor(app)
        val cases = listOf(
            "set_alarm" to JSONObject().put("hour", "9").put("minute", 0),
            "set_timer" to JSONObject().put("seconds", -1),
            "set_timer" to JSONObject().put("seconds", 1.5),
            "open_url" to JSONObject().put("url", "intent://settings"),
            "place_call" to JSONObject().put("number", "*21*123#"),
            "send_sms" to JSONObject().put("number", "123456").put("message", true),
        )
        cases.forEach { (name, args) ->
            try { executor.intentFor(name, args); fail("Accepted $name: $args") } catch (_: IllegalArgumentException) { }
        }
    }
    @Test fun declarationsMatchAllowlistAndOmitRemovedPermissions() {
        val declarations = ToolRegistry.declarations()
        val names = (0 until declarations.length()).map { declarations.getJSONObject(it).getString("name") }.toSet()
        assertEquals(ToolRegistry.names, names)
        assertFalse(names.contains("read_messages"))
        assertFalse(names.contains("read_notifications"))
        assertFalse(declarations.toString().contains("confirmed"))
    }
    @Test fun queuedContractContainsExactlyElevenToolsAndRequiredFields() {
        assertEquals(setOf("open_app", "open_url", "navigate_to", "set_alarm", "set_timer", "create_calendar_event", "get_location", "read_screen", "tap_node", "scroll", "go_back"), ToolRegistry.queuedNames)
        val declarations = ToolRegistry.declarations()
        val params = (0 until declarations.length()).associate { i -> declarations.getJSONObject(i).let { it.getString("name") to it.getJSONObject("parameters") } }
        assertEquals("[\"package_name\"]", params.getValue("open_app").getJSONArray("required").toString())
        assertEquals("[\"hour\",\"minute\"]", params.getValue("set_alarm").getJSONArray("required").toString())
        assertEquals("[\"title\",\"start_time\"]", params.getValue("create_calendar_event").getJSONArray("required").toString())
        assertEquals("[\"ref\",\"direction\"]", params.getValue("scroll").getJSONArray("required").toString())
    }
    @Test fun calendarRequiresTimezoneAndPreservesDescription() {
        val executor = DeviceToolExecutor(app)
        assertThrows(IllegalArgumentException::class.java) { executor.intentFor("create_calendar_event", JSONObject().put("title", "Meeting").put("start_time", "2026-09-07T12:00:00")) }
        val event = executor.intentFor("create_calendar_event", JSONObject().put("title", "Meeting").put("start_time", "2026-09-07T12:00:00+02:00").put("description", "Bring notes"))
        assertEquals("Bring notes", event.getStringExtra(CalendarContract.Events.DESCRIPTION))
    }
    @Test fun expiredCommandNeverReachesAnActionOrConsent() = runBlocking {
        assertEquals("expired", DeviceToolExecutor(app).execute("open_url", JSONObject().put("url", "https://example.com"), System.currentTimeMillis() - 1).getString("status"))
        assertNull(ToolApprovals.pending.value)
    }
    @Test fun disabledToolsFailWithoutLaunchingAnything() = runBlocking {
        ToolsPreferences(app).setEnabled(false)
        assertEquals("disabled", DeviceToolExecutor(app).execute("place_call", JSONObject().put("confirmed", true)).getString("status"))
    }
}
