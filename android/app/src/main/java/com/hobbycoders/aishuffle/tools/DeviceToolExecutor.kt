package com.hobbycoders.aishuffle.tools

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.location.LocationManager
import android.net.Uri
import android.provider.AlarmClock
import android.provider.CalendarContract
import androidx.core.content.ContextCompat
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.time.Instant
import java.time.LocalDateTime
import java.time.ZoneId

/** Shared by the foreground command queue and realtime data-channel calls. */
class DeviceToolExecutor(context: Context) {
    private val app = context.applicationContext
    companion object { private val execution = Mutex() }
    suspend fun execute(name: String, args: JSONObject, deadline: Long = System.currentTimeMillis() + 20_000): JSONObject = execution.withLock {
        try {
            if (System.currentTimeMillis() >= deadline) return@withLock ToolRegistry.error("expired", "Command expired; no action was performed.")
            if (!ToolsPreferences(app).enabled) return@withLock ToolRegistry.error("disabled", "Android tools are disabled on this device.")
            if (name !in ToolRegistry.names) return@withLock ToolRegistry.error("unsupported", "Unsupported Android tool: $name")
            if (!DeviceToolsRuntime.hasForegroundActivity() && name !in setOf("read_screen", "tap_node", "scroll", "go_back")) return@withLock ToolRegistry.error("foreground_required", "Open AI Shuffle to use standard Android tools.")
            // This field NEVER grants authorization, even for legacy realtime callers.
            if (name in setOf("read_screen", "tap_node", "scroll", "go_back")) {
                return@withLock withContext(Dispatchers.Main) {
                    val service = ShuffleAccessibilityService.current
                        ?: return@withContext ToolRegistry.error("accessibility_disabled", "Enable AI Shuffle accessibility explicitly in Android settings first.")
                    service.execute(name, args, deadline)
                }
            }
            if (name == "get_location") return@withLock location(deadline)
            val intent = withContext(Dispatchers.IO) { intentFor(name, args) }
            if (name !in setOf("place_call", "send_sms", "create_calendar_event", "create_reminder")) {
                if (!ToolApprovals.request("Allow Android action?", "$name\n${args.toString().take(1000)}", deadline)) {
                    return@withLock ToolRegistry.error("denied", "Local approval was denied, dismissed or expired; no action was performed.")
                }
            }
            withContext(Dispatchers.Main) {
                if (System.currentTimeMillis() >= deadline) return@withContext ToolRegistry.error("expired", "Command expired; no action was performed.")
                if (!ToolsPreferences(app).enabled) return@withContext ToolRegistry.error("disabled", "Android tools were disabled.")
                DeviceToolsRuntime.launch(intent, deadline)
            }
        } catch (e: kotlinx.coroutines.CancellationException) { throw e }
        catch (e: IllegalArgumentException) { ToolRegistry.error("invalid_arguments", e.message ?: "Invalid arguments.") }
        catch (_: SecurityException) { ToolRegistry.error("permission_denied", "Android denied this action. Grant only the contextual permission in system settings if desired.") }
        catch (_: android.content.ActivityNotFoundException) { ToolRegistry.error("unavailable", "No installed app can handle this action.") }
        catch (_: Exception) { ToolRegistry.error("unavailable", "The Android action could not be completed.") }
    }

    internal fun intentFor(name: String, args: JSONObject): Intent = when (name) {
        "place_call", "send_sms" -> {
            val number = ToolRegistry.text(args, "number")
            require(number.matches(Regex("[+0-9() .-]{3,40}")) && number.any { it.isDigit() }) { "Use an explicit phone number; service codes are not allowed." }
            if (name == "place_call") Intent(Intent.ACTION_DIAL, Uri.fromParts("tel", number, null))
            else Intent(Intent.ACTION_SENDTO, Uri.fromParts("smsto", number, null)).putExtra("sms_body", ToolRegistry.text(args, "message"))
        }
        "open_url" -> {
            val uri = Uri.parse(ToolRegistry.text(args, "url"))
            require(uri.scheme in setOf("https", "http") && !uri.host.isNullOrBlank() && uri.userInfo == null) { "Only http(s) web URLs without credentials are supported." }
            Intent(Intent.ACTION_VIEW, uri)
        }
        "open_app" -> {
            val targetPackage = ToolRegistry.text(args, "package_name")
            val pm = app.packageManager
            val matches = pm.queryIntentActivities(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER), 0)
                .filter { it.activityInfo.packageName == targetPackage }
                .distinctBy { it.activityInfo.packageName }
            require(matches.size == 1) { "package_name must identify an installed launcher app." }
            pm.getLaunchIntentForPackage(matches.single().activityInfo.packageName)
                ?: throw IllegalArgumentException("App cannot be launched.")
        }
        "navigate_to" -> Intent(Intent.ACTION_VIEW, Uri.parse("geo:0,0?q=${Uri.encode(ToolRegistry.text(args, "destination"))}"))
        "set_alarm" -> Intent(AlarmClock.ACTION_SET_ALARM)
            .putExtra(AlarmClock.EXTRA_HOUR, ToolRegistry.integer(args, "hour", 0..23))
            .putExtra(AlarmClock.EXTRA_MINUTES, ToolRegistry.integer(args, "minute", 0..59))
            .putExtra(AlarmClock.EXTRA_MESSAGE, ToolRegistry.text(args, "label", true))
            .putExtra(AlarmClock.EXTRA_SKIP_UI, false)
        "set_timer" -> Intent(AlarmClock.ACTION_SET_TIMER)
            .putExtra(AlarmClock.EXTRA_LENGTH, ToolRegistry.integer(args, "seconds", 1..86400))
            .putExtra(AlarmClock.EXTRA_MESSAGE, ToolRegistry.text(args, "label", true))
            .putExtra(AlarmClock.EXTRA_SKIP_UI, false)
        "create_calendar_event", "create_reminder" -> {
            val start = parseTime(ToolRegistry.text(args, "start_time"))
            val endText = ToolRegistry.text(args, "end_time", true)
            val end = if (endText.isBlank()) start + 3_600_000 else parseTime(endText)
            require(end > start) { "end must be after start." }
            Intent(Intent.ACTION_INSERT, CalendarContract.Events.CONTENT_URI)
                .putExtra(CalendarContract.Events.TITLE, ToolRegistry.text(args, "title"))
                .putExtra(CalendarContract.EXTRA_EVENT_BEGIN_TIME, start)
                .putExtra(CalendarContract.EXTRA_EVENT_END_TIME, end)
                .putExtra(CalendarContract.Events.EVENT_LOCATION, ToolRegistry.text(args, "location", true))
                .putExtra(CalendarContract.Events.DESCRIPTION, ToolRegistry.text(args, "description", true))
        }
        else -> throw IllegalArgumentException("Unsupported intent tool.")
    }
    private fun parseTime(value: String): Long = try { java.time.OffsetDateTime.parse(value).toInstant().toEpochMilli() }
        catch (_: Exception) { throw IllegalArgumentException("Use an ISO8601 date-time with timezone.") }

    private suspend fun location(deadline: Long): JSONObject {
        val fine = ContextCompat.checkSelfPermission(app, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
        val coarse = ContextCompat.checkSelfPermission(app, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED
        if (!fine && !coarse) return ToolRegistry.error("permission_required", "Location is optional. Grant location while using the app, then retry.")
        if (!ToolApprovals.request("Share device location?", "Send a recent location to the connected AI Shuffle server for this action.", deadline))
            return ToolRegistry.error("denied", "Location sharing was denied, dismissed or expired.")
        return withContext(Dispatchers.IO) {
            if (System.currentTimeMillis() >= deadline || !ToolsPreferences(app).enabled) return@withContext ToolRegistry.error("expired", "Location request expired or was disabled.")
            val manager = app.getSystemService(Context.LOCATION_SERVICE) as LocationManager
            val location = manager.getProviders(true).mapNotNull { provider ->
                runCatching { manager.getLastKnownLocation(provider) }.getOrNull()
            }.filter { android.os.SystemClock.elapsedRealtimeNanos() - it.elapsedRealtimeNanos in 0..120_000_000_000L }
                .maxByOrNull { it.elapsedRealtimeNanos }
                ?: return@withContext ToolRegistry.error("location_unavailable", "No recent location is available. Open Maps to obtain a fix, then retry.")
            JSONObject().put("ok", true).put("latitude", location.latitude).put("longitude", location.longitude)
                .put("accuracy_m", location.accuracy.toDouble()).put("timestamp", location.time).put("source", "recent_cached_location")
        }
    }
}
