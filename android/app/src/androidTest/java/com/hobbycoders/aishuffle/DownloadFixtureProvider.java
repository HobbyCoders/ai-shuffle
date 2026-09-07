package com.hobbycoders.aishuffle;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.os.ParcelFileDescriptor;
import java.io.File;
import java.io.FileNotFoundException;

/** Only in the test APK. Uses Java because this provider has its own non-instrumented process. */
public final class DownloadFixtureProvider extends ContentProvider {
    private File file(Uri uri) {
        if (!"fixture".equals(uri.getLastPathSegment())) throw new IllegalArgumentException();
        return new File(getContext().getFilesDir(), "chat-download-fixture.bin");
    }
    @Override public boolean onCreate() { return true; }
    @Override public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        return ParcelFileDescriptor.open(file(uri), ParcelFileDescriptor.parseMode(mode));
    }
    @Override public String getType(Uri uri) { return "application/octet-stream"; }
    @Override public Cursor query(Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) { return null; }
    @Override public Uri insert(Uri uri, ContentValues values) { return null; }
    @Override public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) { return 0; }
    @Override public int delete(Uri uri, String selection, String[] selectionArgs) { return file(uri).delete() ? 1 : 0; }
    @Override public Bundle call(String method, String arg, Bundle extras) {
        if ("android:deleteDocument".equals(method)) {
            new File(getContext().getFilesDir(), "chat-download-fixture.bin").delete();
            return new Bundle();
        }
        return super.call(method, arg, extras);
    }
}
