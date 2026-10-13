package com.hobbycoders.aishuffle

import org.junit.Assert.*
import org.junit.Test
import java.io.File

/**
 * The app name, launcher icon and store listing carry only the product's own
 * brand. Third-party AI vendor names or marks must not appear in any of them.
 * Runs from the app module directory, as Gradle unit tests do.
 */
class StoreBrandingTest {
    private val forbidden = Regex("claude|anthropic", RegexOption.IGNORE_CASE)
    private val listing = File("../play/listing/en-US")

    @Test fun appNameLabelsAndStringsNameNoVendor() {
        val strings = File("src/main/res/values/strings.xml").readText()
        assertTrue(strings.contains("<string name=\"app_name\">AI Shuffle</string>"))
        assertNull(forbidden.find(strings)?.value)
        val manifest = File("src/main/AndroidManifest.xml").readText()
        for (label in Regex("android:label=\"([^\"]*)\"").findAll(manifest).map { it.groupValues[1] }) {
            assertNull(label, forbidden.find(label)?.value)
        }
    }

    @Test fun iconsAndResourcesNameNoVendor() {
        val resources = File("src/main/res").walkTopDown().filter { it.isFile }.toList()
        assertTrue(resources.any { it.name == "ic_launcher_foreground.png" })
        for (file in resources) {
            assertNull(file.path, forbidden.find(file.name)?.value)
            if (file.extension == "xml") assertNull(file.path, forbidden.find(file.readText())?.value)
        }
    }

    @Test fun storeListingNamesNoVendorAndFitsPlayLimits() {
        val title = File(listing, "title.txt").readText().trim()
        val short = File(listing, "short-description.txt").readText().trim()
        val full = File(listing, "full-description.txt").readText().trim()
        assertEquals("AI Shuffle", title)
        assertTrue("title is at most 30 characters", title.length <= 30)
        assertTrue("short description is at most 80 characters (${short.length})", short.length <= 80)
        assertTrue("full description is at most 4000 characters", full.length <= 4000)
        for (text in listOf(title, short, full)) assertNull(forbidden.find(text)?.value)
        assertTrue("the listing points at the public deletion page", full.contains("https://hobbycoders.com/delete-account"))
    }
}
