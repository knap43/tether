package dev.tether.app

import android.content.res.ColorStateList
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.R as M
import com.google.android.material.button.MaterialButton
import com.google.android.material.slider.LabelFormatter
import com.google.android.material.slider.Slider
import dev.tether.R
import dev.tether.core.MusicEntry
import dev.tether.core.MusicState
import dev.tether.core.Node
import dev.tether.core.RemoteError
import dev.tether.core.musicCommand
import dev.tether.core.musicList
import dev.tether.core.musicPlay
import dev.tether.core.musicState
import java.io.IOException

/** The computer's Music folder: browse it here, and it plays there. */
class MusicActivity : AppCompatActivity() {
    private lateinit var node: Node
    private lateinit var peer: String
    private lateinit var screen: Screen
    private lateinit var ui: Ui
    private lateinit var list: RecyclerView
    private lateinit var message: TextView
    private lateinit var playerCard: View
    private lateinit var trackTitle: TextView
    private lateinit var trackArtist: TextView
    private lateinit var seek: Slider
    private lateinit var volume: Slider
    private lateinit var playPause: MaterialButton

    private val main = Handler(Looper.getMainLooper())
    private val adapter = Adapter()
    private var entries: List<MusicEntry> = emptyList()
    private var path = "/"
    private var seeking = false
    private var settingVolume = false

    private val back = object : OnBackPressedCallback(false) {
        override fun handleOnBackPressed() = load(path.trimEnd('/').substringBeforeLast('/', "").ifEmpty { "/" })
    }

    private val poll = object : Runnable {
        override fun run() {
            request({ node.musicState(peer) }, quiet = true) { show(it) }
            main.postDelayed(this, POLL_MS)
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
        path = savedInstanceState?.getString(STATE_PATH) ?: "/"
        screen = Screen(this, "Music", back = true, scrolling = false)
        ui = screen.ui
        screen.toolbar.subtitle = node.peers.get(peer)?.name ?: "Computer"
        onBackPressedDispatcher.addCallback(this, back)

        val content = screen.content
        message = ui.help("").apply {
            gravity = Gravity.CENTER
            setPadding(ui.dp(24), ui.dp(48), ui.dp(24), ui.dp(48))
            visibility = View.GONE
        }
        content.addView(message, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        list = RecyclerView(this).apply {
            layoutManager = LinearLayoutManager(this@MusicActivity)
            adapter = this@MusicActivity.adapter
            clipToPadding = false
        }
        content.addView(list, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        buildPlayer(content)
        load(path)
    }

    override fun onResume() {
        super.onResume()
        main.post(poll)
    }

    override fun onPause() {
        main.removeCallbacks(poll)
        super.onPause()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(STATE_PATH, path)
    }

    private fun buildPlayer(content: LinearLayout) {
        val card = ui.card(content, padding = 12)
        playerCard = card.parent as View
        playerCard.visibility = View.GONE
        trackTitle = ui.title("")
        trackArtist = ui.text("", lines = 1)
        card.addView(trackTitle)
        card.addView(trackArtist)
        seek = slider().also {
            it.addOnSliderTouchListener(object : Slider.OnSliderTouchListener {
                override fun onStartTrackingTouch(slider: Slider) {
                    seeking = true
                }

                override fun onStopTrackingTouch(slider: Slider) {
                    seeking = false
                    command("position", slider.value.toDouble())
                }
            })
        }
        card.addView(seek)
        playPause = ui.iconButton(R.drawable.ic_pause, "Pause", R.attr.tetherFilledIconButton) { command("toggle") }
        card.addView(
            LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER
                addView(ui.iconButton(R.drawable.ic_close, "Stop") { command("stop") })
                addView(ui.iconButton(R.drawable.ic_skip_previous, "Previous") { command("previous") })
                addView(playPause)
                addView(ui.iconButton(R.drawable.ic_skip_next, "Next") { command("next") })
            },
        )
        volume = slider().also {
            it.valueTo = 100f
            it.stepSize = 1f
            it.addOnSliderTouchListener(object : Slider.OnSliderTouchListener {
                override fun onStartTrackingTouch(slider: Slider) {
                    settingVolume = true
                }

                override fun onStopTrackingTouch(slider: Slider) {
                    settingVolume = false
                    command("volume", slider.value.toDouble())
                }
            })
        }
        card.addView(
            LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                addView(ui.icon(R.drawable.ic_volume))
                addView(volume, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            },
        )
    }

    private fun slider() = Slider(this).apply {
        valueFrom = 0f
        valueTo = 1f
        labelBehavior = LabelFormatter.LABEL_GONE
    }

    private fun show(s: MusicState) {
        playerCard.visibility = if (s.playing) View.VISIBLE else View.GONE
        if (!s.playing) return
        trackTitle.text = s.title ?: "Starting…"
        trackArtist.text = s.artist.orEmpty()
        trackArtist.visibility = if (s.artist.isNullOrEmpty()) View.GONE else View.VISIBLE
        playPause.setIconResource(if (s.paused) R.drawable.ic_play else R.drawable.ic_pause)
        playPause.contentDescription = if (s.paused) "Play" else "Pause"
        if (!seeking) {
            seek.valueTo = maxOf(s.length.toFloat(), 1f)
            seek.value = s.position.toFloat().coerceIn(0f, seek.valueTo)
        }
        if (!settingVolume) volume.value = Math.round(s.volume).toFloat().coerceIn(0f, 100f)
    }

    private fun load(target: String) {
        request({ node.musicList(peer, target) }) { found ->
            path = target
            entries = found
            back.isEnabled = target != "/"
            screen.toolbar.title = if (target == "/") "Music" else target.substringAfterLast('/')
            adapter.notifyDataSetChanged()
            list.scrollToPosition(0)
            message.text = "Nothing to play in this folder"
            message.visibility = if (found.isEmpty()) View.VISIBLE else View.GONE
        }
    }

    private fun child(name: String) = path.trimEnd('/') + "/" + name

    private fun play(target: String) = request({ node.musicPlay(peer, target) }) { command(null) }

    /** Sends an action (or just asks, when null) and shows the state that comes back. */
    private fun command(action: String?, value: Double? = null) = request({
        if (action == null) node.musicState(peer) else node.musicCommand(peer, action, value)
    }) { show(it) }

    private fun <T> request(work: () -> T, quiet: Boolean = false, done: (T) -> Unit) {
        node.pool.execute {
            try {
                val result = work()
                main.post { if (!isDestroyed) done(result) }
            } catch (e: RemoteError) {
                if (!quiet) main.post { fail(e.code) }
            } catch (e: IOException) {
                if (!quiet) main.post { fail("io") }
            }
        }
    }

    private fun fail(code: String) {
        if (isDestroyed) return
        ui.snack(
            screen.root,
            when (code) {
                "denied" -> "The computer has this turned off, or that's outside its Music folder"
                "unsupported" -> "The computer needs mpv to play music"
                "not_found" -> "The computer can't find that"
                else -> "Couldn't reach the computer's player"
            },
        )
    }

    private inner class Holder(row: LinearLayout) : RecyclerView.ViewHolder(row) {
        val badge: FrameLayout = ui.avatar(R.drawable.ic_folder)
        val glyph = badge.getChildAt(0) as ImageView
        val title: TextView = ui.title("")
        val playAll = ui.iconButton(R.drawable.ic_play, "Play all") {}

        init {
            row.orientation = LinearLayout.HORIZONTAL
            row.gravity = Gravity.CENTER_VERTICAL
            row.minimumHeight = ui.dp(64)
            row.setPadding(ui.dp(8), ui.dp(6), ui.dp(4), ui.dp(6))
            row.isClickable = true
            row.isFocusable = true
            val ripple = TypedValue()
            theme.resolveAttribute(android.R.attr.selectableItemBackground, ripple, true)
            row.setBackgroundResource(ripple.resourceId)
            row.layoutParams = RecyclerView.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            row.addView(badge)
            row.addView(title, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            row.addView(playAll)
        }

        fun bind(e: MusicEntry) {
            title.text = e.name
            glyph.setImageResource(if (e.dir) R.drawable.ic_folder else R.drawable.ic_music)
            glyph.imageTintList = ColorStateList.valueOf(
                md(if (e.dir) R.color.md_on_primary_container else R.color.md_on_surface_variant),
            )
            (badge.background as? GradientDrawable)?.setColor(
                md(if (e.dir) R.color.md_primary_container else R.color.md_surface_container_highest),
            )
            playAll.visibility = if (e.dir) View.VISIBLE else View.GONE
            playAll.setOnClickListener { play(child(e.name)) }
            itemView.setOnClickListener { if (e.dir) load(child(e.name)) else play(child(e.name)) }
        }
    }

    private inner class Adapter : RecyclerView.Adapter<Holder>() {
        override fun getItemCount() = entries.size

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) = Holder(LinearLayout(parent.context))

        override fun onBindViewHolder(holder: Holder, position: Int) = holder.bind(entries[position])
    }

    companion object {
        const val EXTRA_PEER = "peer"
        private const val STATE_PATH = "path"
        private const val POLL_MS = 1000L
    }
}
