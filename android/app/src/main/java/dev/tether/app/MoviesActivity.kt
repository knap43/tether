package dev.tether.app

import android.content.Intent
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
import com.google.android.material.progressindicator.LinearProgressIndicator
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout
import dev.tether.R
import dev.tether.core.MovieEntry
import dev.tether.core.Node
import dev.tether.core.RemoteError
import dev.tether.core.moviesList
import dev.tether.core.moviesPlay
import dev.tether.core.moviesSearch
import java.io.IOException
import java.util.Locale

/** The computer's Movies folder: browse or search it here, and the chosen film plays there. */
class MoviesActivity : AppCompatActivity() {
    private lateinit var node: Node
    private lateinit var peer: String
    private lateinit var screen: Screen
    private lateinit var ui: Ui
    private lateinit var list: RecyclerView
    private lateinit var message: TextView
    private lateinit var search: TextInputEditText

    private val main = Handler(Looper.getMainLooper())
    private val adapter = Adapter()
    private var rows: List<Row> = emptyList()
    private var path = "/"
    private var query = ""
    private var latest = 0 // only the answer to the newest question is shown
    private val runSearch = Runnable { refresh(toTop = true) }

    private val back = object : OnBackPressedCallback(false) {
        override fun handleOnBackPressed() {
            if (query.isNotEmpty()) search.setText("") else load(parent(path), toTop = true)
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
        screen = Screen(this, "Movies", back = true, scrolling = false)
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
                getSystemService(InputMethodManager::class.java).hideSoftInputFromWindow(v.windowToken, 0)
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
                hint = "Search all movies"
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
            layoutManager = LinearLayoutManager(this@MoviesActivity)
            adapter = this@MoviesActivity.adapter
            clipToPadding = false
        }
        content.addView(list, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
    }

    override fun onResume() {
        super.onResume()
        if (::node.isInitialized) refresh(toTop = false) // coming back from a film, its progress has moved on
    }

    override fun onDestroy() {
        main.removeCallbacks(runSearch)
        super.onDestroy()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(STATE_PATH, path)
    }

    private fun refresh(toTop: Boolean) {
        if (query.isEmpty()) load(path, toTop) else find(query, toTop)
    }

    private fun load(target: String, toTop: Boolean) {
        request({ node.moviesList(peer, target) }) { folder ->
            path = target
            val resume = folder.resume.take(MAX_CONTINUE).map { Row.Film(it, showFolder = true) }
            show(
                (if (resume.isEmpty()) emptyList() else listOf(Row.Heading("Continue watching")) + resume + Row.Heading("All movies")) +
                    folder.entries.map { Row.Film(it, showFolder = false) },
                "No movies in this folder",
                toTop,
            )
        }
    }

    private fun find(text: String, toTop: Boolean) {
        request({ node.moviesSearch(peer, text) }) { found ->
            show(found.map { Row.Film(it, showFolder = true) }, "No movies match “$text”", toTop)
        }
    }

    private fun show(found: List<Row>, empty: String, toTop: Boolean) {
        rows = found
        back.isEnabled = query.isNotEmpty() || path != "/"
        screen.toolbar.title = if (path == "/") "Movies" else path.substringAfterLast('/')
        adapter.notifyDataSetChanged()
        if (toTop) list.scrollToPosition(0)
        message.text = empty
        message.visibility = if (found.isEmpty()) View.VISIBLE else View.GONE
    }

    private fun choose(film: MovieEntry) {
        if (film.position <= 0) return play(film, resume = false)
        ui.dialog()
            .setTitle(film.name)
            .setPositiveButton("Resume from ${clock(film.position)}") { _, _ -> play(film, resume = true) }
            .setNegativeButton("Start over") { _, _ -> play(film, resume = false) }
            .show()
    }

    private fun play(film: MovieEntry, resume: Boolean) = request({ node.moviesPlay(peer, film.path, resume) }) {
        startActivity(Intent(this, NowPlayingActivity::class.java).putExtra(NowPlayingActivity.EXTRA_PEER, peer))
    }

    private fun <T> request(work: () -> T, done: (T) -> Unit) {
        val asked = ++latest
        node.pool.execute {
            try {
                val result = work()
                main.post { if (!isDestroyed && asked == latest) done(result) }
            } catch (e: RemoteError) {
                main.post { if (asked == latest) fail(e.code) }
            } catch (e: IOException) {
                main.post { if (asked == latest) fail("io") }
            }
        }
    }

    private fun fail(code: String) {
        if (isDestroyed) return
        ui.snack(
            screen.root,
            when (code) {
                "denied" -> "The computer has this turned off, or that's outside its Movies folder"
                "unsupported" -> "The computer needs mpv to play movies"
                "not_found" -> "The computer can't find that. Does its Movies folder exist?"
                else -> "Couldn't reach the computer's player"
            },
        )
    }

    private sealed interface Row {
        data class Heading(val text: String) : Row
        data class Film(val entry: MovieEntry, val showFolder: Boolean) : Row
    }

    private inner class FilmHolder(row: LinearLayout) : RecyclerView.ViewHolder(row) {
        val badge: FrameLayout = ui.avatar(R.drawable.ic_movie)
        val glyph = badge.getChildAt(0) as ImageView
        val title: TextView = ui.title("")
        val detail: TextView = ui.text("", lines = 1)
        val bar = LinearProgressIndicator(row.context).apply { max = BAR_STEPS }

        init {
            row.orientation = LinearLayout.HORIZONTAL
            row.gravity = Gravity.CENTER_VERTICAL
            row.minimumHeight = ui.dp(64)
            row.setPadding(ui.dp(8), ui.dp(6), ui.dp(12), ui.dp(6))
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
                    addView(bar, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = ui.dp(6) })
                },
                LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f),
            )
        }

        fun bind(row: Row.Film) {
            val e = row.entry
            val started = e.position > 0
            title.text = e.name
            glyph.setImageResource(if (e.dir) R.drawable.ic_folder else if (e.finished) R.drawable.ic_check else R.drawable.ic_movie)
            glyph.imageTintList = ColorStateList.valueOf(
                md(if (e.dir) R.color.md_on_primary_container else R.color.md_on_surface_variant),
            )
            (badge.background as? GradientDrawable)?.setColor(
                md(if (e.dir) R.color.md_primary_container else R.color.md_surface_container_highest),
            )
            val folder = if (row.showFolder) e.path.substringBeforeLast('/', "") else ""
            val state = when {
                e.finished -> "Watched"
                !started -> ""
                e.length > 0 -> "${clock(e.position)} of ${clock(e.length)}"
                else -> "Stopped at ${clock(e.position)}"
            }
            detail.text = listOf(folder, state).filter { it.isNotEmpty() }.joinToString(" · ")
            detail.visibility = if (detail.text.isEmpty()) View.GONE else View.VISIBLE
            bar.visibility = if (started && e.length > 0) View.VISIBLE else View.GONE
            if (e.length > 0) bar.progress = (e.position / e.length * BAR_STEPS).toInt()
            itemView.setOnClickListener { if (e.dir) load(e.path.let { "/$it" }, toTop = true) else choose(e) }
        }
    }

    private inner class Adapter : RecyclerView.Adapter<RecyclerView.ViewHolder>() {
        override fun getItemCount() = rows.size

        override fun getItemViewType(position: Int) = if (rows[position] is Row.Heading) HEADING else FILM

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder =
            if (viewType == HEADING) {
                object : RecyclerView.ViewHolder(ui.section("")) {}
            } else {
                FilmHolder(LinearLayout(parent.context))
            }

        override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
            when (val row = rows[position]) {
                is Row.Heading -> (holder.itemView as TextView).text = row.text
                is Row.Film -> (holder as FilmHolder).bind(row)
            }
        }
    }

    companion object {
        const val EXTRA_PEER = "peer"
        private const val STATE_PATH = "path"
        private const val MAX_CONTINUE = 3
        private const val SEARCH_DELAY_MS = 250L
        private const val BAR_STEPS = 1000
        private const val HEADING = 0
        private const val FILM = 1

        private fun parent(path: String) = path.trimEnd('/').substringBeforeLast('/', "").ifEmpty { "/" }

        private fun clock(seconds: Double): String {
            val s = seconds.toLong()
            return if (s >= 3600) {
                String.format(Locale.ROOT, "%d:%02d:%02d", s / 3600, s / 60 % 60, s % 60)
            } else {
                String.format(Locale.ROOT, "%d:%02d", s / 60, s % 60)
            }
        }
    }
}
