package dev.tether.app

import android.annotation.SuppressLint
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.Gravity
import android.view.MotionEvent
import android.view.ViewConfiguration
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.R as M
import com.google.android.material.slider.Slider
import dev.tether.R
import dev.tether.core.Node
import dev.tether.core.RemoteError
import dev.tether.core.remoteClick
import dev.tether.core.remoteClose
import dev.tether.core.remoteKey
import dev.tether.core.remoteMove
import dev.tether.core.remoteOpen
import dev.tether.core.remoteScroll
import dev.tether.core.remoteVolume
import dev.tether.core.setAudioVolume
import java.io.IOException
import java.util.concurrent.Executors
import kotlin.math.abs

/** The phone as the computer's touchpad, with the keys that matter while something is playing. */
class RemoteActivity : AppCompatActivity() {
    private lateinit var node: Node
    private lateinit var peer: String
    private lateinit var screen: Screen
    private val sender = Executors.newSingleThreadExecutor() // keeps events in order
    private lateinit var hint: TextView
    private var ready = false
    private lateinit var volume: Slider
    private var settingVolume = false
    private val main = Handler(Looper.getMainLooper())
    private val followVolume = object : Runnable {
        override fun run() {
            sender.execute {
                val level = runCatching { node.remoteVolume(peer) }.getOrNull()
                if (level != null) {
                    main.post {
                        if (!settingVolume && !isDestroyed) volume.value = Math.round(level * 100).toFloat().coerceIn(0f, 100f)
                    }
                }
            }
            main.postDelayed(this, VOLUME_POLL_MS)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val n = Hub.node
        val id = intent?.getStringExtra(EXTRA_PEER)
        if (n == null || id == null) {
            finish()
            return
        }
        node = n
        peer = id
        screen = Screen(this, "Remote", back = true, scrolling = false)
        screen.toolbar.subtitle = node.peers.get(peer)?.name ?: "Computer"
        val ui = screen.ui
        val content = screen.content
        content.addView(touchpad(ui), LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f).apply {
            setMargins(ui.dp(16), ui.dp(8), ui.dp(16), ui.dp(8))
        })
        fun button(icon: Int, text: String, size: Int, style: Int = R.attr.tetherIconButton, key: String) =
            ui.bigButton(ui.iconButton(icon, text, style) { key(key) }, size)
        content.addView(
            ui.bigRow(
                button(R.drawable.ic_rewind, "Left arrow", SIDE_DP, key = "left"),
                button(R.drawable.ic_play, "Space: play or pause", MAIN_DP, R.attr.tetherFilledIconButton, "space"),
                button(R.drawable.ic_forward, "Right arrow", SIDE_DP, key = "right"),
            ),
        )
        content.addView(
            ui.bigRow(
                button(R.drawable.ic_sync, "F5: reload the page", SMALL_DP, key = "f5"),
                button(R.drawable.ic_close, "Escape", SMALL_DP, key = "escape"),
                button(R.drawable.ic_screen, "F: fullscreen", SMALL_DP, key = "f"),
            ),
        )
        volume = ui.bigSlider(100f).apply {
            stepSize = 1f
            addOnSliderTouchListener(object : Slider.OnSliderTouchListener {
                override fun onStartTrackingTouch(slider: Slider) {
                    settingVolume = true
                }

                override fun onStopTrackingTouch(slider: Slider) {
                    settingVolume = false
                    val level = slider.value / 100.0
                    sender.execute { node.setAudioVolume(peer, level) }
                }
            })
        }
        content.addView(
            ui.volumeRow(volume).apply { setPadding(ui.dp(20), ui.dp(4), ui.dp(20), ui.dp(16)) },
        )
    }

    /** Asks each time we come back, so that switching it on at the computer is all it takes. */
    override fun onResume() {
        super.onResume()
        if (!ready) open()
        main.post(followVolume)
    }

    override fun onPause() {
        main.removeCallbacks(followVolume)
        super.onPause()
    }

    private fun open() = sender.execute {
        val problem = try {
            node.remoteOpen(peer)
            null
        } catch (e: RemoteError) {
            when (e.code) {
                "denied" -> "Remote is switched off on the computer.\n\nIn its Tether window, turn on Settings → Let the phone use mouse and keyboard, or run: tether set phone_remote on\n\nThen come back to this screen."
                "unsupported" -> "This needs a GNOME computer"
                else -> "Couldn't reach the computer"
            }
        } catch (e: IOException) {
            "Couldn't reach the computer"
        }
        runOnUiThread {
            ready = problem == null
            hint.text = problem ?: HELP
        }
    }

    override fun onDestroy() {
        if (::peer.isInitialized) {
            sender.execute { node.remoteClose(peer) }
            sender.shutdown()
        }
        super.onDestroy()
    }

    private fun key(name: String) = sender.execute { node.remoteKey(peer, name) }

    @SuppressLint("ClickableViewAccessibility") // a touchpad has no meaning apart from touch
    private fun touchpad(ui: Ui) = LinearLayout(this).apply {
        gravity = Gravity.CENTER
        orientation = LinearLayout.VERTICAL
        background = ui.rounded(R.color.md_surface_container_high, 28)
        keepScreenOn = true
        hint = ui.help(HELP).apply {
            gravity = Gravity.CENTER
            setPadding(ui.dp(24), 0, ui.dp(24), 0)
        }
        addView(hint)
        val slop = ViewConfiguration.get(context).scaledTouchSlop
        val scrollStep = ui.dp(SCROLL_STEP_DP).toFloat()
        var lastX = 0f
        var lastY = 0f
        var travelled = 0f
        var scrolled = 0f
        var fingers = 1
        var downAt = 0L
        setOnTouchListener { _, e ->
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    lastX = e.x
                    lastY = e.y
                    travelled = 0f
                    scrolled = 0f
                    fingers = 1
                    downAt = SystemClock.uptimeMillis()
                }
                MotionEvent.ACTION_POINTER_DOWN -> {
                    fingers = maxOf(fingers, e.pointerCount)
                    lastX = e.getX(0)
                    lastY = e.getY(0)
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = e.getX(0) - lastX
                    val dy = e.getY(0) - lastY
                    lastX = e.getX(0)
                    lastY = e.getY(0)
                    travelled += abs(dx) + abs(dy)
                    if (e.pointerCount == 1) {
                        if (fingers == 1) sender.execute { node.remoteMove(peer, dx * SPEED, dy * SPEED) }
                    } else {
                        scrolled += dy
                        val steps = (scrolled / scrollStep).toInt()
                        if (steps != 0) {
                            scrolled -= steps * scrollStep
                            sender.execute { node.remoteScroll(peer, -steps) }
                        }
                    }
                }
                MotionEvent.ACTION_UP -> {
                    if (travelled < slop && SystemClock.uptimeMillis() - downAt < TAP_MS) {
                        val button = if (fingers > 1) "right" else "left"
                        sender.execute { node.remoteClick(peer, button) }
                    }
                }
            }
            true
        }
    }

    companion object {
        const val EXTRA_PEER = "peer"
        private const val HELP = "Move with a finger · tap to click\nTwo fingers: scroll · tap to right-click"
        private const val MAIN_DP = 96
        private const val SIDE_DP = 76
        private const val SMALL_DP = 64
        private const val VOLUME_POLL_MS = 1000L
        private const val SPEED = 2f
        private const val SCROLL_STEP_DP = 24
        private const val TAP_MS = 300L
    }
}
