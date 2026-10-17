"""The Files (GTK) sidebar: one bookmark per device the user chose, labelled with its name."""

from pathlib import Path


def is_ours(url: str, label: str, root: str, written: set[str] | None) -> bool:
    """Only entries Tether wrote itself; bookmarks the user made to a device stay theirs.

    `written` is None the first time, when this version doesn't yet know what it wrote:
    then everything under the bridge counts. Older versions wrote a “Phone (Tether)” entry.
    """
    if label.endswith("(Tether)"):
        return True
    return url in written if written is not None else url.startswith(root)


def sync(path: Path, root: str, wanted: list[tuple[str, str]], written: set[str] | None = None) -> list[str]:
    """Makes Tether's own bookmarks exactly `wanted` ([(url, label)]), leaving everything else alone.

    Entries keep their place in the sidebar while their URL stays the same; new ones go last.
    A URL the user already bookmarked is left to them. Returns the URLs Tether now owns.
    """
    try:
        old = path.read_text().splitlines()
    except FileNotFoundError:
        old = []
    except OSError:
        return sorted(written or [])
    want = dict(wanted)
    out, placed = [], set()
    theirs = {line.partition(" ")[0] for line in old if not is_ours(*line.partition(" ")[::2], root, written)}
    wanted = [(u, l) for u, l in wanted if u not in theirs]  # already bookmarked by the user
    for line in old:
        url, _, label = line.partition(" ")
        if not is_ours(url, label, root, written):
            out.append(line)
        elif url in want and url not in placed:
            out.append(f"{url} {want[url]}")
            placed.add(url)
    out += [f"{url} {label}" for url, label in wanted if url not in placed]
    owned = [url for url, _ in wanted]
    if out == old:
        return owned
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tether-tmp")
    tmp.write_text("\n".join(out) + "\n" if out else "")
    tmp.replace(path)
    return owned
