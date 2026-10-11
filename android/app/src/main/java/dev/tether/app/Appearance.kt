package dev.tether.app

import android.app.Application
import android.content.ComponentName
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import androidx.annotation.RequiresApi
import androidx.core.graphics.ColorUtils
import com.google.android.material.color.DynamicColors
import com.google.android.material.color.DynamicColorsOptions
import dev.tether.R
import com.google.android.material.R as M

/** Launcher icons. Each is an <activity-alias> in the manifest; exactly one is enabled. */
enum class AppIcon(val alias: String, val label: String, val enabledByDefault: Boolean) {
    CLASSIC("dev.tether.app.LauncherClassic", "Classic", true),
    COLORFUL("dev.tether.app.LauncherColorful", "Colorful", false),
    SYSTEM("dev.tether.app.LauncherSystem", "System colors", false),
}

/** The app icon and whether the UI takes its colors from the wallpaper (Material You). */
object Appearance {
    private const val PREFS = "appearance"
    private const val KEY_SYSTEM_COLORS = "system_colors"

    /** Wallpaper colors exist from Android 12. */
    val systemColorsAvailable get() = Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && DynamicColors.isDynamicColorAvailable()

    @Volatile private var cached: Boolean? = null

    fun systemColors(context: Context): Boolean =
        cached ?: (systemColorsAvailable && prefs(context).getBoolean(KEY_SYSTEM_COLORS, false)).also { cached = it }

    fun setSystemColors(context: Context, on: Boolean) {
        prefs(context).edit().putBoolean(KEY_SYSTEM_COLORS, on).apply()
        cached = on && systemColorsAvailable
    }

    /** Applies the wallpaper scheme (kept dark) to every activity while the setting is on. */
    fun install(app: Application) {
        DynamicColors.applyToActivitiesIfAvailable(
            app,
            DynamicColorsOptions.Builder()
                .setThemeOverlay(M.style.ThemeOverlay_Material3_DynamicColors_Dark)
                .setPrecondition { activity, _ -> systemColors(activity) }
                .build(),
        )
    }

    fun icons(): List<AppIcon> =
        if (systemColorsAvailable) AppIcon.entries else AppIcon.entries - AppIcon.SYSTEM

    fun icon(context: Context): AppIcon {
        val pm = context.packageManager
        return AppIcon.entries.firstOrNull { icon ->
            when (pm.getComponentEnabledSetting(component(context, icon))) {
                PackageManager.COMPONENT_ENABLED_STATE_ENABLED -> true
                PackageManager.COMPONENT_ENABLED_STATE_DISABLED -> false
                else -> icon.enabledByDefault
            }
        } ?: AppIcon.CLASSIC
    }

    fun setIcon(context: Context, icon: AppIcon) {
        val pm = context.packageManager
        // Enable the new entry first so the app is never left without one in the launcher.
        pm.setComponentEnabledSetting(
            component(context, icon),
            PackageManager.COMPONENT_ENABLED_STATE_ENABLED,
            PackageManager.DONT_KILL_APP,
        )
        AppIcon.entries.filter { it != icon }.forEach {
            pm.setComponentEnabledSetting(
                component(context, it),
                PackageManager.COMPONENT_ENABLED_STATE_DISABLED,
                PackageManager.DONT_KILL_APP,
            )
        }
    }

    private fun component(context: Context, icon: AppIcon) = ComponentName(context.packageName, icon.alias)

    private fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
}

/**
 * Resolves one of the app's `md_*` scheme colors: the amber value from colors.xml, or, when
 * system colors are on, the matching role of Material's dark dynamic scheme.
 */
fun Context.md(res: Int): Int {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S || !Appearance.systemColors(this)) return getColor(res)
    return DynamicScheme.color(this, res) ?: getColor(res)
}

/** Material 3 dark-scheme roles mapped onto the system's tonal palettes (Android 12+). */
@RequiresApi(Build.VERSION_CODES.S)
private object DynamicScheme {
    fun color(ctx: Context, res: Int): Int? {
        fun c(id: Int) = ctx.getColor(id)
        // Neutral tones between the palette's fixed steps (tone 0, 10, 20, 30).
        fun neutral(tone: Float): Int {
            val steps = intArrayOf(
                android.R.color.system_neutral1_1000, android.R.color.system_neutral1_900,
                android.R.color.system_neutral1_800, android.R.color.system_neutral1_700,
            )
            val i = (tone / 10f).toInt().coerceIn(0, steps.size - 2)
            return ColorUtils.blendARGB(c(steps[i]), c(steps[i + 1]), tone / 10f - i)
        }
        return when (res) {
            R.color.md_primary -> c(android.R.color.system_accent1_200)
            R.color.md_on_primary -> c(android.R.color.system_accent1_800)
            R.color.md_primary_container -> c(android.R.color.system_accent1_700)
            R.color.md_on_primary_container -> c(android.R.color.system_accent1_100)
            R.color.md_inverse_primary -> c(android.R.color.system_accent1_600)
            R.color.md_secondary -> c(android.R.color.system_accent2_200)
            R.color.md_on_secondary -> c(android.R.color.system_accent2_800)
            R.color.md_secondary_container -> c(android.R.color.system_accent2_700)
            R.color.md_on_secondary_container -> c(android.R.color.system_accent2_100)
            R.color.md_tertiary -> c(android.R.color.system_accent3_200)
            R.color.md_on_tertiary -> c(android.R.color.system_accent3_800)
            R.color.md_tertiary_container -> c(android.R.color.system_accent3_700)
            R.color.md_on_tertiary_container -> c(android.R.color.system_accent3_100)
            R.color.md_background, R.color.md_surface -> neutral(6f)
            R.color.md_on_background, R.color.md_on_surface -> c(android.R.color.system_neutral1_100)
            R.color.md_surface_variant -> c(android.R.color.system_neutral2_700)
            R.color.md_on_surface_variant -> c(android.R.color.system_neutral2_200)
            R.color.md_outline -> c(android.R.color.system_neutral2_400)
            R.color.md_outline_variant -> c(android.R.color.system_neutral2_700)
            R.color.md_inverse_surface -> c(android.R.color.system_neutral1_100)
            R.color.md_inverse_on_surface -> c(android.R.color.system_neutral1_800)
            R.color.md_surface_container_lowest -> neutral(4f)
            R.color.md_surface_container_low -> neutral(10f)
            R.color.md_surface_container -> neutral(12f)
            R.color.md_surface_container_high -> neutral(17f)
            R.color.md_surface_container_highest -> neutral(22f)
            else -> null // error and success colors stay as they are
        }
    }
}
