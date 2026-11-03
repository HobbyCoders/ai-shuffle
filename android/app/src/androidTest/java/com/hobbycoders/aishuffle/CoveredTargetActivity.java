package com.hobbycoders.aishuffle;

/** A separate test-APK screen whose controls sit under the consent panel and can really change on command. */
public class CoveredTargetActivity extends android.app.Activity {
    static final String COMMAND = "com.hobbycoders.aishuffle.test.COVERED_TARGET";
    static final String RESTING = "Taps: 0";
    private static final int TOP = 240, MOVED_TOP = 440, BLIP_MS = 150;
    private int taps = 0;
    private android.widget.LinearLayout layout;
    private android.widget.TextView status;
    /** Replies only once the change has been laid out, so the sender knows accessibility can see it. */
    private final android.content.BroadcastReceiver commands = new android.content.BroadcastReceiver() {
        @Override public void onReceive(android.content.Context context, android.content.Intent intent) {
            PendingResult reply = goAsync();
            String command = intent.getStringExtra("command");
            if ("text".equals(command)) status.setText("Different content");
            else layout.setPadding(32, MOVED_TOP, 32, 32);
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
        layout.addView(target);
        android.widget.Space below = new android.widget.Space(this);
        layout.addView(below, new android.widget.LinearLayout.LayoutParams(-1, 1000));
        android.widget.Button ready = new android.widget.Button(this);
        ready.setText("Get ready"); ready.setAllCaps(false);
        ready.setOnClickListener(v -> status.setText(RESTING));
        layout.addView(ready);
        setContentView(layout);
        registerReceiver(commands, new android.content.IntentFilter(COMMAND), RECEIVER_EXPORTED);
    }
    @Override public void onDestroy() { unregisterReceiver(commands); super.onDestroy(); }
}
