package com.hobbycoders.aishuffle.tools

import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.net.Uri

/** An app must be selected locally AND pass this deny policy. Selection is not authorization. */
internal object AppControlPolicy {
    private val blocked = Regex("bank|payment|wallet|paypal|credential|password|authenticator|permission|keyguard|systemui|security|settings|launcher|chrome|firefox|browser|brave|edge|opera|bitwarden|onepassword|1password|lastpass|keeper|dashlane|revolut|venmo|cashapp|chase|monzo|wise|coinbase|binance", RegexOption.IGNORE_CASE)
    fun permitsName(pkg: String, label: String): Boolean = !blocked.containsMatchIn("$pkg $label")
    fun candidates(context: Context): Map<String, String> {
        val pm = context.packageManager
        val browsers = pm.queryIntentActivities(Intent(Intent.ACTION_VIEW, Uri.parse("https://example.com")), 0).map { it.activityInfo.packageName }.toSet()
        return pm.queryIntentActivities(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER), 0)
            .filter { it.activityInfo.packageName != context.packageName && it.activityInfo.packageName !in browsers &&
                it.activityInfo.applicationInfo.flags and ApplicationInfo.FLAG_SYSTEM == 0 &&
                permitsName(it.activityInfo.packageName, it.loadLabel(pm).toString()) }
            .associate { it.activityInfo.packageName to it.loadLabel(pm).toString() }.toSortedMap()
    }
}
