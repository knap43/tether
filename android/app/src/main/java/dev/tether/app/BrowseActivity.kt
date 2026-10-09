package dev.tether.app

import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.res.ColorStateList
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Bundle
import android.provider.DocumentsContract
import android.provider.OpenableColumns
import android.text.TextUtils
import android.text.format.DateUtils
import android.util.TypedValue
import android.view.Gravity
import android.view.MenuItem
import android.view.View
import android.view.ViewGroup
import android.webkit.MimeTypeMap
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.PopupMenu
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.progressindicator.LinearProgressIndicator
import dev.tether.R
import dev.tether.core.NodeEvent
import dev.tether.core.RemoteError
import dev.tether.core.RemoteFs
import java.io.IOException
import com.google.android.material.R as M

/**
 * Browses a computer's shared folders inside the app: open, download, upload,
 * rename, delete and new folders. Paths are the protocol's virtual paths,
 * `/<share>/<relative path>`, with `/` listing the shares.
 */
class BrowseActivity : AppCompatActivity() {
    companion object {
        const val EXTRA_PEER = "peer"
        private const val STATE_PATH = "path"
        private const val STATE_ROOT = "root"
    }

    private lateinit var screen: Screen
    private val ui get() = screen.ui
    private lateinit var peer: String
    private lateinit var computer: String
    private lateinit var fs: RemoteFs

    /** Where "up" stops: `/`, or the only share when the computer shares just one folder. */
    private var rootPath = "/"
    private var path = "/"
    private var entries: List<RemoteFs.Entry> = emptyList()

    /** Every name in the folder, hidden ones included, so uploads never overwrite them. */
    private var names: Set<String> = emptySet()
    private var loadSeq = 0
    private var failed = false

    private lateinit var progress: LinearProgressIndicator
    private lateinit var location: TextView
    private lateinit var message: TextView
    private lateinit var list: RecyclerView
    private val adapter = Adapter()
    private var uploadItem: MenuItem? = null
    private var folderItem: MenuItem? = null

    private val listener: (NodeEvent) -> Unit = { onNodeEvent(it) }

    private val pickUploads = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if (!uris.isNullOrEmpty()) upload(uris)
    }

    private val back = object : OnBackPressedCallback(false) {
        override fun handleOnBackPressed() = goUp()
    }

    // -- lifecycle ------------------------------------------------------------------

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val id = intent?.getStringExtra(EXTRA_PEER)
        val node = Hub.node
        if (id == null || node == null) {
            finish()
            return
        }
        peer = id
        computer = node.peers.get(id)?.name ?: "Computer"
        fs = RemoteFs(node, id)
        savedInstanceState?.let {
            rootPath = it.getString(STATE_ROOT) ?: "/"
            path = it.getString(STATE_PATH) ?: rootPath
        }

        screen = Screen(this, computer, back = true, scrolling = false)
        screen.toolbar.setNavigationOnClickListener { goUp() }
        onBackPressedDispatcher.addCallback(this, back)
        buildMenu()

        val content = screen.content
        location = ui.text("", M.style.TextAppearance_Material3_LabelLarge, R.color.md_primary, 1, TextUtils.TruncateAt.START)
            .apply { setPadding(ui.dp(4), ui.dp(4), ui.dp(4), ui.dp(8)) }
        content.addView(location)
        progress = LinearProgressIndicator(this).apply {
            isIndeterminate = true
            visibility = View.INVISIBLE
        }
        content.addView(progress)
        message = ui.help("").apply {
            gravity = Gravity.CENTER
            setPadding(ui.dp(24), ui.dp(48), ui.dp(24), ui.dp(48))
            visibility = View.GONE
        }
        content.addView(message, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        list = RecyclerView(this).apply {
            layoutManager = LinearLayoutManager(this@BrowseActivity)
            adapter = this@BrowseActivity.adapter
            clipToPadding = false
        }
        content.addView(list, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        load(path)
    }

    override fun onResume() {
        super.onResume()
        Hub.addListener(listener)
    }

    override fun onPause() {
        Hub.removeListener(listener)
        super.onPause()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(STATE_PATH, path)
        outState.putString(STATE_ROOT, rootPath)
    }

    private fun onNodeEvent(e: NodeEvent) {
        if (e !is NodeEvent.StatusChanged) return
        val online = Hub.node?.isConnected(peer) == true
        if (!online) showMessage("$computer is not connected.")
        else if (failed) load(path)
    }

    // -- toolbar ----------------------------------------------------------------------

    private fun buildMenu() {
        val tint = ColorStateList.valueOf(getColor(R.color.md_on_surface_variant))
        fun add(title: String, icon: Int, action: () -> Unit) =
            screen.toolbar.menu.add(title).apply {
                setIcon(icon)
                iconTintList = tint
                setShowAsAction(MenuItem.SHOW_AS_ACTION_IF_ROOM)
                setOnMenuItemClickListener { action(); true }
            }
        uploadItem = add("Upload files", R.drawable.ic_upload) { pickUploads.launch(arrayOf("*/*")) }
        folderItem = add("New folder", R.drawable.ic_create_folder) {
            ui.prompt("New folder", "", "Name", ok = "Create") { name -> createFolder(name) }
        }
        add("Refresh", R.drawable.ic_sync) { load(path) }
    }

    // -- navigation -------------------------------------------------------------------

    private fun child(name: String) = if (path.endsWith("/")) path + name else "$path/$name"

    private fun parentOf(p: String) = p.trimEnd('/').substringBeforeLast('/').ifEmpty { "/" }

    /** `/` lists the shares; a share and anything inside it can hold files. */
    private val inShare get() = path != "/"

    /** The shares themselves can't be renamed or deleted from here. */
    private fun isShare(p: String) = p.trim('/').let { it.isNotEmpty() && '/' !in it }

    private fun goUp() {
        if (path == rootPath) finish() else load(parentOf(path))
    }

    private fun load(target: String) {
        val seq = ++loadSeq
        progress.visibility = View.VISIBLE
        Thread {
            val result = runCatching { fs.list(target) }
            runOnUiThread {
                if (seq != loadSeq || isDestroyed) return@runOnUiThread
                progress.visibility = View.INVISIBLE
                result.onSuccess { show(target, it) }.onFailure { showMessage(describe(it)) }
            }
        }.start()
    }

    private fun show(target: String, listing: List<RemoteFs.Entry>) {
        val visible = listing
            .filter { !it.name.startsWith(".") } // hidden files, and Tether's own temporaries
            .sortedWith(compareBy<RemoteFs.Entry>({ !it.dir }, { it.name.lowercase() }))
        // A computer sharing a single folder opens straight into it.
        if (target == "/" && rootPath == "/" && visible.size == 1 && visible[0].dir) {
            rootPath = "/" + visible[0].name
            load(rootPath)
            return
        }
        failed = false
        path = target
        entries = visible
        names = listing.map { it.name }.toSet()
        adapter.notifyDataSetChanged()
        list.scrollToPosition(0)

        val atRoot = path == rootPath
        screen.toolbar.title = if (atRoot && rootPath == "/") computer else path.substringAfterLast('/')
        screen.toolbar.subtitle = if (atRoot && rootPath == "/") "Shared folders" else computer
        location.text = if (path == "/") "" else path.trim('/').replace("/", "  ›  ")
        location.visibility = if (path == "/") View.GONE else View.VISIBLE
        uploadItem?.isVisible = inShare
        folderItem?.isVisible = inShare
        back.isEnabled = !atRoot

        list.visibility = if (visible.isEmpty()) View.GONE else View.VISIBLE
        message.visibility = if (visible.isEmpty()) View.VISIBLE else View.GONE
        message.text = when {
            visible.isNotEmpty() -> ""
            path == "/" -> "$computer shares no folders. Add one in the Tether window, or run “tether share add NAME PATH”."
            else -> "This folder is empty."
        }
    }

    private fun showMessage(text: String) {
        failed = true
        progress.visibility = View.INVISIBLE
        list.visibility = View.GONE
        message.visibility = View.VISIBLE
        message.text = text
    }

    private fun describe(e: Throwable): String = when ((e as? RemoteError)?.code) {
        "not_found" -> "That's no longer there."
        "denied" -> "The computer refused: it's outside the shared folders."
        "exists" -> "Something with that name already exists."
        "cancelled" -> "Cancelled."
        else -> e.message ?: e.javaClass.simpleName
    }

    // -- actions ----------------------------------------------------------------------

    private fun snack(msg: String) = ui.snack(screen.root, msg)

    /** Runs remote work off the main thread, then reloads the folder; errors become snackbars. */
    private fun act(done: String? = null, work: () -> Unit) {
        progress.visibility = View.VISIBLE
        Thread {
            val error = runCatching(work).exceptionOrNull()
            runOnUiThread {
                if (isDestroyed) return@runOnUiThread
                if (error != null) snack(describe(error)) else if (done != null) snack(done)
                load(path)
            }
        }.start()
    }

    /** Streams the file into another app through the documents provider, so nothing is copied first. */
    private fun open(e: RemoteFs.Entry, choose: Boolean = false) {
        val ext = e.name.substringAfterLast('.', "").lowercase()
        val mime = MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext) ?: "application/octet-stream"
        val uri = DocumentsContract.buildDocumentUri(PcDocumentsProvider.AUTHORITY, "$peer:${child(e.name)}")
        val view = Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, mime)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        try {
            startActivity(if (choose) Intent.createChooser(view, e.name) else view)
        } catch (x: ActivityNotFoundException) {
            ui.dialog()
                .setTitle("No app can open ${e.name}")
                .setMessage("Download it to Download/Tether instead?")
                .setPositiveButton("Download") { _, _ -> download(e) }
                .setNegativeButton("Cancel", null)
                .show()
        }
    }

    private fun download(e: RemoteFs.Entry) {
        val node = Hub.node ?: return
        val from = child(e.name)
        snack("Downloading ${e.name}…")
        Thread {
            val error = runCatching { node.downloadFile(peer, from) }.exceptionOrNull()
            runOnUiThread {
                if (isDestroyed) return@runOnUiThread
                snack(if (error == null) "${e.name} saved to Download/Tether" else describe(error))
            }
        }.start()
    }

    private fun upload(uris: List<Uri>) {
        val node = Hub.node ?: return
        val dir = path
        val taken = names.toMutableSet()
        val noun = if (uris.size == 1) "file" else "files"
        snack("Uploading ${uris.size} $noun…")
        progress.visibility = View.VISIBLE
        Thread {
            var sent = 0
            var error: Throwable? = null
            for (u in uris) {
                try {
                    val name = unique(Transfers.displayName(this, u).replace('/', '_'), taken)
                    taken += name
                    val target = if (dir.endsWith("/")) dir + name else "$dir/$name"
                    node.uploadFile(peer, target, sizeOf(u)) {
                        contentResolver.openInputStream(u) ?: throw IOException("cannot open $u")
                    }
                    sent++
                } catch (e: Exception) {
                    error = e
                    if ((e as? RemoteError)?.code == "cancelled") break
                }
            }
            runOnUiThread {
                if (isDestroyed) return@runOnUiThread
                snack(
                    if (error == null) "Uploaded $sent $noun"
                    else "Uploaded $sent of ${uris.size}: ${describe(error!!)}",
                )
                if (path == dir) load(dir)
            }
        }.start()
    }

    private fun sizeOf(uri: Uri): Long? = runCatching {
        contentResolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)?.use { c ->
            if (c.moveToFirst() && !c.isNull(0)) c.getLong(0) else null
        }
    }.getOrNull()

    /** "name.ext", or "name (1).ext" and so on when the folder already has it. */
    private fun unique(name: String, taken: Set<String>): String {
        if (name !in taken) return name
        val dot = name.lastIndexOf('.').takeIf { it > 0 } ?: name.length
        var i = 1
        while (true) {
            val candidate = "${name.substring(0, dot)} ($i)${name.substring(dot)}"
            if (candidate !in taken) return candidate
            i++
        }
    }

    private fun createFolder(name: String) {
        val clean = name.replace('/', '_')
        val target = child(clean)
        act { fs.mkdir(target) }
    }

    private fun rename(e: RemoteFs.Entry) {
        ui.prompt("Rename", e.name, "Name", ok = "Rename") { name ->
            val clean = name.replace('/', '_')
            if (clean != e.name) act { fs.move(child(e.name), child(clean), false) }
        }
    }

    private fun delete(e: RemoteFs.Entry) {
        ui.dialog()
            .setTitle("Delete ${e.name}?")
            .setMessage(
                if (e.dir) "The folder and everything in it will be deleted from $computer."
                else "It will be deleted from $computer.",
            )
            .setPositiveButton("Delete") { _, _ -> act("Deleted ${e.name}") { fs.delete(child(e.name)) } }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun showMenu(anchor: View, e: RemoteFs.Entry) {
        val editable = !isShare(child(e.name))
        PopupMenu(this, anchor).apply {
            if (e.dir) {
                menu.add("Open").setOnMenuItemClickListener { load(child(e.name)); true }
            } else {
                menu.add("Open with…").setOnMenuItemClickListener { open(e, choose = true); true }
                menu.add("Download").setOnMenuItemClickListener { download(e); true }
            }
            if (editable) {
                menu.add("Rename").setOnMenuItemClickListener { rename(e); true }
                menu.add("Delete").setOnMenuItemClickListener { delete(e); true }
            }
            show()
        }
    }

    // -- list -------------------------------------------------------------------------

    private fun subtitle(e: RemoteFs.Entry): String {
        val parts = mutableListOf<String>()
        if (!e.dir) parts += humanSize(e.size)
        if (e.mtime > 0) {
            parts += DateUtils.formatDateTime(
                this, e.mtime,
                DateUtils.FORMAT_SHOW_DATE or DateUtils.FORMAT_SHOW_TIME or DateUtils.FORMAT_ABBREV_MONTH,
            )
        }
        return parts.joinToString("  ·  ")
    }

    private inner class Holder(row: LinearLayout) : RecyclerView.ViewHolder(row) {
        val badge: FrameLayout = ui.avatar(R.drawable.ic_folder)
        val glyph = badge.getChildAt(0) as ImageView
        val title: TextView = ui.title("")
        val sub: TextView = ui.text("", lines = 1)
        val more = ui.iconButton(R.drawable.ic_more, "More") {}

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
            val texts = ui.column().apply {
                addView(title)
                addView(sub)
            }
            row.addView(texts, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            row.addView(more)
        }

        fun bind(e: RemoteFs.Entry) {
            title.text = e.name
            val s = subtitle(e)
            sub.text = s
            sub.visibility = if (s.isEmpty()) View.GONE else View.VISIBLE
            glyph.setImageResource(if (e.dir) R.drawable.ic_folder else R.drawable.ic_file)
            glyph.imageTintList = ColorStateList.valueOf(
                getColor(if (e.dir) R.color.md_on_primary_container else R.color.md_on_surface_variant),
            )
            (badge.background as? GradientDrawable)?.setColor(
                getColor(if (e.dir) R.color.md_primary_container else R.color.md_surface_container_highest),
            )
            itemView.setOnClickListener { if (e.dir) load(child(e.name)) else open(e) }
            itemView.setOnLongClickListener { showMenu(more, e); true }
            more.setOnClickListener { showMenu(it, e) }
        }
    }

    private inner class Adapter : RecyclerView.Adapter<Holder>() {
        override fun getItemCount() = entries.size

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) = Holder(LinearLayout(parent.context))

        override fun onBindViewHolder(holder: Holder, position: Int) = holder.bind(entries[position])
    }
}
