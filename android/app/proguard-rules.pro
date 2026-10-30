# Release uses proguard-android-optimize.txt and dependency consumer rules.
# Do not keep whole app/library packages: that disables useful obfuscation.

# WebView discovers these methods by annotation/name, not a Java call site.
# Chat currently uses AndroidX WebMessageListener callbacks (no reflective
# JavascriptInterface); keep the annotation contract for any annotated bridge.
-keepclassmembers,allowoptimization class * {
    @android.webkit.JavascriptInterface <methods>;
}

# AGP deduplicates AndroidJUnitRunner's tracing dependency against the app.
# Keep this single public API available in both release and releaseSmoke so
# instrumentation can start without adding test-only app/bridge keep rules.
-keep,allowoptimization class androidx.tracing.Trace {
    public static <methods>;
}

# Framework parcel deserialization looks up CREATOR by name. AGP's defaults
# also cover Parcelable, manifest components and XML-inflated view constructors.
-keepclassmembers class * implements android.os.Parcelable {
    public static final android.os.Parcelable$Creator CREATOR;
}

# Java serialization hooks are invoked reflectively (including library types).
-keepclassmembers class * implements java.io.Serializable {
    static final long serialVersionUID;
    private void writeObject(java.io.ObjectOutputStream);
    private void readObject(java.io.ObjectInputStream);
    java.lang.Object writeReplace();
    java.lang.Object readResolve();
}

# Audit of the current dependencies/code:
# - Firebase Messaging, AppAuth, accessibility and foreground services are rooted
#   by their merged manifest declarations / AGP generated aapt rules.
# - AppAuth and our stored models explicitly read/write org.json: no Gson/Moshi,
#   reflective field serialization or persisted enum names need blanket keeps.
#   VoiceCallCommand uses explicit wire strings, not enum.name.
# - OkHttp 4.12 bundles its PublicSuffixDatabase resource/name and optional TLS
#   provider rules; Okio 3.6 needs no additional reflection rules.
# - ZXing views/constructors come from aapt's XML rules; decoding is direct calls.
# - Coroutines 1.7.3 bundles version-specific R8 rules for atomic updaters and
#   Android dispatcher/service-loader optimization. Do not duplicate legacy keeps.
# Missing-class warnings must be investigated, never globally suppressed.
