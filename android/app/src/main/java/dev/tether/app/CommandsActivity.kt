package dev.tether.app

import android.graphics.Typeface
import android.os.Bundle
import android.text.InputType
import android.view.KeyEvent
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.LinearLayout
import androidx.appcompat.app.AppCompatActivity
import androidx.core.widget.NestedScrollView
import com.google.android.material.chip.Chip
import com.google.android.material.progressindicator.LinearProgressIndicator
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout
import dev.tether.R
import dev.tether.core.RemoteCommands
import com.google.android.material.R as M

/** Runs the computer's saved commands, or free-form shell when the computer allows it, and shows the output. */
class CommandsActivity : AppCompatActivity() {
    companion object {
        const val EXTRA_PEER = "peer"
    }

    private lateinit var screen: Screen
    private val ui get() = screen.ui
    private lateinit var savedCard: LinearLayout
    private lateinit var shellField: TextInputLayout
    private lateinit var shellInput: TextInputEditText
    private lateinit var progress: LinearProgressIndicator
    private lateinit var output: android.widget.TextView
    private var remote: RemoteCommands? = null
    private var busy = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val peer = intent?.getStringExtra(EXTRA_PEER)
        val node = Hub.node
        if (peer == null || node == null) {
            finish()
            return
        }
        remote = RemoteCommands(node, peer)
        screen = Screen(this, "Commands", back = true)
        screen.toolbar.subtitle = node.peers.get(peer)?.name ?: "Computer"
        val content = screen.content

        content.addView(ui.section("Saved commands"))
        savedCard = ui.card(content, padding = 12)
        savedCard.addView(ui.help("Loading…"))

        shellInput = TextInputEditText(this).apply {
            typeface = Typeface.MONOSPACE
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            imeOptions = EditorInfo.IME_ACTION_SEND
            setSingleLine()
            setOnEditorActionListener { _, action, event ->
                if (action == EditorInfo.IME_ACTION_SEND ||
                    (event != null && event.keyCode == KeyEvent.KEYCODE_ENTER && event.action == KeyEvent.ACTION_DOWN)
                ) {
                    runShell()
                    true
                } else {
                    false
                }
            }
        }
        shellField = TextInputLayout(this, null, R.attr.tetherOutlinedField).apply {
            hint = "Shell command"
            endIconMode = TextInputLayout.END_ICON_CUSTOM
            setEndIconDrawable(R.drawable.ic_send)
            endIconContentDescription = "Run"
            setEndIconOnClickListener { runShell() }
            addView(shellInput, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
            visibility = View.GONE
            layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)
                .apply { topMargin = ui.dp(4) }
        }
        content.addView(shellField)

        content.addView(ui.section("Output"))
        progress = LinearProgressIndicator(this).apply {
            isIndeterminate = true
            visibility = View.INVISIBLE
        }
        content.addView(progress)
        output = ui.text("", M.style.TextAppearance_Material3_BodySmall, R.color.md_on_surface).apply {
            typeface = Typeface.MONOSPACE
            setTextIsSelectable(true)
            minHeight = ui.dp(160)
            background = ui.rounded(R.color.md_surface_container_lowest, 16)
            setPadding(ui.dp(14), ui.dp(12), ui.dp(14), ui.dp(12))
        }
        content.addView(output, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        load()
    }

    private fun load() {
        val rc = remote ?: return
        Thread {
            try {
                val listing = rc.list()
                runOnUiThread { show(listing) }
            } catch (e: Exception) {
                runOnUiThread {
                    savedCard.removeAllViews()
                    savedCard.addView(ui.help("Couldn't reach the computer: ${e.message}"))
                }
            }
        }.start()
    }

    private fun show(listing: RemoteCommands.Listing) {
        savedCard.removeAllViews()
        if (listing.commands.isEmpty()) {
            savedCard.addView(ui.help("No saved commands. Add some on the computer, in the Tether window or with:"))
            savedCard.addView(ui.code("tether command add \"Lock screen\" 'loginctl lock-session'") {})
        } else {
            val chips = listing.commands.map { c ->
                ui.chip(c.name, R.drawable.ic_play) { execute("▶ ${c.name}") { it.run(c.id) } }
            }.toTypedArray<Chip>()
            savedCard.addView(ui.chips(*chips))
        }
        shellField.visibility = if (listing.shell) View.VISIBLE else View.GONE
        if (!listing.shell) {
            screen.content.addView(
                ui.help("Free-form commands are off. To allow them, run “tether set remote_shell on” on the computer.")
                    .apply { setPadding(ui.dp(4), ui.dp(8), ui.dp(4), 0) },
                screen.content.indexOfChild(shellField) + 1,
            )
        }
    }

    private fun runShell() {
        val line = shellInput.text?.toString()?.trim().orEmpty()
        if (line.isNotEmpty()) {
            execute("$ $line") { it.shell(line) }
            shellInput.setText("")
        }
    }

    private fun execute(label: String, run: (RemoteCommands) -> RemoteCommands.Result) {
        val rc = remote ?: return
        if (busy) return
        busy = true
        progress.visibility = View.VISIBLE
        append(if (output.text.isNullOrEmpty()) "$label\n" else "\n$label\n")
        Thread {
            val text = try {
                val r = run(rc)
                buildString {
                    if (r.output.isNotEmpty()) append(r.output.trimEnd()).append('\n')
                    if (r.truncated) append("[output truncated]\n")
                    append(
                        when {
                            r.timedOut -> "[stopped: took too long]"
                            r.exit == 0 -> "[done]"
                            else -> "[exit ${r.exit}]"
                        },
                    )
                    append('\n')
                }
            } catch (e: Exception) {
                "[failed: ${e.message}]\n"
            }
            runOnUiThread {
                append(text)
                busy = false
                progress.visibility = View.INVISIBLE
            }
        }.start()
    }

    private fun append(s: String) {
        output.append(s)
        (screen.content.parent as? NestedScrollView)?.let { sv -> sv.post { sv.fullScroll(View.FOCUS_DOWN) } }
    }
}
