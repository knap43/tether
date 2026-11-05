package dev.tether.app

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.drawable.Icon
import dev.tether.R
import dev.tether.core.TransferInfo

fun humanSize(n: Long?): String {
    var v = (n ?: 0L).toDouble()
    for (unit in listOf("B", "KB", "MB", "GB")) {
        if (v < 1024 || unit == "GB") return if (unit == "B") "${v.toLong()} B" else "%.1f %s".format(v, unit)
        v /= 1024
    }
    return "%.1f GB".format(v)
}

/** A progress notification per file transfer, with a Cancel button. */
object TransferNotifier {
    private fun id(info: TransferInfo) = 10_000 + info.id

    fun update(context: Context, info: TransferInfo) {
        val nm = context.getSystemService(NotificationManager::class.java) ?: return
        when (info.state) {
            "active" -> {
                val cancel = PendingIntent.getService(
                    context, id(info),
                    Intent(context, TetherService::class.java)
                        .setAction(TetherService.ACTION_CANCEL_TRANSFER)
                        .putExtra(TetherService.EXTRA_TRANSFER, info.id),
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
                )
                val total = info.total
                val pct = if (total != null && total > 0) (100 * info.done / total).toInt() else 0
                val verb = if (info.incoming) "Receiving" else "Sending"
                val where = if (info.incoming) "from ${info.peerName}" else "to ${info.peerName}"
                val n = Notification.Builder(context, Notifications.CH_TRANSFERS)
                    .setSmallIcon(if (info.incoming) android.R.drawable.stat_sys_download else android.R.drawable.stat_sys_upload)
                    .setColor(context.md(R.color.md_primary))
                    .setContentTitle("$verb ${info.name}")
                    .setContentText("${humanSize(info.done)} of ${humanSize(total)} $where")
                    .setProgress(100, pct, total == null)
                    .setOngoing(true)
                    .setOnlyAlertOnce(true)
                    .addAction(
                        Notification.Action.Builder(
                            Icon.createWithResource(context, android.R.drawable.ic_menu_close_clear_cancel), "Cancel", cancel,
                        ).build(),
                    )
                    .build()
                try {
                    nm.notify(id(info), n)
                } catch (e: SecurityException) {
                    // notifications not allowed
                }
            }
            "failed" -> {
                nm.cancel(id(info))
                Notifications.post(context, "Couldn't ${if (info.incoming) "receive" else "send"} ${info.name}",
                    "The transfer with ${info.peerName} was interrupted.")
            }
            else -> nm.cancel(id(info))
        }
    }
}
