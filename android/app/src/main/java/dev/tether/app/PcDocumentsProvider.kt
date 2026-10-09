package dev.tether.app

import android.database.Cursor
import android.database.MatrixCursor
import android.os.CancellationSignal
import android.os.Handler
import android.os.HandlerThread
import android.os.ParcelFileDescriptor
import android.os.ProxyFileDescriptorCallback
import android.os.storage.StorageManager
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import android.provider.DocumentsContract.Root
import android.provider.DocumentsProvider
import android.system.ErrnoException
import android.system.OsConstants
import android.webkit.MimeTypeMap
import dev.tether.R
import dev.tether.core.RemoteError
import dev.tether.core.RemoteFs
import java.io.File
import java.io.FileInputStream
import java.io.FileNotFoundException
import java.io.FileOutputStream

/**
 * Shows each connected computer's shared folders in the system Files app (and every
 * "open file" dialog). Document ids are "<peer id>:<virtual path>".
 */
class PcDocumentsProvider : DocumentsProvider() {
    companion object {
        const val AUTHORITY = "dev.tether.documents"
        private const val BLOCK = 1 shl 20

        private val ROOT_COLUMNS = arrayOf(
            Root.COLUMN_ROOT_ID, Root.COLUMN_DOCUMENT_ID, Root.COLUMN_TITLE, Root.COLUMN_SUMMARY,
            Root.COLUMN_FLAGS, Root.COLUMN_ICON,
        )
        private val DOC_COLUMNS = arrayOf(
            Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME, Document.COLUMN_MIME_TYPE,
            Document.COLUMN_SIZE, Document.COLUMN_LAST_MODIFIED, Document.COLUMN_FLAGS,
        )
    }

    private val ioThread by lazy { HandlerThread("tether-documents").apply { start() } }
    private val ioHandler by lazy { Handler(ioThread.looper) }

    override fun onCreate(): Boolean = true

    // -- ids ------------------------------------------------------------------------

    private fun split(docId: String?): Pair<String, String> {
        val id = docId ?: throw FileNotFoundException("no document id")
        val colon = id.indexOf(':')
        if (colon < 0) throw FileNotFoundException(id)
        return id.substring(0, colon) to id.substring(colon + 1).ifEmpty { "/" }
    }

    private fun docId(peer: String, path: String) = "$peer:$path"

    private fun fs(peer: String): RemoteFs {
        val node = Hub.node ?: throw FileNotFoundException("Tether is not running")
        if (!node.isConnected(peer)) throw FileNotFoundException("computer not connected")
        return RemoteFs(node, peer)
    }

    private fun parentOf(path: String) = path.trimEnd('/').substringBeforeLast('/').ifEmpty { "/" }

    private fun join(dir: String, name: String) = if (dir.endsWith("/")) dir + name else "$dir/$name"

    private inline fun <T> remote(block: () -> T): T = try {
        block()
    } catch (e: RemoteError) {
        throw FileNotFoundException(e.message)
    } catch (e: java.io.IOException) {
        throw FileNotFoundException(e.message)
    }

    // -- rows -----------------------------------------------------------------------

    private fun mimeFor(name: String, dir: Boolean): String {
        if (dir) return Document.MIME_TYPE_DIR
        val ext = name.substringAfterLast('.', "").lowercase()
        return MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext) ?: "application/octet-stream"
    }

    private fun addRow(c: MatrixCursor, peer: String, path: String, e: RemoteFs.Entry, displayName: String) {
        val topLevel = path.trim('/').count { it == '/' } == 0 // "/" or "/<share>"
        var flags = 0
        if (e.dir) {
            flags = flags or Document.FLAG_DIR_SUPPORTS_CREATE
        } else {
            flags = flags or Document.FLAG_SUPPORTS_WRITE
        }
        if (!topLevel) flags = flags or Document.FLAG_SUPPORTS_DELETE or Document.FLAG_SUPPORTS_RENAME
        if (path == "/") flags = flags and Document.FLAG_DIR_SUPPORTS_CREATE.inv()
        c.newRow()
            .add(Document.COLUMN_DOCUMENT_ID, docId(peer, path))
            .add(Document.COLUMN_DISPLAY_NAME, displayName)
            .add(Document.COLUMN_MIME_TYPE, mimeFor(e.name, e.dir))
            .add(Document.COLUMN_SIZE, if (e.dir) null else e.size)
            .add(Document.COLUMN_LAST_MODIFIED, if (e.mtime > 0) e.mtime else null)
            .add(Document.COLUMN_FLAGS, flags)
    }

    // -- queries --------------------------------------------------------------------

    override fun queryRoots(projection: Array<out String>?): Cursor {
        val c = MatrixCursor(projection ?: ROOT_COLUMNS)
        for (s in Hub.computers()) {
            c.newRow()
                .add(Root.COLUMN_ROOT_ID, s.peerId)
                .add(Root.COLUMN_DOCUMENT_ID, docId(s.peerId, "/"))
                .add(Root.COLUMN_TITLE, s.name)
                .add(Root.COLUMN_SUMMARY, "Tether")
                .add(Root.COLUMN_FLAGS, Root.FLAG_SUPPORTS_CREATE or Root.FLAG_SUPPORTS_IS_CHILD)
                .add(Root.COLUMN_ICON, R.mipmap.ic_launcher)
        }
        context?.let { c.setNotificationUri(it.contentResolver, DocumentsContract.buildRootsUri(AUTHORITY)) }
        return c
    }

    override fun queryDocument(documentId: String?, projection: Array<out String>?): Cursor {
        val (peer, path) = split(documentId)
        val c = MatrixCursor(projection ?: DOC_COLUMNS)
        val e = remote { fs(peer).stat(path) }
        val name = if (path == "/") Hub.node?.peers?.get(peer)?.name ?: "Computer" else e.name
        addRow(c, peer, path, e, name)
        return c
    }

    override fun queryChildDocuments(parentDocumentId: String?, projection: Array<out String>?, sortOrder: String?): Cursor {
        val (peer, path) = split(parentDocumentId)
        val c = MatrixCursor(projection ?: DOC_COLUMNS)
        for (e in remote { fs(peer).list(path) }) {
            if (e.name.startsWith(".tether")) continue
            addRow(c, peer, join(path, e.name), e, e.name)
        }
        context?.let {
            c.setNotificationUri(it.contentResolver, DocumentsContract.buildChildDocumentsUri(AUTHORITY, parentDocumentId))
        }
        return c
    }

    override fun getDocumentType(documentId: String?): String {
        val (peer, path) = split(documentId)
        val e = remote { fs(peer).stat(path) }
        return mimeFor(e.name, e.dir)
    }

    override fun isChildDocument(parentDocumentId: String?, documentId: String?): Boolean {
        if (parentDocumentId == null || documentId == null) return false
        val (pp, ppath) = split(parentDocumentId)
        val (cp, cpath) = split(documentId)
        return pp == cp && (ppath == "/" || cpath.startsWith(ppath.trimEnd('/') + "/"))
    }

    // -- changes --------------------------------------------------------------------

    private fun notifyChildren(peer: String, dir: String) {
        context?.contentResolver?.notifyChange(
            DocumentsContract.buildChildDocumentsUri(AUTHORITY, docId(peer, dir)), null,
        )
    }

    override fun createDocument(parentDocumentId: String?, mimeType: String?, displayName: String?): String {
        val (peer, dir) = split(parentDocumentId)
        val fs = fs(peer)
        val base = (displayName ?: "New").replace('/', '_')
        val existing = remote { fs.list(dir) }.map { it.name }.toSet()
        var name = base
        var i = 1
        while (name in existing) {
            val dot = base.lastIndexOf('.').takeIf { it > 0 && mimeType != Document.MIME_TYPE_DIR } ?: base.length
            name = "${base.substring(0, dot)} ($i)${base.substring(dot)}"
            i++
        }
        val path = join(dir, name)
        remote {
            if (mimeType == Document.MIME_TYPE_DIR) fs.mkdir(path) else fs.write(path, ByteArray(0).inputStream(), 0)
        }
        notifyChildren(peer, dir)
        return docId(peer, path)
    }

    override fun deleteDocument(documentId: String?) {
        val (peer, path) = split(documentId)
        remote { fs(peer).delete(path) }
        notifyChildren(peer, parentOf(path))
    }

    override fun renameDocument(documentId: String?, displayName: String?): String {
        val (peer, path) = split(documentId)
        val name = (displayName ?: throw FileNotFoundException("no name")).replace('/', '_')
        val target = join(parentOf(path), name)
        remote { fs(peer).move(path, target, false) }
        notifyChildren(peer, parentOf(path))
        return docId(peer, target)
    }

    // -- content --------------------------------------------------------------------

    override fun openDocument(documentId: String?, mode: String?, signal: CancellationSignal?): ParcelFileDescriptor {
        val (peer, path) = split(documentId)
        val m = mode ?: "r"
        val fs = fs(peer)
        val ctx = context ?: throw FileNotFoundException("no context")
        if (m == "r") {
            val size = remote { fs.stat(path) }.size
            val sm = ctx.getSystemService(StorageManager::class.java) ?: throw FileNotFoundException("no storage manager")
            return sm.openProxyFileDescriptor(ParcelFileDescriptor.MODE_READ_ONLY, RemoteReader(fs, path, size), ioHandler)
        }
        // Writable modes: work on a local copy and upload it when the caller closes the file.
        val tmp = File.createTempFile("edit-", ".tmp", ctx.cacheDir)
        if (!m.contains('t') && m != "w") {
            remote { FileOutputStream(tmp).use { fs.read(path, 0, -1, it) } }
        }
        return ParcelFileDescriptor.open(tmp, ParcelFileDescriptor.parseMode(m), ioHandler) { err ->
            Thread {
                try {
                    if (err == null) {
                        FileInputStream(tmp).use { fs.write(path, it, tmp.length()) }
                        notifyChildren(peer, parentOf(path))
                    }
                } catch (e: Exception) {
                    Hub.node?.platform?.log("upload of $path failed: $e")
                } finally {
                    tmp.delete()
                }
            }.start()
        }
    }

    /** Random-access reads straight from the computer, in 1 MiB blocks with a one-block cache. */
    private class RemoteReader(private val fs: RemoteFs, private val path: String, private val size: Long) :
        ProxyFileDescriptorCallback() {
        private var blockStart = -1L
        private var block = ByteArray(0)

        override fun onGetSize(): Long = size

        override fun onRead(offset: Long, size: Int, data: ByteArray): Int {
            if (offset >= this.size) return 0
            var copied = 0
            try {
                while (copied < size && offset + copied < this.size) {
                    val pos = offset + copied
                    if (pos < blockStart || pos >= blockStart + block.size || blockStart < 0) {
                        blockStart = pos - (pos % BLOCK)
                        block = fs.readBytes(path, blockStart, BLOCK)
                        if (block.isEmpty()) break
                    }
                    val inBlock = (pos - blockStart).toInt()
                    val n = minOf(size - copied, block.size - inBlock)
                    System.arraycopy(block, inBlock, data, copied, n)
                    copied += n
                }
            } catch (e: Exception) {
                throw ErrnoException("onRead", OsConstants.EIO)
            }
            return copied
        }

        override fun onRelease() {
            block = ByteArray(0)
        }
    }
}
