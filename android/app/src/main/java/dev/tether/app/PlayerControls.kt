package dev.tether.app

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.button.MaterialButton
import com.google.android.material.slider.LabelFormatter
import com.google.android.material.slider.Slider
import dev.tether.R
import dev.tether.core.MusicState
import dev.tether.core.Node
import dev.tether.core.NodeEvent
import dev.tether.core.RemoteError
import dev.tether.core.musicCommand
import dev.tether.core.musicState
import java.io.IOException

/** The controls for whatever the computer's player is playing, music or video: a card that polls while shown. */
class PlayerControls(
    private val activity: AppCompatActivity,
    private val ui: Ui,
    private val node: Node,
    private val peer: String,
    parent: LinearLayout,
    private val onState: (MusicState) -> Unit = {},
) {
    private val main = Handler(Looper.getMainLooper())
    private val card = ui.card(parent, padding = 12)
    private val view = card.parent as View
    private val title: TextView = ui.title("")
    private val artist: TextView = ui.text("", lines = 1)
    private val seek = slider()
    private val volume = slider()
    private val previous = ui.iconButton(R.drawable.ic_skip_previous, "Previous") { command("previous") }
    private val next = ui.iconButton(R.drawable.ic_skip_next, "Next") { command("next") }
    private val playPause: MaterialButton =
        ui.iconButton(R.drawable.ic_pause, "Pause", R.attr.tetherFilledIconButton) { command("toggle") }
    private var position = 0.0
    private var seeking = false
    private var settingVolume = false

    private val poll = object : Runnable {
        override fun run() {
            refresh()
            main.postDelayed(this, POLL_MS)
        }
    }

    init {
        view.visibility = View.GONE
        card.addView(title)
        card.addView(artist)
        seek.addOnSliderTouchListener(touch({ seeking = it }) { command("position", it.toDouble()) })
        card.addView(seek)
        card.addView(
            LinearLayout(activity).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER
                addView(ui.iconButton(R.drawable.ic_close, "Stop") { command("stop") })
                addView(previous)
                addView(ui.iconButton(R.drawable.ic_rewind, "Back 10 seconds") { command("position", maxOf(0.0, position - SKIP_S)) })
                addView(playPause)
                addView(ui.iconButton(R.drawable.ic_forward, "Forward 10 seconds") { command("position", position + SKIP_S) })
                addView(next)
            },
        )
        volume.valueTo = 100f
        volume.stepSize = 1f
        volume.addOnSliderTouchListener(touch({ settingVolume = it }) { command("volume", it.toDouble()) })
        card.addView(
            LinearLayout(activity).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                addView(ui.icon(R.drawable.ic_volume))
                addView(volume, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            },
        )
    }

    fun start() = main.post(poll)

    fun stop() = main.removeCallbacks(poll)

    fun refresh() = request { node.musicState(peer) }

    private fun command(action: String, value: Double? = null) = request { node.musicCommand(peer, action, value) }

    private fun request(work: () -> MusicState) {
        node.pool.execute {
            try {
                val state = work()
                main.post { if (!activity.isDestroyed) show(state) }
            } catch (e: RemoteError) {
                // the next poll tells the truth
            } catch (e: IOException) {
                // likewise
            }
        }
    }

    private fun show(s: MusicState) {
        onState(s)
        view.visibility = if (s.playing) View.VISIBLE else View.GONE
        if (!s.playing) return
        title.text = s.title ?: "Opening…"
        artist.text = s.artist.orEmpty()
        artist.visibility = if (s.artist.isNullOrEmpty()) View.GONE else View.VISIBLE
        playPause.setIconResource(if (s.paused) R.drawable.ic_play else R.drawable.ic_pause)
        playPause.contentDescription = if (s.paused) "Play" else "Pause"
        val several = if (s.count > 1) View.VISIBLE else View.GONE
        previous.visibility = several
        next.visibility = several
        position = s.position
        seek.isEnabled = s.length > 0
        if (!seeking) {
            seek.valueTo = maxOf(s.length.toFloat(), 1f)
            seek.value = s.position.toFloat().coerceIn(0f, seek.valueTo)
        }
        if (!settingVolume) volume.value = Math.round(s.volume).toFloat().coerceIn(0f, 100f)
    }

    private fun slider() = Slider(activity).apply {
        valueFrom = 0f
        valueTo = 1f
        labelBehavior = LabelFormatter.LABEL_GONE
    }

    private fun touch(active: (Boolean) -> Unit, done: (Float) -> Unit) = object : Slider.OnSliderTouchListener {
        override fun onStartTrackingTouch(slider: Slider) = active(true)

        override fun onStopTrackingTouch(slider: Slider) {
            active(false)
            done(slider.value)
        }
    }

    private companion object {
        const val POLL_MS = 1000L
        const val SKIP_S = 10.0
    }
}

/** What's playing on the computer, and the controls for it. */
class NowPlayingActivity : AppCompatActivity() {
    private lateinit var controls: PlayerControls

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val node = Hub.node
        val peer = intent?.getStringExtra(EXTRA_PEER)
        if (node == null || peer == null) {
            finish()
            return
        }
        val name = node.peers.get(peer)?.name ?: "the computer"
        val screen = Screen(this, "Now playing", back = true, scrolling = false)
        screen.toolbar.subtitle = name
        val ui = screen.ui
        val content = screen.content
        content.gravity = Gravity.CENTER_VERTICAL
        val idle = ui.help("Nothing is playing on $name").apply {
            gravity = Gravity.CENTER
            setPadding(ui.dp(24), ui.dp(48), ui.dp(24), ui.dp(48))
            visibility = View.GONE
        }
        content.addView(idle, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        controls = PlayerControls(this, ui, node, peer, content) { idle.visibility = if (it.playing) View.GONE else View.VISIBLE }
    }

    override fun onResume() {
        super.onResume()
        if (::controls.isInitialized) controls.start()
    }

    override fun onPause() {
        if (::controls.isInitialized) controls.stop()
        super.onPause()
    }

    companion object {
        const val EXTRA_PEER = "peer"
    }
}

/** A notification while the computer plays something a phone asked for, so it can always be paused or stopped. */
object PlaybackNotifier {
    private const val ID = 5
    private var showingFor: String? = null

    fun update(context: Context, e: NodeEvent.PlayState) {
        val nm = context.getSystemService(NotificationManager::class.java) ?: return
        if (!e.on) {
            nm.cancel(ID)
            showingFor = null
            return
        }
        fun action(label: String, what: String, code: Int): Notification.Action {
            val intent = Intent(context, PlaybackReceiver::class.java)
                .putExtra(PlaybackReceiver.EXTRA_ACTION, what)
                .putExtra(PlaybackReceiver.EXTRA_PEER, e.from)
            val pending = PendingIntent.getBroadcast(context, code, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            return Notification.Action.Builder(null, label, pending).build()
        }
        val open = Intent(context, NowPlayingActivity::class.java)
            .putExtra(NowPlayingActivity.EXTRA_PEER, e.from)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val n = Notification.Builder(context, Notifications.CH_AUDIO)
            .setSmallIcon(R.drawable.ic_play)
            .setColor(context.md(R.color.md_primary))
            .setContentTitle("Playing on ${e.fromName}")
            .setContentText("Tap for all the controls")
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(PendingIntent.getActivity(context, ID, open, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
            .addAction(action("Play/Pause", "toggle", 10))
            .addAction(action("Stop", "stop", 11))
            .build()
        try {
            nm.notify(ID, n)
            showingFor = e.from
        } catch (ex: SecurityException) {
            // notification permission not granted
        }
    }

    /** A computer that's gone can't be controlled; it says again if it's still playing when it returns. */
    fun connectionChanged(context: Context) {
        val peer = showingFor ?: return
        if (Hub.node?.isConnected(peer) != true) {
            context.getSystemService(NotificationManager::class.java)?.cancel(ID)
            showingFor = null
        }
    }
}

class PlaybackReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val node = Hub.node ?: return
        val peer = intent.getStringExtra(EXTRA_PEER) ?: return
        val action = intent.getStringExtra(EXTRA_ACTION) ?: return
        val done = goAsync()
        node.pool.execute {
            try {
                node.musicCommand(peer, action)
            } catch (e: RemoteError) {
                // nothing left to control
            } catch (e: IOException) {
                // nor can we reach it
            } finally {
                done.finish()
            }
        }
    }

    companion object {
        const val EXTRA_PEER = "peer"
        const val EXTRA_ACTION = "action"
    }
}
