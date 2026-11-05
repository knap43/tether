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
import dev.tether.R
import dev.tether.core.MusicEntry
import dev.tether.core.Node
import dev.tether.core.RemoteError
import dev.tether.core.musicList
import dev.tether.core.musicPlay
import java.io.IOException

/** The computer's Music folder: browse it here, and it plays there. */
class MusicActivity : AppCompatActivity() {
    private lateinit var node: Node
    private lateinit var peer: String
    private lateinit var screen: Screen
    private lateinit var ui: Ui
    private lateinit var list: RecyclerView
    private lateinit var message: TextView
    private lateinit var controls: PlayerControls

    private val main = Handler(Looper.getMainLooper())
    private val adapter = Adapter()
    private var entries: List<MusicEntry> = emptyList()
    private var path = "/"

    private val back = object : OnBackPressedCallback(false) {
        override fun handleOnBackPressed() = load(path.trimEnd('/').substringBeforeLast('/', "").ifEmpty { "/" })
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
        controls = PlayerControls(this, ui, node, peer, content)
        load(path)
    }

    override fun onResume() {
        super.onResume()
        if (::controls.isInitialized) controls.start()
    }

    override fun onPause() {
        if (::controls.isInitialized) controls.stop()
        super.onPause()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(STATE_PATH, path)
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

    private fun play(target: String) = request({ node.musicPlay(peer, target) }) { controls.refresh() }

    private fun <T> request(work: () -> T, done: (T) -> Unit) {
        node.pool.execute {
            try {
                val result = work()
                main.post { if (!isDestroyed) done(result) }
            } catch (e: RemoteError) {
                main.post { fail(e.code) }
            } catch (e: IOException) {
                main.post { fail("io") }
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
    }
}
