package dev.tether.app

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.ParcelFileDescriptor
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.progressindicator.LinearProgressIndicator
import dev.tether.R
import dev.tether.core.PlayableFile
import dev.tether.core.RemoteError
import dev.tether.core.playFiles
import dev.tether.core.playUrl
import java.io.FileInputStream
import java.io.IOException
import java.nio.ByteBuffer
import com.google.android.material.R as MR

/**
 * Target of the share sheet and "Open with": the computer plays the original link or file itself.
 * A link it can't fetch (a page behind a login, say) opens in its browser instead.
 */
class PlayActivity : AppCompatActivity() {
    private lateinit var ui: Ui

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Hub.start(this)
        ui = Ui(this)
        val box = ui.column(ui.dp(24)).apply {
            background = ui.rounded(R.color.md_surface_container_high, 28)
            minimumWidth = (resources.displayMetrics.widthPixels * 0.8).toInt()
            addView(ui.avatar(R.drawable.ic_screen))
            addView(
                ui.text("Opening on the computer…", MR.style.TextAppearance_Material3_TitleLarge, R.color.md_on_surface)
                    .apply { setPadding(0, ui.dp(16), 0, ui.dp(16)) },
            )
            addView(LinearProgressIndicator(this@PlayActivity).apply { isIndeterminate = true })
        }
        setContentView(box)
        setFinishOnTouchOutside(false)
        if (savedInstanceState != null) return

        val viewedLink = intent?.data?.takeIf { intent?.action == Intent.ACTION_VIEW && it.scheme != "content" }
        val uris = if (viewedLink != null) emptyList() else uris(intent)
        val url = viewedLink?.toString() ?: intent?.getStringExtra(Intent.EXTRA_TEXT)?.let { LINK.find(it)?.value }
        if (uris.isEmpty() && url == null) return finishWith("Nothing here that can be played")
        // The permission to read a shared file lasts only as long as this activity, so open them now.
        Thread {
            val files = try {
                uris.mapIndexed { i, uri -> open(i, uri) }
            } catch (e: Exception) {
                return@Thread finishWith("Couldn't open that file")
            }
            if (Transfers.waitForComputer() == null) {
                files.forEach { it.close() }
                return@Thread finishWith("No computer connected")
            }
            val all = Hub.computers()
            if (all.size <= 1) {
                play(all.first().peerId, all.first().name, url, files)
            } else {
                runOnUiThread { choose(all.map { it.peerId to it.name }, url, files) }
            }
        }.start()
    }

    private fun choose(computers: List<Pair<String, String>>, url: String?, files: List<PlayableFile>) {
        if (isFinishing) return
        ui.dialog()
            .setTitle("Play on")
            .setItems(computers.map { it.second }.toTypedArray()) { _, which ->
                Thread { play(computers[which].first, computers[which].second, url, files) }.start()
            }
            .setNegativeButton("Cancel") { _, _ -> files.forEach { it.close() }; finish() }
            .setOnCancelListener { files.forEach { it.close() }; finish() }
            .show()
    }

    private fun play(peer: String, name: String, url: String?, files: List<PlayableFile>) {
        val node = Hub.node ?: return finishWith("No computer connected")
        try {
            when {
                files.isNotEmpty() -> {
                    node.playFiles(peer, files)
                    showControls(peer)
                    finishWith("Playing on $name")
                }
                node.playUrl(peer, url!!) -> runOnUiThread { // a browser needs a click to start playing
                    startActivity(Intent(this, RemoteActivity::class.java).putExtra(RemoteActivity.EXTRA_PEER, peer))
                    finishWith("Opened in the browser on $name")
                }
                else -> {
                    showControls(peer)
                    finishWith("Opening on $name; a video can take a few seconds to start", long = true)
                }
            }
        } catch (e: RemoteError) {
            when (e.code) {
                "not_playable" -> finishWith("$name couldn't open that link")
                "denied" -> finishWith("$name has playing shared things turned off")
                "unsupported" -> finishWith("$name needs mpv to play this")
                else -> finishWith("Couldn't play it: ${e.message}")
            }
        } catch (e: IOException) {
            finishWith("Lost the connection to $name")
        }
    }

    private fun showControls(peer: String) = runOnUiThread {
        startActivity(Intent(this, NowPlayingActivity::class.java).putExtra(NowPlayingActivity.EXTRA_PEER, peer))
    }

    private fun finishWith(msg: String, long: Boolean = false) = runOnUiThread {
        Toast.makeText(this, msg, if (long) Toast.LENGTH_LONG else Toast.LENGTH_SHORT).show()
        finish()
    }

    private fun uris(intent: Intent?): List<Uri> = when (intent?.action) {
        Intent.ACTION_VIEW -> listOfNotNull(intent.data)
        Intent.ACTION_SEND -> listOfNotNull(
            if (Build.VERSION.SDK_INT >= 33) {
                intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
            } else {
                @Suppress("DEPRECATION")
                intent.getParcelableExtra(Intent.EXTRA_STREAM)
            },
        )
        Intent.ACTION_SEND_MULTIPLE -> if (Build.VERSION.SDK_INT >= 33) {
            intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
        } else {
            @Suppress("DEPRECATION")
            intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM)
        } ?: emptyList()
        else -> emptyList()
    }

    private fun open(index: Int, uri: Uri): PlayableFile {
        val fd = contentResolver.openFileDescriptor(uri, "r") ?: throw IOException("no descriptor")
        if (fd.statSize <= 0) { // a pipe, which can't seek
            fd.close()
            throw IOException("not a regular file")
        }
        return SharedFile("$index", Transfers.displayName(this, uri), fd, contentResolver.getType(uri))
    }

    private class SharedFile(
        override val id: String,
        override val name: String,
        private val fd: ParcelFileDescriptor,
        override val mime: String?,
    ) : PlayableFile {
        private val channel = FileInputStream(fd.fileDescriptor).channel
        override val size = fd.statSize

        override fun read(position: Long, into: ByteArray, off: Int, len: Int) =
            channel.read(ByteBuffer.wrap(into, off, len), position)

        override fun close() {
            runCatching { channel.close() }
            runCatching { fd.close() }
        }
    }

    private companion object {
        val LINK = Regex("""https?://[^\s<>"]+""")
    }
}
