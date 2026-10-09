package dev.tether.app

import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.text.InputType
import android.text.TextUtils
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.widget.NestedScrollView
import com.google.android.material.appbar.AppBarLayout
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView
import com.google.android.material.chip.Chip
import com.google.android.material.chip.ChipGroup
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.materialswitch.MaterialSwitch
import com.google.android.material.snackbar.Snackbar
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout
import dev.tether.R
import com.google.android.material.R as M

private const val MATCH = ViewGroup.LayoutParams.MATCH_PARENT
private const val WRAP = ViewGroup.LayoutParams.WRAP_CONTENT

/**
 * Small toolkit for building the app's Material 3 screens in code.
 * Every row lays its text out with weight 1, so trailing controls never push
 * anything off-screen in portrait; groups of actions wrap as chips.
 */
class Ui(val ctx: Context) {
    fun dp(v: Int) = (v * ctx.resources.displayMetrics.density).toInt()

    fun color(res: Int) = ctx.getColor(res)

    // -- text -------------------------------------------------------------------------

    fun text(
        s: CharSequence,
        appearance: Int = M.style.TextAppearance_Material3_BodyMedium,
        colorRes: Int = R.color.md_on_surface_variant,
        lines: Int = 0,
        ellipsize: TextUtils.TruncateAt? = null,
    ) = TextView(ctx).apply {
        text = s
        setTextAppearance(appearance)
        setTextColor(color(colorRes))
        if (lines > 0) {
            maxLines = lines
            this.ellipsize = ellipsize ?: TextUtils.TruncateAt.END
        }
    }

    fun title(s: CharSequence) =
        text(s, M.style.TextAppearance_Material3_TitleMedium, R.color.md_on_surface, 2)

    fun section(s: String) = text(s, M.style.TextAppearance_Material3_LabelLarge, R.color.md_primary).apply {
        setPadding(dp(4), dp(20), dp(4), dp(8))
    }

    fun help(s: CharSequence) = text(s, M.style.TextAppearance_Material3_BodySmall).apply {
        setPadding(0, dp(4), 0, dp(4))
    }

    // -- containers --------------------------------------------------------------------

    fun column(padding: Int = 0) = LinearLayout(ctx).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(padding, padding, padding, padding)
    }

    /** A filled card; returns its inner column. */
    fun card(parent: ViewGroup, padding: Int = 16): LinearLayout {
        val inner = column(dp(padding))
        val card = MaterialCardView(ctx, null, R.attr.tetherCard).apply {
            setCardBackgroundColor(color(R.color.md_surface_container))
            radius = dp(20).toFloat()
            addView(inner, FrameLayout.LayoutParams(MATCH, WRAP))
        }
        parent.addView(card, LinearLayout.LayoutParams(MATCH, WRAP).apply { bottomMargin = dp(12) })
        return inner
    }

    fun circle(colorRes: Int, sizeDp: Int) = GradientDrawable().apply {
        shape = GradientDrawable.OVAL
        setColor(color(colorRes))
        setSize(dp(sizeDp), dp(sizeDp))
    }

    fun rounded(colorRes: Int, radiusDp: Int) = GradientDrawable().apply {
        cornerRadius = dp(radiusDp).toFloat()
        setColor(color(colorRes))
    }

    /** Round icon badge used at the start of list rows. */
    fun avatar(icon: Int, active: Boolean = true) = FrameLayout(ctx).apply {
        background = circle(if (active) R.color.md_primary_container else R.color.md_surface_container_highest, 40)
        val img = ImageView(ctx).apply {
            setImageResource(icon)
            imageTintList = ColorStateList.valueOf(
                color(if (active) R.color.md_on_primary_container else R.color.md_on_surface_variant),
            )
        }
        addView(img, FrameLayout.LayoutParams(dp(22), dp(22), Gravity.CENTER))
        layoutParams = LinearLayout.LayoutParams(dp(40), dp(40)).apply { marginEnd = dp(16) }
    }

    fun icon(res: Int, tintRes: Int = R.color.md_on_surface_variant, sizeDp: Int = 24) = ImageView(ctx).apply {
        setImageResource(res)
        imageTintList = ColorStateList.valueOf(color(tintRes))
        layoutParams = LinearLayout.LayoutParams(dp(sizeDp), dp(sizeDp))
    }

    /** A list row: [leading] title/subtitle (takes the free width) [trailing…]. */
    fun row(
        title: CharSequence,
        subtitle: CharSequence? = null,
        leading: View? = null,
        trailing: List<View> = emptyList(),
        subtitleEllipsize: TextUtils.TruncateAt? = null,
        onClick: (() -> Unit)? = null,
    ) = LinearLayout(ctx).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        minimumHeight = dp(56)
        setPadding(0, dp(6), 0, dp(6))
        if (leading != null) addView(leading)
        val texts = column().apply {
            addView(title(title))
            if (!subtitle.isNullOrEmpty()) {
                addView(
                    if (subtitleEllipsize != null) {
                        text(subtitle, lines = 1, ellipsize = subtitleEllipsize)
                    } else {
                        text(subtitle)
                    },
                )
            }
        }
        addView(texts, LinearLayout.LayoutParams(0, WRAP, 1f))
        trailing.forEach {
            addView(it, LinearLayout.LayoutParams(WRAP, WRAP).apply { marginStart = dp(4) })
        }
        if (onClick != null) {
            isClickable = true
            isFocusable = true
            setOnClickListener { onClick() }
        }
    }

    fun switchRow(title: String, subtitle: String?, checked: Boolean, onChange: (Boolean) -> Unit): View {
        val sw = MaterialSwitch(ctx).apply {
            isChecked = checked
            setOnCheckedChangeListener { _, on -> onChange(on) }
        }
        return row(title, subtitle, trailing = listOf(sw), onClick = { sw.isChecked = !sw.isChecked })
    }

    fun divider() = View(ctx).apply {
        setBackgroundColor(color(R.color.md_outline_variant))
        layoutParams = LinearLayout.LayoutParams(MATCH, maxOf(1, dp(1) / 2)).apply {
            topMargin = dp(4)
            bottomMargin = dp(4)
        }
    }

    // -- controls ----------------------------------------------------------------------

    enum class Kind { FILLED, TONAL, OUTLINED, TEXT }

    fun button(label: String, kind: Kind = Kind.TONAL, icon: Int? = null, onClick: () -> Unit): MaterialButton {
        val attr = when (kind) {
            Kind.FILLED -> R.attr.tetherFilledButton
            Kind.TONAL -> R.attr.tetherTonalButton
            Kind.OUTLINED -> R.attr.tetherOutlinedButton
            Kind.TEXT -> R.attr.tetherTextButton
        }
        return MaterialButton(ctx, null, attr).apply {
            text = label
            isAllCaps = false
            if (icon != null) setIconResource(icon)
            setOnClickListener { onClick() }
        }
    }

    fun iconButton(icon: Int, description: String, style: Int = R.attr.tetherIconButton, onClick: () -> Unit) =
        MaterialButton(ctx, null, style).apply {
            setIconResource(icon)
            contentDescription = description
            tooltipText = description
            setOnClickListener { onClick() }
        }

    /** Buttons sharing a row in equal parts, so they always fit the width. */
    fun buttons(vararg bs: View) = LinearLayout(ctx).apply {
        orientation = LinearLayout.HORIZONTAL
        setPadding(0, dp(8), 0, 0)
        bs.forEachIndexed { i, b ->
            addView(b, LinearLayout.LayoutParams(0, WRAP, 1f).apply { if (i > 0) marginStart = dp(8) })
        }
    }

    fun fullWidth(b: View) = b.apply {
        layoutParams = LinearLayout.LayoutParams(MATCH, WRAP).apply { topMargin = dp(8) }
    }

    fun chip(label: String, icon: Int?, onClick: () -> Unit) = Chip(ctx, null, R.attr.tetherAssistChip).apply {
        text = label
        if (icon != null) {
            setChipIconResource(icon)
            chipIconTint = ColorStateList.valueOf(color(R.color.md_primary))
        }
        setOnClickListener { onClick() }
    }

    /** Chips flow onto further lines when they don't fit. */
    fun chips(vararg cs: Chip) = ChipGroup(ctx).apply {
        chipSpacingHorizontal = dp(8)
        chipSpacingVertical = dp(4)
        cs.forEach { addView(it) }
        setPadding(0, dp(4), 0, 0)
    }

    /** Monospace, selectable text in a dark well, with a copy button. */
    fun code(textValue: String, onCopy: () -> Unit) = LinearLayout(ctx).apply {
        orientation = LinearLayout.HORIZONTAL
        background = rounded(R.color.md_surface_container_lowest, 12)
        setPadding(dp(12), dp(10), dp(4), dp(10))
        gravity = Gravity.CENTER_VERTICAL
        val t = text(textValue, M.style.TextAppearance_Material3_BodySmall, R.color.md_on_surface).apply {
            typeface = Typeface.MONOSPACE
            setTextIsSelectable(true)
        }
        addView(t, LinearLayout.LayoutParams(0, WRAP, 1f))
        addView(iconButton(R.drawable.ic_copy, "Copy", onClick = onCopy))
        layoutParams = LinearLayout.LayoutParams(MATCH, WRAP).apply { topMargin = dp(8) }
    }

    // -- feedback ----------------------------------------------------------------------

    fun dialog() = MaterialAlertDialogBuilder(ctx)

    fun snack(anchor: View, msg: String) = Snackbar.make(anchor, msg, Snackbar.LENGTH_SHORT).show()

    fun prompt(title: String, initial: String, hint: String, ok: String = "Save", onOk: (String) -> Unit) {
        val input = TextInputEditText(ctx).apply {
            setText(initial)
            inputType = InputType.TYPE_CLASS_TEXT
            setSingleLine()
        }
        val layout = TextInputLayout(ctx, null, R.attr.tetherOutlinedField).apply {
            this.hint = hint
            addView(input, LinearLayout.LayoutParams(MATCH, WRAP))
        }
        val frame = FrameLayout(ctx).apply {
            setPadding(dp(24), dp(8), dp(24), 0)
            addView(layout)
        }
        dialog()
            .setTitle(title)
            .setView(frame)
            .setPositiveButton(ok) { _, _ -> input.text?.toString()?.trim()?.takeIf { it.isNotEmpty() }?.let(onOk) }
            .setNegativeButton("Cancel", null)
            .show()
    }
}

/**
 * A screen with a top app bar and a body that respects the system bars, display
 * cutouts and the keyboard (Android 15 draws apps edge to edge).
 */
class Screen(activity: AppCompatActivity, title: String, back: Boolean, scrolling: Boolean = true) {
    val ui = Ui(activity)
    val root = LinearLayout(activity).apply {
        orientation = LinearLayout.VERTICAL
        setBackgroundColor(activity.getColor(R.color.md_background))
    }
    val toolbar = MaterialToolbar(activity).apply {
        this.title = title
        setTitleTextColor(activity.getColor(R.color.md_on_surface))
        setSubtitleTextColor(activity.getColor(R.color.md_on_surface_variant))
        if (back) {
            setNavigationIcon(R.drawable.ic_back)
            navigationContentDescription = "Back"
            setNavigationOnClickListener { activity.finish() }
        }
    }
    private val appBar = AppBarLayout(activity).apply {
        setBackgroundColor(activity.getColor(R.color.md_background))
        addView(toolbar, LinearLayout.LayoutParams(MATCH, WRAP))
    }

    /** Where the screen's content goes. */
    val content: LinearLayout = ui.column().apply {
        setPadding(ui.dp(16), ui.dp(4), ui.dp(16), ui.dp(24))
    }
    private val body: View

    init {
        WindowCompat.setDecorFitsSystemWindows(activity.window, false)
        root.addView(appBar, LinearLayout.LayoutParams(MATCH, WRAP))
        body = if (scrolling) {
            NestedScrollView(activity).apply {
                clipToPadding = false
                addView(content, FrameLayout.LayoutParams(MATCH, WRAP))
            }
        } else {
            content
        }
        root.addView(body, LinearLayout.LayoutParams(MATCH, 0, 1f))
        ViewCompat.setOnApplyWindowInsetsListener(root) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            appBar.setPadding(bars.left, bars.top, bars.right, 0)
            body.setPadding(
                bars.left + (if (scrolling) 0 else ui.dp(16)),
                if (scrolling) 0 else ui.dp(4),
                bars.right + (if (scrolling) 0 else ui.dp(16)),
                maxOf(bars.bottom, ime.bottom) + (if (scrolling) 0 else ui.dp(16)),
            )
            insets
        }
        activity.setContentView(root)
    }
}
