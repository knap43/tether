package dev.tether.app

import android.content.res.ColorStateList
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.Editable
import android.text.InputType
import android.text.TextWatcher
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.R as M
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout
import dev.tether.R
import dev.tether.core.MusicEntry
import dev.tether.core.Node
import dev.tether.core.RemoteError
import dev.tether.core.musicList
import dev.tether.core.musicPlay
import dev.tether.core.musicSearch
import java.io.IOException

/** The computer's Music folder: browse or search it here, and it plays there. */
class MusicActivity : AppCompatActivity() {
    private lateinit var node: Node
    private lateinit var peer: String
    private lateinit var screen: Screen
    private lateinit var ui: Ui
    private lateinit var list: RecyclerView
    private lateinit var message: TextView
    private lateinit var controls: PlayerControls
    private lateinit var search: TextInputEditText

    private val main = Handler(Looper.getMainLooper())
    private val adapter = Adapter()
    private var entries: List<MusicEntry> = emptyList()
    private var path = "/"
    private var query = ""
    private var latest = 0 // only the answer to the newest question is shown
    private val runSearch = Runnable { refresh() }

    private val back = object : OnBackPressedCallback(false) {
        override fun handleOnBackPressed() {
            if (query.isNotEmpty()) search.setText("") else load(path.trimEnd('/').substringBeforeLast('/', "").ifEmpty { "/" })
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
        search = TextInputEditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT
            imeOptions = EditorInfo.IME_ACTION_SEARCH
            setSingleLine()
            setOnEditorActionListener { v, action, _ ->
                if (action != EditorInfo.IME_ACTION_SEARCH) return@setOnEditorActionListener false
                hideKeyboard(v)
                true
            }
            addTextChangedListener(object : TextWatcher {
                override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit
                override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) = Unit
                override fun afterTextChanged(s: Editable?) {
                    query = s?.toString()?.trim().orEmpty()
                    main.removeCallbacks(runSearch)
                    main.postDelayed(runSearch, SEARCH_DELAY_MS)
                }
            })
        }
        content.addView(
            TextInputLayout(this, null, R.attr.tetherOutlinedField).apply {
                hint = "Search all music"
                setStartIconDrawable(R.drawable.ic_search)
                endIconMode = TextInputLayout.END_ICON_CLEAR_TEXT
                addView(search, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
            },
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply {
                setMargins(ui.dp(12), 0, ui.dp(12), ui.dp(8))
            },
        )
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

    override fun onDestroy() {
        main.removeCallbacks(runSearch)
        super.onDestroy()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(STATE_PATH, path)
    }

    private fun refresh() {
        if (query.isEmpty()) load(path) else find(query)
    }

    private fun load(target: String) {
        request({ node.musicList(peer, target) }) { found ->
            path = target
            show(found, "Nothing to play in this folder")
        }
    }

    private fun find(text: String) {
        request({ node.musicSearch(peer, text) }, searching = true) { found ->
            show(found, "No music matches “$text”")
        }
    }

    private fun show(found: List<MusicEntry>, empty: String) {
        entries = found
        back.isEnabled = query.isNotEmpty() || path != "/"
        screen.toolbar.title = if (path == "/") "Music" else path.substringAfterLast('/')
        adapter.notifyDataSetChanged()
        list.scrollToPosition(0)
        message.text = empty
        message.visibility = if (found.isEmpty()) View.VISIBLE else View.GONE
    }

    /** A folder found by searching opens like any other, and the search is put away. */
    private fun open(folder: String) {
        hideKeyboard(search)
        search.setText("")
        main.removeCallbacks(runSearch)
        load(folder)
    }

    private fun hideKeyboard(v: View) {
        getSystemService(InputMethodManager::class.java).hideSoftInputFromWindow(v.windowToken, 0)
        v.clearFocus()
    }

    /** Where an entry lives: a search result carries its whole path, a listed one is in the open folder. */
    private fun where(e: MusicEntry) = if (e.path.isNotEmpty()) "/" + e.path else path.trimEnd('/') + "/" + e.name

    private fun play(target: String) = node.pool.execute {
        try {
            node.musicPlay(peer, target)
            main.post { if (!isDestroyed) controls.refresh() }
        } catch (e: RemoteError) {
            main.post { fail(e.code) }
        } catch (e: IOException) {
            main.post { fail("io") }
        }
    }

    /** Browsing and searching: only the answer to the newest question is shown. */
    private fun <T> request(work: () -> T, searching: Boolean = false, done: (T) -> Unit) {
        val asked = ++latest
        node.pool.execute {
            try {
                val result = work()
                main.post { if (!isDestroyed && asked == latest) done(result) }
            } catch (e: RemoteError) {
                main.post { if (asked == latest) fail(e.code, searching) }
            } catch (e: IOException) {
                main.post { if (asked == latest) fail("io") }
            }
        }
    }

    private fun fail(code: String, searching: Boolean = false) {
        if (isDestroyed) return
        ui.snack(
            screen.root,
            when {
                code == "unsupported" && searching -> "Update Tether on the computer to search its music"
                code == "denied" -> "The computer has this turned off, or that's outside its Music folder"
                code == "unsupported" -> "The computer needs mpv to play music"
                code == "not_found" -> "The computer can't find that"
                else -> "Couldn't reach the computer's player"
            },
        )
    }

    private inner class Holder(row: LinearLayout) : RecyclerView.ViewHolder(row) {
        val badge: FrameLayout = ui.avatar(R.drawable.ic_folder)
        val glyph = badge.getChildAt(0) as ImageView
        val title: TextView = ui.title("")
        val detail: TextView = ui.text("", lines = 1)
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
            row.addView(
                ui.column().apply {
                    addView(title)
                    addView(detail)
                },
                LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f),
            )
            row.addView(playAll)
        }

        fun bind(e: MusicEntry) {
            title.text = e.name
            detail.text = e.path.substringBeforeLast('/', "") // a search result's folder; nothing when browsing
            detail.visibility = if (detail.text.isEmpty()) View.GONE else View.VISIBLE
            glyph.setImageResource(if (e.dir) R.drawable.ic_folder else R.drawable.ic_music)
            glyph.imageTintList = ColorStateList.valueOf(
                md(if (e.dir) R.color.md_on_primary_container else R.color.md_on_surface_variant),
            )
            (badge.background as? GradientDrawable)?.setColor(
                md(if (e.dir) R.color.md_primary_container else R.color.md_surface_container_highest),
            )
            playAll.visibility = if (e.dir) View.VISIBLE else View.GONE
            playAll.setOnClickListener { play(where(e)) }
            itemView.setOnClickListener { if (e.dir) open(where(e)) else play(where(e)) }
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
        private const val SEARCH_DELAY_MS = 250L
    }
}
