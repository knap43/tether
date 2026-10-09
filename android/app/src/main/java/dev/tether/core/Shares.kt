package dev.tether.core

import java.io.File
import java.io.IOException
import java.nio.file.AccessDeniedException
import java.nio.file.DirectoryNotEmptyException
import java.nio.file.FileAlreadyExistsException
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.NoSuchFileException
import java.nio.file.NotDirectoryException
import java.nio.file.StandardCopyOption

const val TMP_PREFIX = ".tether-tmp-"

fun splitPath(vpath: String?): List<String> {
    if (vpath == null || vpath.contains('\u0000')) throw RemoteError("bad_request")
    val parts = vpath.split('/').filter { it.isNotEmpty() && it != "." }
    if (parts.contains("..")) throw RemoteError("denied")
    return parts
}

fun within(root: String, path: String): Boolean =
    path == root || path.startsWith(root.trimEnd('/') + "/")

fun ioError(e: Exception): RemoteError = when (e) {
    is RemoteError -> e
    is NoSuchFileException, is java.io.FileNotFoundException -> RemoteError("not_found", e.message ?: "")
    is AccessDeniedException, is SecurityException -> RemoteError("denied", e.message ?: "")
    is FileAlreadyExistsException, is DirectoryNotEmptyException -> RemoteError("exists", e.message ?: "")
    is NotDirectoryException -> RemoteError("not_dir", e.message ?: "")
    else -> RemoteError("io", e.message ?: e.javaClass.simpleName)
}

/** Recursive delete that never follows symlinks out of the tree. */
fun deleteTree(root: java.nio.file.Path) {
    Files.walkFileTree(root, object : java.nio.file.SimpleFileVisitor<java.nio.file.Path>() {
        override fun visitFile(file: java.nio.file.Path, attrs: java.nio.file.attribute.BasicFileAttributes):
            java.nio.file.FileVisitResult {
            Files.delete(file)
            return java.nio.file.FileVisitResult.CONTINUE
        }

        override fun postVisitDirectory(dir: java.nio.file.Path, exc: IOException?): java.nio.file.FileVisitResult {
            if (exc != null) throw exc
            Files.delete(dir)
            return java.nio.file.FileVisitResult.CONTINUE
        }
    })
}

fun entryFor(name: String, f: File): Map<String, Any> = mapOf(
    "name" to name,
    "dir" to f.isDirectory,
    "size" to if (f.isDirectory) 0L else f.length(),
    "mtime" to f.lastModified(),
)

/** Folders exposed for live browsing as /<share>/<relative path>. */
class Shares(shares: Map<String, String>) {
    val roots: Map<String, String> = shares
        .filterKeys { it.isNotEmpty() && !it.contains('/') }
        .mapValues { File(it.value).canonicalPath }

    /** Real file for a virtual path, or null for the virtual root. */
    fun resolve(vpath: String?, mustExist: Boolean = true, follow: Boolean = true): File? {
        val parts = splitPath(vpath)
        if (parts.isEmpty()) return null
        val root = roots[parts[0]] ?: throw RemoteError("not_found")
        val rest = parts.drop(1)
        val real = if (follow || rest.isEmpty()) {
            File(root, rest.joinToString("/")).canonicalFile
        } else {
            File(File(root, rest.dropLast(1).joinToString("/")).canonicalFile, rest.last())
        }
        if (!within(root, real.path)) throw RemoteError("denied")
        if (mustExist && !Files.exists(real.toPath(), LinkOption.NOFOLLOW_LINKS)) throw RemoteError("not_found")
        return real
    }

    private fun isRoot(f: File) = roots.values.contains(f.path)

    fun list(vpath: String?): List<Map<String, Any>> {
        val real = resolve(vpath)
            ?: return roots.mapNotNull { (n, p) -> File(p).takeIf { it.exists() }?.let { entryFor(n, it) } }
        if (!real.isDirectory) throw RemoteError("not_dir")
        val kids = real.listFiles() ?: throw RemoteError("denied")
        return kids.filter { !it.name.startsWith(TMP_PREFIX) }.map { entryFor(it.name, it) }
    }

    fun stat(vpath: String?): Map<String, Any> {
        val real = resolve(vpath) ?: return mapOf("name" to "", "dir" to true, "size" to 0L, "mtime" to 0L)
        return entryFor(real.name, real)
    }

    fun beginWrite(vpath: String?): Pair<File, File> {
        val real = resolve(vpath, mustExist = false, follow = false)
        if (real == null || isRoot(real)) throw RemoteError("denied")
        val parent = real.parentFile
        if (parent == null || !parent.isDirectory) throw RemoteError("not_found")
        if (real.isDirectory) throw RemoteError("is_dir")
        val tmp = try {
            File.createTempFile(TMP_PREFIX, "", parent)
        } catch (e: IOException) {
            throw ioError(e)
        }
        return tmp to real
    }

    fun mkdir(vpath: String?) {
        val real = resolve(vpath, mustExist = false, follow = false) ?: throw RemoteError("denied")
        try {
            Files.createDirectory(real.toPath())
        } catch (e: Exception) {
            throw ioError(e)
        }
    }

    fun delete(vpath: String?) {
        val real = resolve(vpath, follow = false)
        if (real == null || isRoot(real)) throw RemoteError("denied")
        try {
            if (Files.isDirectory(real.toPath(), LinkOption.NOFOLLOW_LINKS)) {
                deleteTree(real.toPath())
            } else {
                Files.delete(real.toPath())
            }
        } catch (e: Exception) {
            throw ioError(e)
        }
    }

    fun move(src: String?, dst: String?, overwrite: Boolean) {
        val s = resolve(src, follow = false)
        val d = resolve(dst, mustExist = false, follow = false)
        if (s == null || d == null || isRoot(s) || isRoot(d)) throw RemoteError("denied")
        if (within(s.path, d.path)) throw RemoteError("bad_request")
        try {
            if (Files.exists(d.toPath(), LinkOption.NOFOLLOW_LINKS)) {
                if (!overwrite) throw RemoteError("exists")
                if (Files.isDirectory(d.toPath(), LinkOption.NOFOLLOW_LINKS)) deleteTree(d.toPath())
            }
            Files.move(s.toPath(), d.toPath(), StandardCopyOption.REPLACE_EXISTING)
        } catch (e: Exception) {
            throw ioError(e)
        }
    }
}
