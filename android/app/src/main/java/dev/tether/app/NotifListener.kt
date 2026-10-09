package dev.tether.app

import android.app.Notification
import android.app.PendingIntent
import android.app.RemoteInput
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Canvas
import android.os.Bundle
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import dev.tether.core.PhoneNotification
import java.io.ByteArrayOutputStream
import java.util.Base64

/**
 * Forwards the phone's notifications to connected computers, and carries out
 * replies and dismissals that come back from them. Needs "Notification access".
 */
class NotifListener : NotificationListenerService() {
    companion object {
        @Volatile
        var instance: NotifListener? = null
            private set

        fun enabled(context: Context): Boolean =
            Settings.Secure.getString(context.contentResolver, "enabled_notification_listeners")
                ?.contains(context.packageName) == true

        private val SKIPPED_CATEGORIES = setOf(
            Notification.CATEGORY_TRANSPORT, Notification.CATEGORY_PROGRESS, Notification.CATEGORY_SERVICE,
            Notification.CATEGORY_SYSTEM,
        )
    }

    private val icons = HashMap<String, String?>()

    override fun onListenerConnected() {
        instance = this
    }

    override fun onListenerDisconnected() {
        if (instance === this) instance = null
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        if (sbn == null || !shouldForward(sbn)) return
        val node = Hub.node ?: return
        if (!node.settings.forwardNotifications || Hub.computers().isEmpty()) return
        val n = describe(sbn) ?: return
        node.pool.execute { node.forwardNotification(n) }
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification?) {
        if (sbn == null || !shouldForward(sbn)) return
        val node = Hub.node ?: return
        node.pool.execute { node.notificationRemoved(sbn.key) }
    }

    private fun shouldForward(sbn: StatusBarNotification): Boolean {
        if (sbn.packageName == packageName || sbn.isOngoing || !sbn.isClearable) return false
        val n = sbn.notification ?: return false
        if (n.flags and Notification.FLAG_GROUP_SUMMARY != 0) return false
        if (n.flags and Notification.FLAG_LOCAL_ONLY != 0) return false
        return n.category !in SKIPPED_CATEGORIES
    }

    private fun describe(sbn: StatusBarNotification): PhoneNotification? {
        val n = sbn.notification ?: return null
        val extras = n.extras ?: return null
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        val text = (extras.getCharSequence(Notification.EXTRA_BIG_TEXT) ?: extras.getCharSequence(Notification.EXTRA_TEXT))
            ?.toString().orEmpty()
        if (title.isBlank() && text.isBlank()) return null
        val pkg = sbn.packageName
        val label = runCatching {
            packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString()
        }.getOrDefault(pkg)
        return PhoneNotification(
            sbn.key, pkg, label, title.take(300), text.take(2000), sbn.postTime, replyAction(n) != null, icon(pkg),
        )
    }

    /** The app icon as a small PNG, base64 (cached per app). */
    private fun icon(pkg: String): String? = icons.getOrPut(pkg) {
        runCatching {
            val d = packageManager.getApplicationIcon(pkg)
            val bmp = Bitmap.createBitmap(64, 64, Bitmap.Config.ARGB_8888)
            val canvas = Canvas(bmp)
            d.setBounds(0, 0, 64, 64)
            d.draw(canvas)
            val out = ByteArrayOutputStream()
            bmp.compress(Bitmap.CompressFormat.PNG, 100, out)
            Base64.getEncoder().encodeToString(out.toByteArray())
        }.getOrNull()
    }

    private fun replyAction(n: Notification): Notification.Action? =
        n.actions?.firstOrNull { a -> a?.remoteInputs?.any { it.allowFreeFormInput } == true }

    fun dismiss(key: String) {
        runCatching { cancelNotification(key) }
    }

    fun reply(key: String, text: String): Boolean {
        val sbn = runCatching { activeNotifications }.getOrNull()?.firstOrNull { it.key == key } ?: return false
        val action = replyAction(sbn.notification ?: return false) ?: return false
        val inputs = action.remoteInputs ?: return false
        val results = Bundle()
        for (ri in inputs) if (ri.allowFreeFormInput) results.putCharSequence(ri.resultKey, text)
        val intent = Intent()
        RemoteInput.addResultsToIntent(inputs, intent, results)
        return try {
            action.actionIntent.send(this, 0, intent)
            true
        } catch (e: PendingIntent.CanceledException) {
            false
        }
    }
}
