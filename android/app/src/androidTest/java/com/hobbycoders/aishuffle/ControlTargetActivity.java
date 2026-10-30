package com.hobbycoders.aishuffle;

/** A separate test-APK launcher surface. Never packaged in the production APK. */
public class ControlTargetActivity extends android.app.Activity {
    @Override public void onCreate(android.os.Bundle state) {
        super.onCreate(state);
        android.widget.LinearLayout layout = new android.widget.LinearLayout(this);
        layout.setOrientation(android.widget.LinearLayout.VERTICAL);
        layout.setPadding(32, 240, 32, 32);
        android.widget.TextView status = new android.widget.TextView(this);
        status.setText("Opening local player"); status.setTextSize(24);
        layout.addView(status);
        android.widget.Button play = new android.widget.Button(this);
        play.setText("Play local track");
        play.setAllCaps(false);
        play.setEnabled(false);
        play.setOnClickListener(v -> status.setText("Local track playing"));
        layout.addView(play);
        android.widget.Button protectedContent = new android.widget.Button(this);
        protectedContent.setText("Show protected content");
        protectedContent.setAllCaps(false);
        protectedContent.setEnabled(false);
        protectedContent.setOnClickListener(v -> status.setText("Payment method"));
        layout.addView(protectedContent);
        android.widget.Button prepare = new android.widget.Button(this);
        prepare.setText("Prepare local player");
        prepare.setAllCaps(false);
        prepare.setOnClickListener(v -> {
            status.setText("Playlist ready");
            play.setEnabled(true);
            protectedContent.setEnabled(true);
            prepare.setEnabled(false);
        });
        layout.addView(prepare);
        setContentView(layout);
    }
}
