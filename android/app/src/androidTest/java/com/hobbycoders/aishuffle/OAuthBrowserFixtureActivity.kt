package com.hobbycoders.aishuffle

/** Only lets AppAuth discover a browser in instrumentation; it never handles real sign-in. */
class OAuthBrowserFixtureActivity : android.app.Activity() {
    override fun onCreate(savedInstanceState: android.os.Bundle?) {
        super.onCreate(savedInstanceState)
        finish()
    }
}
