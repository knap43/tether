package dev.tether.app

import android.annotation.SuppressLint
import android.os.Bundle
import android.os.SystemClock
import android.view.Gravity
import android.view.MotionEvent
import android.view.ViewConfiguration
import android.view.ViewGroup
import android.widget.LinearLayout
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.R as M
import dev.tether.R
import dev.tether.core.Node
import dev.tether.core.RemoteError
import dev.tether.core.remoteClick
import dev.tether.core.remoteClose
import dev.tether.core.remoteKey
import dev.tether.core.remoteMove
import dev.tether.core.remoteOpen
import dev.tether.core.remoteScroll
import java.io.IOException
import java.util.concurrent.Executors
import kotlin.math.abs

/** The phone as the computer's touchpad, with the keys that matter while something is playing. */
class RemoteActivity : AppCompatActivity() {
    private lateinit var node: Node
    private lateinit var peer: String
    private lateinit var screen: Screen
    private val sender = Executors.newSingleThreadExecutor() // keeps events in order

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
        content.addView(
            LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER
                setPadding(0, ui.dp(8), 0, ui.dp(16))
                addView(ui.iconButton(R.drawable.ic_sync, "F5: reload the page") { key("f5") })
                addView(ui.iconButton(R.drawable.ic_close, "Escape") { key("escape") })
                addView(ui.iconButton(R.drawable.ic_rewind, "Left arrow") { key("left") })
                addView(ui.iconButton(R.drawable.ic_play, "Space: play or pause", R.attr.tetherFilledIconButton) { key("space") })
                addView(ui.iconButton(R.drawable.ic_forward, "Right arrow") { key("right") })
                addView(ui.iconButton(R.drawable.ic_screen, "F: fullscreen") { key("f") })
            },
        )
        sender.execute {
            try {
                node.remoteOpen(peer)
            } catch (e: RemoteError) {
                runOnUiThread {
                    ui.snack(
                        screen.root,
                        when (e.code) {
                            "denied" -> "Turn on Settings → Let the phone use mouse and keyboard on the computer"
                            "unsupported" -> "This needs a GNOME computer"
                            else -> "Couldn't reach the computer"
                        },
                    )
                }
            } catch (e: IOException) {
                runOnUiThread { ui.snack(screen.root, "Couldn't reach the computer") }
            }
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
        addView(ui.help("Move with a finger · tap to click\nTwo fingers: scroll · tap to right-click").apply { gravity = Gravity.CENTER })
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
        private const val SPEED = 2f
        private const val SCROLL_STEP_DP = 24
        private const val TAP_MS = 300L
    }
}
