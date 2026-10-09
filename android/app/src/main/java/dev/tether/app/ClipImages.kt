package dev.tether.app

import android.content.ClipData
import android.content.ClipboardManager
import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.MatrixCursor
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.provider.OpenableColumns
import dev.tether.core.MAX_CLIP_IMAGE
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileNotFoundException

/** Serves images received from the computer so other apps can paste them. */
class ClipImageProvider : ContentProvider() {
    companion object {
        const val AUTHORITY = "dev.tether.clip"

        private fun dir(context: Context) = File(context.cacheDir, "clipboard").apply { mkdirs() }

        private fun extFor(mime: String) = when (mime) {
            "image/jpeg" -> "jpg"
            "image/webp" -> "webp"
            "image/gif" -> "gif"
            else -> "png"
        }

        /** Stores the image and returns a content URI for it; older clipboard images are removed. */
        fun publish(context: Context, mime: String, data: ByteArray): Uri {
            val d = dir(context)
            d.listFiles()?.forEach { it.delete() }
            val name = "clipboard-${System.currentTimeMillis()}.${extFor(mime)}"
            File(d, name).writeBytes(data)
            return Uri.parse("content://$AUTHORITY/$name")
        }

        fun setClipboard(context: Context, mime: String, data: ByteArray) {
            val uri = publish(context, mime, data)
            Hub.runOnMain {
                val cm = context.getSystemService(ClipboardManager::class.java)
                cm?.setPrimaryClip(ClipData.newUri(context.contentResolver, "Image from computer", uri))
            }
        }
    }

    override fun onCreate(): Boolean = true

    private fun fileFor(uri: Uri): File {
        val ctx = context ?: throw FileNotFoundException("no context")
        val name = uri.lastPathSegment ?: throw FileNotFoundException("$uri")
        if (name.contains('/') || name.startsWith(".")) throw FileNotFoundException(name)
        val f = File(dir(ctx), name)
        if (!f.isFile) throw FileNotFoundException(name)
        return f
    }

    override fun getType(uri: Uri): String? = when (uri.lastPathSegment?.substringAfterLast('.')) {
        "jpg" -> "image/jpeg"
        "webp" -> "image/webp"
        "gif" -> "image/gif"
        "png" -> "image/png"
        else -> null
    }

    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor =
        ParcelFileDescriptor.open(fileFor(uri), ParcelFileDescriptor.MODE_READ_ONLY)

    override fun query(
        uri: Uri,
        projection: Array<out String>?,
        selection: String?,
        selectionArgs: Array<out String>?,
        sortOrder: String?,
    ): Cursor {
        val f = fileFor(uri)
        val c = MatrixCursor(arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE))
        c.newRow().add(OpenableColumns.DISPLAY_NAME, f.name).add(OpenableColumns.SIZE, f.length())
        return c
    }

    override fun insert(uri: Uri, values: ContentValues?): Uri? = null

    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?): Int = 0

    override fun update(uri: Uri, values: ContentValues?, selection: String?, selectionArgs: Array<out String>?): Int = 0
}

/** Reads the clipboard (only possible while one of our windows has focus) and sends it on. */
object ClipCapture {
    /** Returns "text", "image", or null when there was nothing (new) to send. */
    fun capture(context: Context, force: Boolean): String? {
        val node = Hub.node ?: return null
        val cm = context.getSystemService(ClipboardManager::class.java) ?: return null
        val clip = try {
            cm.primaryClip
        } catch (e: SecurityException) {
            null
        } ?: return null
        if (clip.itemCount == 0) return null
        val item = clip.getItemAt(0) ?: return null
        val uri = item.uri
        if (uri != null) {
            if (uri.authority == ClipImageProvider.AUTHORITY) return null // what we put there ourselves
            val mime = runCatching { context.contentResolver.getType(uri) }.getOrNull().orEmpty()
            if (mime.startsWith("image/")) {
                val png = readAsPng(context, uri, mime) ?: return null
                node.pool.execute { node.sendClipboardImage("image/png", png, force) }
                return "image"
            }
        }
        val text = item.coerceToText(context)?.toString()
        if (text.isNullOrEmpty() || (!force && text == Hub.lastRemoteClip)) return null
        node.pool.execute { node.onLocalClipboard(text, force) }
        return "text"
    }

    private fun readAsPng(context: Context, uri: Uri, mime: String): ByteArray? = runCatching {
        val bytes = context.contentResolver.openInputStream(uri)?.use { input ->
            val out = ByteArrayOutputStream()
            val buf = ByteArray(64 * 1024)
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                out.write(buf, 0, n)
                if (out.size() > MAX_CLIP_IMAGE) return null
            }
            out.toByteArray()
        } ?: return null
        if (mime == "image/png") return bytes
        // GNOME apps paste PNG most reliably; convert anything else.
        val bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: return null
        val out = ByteArrayOutputStream()
        bmp.compress(Bitmap.CompressFormat.PNG, 100, out)
        out.toByteArray().takeIf { it.size <= MAX_CLIP_IMAGE }
    }.getOrNull()
}
