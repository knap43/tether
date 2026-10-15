"""The Files (GTK) sidebar: one bookmark per device the user chose, labelled with its name."""

from pathlib import Path


def is_ours(url: str, label: str, root: str) -> bool:
    # Older versions wrote a single “Phone (Tether)” entry, possibly for another port or token.
    return url.startswith(root) or label.endswith("(Tether)")


def sync(path: Path, root: str, wanted: list[tuple[str, str]]) -> bool:
    """Makes Tether's bookmarks exactly `wanted` ([(url, label)]), leaving everything else alone.

    Entries keep their place in the sidebar while their URL stays the same; new ones go last.
    Returns True if the file changed.
    """
    try:
        old = path.read_text().splitlines()
    except FileNotFoundError:
        old = []
    except OSError:
        return False
    want = dict(wanted)
    out, placed = [], set()
    for line in old:
        url, _, label = line.partition(" ")
        if not is_ours(url, label, root):
            out.append(line)
        elif url in want and url not in placed:
            out.append(f"{url} {want[url]}")
            placed.add(url)
    out += [f"{url} {label}" for url, label in wanted if url not in placed]
    if out == old:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tether-tmp")
    tmp.write_text("\n".join(out) + "\n" if out else "")
    tmp.replace(path)
    return True
