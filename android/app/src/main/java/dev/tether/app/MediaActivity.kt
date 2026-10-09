package dev.tether.app

import android.content.res.ColorStateList
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.button.MaterialButton
import com.google.android.material.slider.LabelFormatter
import com.google.android.material.slider.Slider
import dev.tether.R
import dev.tether.core.MediaState
import dev.tether.core.NodeEvent
import dev.tether.core.RemoteMedia
import com.google.android.material.R as M

/** Remote control for whatever is playing on a computer. */
class MediaActivity : AppCompatActivity() {
    companion object {
        const val EXTRA_PEER = "peer"
    }

    private lateinit var screen: Screen
    private val ui get() = screen.ui
    private var peer: String = ""
    private var remote: RemoteMedia? = null
    private var state: MediaState? = null
    private var dragging = false
    private var volumeDragging = false

    private lateinit var playerLabel: android.widget.TextView
    private lateinit var titleLabel: android.widget.TextView
    private lateinit var artistLabel: android.widget.TextView
    private lateinit var elapsed: android.widget.TextView
    private lateinit var remaining: android.widget.TextView
    private lateinit var seek: Slider
    private lateinit var playPause: MaterialButton
    private lateinit var controls: LinearLayout
    private lateinit var timeRow: LinearLayout
    private lateinit var volume: Slider
    private lateinit var muteButton: MaterialButton

    private val listener: (NodeEvent) -> Unit = { e ->
        if (e is NodeEvent.MediaUpdate && e.from == peer) show(e.state)
    }
    private val ticker = object : Runnable {
        override fun run() {
            updatePosition()
            seek.postDelayed(this, 500)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val node = Hub.node
        peer = intent?.getStringExtra(EXTRA_PEER) ?: ""
        if (node == null || peer.isEmpty()) {
            finish()
            return
        }
        remote = RemoteMedia(node, peer)
        screen = Screen(this, "Media", back = true)
        screen.toolbar.subtitle = node.peers.get(peer)?.name ?: "Computer"
        val content = screen.content
        content.gravity = Gravity.CENTER_HORIZONTAL

        // Artwork placeholder: square, never wider than the screen allows.
        val metrics = resources.displayMetrics
        val side = minOf(metrics.widthPixels - ui.dp(96), ui.dp(260), (metrics.heightPixels * 0.32).toInt())
        val art = FrameLayout(this).apply {
            background = ui.rounded(R.color.md_surface_container_high, 28)
            addView(
                ImageView(this@MediaActivity).apply {
                    setImageResource(R.drawable.ic_music)
                    imageTintList = ColorStateList.valueOf(getColor(R.color.md_primary))
                },
                FrameLayout.LayoutParams(side / 3, side / 3, Gravity.CENTER),
            )
        }
        content.addView(art, LinearLayout.LayoutParams(side, side).apply { topMargin = ui.dp(16) })

        playerLabel = ui.text("", M.style.TextAppearance_Material3_LabelLarge, R.color.md_primary, lines = 1).apply {
            gravity = Gravity.CENTER
            setPadding(0, ui.dp(20), 0, 0)
        }
        titleLabel = ui.text("Loading…", M.style.TextAppearance_Material3_HeadlineSmall, R.color.md_on_surface, lines = 2)
            .apply { gravity = Gravity.CENTER; setPadding(0, ui.dp(4), 0, 0) }
        artistLabel = ui.text("", M.style.TextAppearance_Material3_BodyLarge, lines = 1).apply { gravity = Gravity.CENTER }
        for (v in listOf(playerLabel, titleLabel, artistLabel)) {
            content.addView(v, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        }

        seek = Slider(this).apply {
            valueFrom = 0f
            valueTo = 1f
            labelBehavior = LabelFormatter.LABEL_GONE
            addOnSliderTouchListener(object : Slider.OnSliderTouchListener {
                override fun onStartTrackingTouch(slider: Slider) {
                    dragging = true
                }

                override fun onStopTrackingTouch(slider: Slider) {
                    dragging = false
                    val p = state?.active ?: return
                    send("position", p.name, (slider.value * 1000).toLong())
                }
            })
        }
        content.addView(seek, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply {
            topMargin = ui.dp(16)
        })
        elapsed = ui.text("0:00", M.style.TextAppearance_Material3_LabelMedium)
        remaining = ui.text("", M.style.TextAppearance_Material3_LabelMedium).apply { gravity = Gravity.END }
        timeRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(ui.dp(16), 0, ui.dp(16), 0)
            addView(elapsed, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            addView(remaining, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        }
        content.addView(timeRow, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))

        // Five equal slots: the controls always share the width, whatever the screen.
        playPause = ui.iconButton(R.drawable.ic_play, "Play", R.attr.tetherFilledIconButton) {
            send("play_pause", state?.active?.name)
        }.apply {
            iconSize = ui.dp(30)
            layoutParams = FrameLayout.LayoutParams(ui.dp(64), ui.dp(64), Gravity.CENTER)
        }
        controls = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, ui.dp(8), 0, ui.dp(8))
            for (b in listOf(
                ui.iconButton(R.drawable.ic_rewind, "Back 10 seconds") { send("seek", state?.active?.name, -10) },
                ui.iconButton(R.drawable.ic_skip_previous, "Previous") { send("previous", state?.active?.name) }
                    .apply { iconSize = ui.dp(28) },
                playPause,
                ui.iconButton(R.drawable.ic_skip_next, "Next") { send("next", state?.active?.name) }
                    .apply { iconSize = ui.dp(28) },
                ui.iconButton(R.drawable.ic_forward, "Forward 10 seconds") { send("seek", state?.active?.name, 10) },
            )) {
                val slot = FrameLayout(this@MediaActivity)
                if (b.layoutParams == null) b.layoutParams = FrameLayout.LayoutParams(ui.dp(48), ui.dp(48), Gravity.CENTER)
                slot.addView(b)
                addView(slot, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            }
        }
        content.addView(controls, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))

        val volCard = ui.card(content, padding = 8)
        (volCard.parent as View).layoutParams = (volCard.parent as View).layoutParams.apply {
            (this as LinearLayout.LayoutParams).topMargin = ui.dp(12)
        }
        muteButton = ui.iconButton(R.drawable.ic_volume, "Mute") { send("mute", null) }
        volume = Slider(this).apply {
            valueFrom = 0f
            valueTo = 100f
            stepSize = 1f
            labelBehavior = LabelFormatter.LABEL_GONE
            addOnSliderTouchListener(object : Slider.OnSliderTouchListener {
                override fun onStartTrackingTouch(slider: Slider) {
                    volumeDragging = true
                }

                override fun onStopTrackingTouch(slider: Slider) {
                    volumeDragging = false
                    send("volume", null, slider.value / 100.0)
                }
            })
        }
        volCard.addView(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            addView(muteButton)
            addView(volume, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        })
    }

    override fun onResume() {
        super.onResume()
        Hub.addListener(listener)
        seek.post(ticker)
        Hub.media[peer]?.let { show(it) }
        val rc = remote ?: return
        Thread {
            try {
                val st = rc.state()
                runOnUiThread { show(st) }
            } catch (e: Exception) {
                runOnUiThread { titleLabel.text = "Couldn't reach the computer" }
            }
        }.start()
    }

    override fun onPause() {
        Hub.removeListener(listener)
        seek.removeCallbacks(ticker)
        super.onPause()
    }

    private fun send(action: String, player: String?, value: Number? = null) {
        val rc = remote ?: return
        Thread {
            try {
                val st = rc.command(action, player, value)
                runOnUiThread { show(st) }
            } catch (e: Exception) {
                runOnUiThread { ui.snack(screen.root, e.message ?: "Failed") }
            }
        }.start()
    }

    private fun fmt(ms: Long?): String {
        if (ms == null) return "–:––"
        val s = ms / 1000
        return if (s >= 3600) "%d:%02d:%02d".format(s / 3600, s / 60 % 60, s % 60) else "%d:%02d".format(s / 60, s % 60)
    }

    private fun show(st: MediaState) {
        state = st
        val p = st.active
        when {
            !st.available -> {
                playerLabel.text = ""
                titleLabel.text = "Media control isn't available"
                artistLabel.text = "Install playerctl on the computer"
            }
            p == null -> {
                playerLabel.text = ""
                titleLabel.text = "Nothing is playing"
                artistLabel.text = "Start music or a video on the computer"
            }
            else -> {
                playerLabel.text = p.identity + if (st.players.size > 1) "  ·  ${st.players.size - 1} more" else ""
                titleLabel.text = p.title.ifEmpty { p.identity }
                artistLabel.text = listOf(p.artist, p.album).filter { it.isNotEmpty() }.joinToString(" — ")
                playPause.setIconResource(if (p.playing) R.drawable.ic_pause else R.drawable.ic_play)
                playPause.contentDescription = if (p.playing) "Pause" else "Play"
            }
        }
        controls.visibility = if (p == null) View.INVISIBLE else View.VISIBLE
        val hasLength = p?.lengthMs != null && (p.lengthMs ?: 0) > 0
        seek.visibility = if (hasLength) View.VISIBLE else View.INVISIBLE
        timeRow.visibility = if (hasLength) View.VISIBLE else View.INVISIBLE
        volume.isEnabled = st.volume != null
        muteButton.setIconResource(if (st.muted) R.drawable.ic_volume_off else R.drawable.ic_volume)
        if (!volumeDragging) st.volume?.let { volume.value = (it * 100).toFloat().coerceIn(0f, 100f).let { v -> Math.round(v).toFloat() } }
        updatePosition()
    }

    private fun updatePosition() {
        val p = state?.active ?: return
        val length = p.lengthMs ?: return
        if (length <= 0) return
        val pos = (p.positionNow() ?: 0).coerceIn(0, length)
        if (!dragging) {
            val to = (length / 1000f).coerceAtLeast(1f)
            if (seek.valueTo != to) {
                seek.value = 0f
                seek.valueTo = to
            }
            seek.value = (pos / 1000f).coerceIn(0f, to)
        }
        elapsed.text = fmt(pos)
        remaining.text = "−" + fmt(length - pos)
    }
}
