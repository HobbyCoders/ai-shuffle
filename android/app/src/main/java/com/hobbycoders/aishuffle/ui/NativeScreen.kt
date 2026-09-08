package com.hobbycoders.aishuffle.ui

import android.content.res.ColorStateList
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout
import com.hobbycoders.aishuffle.R
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch

/** Native navigation and form controls. Work is single-flight and cancelled when hidden. */
abstract class NativeScreen : AppCompatActivity() {
    protected lateinit var content: LinearLayout
    protected lateinit var status: TextView
    private lateinit var toolbar: LinearLayout
    private var work: Job? = null
    protected var busy = false
    protected fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()
    protected fun color(id: Int) = ContextCompat.getColor(this, id)
    protected fun rounded(fill: Int, radius: Int = 18) = GradientDrawable().apply {
        setColor(color(fill)); cornerRadius = dp(radius).toFloat()
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE)
        setContentView(R.layout.activity_native_screen)
        content = findViewById(R.id.screenContent)
        status = findViewById(R.id.screenStatus)
        toolbar = findViewById(R.id.screenToolbar)
        status.accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        navigation("Shuffle")
    }
    override fun onResume() { super.onResume(); com.hobbycoders.aishuffle.tools.ToolApprovals.attach(this) }
    override fun onPause() { com.hobbycoders.aishuffle.tools.ToolApprovals.detach(this); super.onPause() }
    protected fun navigation(title: String, back: Boolean = true) {
        toolbar.removeAllViews()
        if (back) toolbar.addView(iconButton(R.drawable.ic_back, "Back") { onBackPressedDispatcher.onBackPressed() })
        toolbar.addView(TextView(this).apply {
            text = title; textSize = 18f; setTextColor(color(R.color.ink))
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
            setPadding(dp(if (back) 4 else 12), 0, 0, 0)
        }, LinearLayout.LayoutParams(0, -2, 1f))
    }
    protected fun navigationAction(icon: Int, label: String, action: () -> Unit) {
        toolbar.addView(iconButton(icon, label) { if (!busy) action() })
    }
    protected fun iconButton(icon: Int, label: String, action: () -> Unit) = ImageButton(this).apply {
        setImageResource(icon); imageTintList = ColorStateList.valueOf(color(R.color.ink))
        contentDescription = label; setPadding(dp(12), dp(12), dp(12), dp(12))
        background = rounded(R.color.surface, 24)
        layoutParams = LinearLayout.LayoutParams(dp(48), dp(48))
        com.hobbycoders.aishuffle.tools.guardConsentTouch(this)
        setOnClickListener { action() }
    }
    protected fun heading(title: String, description: String) {
        content.removeAllViews(); status.text = ""
        text(title, 28f).apply {
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
            setPadding(0, 0, 0, dp(8))
        }
        if (description.isNotBlank()) text(description, 15f).setTextColor(color(R.color.slate))
        gap(16)
    }
    protected fun gap(height: Int) { content.addView(View(this), LinearLayout.LayoutParams(1, dp(height))) }
    protected fun text(value: String, size: Float = 16f): TextView = TextView(this).also {
        it.text = value; it.textSize = size; it.setTextColor(color(R.color.ink))
        it.setLineSpacing(dp(3).toFloat(), 1f); it.setPadding(0, dp(4), 0, dp(10))
        content.addView(it, LinearLayout.LayoutParams(-1, -2))
    }
    protected fun section(label: String) {
        gap(16)
        text(label, 12f).apply {
            letterSpacing = 0.06f; setTextColor(color(R.color.slate))
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        }
    }
    protected fun group(block: () -> Unit): LinearLayout {
        val parent = content
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; background = rounded(R.color.card)
            setPadding(dp(16), dp(12), dp(16), dp(12))
        }
        parent.addView(card, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = dp(12) })
        content = card
        try { block() } finally { content = parent }
        return card
    }
    protected fun button(label: String, primary: Boolean = false, action: () -> Unit): MaterialButton = MaterialButton(this).also {
        com.hobbycoders.aishuffle.tools.guardConsentTouch(it)
        it.text = label; it.textSize = 15f; it.isAllCaps = false
        it.minHeight = dp(54); it.cornerRadius = dp(14); it.insetTop = 0; it.insetBottom = 0
        it.backgroundTintList = ColorStateList.valueOf(color(if (primary) R.color.primary else R.color.card))
        it.setTextColor(color(if (primary) R.color.on_primary else R.color.ink))
        it.elevation = 0f
        if (!primary) { it.strokeWidth = dp(1); it.strokeColor = ColorStateList.valueOf(color(R.color.outline)) }
        content.addView(it, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(4); bottomMargin = dp(8) })
        it.setOnClickListener { if (!busy) action() }
    }
    protected fun link(label: String, action: () -> Unit): TextView = text(label, 14f).apply {
        gravity = Gravity.CENTER; minHeight = dp(48); setPadding(dp(8), dp(14), dp(8), dp(14))
        isClickable = true; isFocusable = true
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        com.hobbycoders.aishuffle.tools.guardConsentTouch(this)
        setOnClickListener { if (!busy) action() }
    }
    protected fun row(title: String, detail: String, icon: Int, action: () -> Unit): LinearLayout {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            background = rounded(R.color.card); minimumHeight = dp(84)
            setPadding(dp(16), dp(14), dp(12), dp(14)); isFocusable = true; isClickable = true
            com.hobbycoders.aishuffle.tools.guardConsentTouch(this)
            setOnClickListener { if (!busy) action() }
        }
        row.addView(ImageView(this).apply {
            setImageResource(icon); imageTintList = ColorStateList.valueOf(color(R.color.ink))
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
        }, LinearLayout.LayoutParams(dp(24), dp(24)).apply { marginEnd = dp(14) })
        val labels = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        labels.addView(TextView(this).apply {
            text = title; textSize = 16f; setTextColor(color(R.color.ink))
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        })
        labels.addView(TextView(this).apply {
            text = detail; textSize = 13f; setTextColor(color(R.color.slate)); setPadding(0, dp(4), 0, 0)
        })
        row.addView(labels, LinearLayout.LayoutParams(0, -2, 1f))
        row.addView(ImageView(this).apply {
            setImageResource(R.drawable.ic_chevron); imageTintList = ColorStateList.valueOf(color(R.color.slate))
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
        }, LinearLayout.LayoutParams(dp(20), dp(20)))
        content.addView(row, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = dp(12) })
        return row
    }
    protected fun input(label: String, secret: Boolean = false): TextInputEditText {
        val layout = TextInputLayout(this)
        layout.hint = label; layout.boxBackgroundMode = TextInputLayout.BOX_BACKGROUND_OUTLINE
        layout.setBoxCornerRadii(dp(12).toFloat(), dp(12).toFloat(), dp(12).toFloat(), dp(12).toFloat())
        layout.boxBackgroundColor = color(R.color.card); layout.boxStrokeColor = color(R.color.ink)
        layout.defaultHintTextColor = ColorStateList.valueOf(color(R.color.slate))
        if (secret) layout.endIconMode = TextInputLayout.END_ICON_PASSWORD_TOGGLE
        val field = TextInputEditText(layout.context)
        field.isSingleLine = true; field.textSize = 16f; field.setTextColor(color(R.color.ink))
        field.inputType = if (secret) InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD else InputType.TYPE_CLASS_TEXT
        field.isSaveEnabled = !secret; field.importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO
        field.setPadding(dp(16), dp(18), dp(16), dp(18))
        layout.addView(field)
        content.addView(layout, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(4); bottomMargin = dp(12) })
        return field
    }
    protected fun confirm(title: String, message: String, action: () -> Unit) {
        val dialog = MaterialAlertDialogBuilder(this).setTitle(title).setMessage(message)
            .setNegativeButton("Cancel", null).setPositiveButton("Continue") { _, _ -> action() }.show()
        com.hobbycoders.aishuffle.tools.guardConsentTouch(dialog.getButton(androidx.appcompat.app.AlertDialog.BUTTON_POSITIVE))
    }
    protected fun runWork(block: suspend () -> Unit) {
        if (work?.isActive == true) return
        work = lifecycleScope.launch {
            busy = true; status.text = "Connecting…"
            try { block() } catch (e: CancellationException) { throw e }
            catch (e: Exception) {
                val repo = com.hobbycoders.aishuffle.account.AccountRepository(this@NativeScreen)
                if (e is com.hobbycoders.aishuffle.account.HubFailure && e.status == 401 && repo.store.hasStoredSession()) {
                    repo.clearLocal()
                    startActivity(android.content.Intent(this@NativeScreen, ServerListActivity::class.java)
                        .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK or android.content.Intent.FLAG_ACTIVITY_CLEAR_TASK)
                        .putExtra("auth_expired", true))
                    finish()
                }
                status.text = e.message ?: "Could not complete this action. Check your connection and retry."
            }
            finally { busy = false }
        }
    }
    override fun onStop() {
        if (work?.isActive == true) status.text = "Action paused. Try again when ready."
        work?.cancel(); busy = false
        super.onStop()
    }
}