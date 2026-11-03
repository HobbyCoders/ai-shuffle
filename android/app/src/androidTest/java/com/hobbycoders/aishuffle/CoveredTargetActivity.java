package com.hobbycoders.aishuffle;

/** A separate test-APK screen whose controls sit under the consent panel and can really change on command. */
public class CoveredTargetActivity extends android.app.Activity {
    static final String COMMAND = "com.hobbycoders.aishuffle.test.COVERED_TARGET";
    static final String RESTING = "Taps: 0";
    private static final int TOP = 240, MOVED_TOP = 440, BLIP_MS = 150, TEXT_BLIP_MS = 400;
    private int taps = 0;
    private android.widget.LinearLayout layout, tracks, rows;
    private android.widget.TextView status;
    /** Every dense row takes this long to answer each accessibility read, and the last row this long on top. */
    private volatile int rowDelay = 0, lastRowDelay = 0;
    /** The target answers no read until this uptime, and takes this long over its next action. */
    private volatile long stallReadsUntil = 0;
    private volatile int stallAction = 0;
    private int noiseEvery = 0;
    /** An accessibility event with nothing changed, as a busy app sends. */
    private void sendNoise() { status.sendAccessibilityEvent(android.view.accessibility.AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED); }
    private final Runnable noise = new Runnable() {
        @Override public void run() { sendNoise(); if (noiseEvery > 0) status.postDelayed(this, noiseEvery); }
    };
    /** Replies only once the change has been laid out, so the sender knows accessibility can see it. */
    private final android.content.BroadcastReceiver commands = new android.content.BroadcastReceiver() {
        @Override public void onReceive(android.content.Context context, android.content.Intent intent) {
            PendingResult reply = goAsync();
            String command = intent.getStringExtra("command");
            String[] parts = command.split(":");
            int value = parts.length > 1 ? Integer.parseInt(parts[1]) : 0;
            switch (parts[0]) {
                case "text": status.setText("Different content"); break;
                case "textblip": status.setText("Different content"); status.postDelayed(() -> { if ("Different content".contentEquals(status.getText())) status.setText(RESTING); }, TEXT_BLIP_MS); break;
                case "back": layout.setPadding(32, TOP, 32, 32); break;
                case "swap": swapTracks(); break;
                case "replace": replaceFirstTrack(); break;
                case "dense": addRows(value); break;
                case "delay": rowDelay = value; break;
                case "hang": lastRowDelay = value; break;
                case "notify": sendNoise(); break;
                case "noisy": noiseEvery = value; status.removeCallbacks(noise); if (value > 0) noise.run(); break;
                case "stallread": stallReadsUntil = android.os.SystemClock.uptimeMillis() + value; break;
                case "stallaction": stallAction = value; break;
                default: layout.setPadding(32, MOVED_TOP, 32, 32);
            }
            layout.getViewTreeObserver().addOnPreDrawListener(new android.view.ViewTreeObserver.OnPreDrawListener() {
                @Override public boolean onPreDraw() {
                    layout.getViewTreeObserver().removeOnPreDrawListener(this);
                    if ("blip".equals(command)) layout.postDelayed(() -> layout.setPadding(32, TOP, 32, 32), BLIP_MS);
                    reply.setResultCode(RESULT_OK); reply.finish();
                    return true;
                }
            });
            layout.invalidate();
        }
    };
    private android.widget.Button track(String text) {
        android.widget.Button button = new android.widget.Button(this);
        button.setText(text); button.setAllCaps(false);
        button.setOnClickListener(v -> status.setText("Tapped: " + ((android.widget.Button) v).getText()));
        return button;
    }
    /** The screen reads exactly as it did, but each view now shows the other item, as a recycled list row would. */
    private void swapTracks() {
        android.widget.Button first = (android.widget.Button) tracks.getChildAt(0), second = (android.widget.Button) tracks.getChildAt(1);
        tracks.removeAllViews();
        CharSequence label = first.getText(); first.setText(second.getText()); second.setText(label);
        tracks.addView(second); tracks.addView(first);
    }
    /** The screen reads exactly as it did, from a view that was not there when it was approved. */
    private void replaceFirstTrack() {
        CharSequence label = ((android.widget.Button) tracks.getChildAt(0)).getText();
        tracks.removeViewAt(0);
        tracks.addView(track(label.toString()), 0);
    }
    private void addRows(int count) {
        for (int i = 0; i < count; i++) {
            android.widget.TextView row = new android.widget.TextView(this);
            row.setText("Row " + i);
            final boolean last = i == count - 1;
            row.setAccessibilityDelegate(new android.view.View.AccessibilityDelegate() {
                @Override public void onInitializeAccessibilityNodeInfo(android.view.View host, android.view.accessibility.AccessibilityNodeInfo info) {
                    super.onInitializeAccessibilityNodeInfo(host, info);
                    android.os.SystemClock.sleep(rowDelay + (last ? lastRowDelay : 0));
                }
            });
            rows.addView(row, new android.widget.LinearLayout.LayoutParams(-1, 1));
        }
    }
    @Override public void onCreate(android.os.Bundle state) {
        super.onCreate(state);
        layout = new android.widget.LinearLayout(this);
        layout.setOrientation(android.widget.LinearLayout.VERTICAL);
        layout.setPadding(32, TOP, 32, 32);
        // A fixed size: changing this text must not also move anything.
        status = new android.widget.TextView(this);
        status.setText("Opening"); status.setTextSize(24);
        layout.addView(status, new android.widget.LinearLayout.LayoutParams(-1, 120));
        android.widget.Button target = new android.widget.Button(this);
        target.setText("Covered target"); target.setAllCaps(false);
        target.setOnClickListener(v -> status.setText("Taps: " + ++taps));
        target.setAccessibilityDelegate(new android.view.View.AccessibilityDelegate() {
            @Override public void onInitializeAccessibilityNodeInfo(android.view.View host, android.view.accessibility.AccessibilityNodeInfo info) {
                super.onInitializeAccessibilityNodeInfo(host, info);
                android.os.SystemClock.sleep(Math.max(0, stallReadsUntil - android.os.SystemClock.uptimeMillis()));
            }
            @Override public boolean performAccessibilityAction(android.view.View host, int action, android.os.Bundle args) {
                int stall = stallAction; stallAction = 0;
                android.os.SystemClock.sleep(stall);
                return super.performAccessibilityAction(host, action, args);
            }
        });
        layout.addView(target);
        tracks = new android.widget.LinearLayout(this);
        tracks.setOrientation(android.widget.LinearLayout.VERTICAL);
        tracks.addView(track("Track One")); tracks.addView(track("Track Two"));
        layout.addView(tracks);
        android.widget.Space below = new android.widget.Space(this);
        layout.addView(below, new android.widget.LinearLayout.LayoutParams(-1, 550));
        android.widget.Button ready = new android.widget.Button(this);
        ready.setText("Get ready"); ready.setAllCaps(false);
        ready.setOnClickListener(v -> status.setText(RESTING));
        layout.addView(ready);
        rows = new android.widget.LinearLayout(this);
        rows.setOrientation(android.widget.LinearLayout.VERTICAL);
        layout.addView(rows);
        setContentView(layout);
        registerReceiver(commands, new android.content.IntentFilter(COMMAND), RECEIVER_EXPORTED);
    }
    @Override public void onDestroy() { unregisterReceiver(commands); super.onDestroy(); }
}
