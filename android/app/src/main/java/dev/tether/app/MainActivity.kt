package dev.tether.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.PopupMenu
import androidx.appcompat.widget.Toolbar
import com.google.android.material.button.MaterialButton
import com.google.android.material.progressindicator.LinearProgressIndicator
import com.google.android.material.slider.Slider
import dev.tether.R
import dev.tether.core.Node
import dev.tether.core.NodeEvent
import dev.tether.core.suspendComputer
import com.google.android.material.R as M

/** One computer at a time, and everything you can do with it. Setting things up lives in [SettingsActivity]. */
class MainActivity : AppCompatActivity() {
    companion object {
        const val EXTRA_PAIR = "pair"
        private const val PREFS = "main"
        private const val KEY_COMPUTER = "computer"
        private const val WAKE_MS = 60_000L
    }

    private lateinit var screen: Screen
    private lateinit var switcher: MaterialButton
    private val ui get() = screen.ui
    private val content get() = screen.content
    private var pairDialog: AlertDialog? = null
    private var sendTarget: String? = null
    private var pendingPairPrompt: String? = null
    private val listener: (NodeEvent) -> Unit = { onNodeEvent(it) }
    private var audioSlider: Pair<String, Slider>? = null
    private var audioSliderDragging = false
    private val wakingUntil = mutableMapOf<String, Long>()

    private val pickFiles = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        val target = sendTarget
        if (target != null && !uris.isNullOrEmpty()) {
            Transfers.send(this, target, uris) { _, msg -> runOnUiThread { snack(msg) } }
        }
    }

    // -- lifecycle ------------------------------------------------------------------

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Hub.start(this)
        screen = Screen(this, "", back = false, scrolling = false)
        buildToolbar()
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 10)
        }
        pendingPairPrompt = intent?.getStringExtra(EXTRA_PAIR)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        pendingPairPrompt = intent.getStringExtra(EXTRA_PAIR)
        render()
    }

    override fun onResume() {
        super.onResume()
        Hub.addListener(listener)
        render()
        Hub.node?.let { n -> n.pool.execute { runCatching { n.checkConnections() } } }
    }

    override fun onPause() {
        Hub.removeListener(listener)
        super.onPause()
    }

    private fun onNodeEvent(e: NodeEvent) {
        when (e) {
            is NodeEvent.PairRequest -> showPairDialog(e.id, e.name, e.sas)
            is NodeEvent.Paired -> {
                pairDialog?.dismiss()
                snack("Paired with ${e.name}")
            }
            is NodeEvent.PairFailed -> {
                pairDialog?.dismiss()
                snack("Pairing ${if (e.reason == "timeout") "timed out" else "was cancelled"}")
            }
            is NodeEvent.AudioVolume -> {
                val (peer, slider) = audioSlider ?: return
                if (peer == e.from && !audioSliderDragging) {
                    slider.value = Math.round(e.volume * 100).toFloat().coerceIn(0f, 100f)
                }
                return
            }
            is NodeEvent.StatusChanged, is NodeEvent.Unpaired, is NodeEvent.Introduced -> {}
            else -> return // transfers, media and clipboard events don't change this screen
        }
        render()
    }

    private fun snack(msg: String) = ui.snack(screen.root, msg)

    private fun code(sas: String) = "${sas.substring(0, 3)} ${sas.substring(3)}"

    private fun openSettings() = startActivity(Intent(this, SettingsActivity::class.java))

    // -- the computer in view --------------------------------------------------------------

    private fun buildToolbar() {
        switcher = ui.button("Tether", Ui.Kind.TEXT) { showSwitcher() }.apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 22f)
            setTextColor(md(R.color.md_on_surface))
            maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.END
        }
        screen.toolbar.addView(switcher, Toolbar.LayoutParams(Toolbar.LayoutParams.WRAP_CONTENT, Toolbar.LayoutParams.WRAP_CONTENT, Gravity.START or Gravity.CENTER_VERTICAL))
        screen.toolbar.addView(
            ui.iconButton(R.drawable.ic_settings, "Settings") { openSettings() },
            Toolbar.LayoutParams(Toolbar.LayoutParams.WRAP_CONTENT, Toolbar.LayoutParams.WRAP_CONTENT, Gravity.END or Gravity.CENTER_VERTICAL),
        )
    }

    /** The computer last chosen, or else whichever is connected. */
    private fun selected(node: Node): String? {
        val peers = node.peers.all()
        val chosen = getSharedPreferences(PREFS, MODE_PRIVATE).getString(KEY_COMPUTER, null)
        return peers.firstOrNull { it.id == chosen }?.id
            ?: peers.firstOrNull { node.isConnected(it.id) }?.id
            ?: peers.firstOrNull()?.id
    }

    private fun showSwitcher() {
        val node = Hub.node ?: return
        PopupMenu(this, switcher).apply {
            for (p in node.peers.all()) {
                val state = when {
                    node.isConnected(p.id) -> "connected"
                    node.isAsleep(p.id) -> "asleep"
                    else -> "not connected"
                }
                menu.add("${p.name} · $state").setOnMenuItemClickListener {
                    getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(KEY_COMPUTER, p.id).apply()
                    render()
                    true
                }
            }
            menu.add("Pair another computer").setOnMenuItemClickListener {
                openSettings()
                true
            }
            show()
        }
    }

    // -- rendering ------------------------------------------------------------------------

    private fun render() {
        content.removeAllViews()
        val node = Hub.node
        if (node == null) {
            content.addView(LinearProgressIndicator(this).apply { isIndeterminate = true })
            content.postDelayed({ if (!isFinishing) render() }, 500)
            return
        }
        renderWaitingAnswers(node)
        val id = selected(node)
        val peer = id?.let { node.peers.get(it) }
        switcher.text = if (peer == null) "Tether" else "${peer.name} ▾"
        when {
            peer == null -> content.addView(bigButton("Pair a computer", R.drawable.ic_computer) { openSettings() }, fill())
            node.isConnected(peer.id) -> renderControls(peer.id)
            node.canWake(peer.id) -> renderWake(node, peer.id, peer.name)
            else -> content.addView(
                ui.help("${peer.name} isn't connected. Connect once while it's on, and you can wake it from here later.").apply {
                    gravity = Gravity.CENTER
                },
                fill(),
            )
        }
        pendingPairPrompt?.let { pair ->
            node.pendingPairs().firstOrNull { it.id == pair && !it.confirmed }?.let { showPairDialog(it.id, it.name, it.sas) }
            pendingPairPrompt = null
        }
    }

    /** Someone is waiting on an answer that's given in Settings. */
    private fun renderWaitingAnswers(node: Node) {
        val waiting = node.pendingPairs().size + node.pendingIntroductions().size
        if (waiting == 0) return
        content.addView(
            ui.fullWidth(ui.button("A device is waiting for your answer", Ui.Kind.TONAL, R.drawable.ic_link) { openSettings() }),
        )
    }

    private fun fill() = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f)

    /** The one thing to do here, filling the screen. */
    private fun bigButton(label: String, icon: Int, enabled: Boolean = true, onClick: () -> Unit) =
        ui.button(label, Ui.Kind.FILLED, icon) { onClick() }.apply {
            isEnabled = enabled
            iconGravity = MaterialButton.ICON_GRAVITY_TEXT_TOP
            iconSize = ui.dp(72)
            iconPadding = ui.dp(16)
            cornerRadius = ui.dp(28)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 26f)
        }

    private fun renderWake(node: Node, id: String, name: String) {
        val now = System.currentTimeMillis()
        val until = wakingUntil[id] ?: 0L
        val waking = until > now
        content.addView(
            bigButton(if (waking) "Waking $name…" else "Wake up $name", R.drawable.ic_power, enabled = !waking) {
                wakingUntil[id] = System.currentTimeMillis() + WAKE_MS
                render()
                node.pool.execute {
                    val failed = runCatching { node.wake(id) }.exceptionOrNull()
                    if (failed != null) {
                        runOnUiThread {
                            wakingUntil.remove(id)
                            render()
                            snack(failed.message ?: "Couldn't send the wake-up")
                        }
                    }
                }
                content.postDelayed({ if (!isFinishing) render() }, WAKE_MS + 100)
            },
            fill(),
        )
    }

    private fun renderControls(id: String) {
        fun open(activity: Class<*>, extra: String) = startActivity(Intent(this, activity).putExtra(extra, id))
        val audioOn = PhoneAudio.streamingTo == id
        val screenOn = PhoneScreen.streamingTo == id
        val rows = listOf(
            tile("Movies", R.drawable.ic_movie) { open(MoviesActivity::class.java, MoviesActivity.EXTRA_PEER) } to
                tile("Now playing", R.drawable.ic_play) { open(NowPlayingActivity::class.java, NowPlayingActivity.EXTRA_PEER) },
            tile("Music", R.drawable.ic_music) { open(MusicActivity::class.java, MusicActivity.EXTRA_PEER) } to
                tile("Browse", R.drawable.ic_folder) { open(BrowseActivity::class.java, BrowseActivity.EXTRA_PEER) },
            tile("Send files", R.drawable.ic_upload) {
                sendTarget = id
                pickFiles.launch(arrayOf("*/*"))
            } to tile("Clipboard", R.drawable.ic_clipboard) { sendClipboardNow() },
            tile("Commands", R.drawable.ic_terminal) { open(CommandsActivity::class.java, CommandsActivity.EXTRA_PEER) } to
                tile("Suspend", R.drawable.ic_power) { confirmSuspend(id) },
            tile(if (audioOn) "Stop audio" else "Phone audio", if (audioOn) R.drawable.ic_volume_off else R.drawable.ic_volume, audioOn) {
                if (audioOn) PhoneAudio.stop(this) else PhoneAudio.start(this, id)
            } to tile(if (screenOn) "Stop screen" else "Show screen", R.drawable.ic_screen, screenOn) {
                if (screenOn) PhoneScreen.stop(this) else PhoneScreen.start(this, id)
            },
        )
        val gap = ui.dp(5)
        for ((left, right) in rows) {
            content.addView(
                LinearLayout(this).apply {
                    orientation = LinearLayout.HORIZONTAL
                    for (t in listOf(left, right)) {
                        addView(t, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f).apply { setMargins(gap, gap, gap, gap) })
                    }
                },
                fill(),
            )
        }
        if (audioOn) content.addView(audioVolumeRow(id))
    }

    private fun tile(label: String, icon: Int, active: Boolean = false, onClick: () -> Unit): View = ui.column().apply {
        gravity = Gravity.CENTER
        background = ui.rounded(if (active) R.color.md_primary_container else R.color.md_surface_container_high, 28)
        clipToOutline = true
        isClickable = true
        isFocusable = true
        contentDescription = label
        val ripple = TypedValue()
        theme.resolveAttribute(android.R.attr.selectableItemBackground, ripple, true)
        foreground = getDrawable(ripple.resourceId)
        setOnClickListener { onClick() }
        addView(ui.icon(icon, if (active) R.color.md_on_primary_container else R.color.md_primary, sizeDp = 36))
        addView(
            ui.text(label, M.style.TextAppearance_Material3_TitleMedium, if (active) R.color.md_on_primary_container else R.color.md_on_surface).apply {
                gravity = Gravity.CENTER
                setPadding(0, ui.dp(8), 0, 0)
            },
        )
    }

    /** Volume the computer plays this phone's audio at; sent when the finger lifts. */
    private fun audioVolumeRow(peer: String): View {
        val slider = ui.bigSlider(100f).apply {
            stepSize = 1f
            value = Math.round((PhoneAudio.volume[peer] ?: 1.0) * 100).toFloat().coerceIn(0f, 100f)
            addOnSliderTouchListener(object : Slider.OnSliderTouchListener {
                override fun onStartTrackingTouch(slider: Slider) {
                    audioSliderDragging = true
                }

                override fun onStopTrackingTouch(slider: Slider) {
                    audioSliderDragging = false
                    PhoneAudio.setVolume(this@MainActivity, peer, slider.value / 100.0)
                }
            })
        }
        audioSlider = peer to slider
        return ui.volumeRow(slider).apply { setPadding(ui.dp(8), ui.dp(8), ui.dp(8), 0) }
    }

    // -- actions --------------------------------------------------------------------------

    private fun showPairDialog(id: String, name: String, sas: String) {
        if (isFinishing) return
        pairDialog?.dismiss()
        val codeView = ui.text(code(sas), M.style.TextAppearance_Material3_DisplaySmall, R.color.md_primary).apply {
            gravity = Gravity.CENTER
            letterSpacing = 0.08f
            setPadding(0, ui.dp(16), 0, ui.dp(4))
        }
        pairDialog = ui.dialog()
            .setTitle("Pair with $name?")
            .setMessage("Make sure $name shows the same code.")
            .setView(codeView)
            .setPositiveButton("Codes match") { _, _ -> Thread { Hub.node?.confirmPairing(id, true) }.start() }
            .setNegativeButton("Cancel") { _, _ -> Thread { Hub.node?.confirmPairing(id, false) }.start() }
            .setCancelable(false)
            .show()
    }

    /** A stray tap shouldn't put the computer to sleep in the middle of something. */
    private fun confirmSuspend(id: String) {
        val node = Hub.node ?: return
        val name = node.peers.get(id)?.name ?: "the computer"
        ui.dialog()
            .setTitle("Suspend $name?")
            .setPositiveButton("Suspend") { _, _ ->
                node.pool.execute {
                    val failed = runCatching { node.suspendComputer(id) }.exceptionOrNull()
                    if (failed != null) runOnUiThread { snack("Couldn't suspend $name") }
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun sendClipboardNow() {
        if (Hub.computers().isEmpty()) {
            snack("No computer connected")
            return
        }
        when (ClipCapture.capture(this, force = true)) {
            null -> snack("The clipboard is empty")
            "image" -> snack("Image sent")
            else -> snack("Clipboard sent")
        }
    }
}
