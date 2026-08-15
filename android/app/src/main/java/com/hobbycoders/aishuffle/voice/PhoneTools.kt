package com.hobbycoders.aishuffle.voice

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.AlarmClock
import android.provider.CalendarContract
import android.provider.ContactsContract
import android.provider.Telephony
import android.telephony.SmsManager
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * The device-action tools the voice agent can call. Definitions go up to the
 * realtime session (via the server's sideband `session.update`); execution
 * happens HERE, on the phone, when a `function_call` event for one of these
 * names arrives on the app's data channel.
 *
 * Safety model: only `send_sms` is confirm-gated — its body is transcribed
 * speech, so the first call without `confirmed:true` returns a
 * `needs_confirmation` preview the model must read back and get a spoken yes
 * for. The gate is enforced in code, not just in the prompt. Everything else
 * (including calls — user decision, 2026-08-15) executes immediately;
 * ambiguous contact matches still come back as errors for the model to
 * resolve by asking.
 */
/** How the voice session was started; changes the agent's contract. */
enum class SessionMode {
    /** Wake word, hands-free: do the one thing, then hang up via end_session. */
    COMMAND,

    /** Button / tile / assist gesture: converse until the user ends it. */
    CHAT,
}

/**
 * A tool execution's result plus its session side-effect: some tools (ending
 * the session, placing a real phone call that needs the mic) must also end
 * the realtime call once their result has been spoken.
 */
data class ToolOutcome(
    val result: JSONObject,
    val endSession: Boolean = false,
    /** How long to let the model's spoken wrap-up play before hanging up. */
    val endDelayMs: Long = 2500,
)

class PhoneTools(private val context: Context) {

    // ------------------------------------------------------------------
    // Declarations
    // ------------------------------------------------------------------

    companion object {
        private val DEVICE_TOOLS_TEXT = """
            You are also the user's hands-free phone assistant, speaking to them through
            their Android phone. Besides dispatching worker agents, you can act on the
            phone itself with the device tools (find_contact, place_call, send_sms,
            read_messages, read_notifications, open_app, open_url, navigate_to,
            set_alarm, set_timer, get_calendar_events, create_calendar_event,
            end_session).
            Execute actions immediately without asking for confirmation — EXCEPT
            send_sms: because the message body is transcribed speech, first call it
            without confirmed=true, read the message back in one short sentence, and
            send with confirmed=true only after a spoken yes. If a contact lookup is
            ambiguous the tool will say so — ask which one, briefly. If a tool reports
            a missing permission, say so in a few words and move on.
            NEVER speak first: after the session starts, stay completely silent until
            the user says something. Keep every reply to ONE short sentence — no
            greetings, no explanations of what you are doing, no "anything else".
        """.trimIndent().replace("\n", " ")

        private val COMMAND_MODE_TEXT = """
            IMPORTANT: this session was started hands-free by wake word — the user may
            be driving and cannot touch the phone. Handle their request quickly, by
            voice alone. As soon as the task is done (or clearly impossible), say a
            very short confirmation and call end_session. Do not ask "anything else?"
            and do not keep the session open. If the user is silent, call end_session.
        """.trimIndent().replace("\n", " ")

        private val CHAT_MODE_TEXT = """
            When the user says goodbye, "that's all", or asks you to hang up, say a
            short goodbye and call end_session.
        """.trimIndent().replace("\n", " ")

        /** Instructions appendix for the given launch mode. */
        fun instructionsFor(mode: SessionMode): String = when (mode) {
            SessionMode.COMMAND -> "$DEVICE_TOOLS_TEXT $COMMAND_MODE_TEXT"
            SessionMode.CHAT -> "$DEVICE_TOOLS_TEXT $CHAT_MODE_TEXT"
        }

        private const val MAX_RESULTS = 10
    }

    /** Realtime function-tool declarations (flat schema, not Chat-Completions nesting). */
    fun declarations(): JSONArray {
        val tools = JSONArray()

        fun tool(name: String, description: String, params: JSONObject.() -> Unit) {
            val properties = JSONObject().apply(params)
            tools.put(JSONObject().apply {
                put("type", "function")
                put("name", name)
                put("description", description)
                put("parameters", JSONObject().apply {
                    put("type", "object")
                    put("properties", properties)
                })
            })
        }

        fun JSONObject.str(name: String, desc: String) =
            put(name, JSONObject().put("type", "string").put("description", desc))

        fun JSONObject.int(name: String, desc: String) =
            put(name, JSONObject().put("type", "integer").put("description", desc))

        fun JSONObject.bool(name: String, desc: String) =
            put(name, JSONObject().put("type", "boolean").put("description", desc))

        tool("find_contact", "Look up a contact by name and get their phone numbers.") {
            str("name", "Full or partial contact name to search for.")
        }
        tool(
            "place_call",
            "Place a phone call immediately. No confirmation needed — dial as soon " +
                "as the contact resolves.",
        ) {
            str("contact", "Contact name to call (resolved via contacts).")
            str("number", "Explicit phone number to call (use instead of contact).")
        }
        tool(
            "send_sms",
            "Send a text message. Confirm-gated: call once without confirmed to get a " +
                "preview to read back, then again with confirmed=true after the user " +
                "says yes.",
        ) {
            str("contact", "Contact name to text (resolved via contacts).")
            str("number", "Explicit phone number to text (use instead of contact).")
            str("message", "The message body to send.")
            bool("confirmed", "true only after the user verbally confirmed the text.")
        }
        tool("read_messages", "Read the most recent SMS messages in the inbox.") {
            int("limit", "How many messages, default 5, max $MAX_RESULTS.")
        }
        tool(
            "read_notifications",
            "Read the notifications currently in the status bar (messages, emails, " +
                "app alerts). Use when asked what's new or what they missed.",
        ) {}
        tool("open_app", "Open an installed app by its name.") {
            str("app_name", "The app's name as shown on the launcher, e.g. 'Spotify'.")
        }
        tool("open_url", "Open a web page in the browser.") {
            str("url", "The URL to open.")
        }
        tool("navigate_to", "Start turn-by-turn navigation in the maps app.") {
            str("destination", "Address or place name to navigate to.")
            str("mode", "d for driving (default), w for walking, b for bicycling.")
        }
        tool("set_alarm", "Set an alarm at a given time.") {
            int("hour", "Hour of day, 0-23.")
            int("minute", "Minute, 0-59.")
            str("label", "Optional alarm label.")
        }
        tool("set_timer", "Start a countdown timer.") {
            int("seconds", "Timer length in seconds.")
            str("label", "Optional timer label.")
        }
        tool("get_calendar_events", "List upcoming calendar events.") {
            int("days", "How many days ahead to look, default 7.")
        }
        tool("create_calendar_event", "Create a calendar event immediately.") {
            str("title", "Event title.")
            str("start", "Start date-time, ISO format like 2026-08-20T14:00.")
            str("end", "Optional end date-time, ISO format. Defaults to one hour after start.")
            str("location", "Optional location.")
        }
        tool(
            "end_session",
            "End this voice session. Call it when the task is done, when the user " +
                "says goodbye, or when they go silent. Say your short wrap-up first.",
        ) {}
        return tools
    }

    /** Whether [name] is one of this registry's tools. */
    fun owns(name: String): Boolean = name in setOf(
        "find_contact", "place_call", "send_sms", "read_messages", "read_notifications",
        "open_app", "open_url", "navigate_to", "set_alarm", "set_timer",
        "get_calendar_events", "create_calendar_event", "end_session",
    )

    // ------------------------------------------------------------------
    // Execution
    // ------------------------------------------------------------------

    /** Execute [name] with [args]; never throws — errors come back as JSON. */
    fun execute(name: String, args: JSONObject): ToolOutcome = try {
        when (name) {
            "find_contact" -> ToolOutcome(findContact(args.optString("name")))
            "place_call" -> placeCall(args)
            "send_sms" -> ToolOutcome(sendSms(args))
            "read_messages" ->
                ToolOutcome(readMessages(args.optInt("limit", 5).coerceIn(1, MAX_RESULTS)))
            "read_notifications" -> ToolOutcome(readNotifications())
            "open_app" -> ToolOutcome(openApp(args.optString("app_name")))
            "open_url" -> ToolOutcome(openUrl(args.optString("url")))
            "navigate_to" ->
                ToolOutcome(navigateTo(args.optString("destination"), args.optString("mode", "d")))
            "set_alarm" -> ToolOutcome(setAlarm(args))
            "set_timer" -> ToolOutcome(setTimer(args))
            "get_calendar_events" ->
                ToolOutcome(getCalendarEvents(args.optInt("days", 7).coerceIn(1, 31)))
            "create_calendar_event" -> ToolOutcome(createCalendarEvent(args))
            "end_session" -> ToolOutcome(
                ok("message" to "Session ending."),
                endSession = true,
            )
            else -> ToolOutcome(fail("Unknown device tool '$name'."))
        }
    } catch (e: SecurityException) {
        ToolOutcome(fail("Missing an Android permission for $name: ${e.message}"))
    } catch (e: Exception) {
        ToolOutcome(fail("$name failed: ${e.message ?: e.javaClass.simpleName}"))
    }

    private fun ok(vararg fields: Pair<String, Any?>): JSONObject =
        JSONObject().put("ok", true).apply { fields.forEach { (k, v) -> put(k, v) } }

    private fun fail(message: String): JSONObject =
        JSONObject().put("ok", false).put("error", message)

    private fun needsConfirmation(preview: String): JSONObject = JSONObject()
        .put("ok", false)
        .put("status", "needs_confirmation")
        .put("preview", preview)
        .put(
            "instruction",
            "Read the preview back to the user in one short sentence and ask for a yes. " +
                "Only if they explicitly say yes, call the same tool again with " +
                "confirmed=true and the same arguments.",
        )

    private fun granted(permission: String): Boolean =
        ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED

    private fun requirePermission(permission: String, friendly: String): JSONObject? =
        if (granted(permission)) null
        else fail(
            "The $friendly permission has not been granted. Tell the user to enable it " +
                "in the AI Shuffle assistant settings.",
        )

    // -- contacts ------------------------------------------------------

    private data class ContactMatch(val name: String, val numbers: List<String>)

    private fun queryContacts(name: String): List<ContactMatch> {
        val matches = linkedMapOf<String, MutableList<String>>()
        context.contentResolver.query(
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
            arrayOf(
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
                ContactsContract.CommonDataKinds.Phone.NUMBER,
            ),
            "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} LIKE ?",
            arrayOf("%$name%"),
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
        )?.use { cursor ->
            while (cursor.moveToNext() && matches.size <= MAX_RESULTS) {
                val display = cursor.getString(0) ?: continue
                val number = cursor.getString(1) ?: continue
                matches.getOrPut(display) { mutableListOf() }.add(number)
            }
        }
        return matches.map { (n, nums) -> ContactMatch(n, nums.distinct()) }
    }

    private fun findContact(name: String): JSONObject {
        if (name.isBlank()) return fail("A name to search for is required.")
        requirePermission(Manifest.permission.READ_CONTACTS, "contacts")?.let { return it }
        val matches = queryContacts(name)
        if (matches.isEmpty()) return fail("No contact matching '$name' was found.")
        val arr = JSONArray()
        matches.forEach { m ->
            arr.put(JSONObject().put("name", m.name).put("numbers", JSONArray(m.numbers)))
        }
        return ok("contacts" to arr)
    }

    /**
     * Resolve the target of a call/text: an explicit number wins; otherwise the
     * contact name must match exactly one contact (ambiguity comes back as an
     * error listing the options so the model can ask the user).
     */
    private fun resolveTarget(args: JSONObject): Pair<String, String>? {
        val number = args.optString("number").trim()
        if (number.isNotBlank()) return number to (args.optString("contact").trim().ifBlank { number })
        val contact = args.optString("contact").trim()
        if (contact.isBlank()) return null
        val matches = queryContacts(contact)
        val exact = matches.firstOrNull { it.name.equals(contact, ignoreCase = true) }
            ?: matches.singleOrNull()
            ?: return null
        return exact.numbers.first() to exact.name
    }

    /**
     * Placing a real phone call must ALSO end the assistant session right away:
     * the dialer needs the microphone, and the user called to talk to a human,
     * not to keep the agent on the line.
     */
    private fun placeCall(args: JSONObject): ToolOutcome {
        requirePermission(Manifest.permission.CALL_PHONE, "phone call")
            ?.let { return ToolOutcome(it) }
        if (args.has("contact") && !args.optString("contact").isNullOrBlank()) {
            requirePermission(Manifest.permission.READ_CONTACTS, "contacts")
                ?.let { return ToolOutcome(it) }
        }
        val target = resolveTarget(args)
            ?: return ToolOutcome(
                fail("Couldn't resolve who to call. Use find_contact first or provide a number."),
            )
        val (number, label) = target
        val intent = Intent(Intent.ACTION_CALL, Uri.parse("tel:$number"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
        return ToolOutcome(
            ok("message" to "Calling $label now. The assistant session is ending."),
            endSession = true,
            endDelayMs = 1200, // the dialer takes the mic — get out of its way
        )
    }

    private fun sendSms(args: JSONObject): JSONObject {
        requirePermission(Manifest.permission.SEND_SMS, "send SMS")?.let { return it }
        val message = args.optString("message").trim()
        if (message.isBlank()) return fail("A message body is required.")
        if (args.has("contact") && !args.optString("contact").isNullOrBlank()) {
            requirePermission(Manifest.permission.READ_CONTACTS, "contacts")?.let { return it }
        }
        val target = resolveTarget(args)
            ?: return fail(
                "Couldn't resolve who to text. Use find_contact first or provide a number.",
            )
        val (number, label) = target
        if (!args.optBoolean("confirmed", false)) {
            return needsConfirmation("Text $label: \"$message\"")
        }
        @Suppress("DEPRECATION")
        val sms = SmsManager.getDefault()
        val parts = sms.divideMessage(message)
        sms.sendMultipartTextMessage(number, null, parts, null, null)
        return ok("message" to "Text sent to $label.")
    }

    private fun readMessages(limit: Int): JSONObject {
        requirePermission(Manifest.permission.READ_SMS, "read SMS")?.let { return it }
        val canResolveNames = granted(Manifest.permission.READ_CONTACTS)
        val arr = JSONArray()
        context.contentResolver.query(
            Telephony.Sms.Inbox.CONTENT_URI,
            arrayOf(Telephony.Sms.ADDRESS, Telephony.Sms.BODY, Telephony.Sms.DATE),
            null, null,
            "${Telephony.Sms.DATE} DESC",
        )?.use { cursor ->
            while (cursor.moveToNext() && arr.length() < limit) {
                val address = cursor.getString(0).orEmpty()
                val sender = if (canResolveNames) contactNameFor(address) ?: address else address
                arr.put(JSONObject().apply {
                    put("from", sender)
                    put("message", cursor.getString(1).orEmpty().take(500))
                    put("when", formatTime(cursor.getLong(2)))
                })
            }
        }
        return ok("messages" to arr)
    }

    private fun contactNameFor(number: String): String? {
        val uri = Uri.withAppendedPath(
            ContactsContract.PhoneLookup.CONTENT_FILTER_URI, Uri.encode(number),
        )
        context.contentResolver.query(
            uri, arrayOf(ContactsContract.PhoneLookup.DISPLAY_NAME), null, null, null,
        )?.use { cursor ->
            if (cursor.moveToFirst()) return cursor.getString(0)
        }
        return null
    }

    // -- notifications -------------------------------------------------

    private fun readNotifications(): JSONObject {
        val snapshot = AssistNotificationListener.snapshot()
            ?: return fail(
                "Notification access is not enabled. Tell the user to grant it in the " +
                    "AI Shuffle assistant settings.",
            )
        return ok("notifications" to snapshot)
    }

    // -- apps / links --------------------------------------------------

    private fun openApp(appName: String): JSONObject {
        if (appName.isBlank()) return fail("An app name is required.")
        val pm = context.packageManager
        val launcher = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val installed = pm.queryIntentActivities(launcher, 0)
        val match = installed.firstOrNull {
            it.loadLabel(pm).toString().equals(appName, ignoreCase = true)
        } ?: installed.firstOrNull {
            it.loadLabel(pm).toString().contains(appName, ignoreCase = true)
        } ?: return fail("No installed app matching '$appName' was found.")
        val launch = pm.getLaunchIntentForPackage(match.activityInfo.packageName)
            ?: return fail("'${match.loadLabel(pm)}' can't be launched.")
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(launch)
        return ok("message" to "Opened ${match.loadLabel(pm)}.")
    }

    private fun openUrl(url: String): JSONObject {
        if (url.isBlank()) return fail("A URL is required.")
        val withScheme = if (url.contains("://")) url else "https://$url"
        context.startActivity(
            Intent(Intent.ACTION_VIEW, Uri.parse(withScheme))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
        return ok("message" to "Opened $withScheme.")
    }

    private fun navigateTo(destination: String, mode: String): JSONObject {
        if (destination.isBlank()) return fail("A destination is required.")
        val safeMode = if (mode in listOf("d", "w", "b")) mode else "d"
        val gmm = Intent(
            Intent.ACTION_VIEW,
            Uri.parse("google.navigation:q=${Uri.encode(destination)}&mode=$safeMode"),
        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (gmm.resolveActivity(context.packageManager) != null) {
            context.startActivity(gmm)
        } else {
            context.startActivity(
                Intent(Intent.ACTION_VIEW, Uri.parse("geo:0,0?q=${Uri.encode(destination)}"))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
        }
        return ok("message" to "Navigation to $destination started.")
    }

    // -- alarms / timers ----------------------------------------------

    private fun setAlarm(args: JSONObject): JSONObject {
        val hour = args.optInt("hour", -1)
        val minute = args.optInt("minute", 0)
        if (hour !in 0..23 || minute !in 0..59) return fail("A valid hour (0-23) is required.")
        val intent = Intent(AlarmClock.ACTION_SET_ALARM).apply {
            putExtra(AlarmClock.EXTRA_HOUR, hour)
            putExtra(AlarmClock.EXTRA_MINUTES, minute)
            args.optString("label").takeIf { it.isNotBlank() }
                ?.let { putExtra(AlarmClock.EXTRA_MESSAGE, it) }
            putExtra(AlarmClock.EXTRA_SKIP_UI, true)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        if (intent.resolveActivity(context.packageManager) == null) {
            return fail("No clock app that can set alarms was found.")
        }
        context.startActivity(intent)
        return ok("message" to "Alarm set for %d:%02d.".format(hour, minute))
    }

    private fun setTimer(args: JSONObject): JSONObject {
        val seconds = args.optInt("seconds", 0)
        if (seconds <= 0) return fail("A timer length in seconds is required.")
        val intent = Intent(AlarmClock.ACTION_SET_TIMER).apply {
            putExtra(AlarmClock.EXTRA_LENGTH, seconds)
            args.optString("label").takeIf { it.isNotBlank() }
                ?.let { putExtra(AlarmClock.EXTRA_MESSAGE, it) }
            putExtra(AlarmClock.EXTRA_SKIP_UI, true)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        if (intent.resolveActivity(context.packageManager) == null) {
            return fail("No clock app that can set timers was found.")
        }
        context.startActivity(intent)
        return ok("message" to "Timer started for $seconds seconds.")
    }

    // -- calendar ------------------------------------------------------

    private fun getCalendarEvents(days: Int): JSONObject {
        requirePermission(Manifest.permission.READ_CALENDAR, "calendar")?.let { return it }
        val now = System.currentTimeMillis()
        val until = now + days * 24L * 60 * 60 * 1000
        val builder = CalendarContract.Instances.CONTENT_URI.buildUpon()
        android.content.ContentUris.appendId(builder, now)
        android.content.ContentUris.appendId(builder, until)
        val arr = JSONArray()
        context.contentResolver.query(
            builder.build(),
            arrayOf(
                CalendarContract.Instances.TITLE,
                CalendarContract.Instances.BEGIN,
                CalendarContract.Instances.END,
                CalendarContract.Instances.EVENT_LOCATION,
            ),
            null, null,
            "${CalendarContract.Instances.BEGIN} ASC",
        )?.use { cursor ->
            while (cursor.moveToNext() && arr.length() < MAX_RESULTS * 2) {
                arr.put(JSONObject().apply {
                    put("title", cursor.getString(0).orEmpty())
                    put("start", formatTime(cursor.getLong(1)))
                    put("end", formatTime(cursor.getLong(2)))
                    cursor.getString(3)?.takeIf { it.isNotBlank() }?.let { put("location", it) }
                })
            }
        }
        return ok("events" to arr, "range_days" to days)
    }

    private fun createCalendarEvent(args: JSONObject): JSONObject {
        requirePermission(Manifest.permission.WRITE_CALENDAR, "calendar")?.let { return it }
        val title = args.optString("title").trim()
        if (title.isBlank()) return fail("An event title is required.")
        val start = parseLocalDateTime(args.optString("start"))
            ?: return fail("A valid start like 2026-08-20T14:00 is required.")
        val end = parseLocalDateTime(args.optString("end")) ?: start.plusHours(1)
        val location = args.optString("location").trim()

        val calendarId = primaryCalendarId()
            ?: return fail("No writable calendar was found on this device.")
        val zone = ZoneId.systemDefault()
        val values = android.content.ContentValues().apply {
            put(CalendarContract.Events.CALENDAR_ID, calendarId)
            put(CalendarContract.Events.TITLE, title)
            put(CalendarContract.Events.DTSTART, start.atZone(zone).toInstant().toEpochMilli())
            put(CalendarContract.Events.DTEND, end.atZone(zone).toInstant().toEpochMilli())
            put(CalendarContract.Events.EVENT_TIMEZONE, zone.id)
            if (location.isNotBlank()) put(CalendarContract.Events.EVENT_LOCATION, location)
        }
        context.contentResolver.insert(CalendarContract.Events.CONTENT_URI, values)
            ?: return fail("The calendar refused the event.")
        return ok("message" to "Event \"$title\" created.")
    }

    private fun primaryCalendarId(): Long? {
        context.contentResolver.query(
            CalendarContract.Calendars.CONTENT_URI,
            arrayOf(
                CalendarContract.Calendars._ID,
                CalendarContract.Calendars.IS_PRIMARY,
                CalendarContract.Calendars.CALENDAR_ACCESS_LEVEL,
            ),
            null, null, null,
        )?.use { cursor ->
            var fallback: Long? = null
            while (cursor.moveToNext()) {
                val id = cursor.getLong(0)
                val primary = cursor.getInt(1) == 1
                val access = cursor.getInt(2)
                if (access >= CalendarContract.Calendars.CAL_ACCESS_CONTRIBUTOR) {
                    if (primary) return id
                    if (fallback == null) fallback = id
                }
            }
            return fallback
        }
        return null
    }

    // -- helpers -------------------------------------------------------

    private fun parseLocalDateTime(raw: String?): LocalDateTime? {
        val text = raw?.trim().orEmpty()
        if (text.isBlank()) return null
        return runCatching { LocalDateTime.parse(text) }.getOrNull()
    }

    private fun formatTime(epochMillis: Long): String =
        Instant.ofEpochMilli(epochMillis)
            .atZone(ZoneId.systemDefault())
            .format(DateTimeFormatter.ofPattern("EEE MMM d h:mm a"))
}
