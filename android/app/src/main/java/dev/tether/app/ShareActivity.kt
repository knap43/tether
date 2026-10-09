package dev.tether.app

import android.app.Activity
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.progressindicator.LinearProgressIndicator
import dev.tether.R
import dev.tether.core.Session
import com.google.android.material.R as MR
import java.io.IOException

object Transfers {
    /** Sends content URIs to a computer on a background thread; `done(ok, message)` runs on that thread. */
    fun send(context: Context, peerId: String, uris: List<Uri>, done: (Boolean, String) -> Unit) {
        Thread {
            val node = Hub.node
            if (node == null) {
                done(false, "Tether isn't running")
            } else {
                var sent = 0
                var error: String? = null
                for (u in uris) {
                    try {
                        node.sendFile(peerId, displayName(context, u)) {
                            context.contentResolver.openInputStream(u) ?: throw IOException("cannot open $u")
                        }
                        sent++
                    } catch (e: Exception) {
                        error = e.message ?: e.javaClass.simpleName
                    }
                }
                val noun = if (uris.size == 1) "file" else "files"
                done(error == null, if (error == null) "Sent $sent $noun" else "Sent $sent of ${uris.size}: $error")
            }
        }.start()
    }

    fun displayName(context: Context, uri: Uri): String {
        runCatching {
            context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
                if (c.moveToFirst()) {
                    val name = c.getString(0)
                    if (!name.isNullOrBlank()) return name
                }
            }
        }
        return uri.lastPathSegment?.substringAfterLast('/') ?: "file"
    }

    /** Waits (blocking) up to `timeoutMs` for a connected computer. */
    fun waitForComputer(timeoutMs: Long = 10_000): Session? {
        val end = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < end) {
            Hub.computers().firstOrNull()?.let { return it }
            Thread.sleep(250)
        }
        return null
    }
}

/** Target of the system share sheet: files go to the computer, plain text to its clipboard. */
class ShareActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Hub.start(this)
        val ui = Ui(this)
        val box = ui.column(ui.dp(24)).apply {
            background = ui.rounded(R.color.md_surface_container_high, 28)
            minimumWidth = (resources.displayMetrics.widthPixels * 0.8).toInt()
            addView(ui.avatar(R.drawable.ic_upload))
            addView(
                ui.text("Sending to your computer", MR.style.TextAppearance_Material3_TitleLarge, R.color.md_on_surface)
                    .apply { setPadding(0, ui.dp(16), 0, ui.dp(16)) },
            )
            addView(LinearProgressIndicator(this@ShareActivity).apply { isIndeterminate = true })
        }
        setContentView(box)
        setFinishOnTouchOutside(false)

        val uris = streams(intent)
        val text = intent?.getStringExtra(Intent.EXTRA_TEXT)
        Thread {
            val pc = Transfers.waitForComputer()
            when {
                pc == null -> finishWith("No computer connected")
                uris.isEmpty() && !text.isNullOrEmpty() -> {
                    Hub.node?.onLocalClipboard(text, force = true)
                    finishWith("Sent to your computer's clipboard")
                }
                uris.isEmpty() -> finishWith("Nothing to send")
                else -> Transfers.send(this, pc.peerId, uris) { _, msg -> finishWith(msg) }
            }
        }.start()
    }

    private fun finishWith(msg: String) = runOnUiThread {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
        finish()
    }

    @Suppress("DEPRECATION")
    private fun streams(intent: Intent?): List<Uri> {
        if (intent == null) return emptyList()
        return when (intent.action) {
            Intent.ACTION_SEND -> listOfNotNull(
                if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
                else intent.getParcelableExtra(Intent.EXTRA_STREAM),
            )
            Intent.ACTION_SEND_MULTIPLE ->
                (
                    if (Build.VERSION.SDK_INT >= 33) intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
                    else intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)
                    )?.filterNotNull() ?: emptyList()
            else -> emptyList()
        }
    }
}

/** “Send to computer” in the text-selection menu. */
class ProcessTextActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Hub.start(this)
        val text = intent?.getCharSequenceExtra(Intent.EXTRA_PROCESS_TEXT)?.toString()
        Thread {
            val pc = Transfers.waitForComputer(5_000)
            val msg = when {
                text.isNullOrEmpty() -> "Nothing selected"
                pc == null -> "No computer connected"
                else -> {
                    Hub.node?.onLocalClipboard(text, force = true)
                    "Sent to your computer's clipboard"
                }
            }
            runOnUiThread {
                Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
                finish()
            }
        }.start()
    }
}

/**
 * Invisible activity that reads the clipboard once it has window focus (the only moment
 * Android allows it) and sends it to the computer.
 */
class ClipboardActivity : Activity() {
    companion object {
        const val EXTRA_AUTO = "auto"
    }

    private var done = false

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (!hasFocus || done) return
        done = true
        val auto = intent?.getBooleanExtra(EXTRA_AUTO, false) == true
        if (Hub.node == null || Hub.computers().isEmpty()) {
            if (!auto) toast("No computer connected")
        } else {
            val sent = ClipCapture.capture(this, force = !auto)
            if (!auto) toast(if (sent == null) "The clipboard is empty" else if (sent == "image") "Image sent" else "Clipboard sent")
        }
        finish()
        overridePendingTransition(0, 0)
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
}
