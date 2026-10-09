package dev.tether.app

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.PowerManager
import android.provider.DocumentsContract
import android.provider.Settings
import android.text.TextUtils
import android.view.Gravity
import android.view.View
import android.widget.LinearLayout
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.PopupMenu
import com.google.android.material.progressindicator.LinearProgressIndicator
import dev.tether.R
import dev.tether.core.Node
import dev.tether.core.NodeEvent
import dev.tether.core.SyncFolder
import com.google.android.material.R as M

class MainActivity : AppCompatActivity() {
    companion object {
        const val EXTRA_PAIR = "pair"
    }

    private lateinit var screen: Screen
    private val ui get() = screen.ui
    private val content get() = screen.content
    private var pairDialog: AlertDialog? = null
    private var sendTarget: String? = null
    private var pendingPairPrompt: String? = null
    private val listener: (NodeEvent) -> Unit = { onNodeEvent(it) }

    private val pickFiles = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        val target = sendTarget
        if (target != null && !uris.isNullOrEmpty()) {
            Transfers.send(this, target, uris) { _, msg -> runOnUiThread { snack(msg) } }
        }
    }
    private val pickShareFolder = registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
        onShareFolderPicked(uri)
    }
    private val pickSyncFolder = registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
        onSyncFolderPicked(uri)
    }

    // -- lifecycle ------------------------------------------------------------------

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Hub.start(this)
        screen = Screen(this, "Tether", back = false)
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
            is NodeEvent.StatusChanged, is NodeEvent.Unpaired -> {}
            else -> return // transfers, media and clipboard events don't change this screen
        }
        render()
    }

    // -- helpers ----------------------------------------------------------------------

    private fun snack(msg: String) = ui.snack(screen.root, msg)

    /** Runs network or disk work off the main thread; errors become snackbars. */
    private fun bg(work: () -> Unit) {
        Thread {
            try {
                work()
            } catch (e: Exception) {
                runOnUiThread { snack(e.message ?: e.javaClass.simpleName) }
            }
        }.start()
    }

    private fun code(sas: String) = "${sas.substring(0, 3)} ${sas.substring(3)}"

    // -- rendering ------------------------------------------------------------------------

    private fun render() {
        content.removeAllViews()
        val node = Hub.node
        if (node == null) {
            screen.toolbar.subtitle = "Starting…"
            content.addView(LinearProgressIndicator(this).apply { isIndeterminate = true })
            content.postDelayed({ if (!isFinishing) render() }, 500)
            return
        }
        val online = Hub.computers()
        screen.toolbar.subtitle = when {
            online.isNotEmpty() -> "Connected to ${online.joinToString { it.name }}"
            node.peers.all().isNotEmpty() -> "Waiting for your computer"
            else -> "Not paired yet"
        }
        renderDevice(node)
        renderComputers(node)
        renderClipboard(node)
        renderNotifications(node)
        renderFiles(node)
        renderBackground()
        pendingPairPrompt?.let { id ->
            node.pendingPairs().firstOrNull { it.id == id && !it.confirmed }?.let { showPairDialog(it.id, it.name, it.sas) }
            pendingPairPrompt = null
        }
    }

    private fun renderDevice(node: Node) {
        val card = ui.card(content, padding = 12)
        card.addView(
            ui.row(
                node.settings.name,
                "This phone · ID ${node.identity.id.take(8)}",
                leading = ui.avatar(R.drawable.ic_phone),
                trailing = listOf(
                    ui.iconButton(R.drawable.ic_edit, "Rename") {
                        ui.prompt("Device name", node.settings.name, "Name shown on your computer") {
                            node.settings.name = it
                            node.settings.save()
                            render()
                        }
                    },
                ),
            ),
        )
    }

    private fun renderComputers(node: Node) {
        content.addView(ui.section("Computers"))
        val peers = node.peers.all()

        for (pp in node.pendingPairs()) {
            val card = ui.card(content)
            card.addView(ui.title("Pair with ${pp.name}?"))
            card.addView(ui.help("Make sure your computer shows the same code."))
            card.addView(
                ui.text(code(pp.sas), M.style.TextAppearance_Material3_DisplaySmall, R.color.md_primary).apply {
                    gravity = Gravity.CENTER
                    letterSpacing = 0.08f
                    setPadding(0, ui.dp(12), 0, ui.dp(8))
                },
            )
            if (pp.confirmed) {
                card.addView(ui.help("Waiting for the computer to confirm…").apply { gravity = Gravity.CENTER })
                card.addView(LinearProgressIndicator(this).apply { isIndeterminate = true })
            } else {
                card.addView(
                    ui.buttons(
                        ui.button("Cancel", Ui.Kind.OUTLINED) { bg { node.confirmPairing(pp.id, false) } },
                        ui.button("Codes match", Ui.Kind.FILLED) { bg { node.confirmPairing(pp.id, true) } },
                    ),
                )
            }
        }

        for (p in peers) {
            val online = node.isConnected(p.id)
            val card = ui.card(content, padding = 12)
            val more = ui.iconButton(R.drawable.ic_more, "More") {}
            more.setOnClickListener { v -> showPeerMenu(v, node, p.id, p.name) }
            card.addView(
                ui.row(
                    p.name,
                    if (online) "Connected" else "Not connected",
                    leading = ui.avatar(R.drawable.ic_computer, active = online),
                    trailing = listOf(more),
                ),
            )
            if (online) {
                card.addView(
                    ui.chips(
                        ui.chip("Send files", R.drawable.ic_upload) {
                            sendTarget = p.id
                            pickFiles.launch(arrayOf("*/*"))
                        },
                        ui.chip("Clipboard", R.drawable.ic_clipboard) { sendClipboardNow() },
                        ui.chip("Media", R.drawable.ic_music) {
                            startActivity(Intent(this, MediaActivity::class.java).putExtra(MediaActivity.EXTRA_PEER, p.id))
                        },
                        ui.chip("Commands", R.drawable.ic_terminal) {
                            startActivity(Intent(this, CommandsActivity::class.java).putExtra(CommandsActivity.EXTRA_PEER, p.id))
                        },
                    ),
                )
            } else if (node.canWake(p.id)) {
                card.addView(
                    ui.chips(
                        ui.chip("Wake up", R.drawable.ic_power) {
                            snack("Waking ${p.name}…")
                            bg {
                                node.wake(p.id)
                                runOnUiThread { snack("Sent. Tether reconnects when ${p.name} is up.") }
                            }
                        },
                    ),
                )
            } else {
                card.addView(ui.help("Connect once while it's on, and you can wake it from here later."))
            }
        }

        val nearby = node.discovery.recent().filter { !node.peers.contains(it.id) }
        if (nearby.isNotEmpty() || peers.isEmpty()) {
            val card = ui.card(content, padding = 12)
            if (nearby.isEmpty()) {
                card.addView(
                    ui.row(
                        "Looking for computers…",
                        "Start Tether on your computer, then run “tether pair” or open the Tether window.",
                        leading = ui.avatar(R.drawable.ic_search, active = false),
                    ),
                )
            }
            nearby.forEachIndexed { i, d ->
                if (i > 0) card.addView(ui.divider())
                card.addView(
                    ui.row(
                        d.name, d.host,
                        leading = ui.avatar(R.drawable.ic_computer, active = false),
                        trailing = listOf(ui.button("Pair", Ui.Kind.TONAL) { bg { node.startPairing(peerId = d.id) } }),
                    ),
                )
            }
        }
        content.addView(
            ui.button("Pair by IP address", Ui.Kind.TEXT, R.drawable.ic_link) {
                ui.prompt("Computer's address", "", "192.168.1.20", ok = "Pair") { addr ->
                    bg { node.startPairing(address = addr) }
                }
            }.apply { layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT) },
        )
    }

    private fun showPeerMenu(anchor: View, node: Node, id: String, name: String) {
        PopupMenu(this, anchor).apply {
            menu.add("Unpair").setOnMenuItemClickListener {
                ui.dialog()
                    .setTitle("Unpair $name?")
                    .setMessage("You'll need to pair again to reconnect.")
                    .setPositiveButton("Unpair") { _, _ -> bg { node.unpair(id) } }
                    .setNegativeButton("Cancel", null)
                    .show()
                true
            }
            show()
        }
    }

    private fun renderClipboard(node: Node) {
        content.addView(ui.section("Clipboard"))
        val card = ui.card(content)
        card.addView(
            ui.switchRow("Sync clipboard", "Text and images, both ways", node.settings.clipboard) {
                node.settings.clipboard = it
                node.settings.save()
            },
        )
        card.addView(ui.fullWidth(ui.button("Send clipboard now", Ui.Kind.TONAL, R.drawable.ic_clipboard) { sendClipboardNow() }))
        if (TetherService.autoClipboardAvailable(this)) {
            card.addView(ui.help("Automatic: anything you copy on the phone goes to your computer."))
            return
        }
        card.addView(
            ui.help(
                "Computer → phone is automatic. Android only lets apps read the clipboard while they're open, " +
                    "so phone → computer uses the button above, the Quick Settings tile, or “Send to computer” " +
                    "when you select text.",
            ).apply { setPadding(0, ui.dp(12), 0, 0) },
        )
        card.addView(
            ui.help("For fully automatic sync, run this once from your computer (USB debugging on), then restart Tether:")
                .apply { setPadding(0, ui.dp(8), 0, 0) },
        )
        val cmds = "adb shell pm grant $packageName android.permission.READ_LOGS\n" +
            "adb shell appops set $packageName SYSTEM_ALERT_WINDOW allow"
        card.addView(
            ui.code(cmds) {
                Hub.lastRemoteClip = cmds
                getSystemService(ClipboardManager::class.java)?.setPrimaryClip(ClipData.newPlainText("adb", cmds))
                snack("Copied")
            },
        )
        card.addView(ui.fullWidth(ui.button("Restart Tether", Ui.Kind.OUTLINED) { restartService() }))
    }

    private fun renderNotifications(node: Node) {
        content.addView(ui.section("Notifications"))
        val card = ui.card(content)
        if (!NotifListener.enabled(this)) {
            card.addView(
                ui.row(
                    "Show notifications on your computer",
                    "Reply to messages from the desktop. Tether needs notification access for this.",
                    leading = ui.avatar(R.drawable.ic_notifications, active = false),
                ),
            )
            card.addView(
                ui.fullWidth(
                    ui.button("Allow notification access", Ui.Kind.FILLED) {
                        startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
                    },
                ),
            )
            return
        }
        card.addView(
            ui.switchRow(
                "Show notifications on your computer",
                "Choose apps in the Tether window's Settings on the computer",
                node.settings.forwardNotifications,
            ) {
                node.settings.forwardNotifications = it
                node.settings.save()
            },
        )
    }

    private fun renderFiles(node: Node) {
        content.addView(ui.section("Files"))
        if (!Environment.isExternalStorageManager()) {
            val warn = ui.card(content)
            warn.addView(
                ui.row(
                    "Allow access to all files",
                    "Needed so your computer can browse and sync folders.",
                    leading = ui.avatar(R.drawable.ic_lock),
                ),
            )
            warn.addView(
                ui.fullWidth(
                    ui.button("Open settings", Ui.Kind.FILLED) {
                        startActivity(
                            Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, Uri.parse("package:$packageName")),
                        )
                    },
                ),
            )
        }

        val shared = ui.card(content)
        shared.addView(ui.title("Shared with your computer"))
        shared.addView(ui.help("Your computer can browse these. Its own folders appear in the Files app under its name."))
        for ((name, path) in node.settings.shares) {
            shared.addView(
                ui.row(
                    name, path,
                    leading = ui.icon(R.drawable.ic_folder, R.color.md_primary).apply {
                        (layoutParams as LinearLayout.LayoutParams).marginEnd = ui.dp(16)
                    },
                    trailing = listOf(
                        ui.iconButton(R.drawable.ic_delete, "Stop sharing $name") {
                            node.updateShares(node.settings.shares - name)
                            render()
                        },
                    ),
                    subtitleEllipsize = TextUtils.TruncateAt.MIDDLE,
                ),
            )
        }
        shared.addView(ui.button("Share a folder", Ui.Kind.TEXT, R.drawable.ic_add) { pickShareFolder.launch(null) })

        val synced = ui.card(content)
        synced.addView(ui.title("Synced folders"))
        synced.addView(ui.help("Kept identical on both devices. Use the same name on the computer."))
        for (f in node.settings.sync) {
            synced.addView(
                ui.row(
                    f.id, f.path,
                    leading = ui.icon(R.drawable.ic_sync, R.color.md_primary).apply {
                        (layoutParams as LinearLayout.LayoutParams).marginEnd = ui.dp(16)
                    },
                    trailing = listOf(
                        ui.iconButton(R.drawable.ic_delete, "Stop syncing ${f.id}") {
                            node.updateSyncFolders(node.settings.sync.filter { it.id != f.id })
                            render()
                        },
                    ),
                    subtitleEllipsize = TextUtils.TruncateAt.MIDDLE,
                ),
            )
        }
        synced.addView(ui.button("Sync a folder", Ui.Kind.TEXT, R.drawable.ic_add) { pickSyncFolder.launch(null) })

        content.addView(ui.help("Received files are saved in Download/Tether.").apply { setPadding(ui.dp(4), 0, ui.dp(4), 0) })
    }

    private fun renderBackground() {
        val pm = getSystemService(PowerManager::class.java)
        if (pm == null || pm.isIgnoringBatteryOptimizations(packageName)) return
        content.addView(ui.section("Background"))
        val card = ui.card(content)
        card.addView(
            ui.row(
                "Stay connected in the background",
                "Without this, Android may pause Tether when the screen is off.",
                leading = ui.avatar(R.drawable.ic_battery),
            ),
        )
        card.addView(
            ui.fullWidth(
                ui.button("Allow", Ui.Kind.FILLED) {
                    @Suppress("BatteryLife")
                    startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
                },
            ),
        )
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
            .setMessage("Make sure your computer shows the same code.")
            .setView(codeView)
            .setPositiveButton("Codes match") { _, _ -> bg { Hub.node?.confirmPairing(id, true) } }
            .setNegativeButton("Cancel") { _, _ -> bg { Hub.node?.confirmPairing(id, false) } }
            .setCancelable(false)
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

    private fun restartService() {
        stopService(Intent(this, TetherService::class.java))
        content.postDelayed({ Hub.start(this); render() }, 800)
    }

    private fun onShareFolderPicked(uri: Uri?) {
        val node = Hub.node ?: return
        uri ?: return
        val path = treeToPath(uri) ?: return snack("That location can't be shared")
        var name = path.trimEnd('/').substringAfterLast('/').ifEmpty { "Storage" }
        while (node.settings.shares.containsKey(name)) name += "+"
        node.updateShares(node.settings.shares + (name to path))
        render()
    }

    private fun onSyncFolderPicked(uri: Uri?) {
        val node = Hub.node ?: return
        uri ?: return
        val path = treeToPath(uri) ?: return snack("That location can't be synced")
        val suggested = path.trimEnd('/').substringAfterLast('/').lowercase()
            .replace(Regex("[^a-z0-9_-]"), "-").trim('-').ifEmpty { "folder" }
        ui.prompt("Name this folder", suggested, "Same name on both devices", ok = "Sync") { raw ->
            val id = raw.lowercase().replace(Regex("[^a-z0-9_-]"), "-")
            node.updateSyncFolders(node.settings.sync.filter { it.id != id } + SyncFolder(id, path))
            snack("Now on your computer: tether sync add $id ~/path")
            render()
        }
    }

    /** Converts a document-tree URI from the system picker into a filesystem path. */
    private fun treeToPath(uri: Uri): String? {
        val docId = runCatching { DocumentsContract.getTreeDocumentId(uri) }.getOrNull() ?: return null
        val volume = docId.substringBefore(':')
        val rel = docId.substringAfter(':', "")
        val base = if (volume == "primary") Environment.getExternalStorageDirectory().path else "/storage/$volume"
        return if (rel.isEmpty()) base else "$base/$rel"
    }
}
