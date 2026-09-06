package com.hobbycoders.aishuffle.tools

import org.junit.Assert.*
import org.junit.Test

class AccessibilityPolicyTest {
    @Test fun securityAndCredentialPackagesAreNotSupported() {
        listOf("com.android.settings", "com.android.systemui", "com.google.android.permissioncontroller",
            "com.android.chrome", "com.paypal.android.p2pmobile", "com.onepassword.android", "com.example.bank")
            .forEach { assertFalse(it, AppControlPolicy.permitsName(it, it)) }
    }
    @Test fun sensitiveScreensInsideSupportedAppsAreBlocked() {
        listOf("Sign in", "Password", "Payment method", "Allow access", "Verify your identity", "Checkout", "Security")
            .forEach { assertTrue(it, ShuffleAccessibilityService.sensitive.containsMatchIn(it)) }
    }
}
