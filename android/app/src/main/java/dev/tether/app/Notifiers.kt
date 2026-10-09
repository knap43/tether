package dev.tether.app

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.drawable.Icon
import dev.tether.R
import dev.tether.core.MediaState
import dev.tether.core.TransferInfo

fun humanSize(n: Long?): String {
    var v = (n ?: 0L).toDouble()
    for (unit in listOf("B", "KB", "MB", "GB")) {
        if (v < 1024 || unit == "GB") return if (unit == "B") "${v.toLong()} B" else "%.1f %s".format(v, unit)
        v /= 1024
    }
    return "%.1f GB".format(v)
}

/** Playback controls for the computer's media, as a notification. */
object MediaNotifier {
    private const val ID = 3

    private fun serviceIntent(context: Context, code: Int, peer: String, action: String): PendingIntent =
        PendingIntent.getService(
            context, code,
            Intent(context, TetherService::class.java)
                .setAction(TetherService.ACTION_MEDIA)
                .putExtra(TetherService.EXTRA_PEER, peer)
                .putExtra(TetherService.EXTRA_MEDIA_ACTION, action),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

    private fun action(context: Context, icon: Int, title: String, pi: PendingIntent) =
        Notification.Action.Builder(Icon.createWithResource(context, icon), title, pi).build()

    fun update(context: Context, peer: String, computer: String, state: MediaState) {
        val nm = context.getSystemService(NotificationManager::class.java) ?: return
        val p = state.active
        if (p == null || p.status == "Stopped") {
            nm.cancel(ID)
            return
        }
        val open = PendingIntent.getActivity(
            context, 7,
            Intent(context, MediaActivity::class.java).putExtra(MediaActivity.EXTRA_PEER, peer)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val n = Notification.Builder(context, Notifications.CH_MEDIA)
            .setSmallIcon(R.drawable.ic_stat)
            .setColor(context.getColor(R.color.md_primary))
            .setContentTitle(p.title.ifEmpty { p.identity })
            .setContentText(listOf(p.artist, "on $computer").filter { it.isNotEmpty() }.joinToString(" · "))
            .setContentIntent(open)
            .setOngoing(p.playing)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .setStyle(Notification.MediaStyle().setShowActionsInCompactView(0, 1, 2))
            .addAction(action(context, android.R.drawable.ic_media_previous, "Previous", serviceIntent(context, 11, peer, "previous")))
            .addAction(
                if (p.playing) {
                    action(context, android.R.drawable.ic_media_pause, "Pause", serviceIntent(context, 12, peer, "play_pause"))
                } else {
                    action(context, android.R.drawable.ic_media_play, "Play", serviceIntent(context, 12, peer, "play_pause"))
                },
            )
            .addAction(action(context, android.R.drawable.ic_media_next, "Next", serviceIntent(context, 13, peer, "next")))
            .build()
        try {
            nm.notify(ID, n)
        } catch (e: SecurityException) {
            // notifications not allowed
        }
    }

    fun clear(context: Context) {
        context.getSystemService(NotificationManager::class.java)?.cancel(ID)
    }
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
                    .setColor(context.getColor(R.color.md_primary))
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
