"""Local semantic search over a project's own files ('find:' command).

Chunks + embeds project files into a small per-project index so the bot can
answer "what do we have about X" without stuffing gigabytes of text into a
prompt. Indexing is incremental (per-file content-hash, resumable — a kill
mid-run only loses the one file in flight) and the index itself is backed up
to Forgejo on an orphan branch so a builder recreate doesn't force a full
re-embed of a large corpus.

Storage, per project (under <project>/.cloud-code-index/):
  index.db      sqlite — chunk text + which file/offset each chunk came from
  vectors.f32   raw float32 vectors, append-only, referenced by row offset

Brute-force cosine search (numpy) rather than a vector-search sqlite
extension — simpler, no extra native dependency to fail to load, and fast
enough at this scale (a few seconds even over ~1M chunks).
"""
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import numpy as np
import requests

OLLAMA = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
INDEX_DIRNAME = ".cloud-code-index"
INDEX_BRANCH = "cloud-code-index"
CHUNK_CHARS = 3000
CHUNK_OVERLAP = 300
MAX_FILE_READ = 2_000_000  # read at most 2MB of any single file
PROGRESS_EVERY = 200        # files between progress pings + checkpoint backups

_SKIP_DIR_NAMES = {".git", INDEX_DIRNAME, "__pycache__", "node_modules", ".venv",
                   ".aider.tags.cache.v4"}
_SKIP_FILE_NAMES = {".aider.chat.history.md", ".aider.input.history",
                     ".plan_chat.json", ".aiderignore"}
_SKIP_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".pdf", ".zip", ".gz",
              ".tar", ".7z", ".bin", ".exe", ".so", ".dll", ".sqlite", ".sqlite3",
              ".db", ".mp3", ".mp4", ".mov", ".woff", ".woff2", ".ttf", ".pyc"}

_notify = print


def set_notifier(fn) -> None:
    """Wire up how progress/heads-up messages get surfaced (session_agent passes tg)."""
    global _notify
    _notify = fn


# ── Embedding ────────────────────────────────────────────────────────────────
_embed_ready = False


def _ensure_embed_model() -> bool:
    global _embed_ready
    if _embed_ready:
        return True
    try:
        r = requests.get(f"{OLLAMA}/api/tags", timeout=10)
        names = [m.get("name", "") for m in r.json().get("models", [])]
        if any(n.startswith(EMBED_MODEL) for n in names):
            _embed_ready = True
            return True
    except Exception:  # noqa: BLE001
        pass
    _notify(f"⬇️ Pulling embedding model ({EMBED_MODEL}, one time only)…")
    try:
        subprocess.run(["ollama", "pull", EMBED_MODEL], timeout=600, check=False)
        _embed_ready = True
        return True
    except Exception:  # noqa: BLE001
        return False


def embed(text: str) -> list | None:
    if not _ensure_embed_model():
        return None
    try:
        r = requests.post(f"{OLLAMA}/api/embeddings", timeout=60,
                           json={"model": EMBED_MODEL, "prompt": text[:8000]})
        vec = r.json().get("embedding")
        return vec if vec else None
    except Exception:  # noqa: BLE001
        return None


# ── Chunking ────────────────────────────────────────────────────────────────
def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list:
    text = text.strip()
    if not text:
        return []
    chunks, buf = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(buf) + len(para) + 2 <= size:
            buf = f"{buf}\n\n{para}" if buf else para
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        if len(para) > size:
            start = 0
            while start < len(para):
                chunks.append(para[start:start + size])
                start += size - overlap
        else:
            buf = para
    if buf:
        chunks.append(buf)
    return chunks


# ── File selection ───────────────────────────────────────────────────────────
def _skip(path: str) -> bool:
    parts = Path(path).parts
    if any(p in _SKIP_DIR_NAMES for p in parts[:-1]):
        return True
    name = parts[-1]
    if name in _SKIP_FILE_NAMES:
        return True
    return Path(name).suffix.lower() in _SKIP_EXTS


def list_current_files(d: Path) -> list:
    """(path, blob_sha) for every tracked file at HEAD, filtered."""
    r = subprocess.run(["git", "-C", str(d), "ls-tree", "-r", "HEAD"],
                        capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        try:
            meta, path = line.split("\t", 1)
            sha = meta.split()[2]
        except (ValueError, IndexError):
            continue
        if not _skip(path):
            out.append((path, sha))
    return out


# ── Storage ──────────────────────────────────────────────────────────────────
# _idx_dir intentionally does NOT create the directory — db_path/vectors_path
# are used in read-only existence checks (stats(), search()) and must stay
# side-effect-free; only the functions that actually write ensure it exists.
def _idx_dir(d: Path) -> Path:
    return d / INDEX_DIRNAME


def db_path(d: Path) -> Path:
    return _idx_dir(d) / "index.db"


def vectors_path(d: Path) -> Path:
    return _idx_dir(d) / "vectors.f32"


def open_db(d: Path) -> sqlite3.Connection:
    _idx_dir(d).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path(d)))
    conn.execute("""CREATE TABLE IF NOT EXISTS chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        content TEXT NOT NULL,
        vec_offset INTEGER NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path)")
    conn.execute("""CREATE TABLE IF NOT EXISTS indexed_files (
        path TEXT PRIMARY KEY,
        blob_sha TEXT NOT NULL
    )""")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value))


def append_vector(d: Path, vec: list) -> int:
    arr = np.asarray(vec, dtype=np.float32)
    _idx_dir(d).mkdir(parents=True, exist_ok=True)
    vp = vectors_path(d)
    offset = vp.stat().st_size // (arr.size * 4) if vp.exists() else 0
    with vp.open("ab") as fh:
        fh.write(arr.tobytes())
    return offset


def load_vectors(d: Path, offsets: list, dim: int) -> np.ndarray:
    mm = np.memmap(vectors_path(d), dtype=np.float32, mode="r").reshape(-1, dim)
    return mm[offsets]


# ── Forgejo backup (orphan branch, plumbing-only — never touches the working
# tree or the branch aider/build commit to) ──────────────────────────────────
def _git_env(d: Path, index_file: Path) -> dict:
    return dict(os.environ, GIT_INDEX_FILE=str(index_file),
                GIT_AUTHOR_NAME="cloud-code", GIT_AUTHOR_EMAIL="builder@local",
                GIT_COMMITTER_NAME="cloud-code", GIT_COMMITTER_EMAIL="builder@local")


def backup_to_origin(d: Path) -> bool:
    db, vecs = db_path(d), vectors_path(d)
    if not db.exists():
        return False
    tmp_index = _idx_dir(d) / ".git-index-tmp"
    env = _git_env(d, tmp_index)
    try:
        if tmp_index.exists():
            tmp_index.unlink()
        subprocess.run(["git", "-C", str(d), "read-tree", "--empty"],
                        env=env, check=True, capture_output=True)
        for f, rel in ((db, "index.db"), (vecs, "vectors.f32")):
            if not f.exists():
                continue
            sha = subprocess.run(["git", "-C", str(d), "hash-object", "-w", str(f)],
                                  env=env, capture_output=True, text=True, check=True).stdout.strip()
            subprocess.run(["git", "-C", str(d), "update-index", "--add", "--cacheinfo",
                             f"100644,{sha},{INDEX_DIRNAME}/{rel}"],
                            env=env, check=True, capture_output=True)
        tree = subprocess.run(["git", "-C", str(d), "write-tree"], env=env,
                               capture_output=True, text=True, check=True).stdout.strip()
        commit = subprocess.run(["git", "-C", str(d), "commit-tree", tree, "-m", "cloud-code index backup"],
                                 env=env, capture_output=True, text=True, check=True).stdout.strip()
        r = subprocess.run(["git", "-C", str(d), "push", "--force", "origin",
                             f"{commit}:refs/heads/{INDEX_BRANCH}"], capture_output=True, text=True)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        if tmp_index.exists():
            tmp_index.unlink()


def restore_from_origin(d: Path) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(d), "fetch", "origin", INDEX_BRANCH],
                            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return False
        idx_dir = _idx_dir(d)
        idx_dir.mkdir(parents=True, exist_ok=True)
        restored = False
        for rel in ("index.db", "vectors.f32"):
            r2 = subprocess.run(["git", "-C", str(d), "show", f"origin/{INDEX_BRANCH}:{INDEX_DIRNAME}/{rel}"],
                                 capture_output=True, timeout=60)
            if r2.returncode == 0 and r2.stdout:
                (idx_dir / rel).write_bytes(r2.stdout)
                restored = True
        return restored
    except Exception:  # noqa: BLE001
        return False


# ── Indexing ─────────────────────────────────────────────────────────────────
def index_project(name: str, d: Path) -> dict:
    restored = False
    if not (db_path(d).exists() and vectors_path(d).exists()):
        restored = restore_from_origin(d)

    conn = open_db(d)
    current = list_current_files(d)
    current_paths = {p for p, _ in current}
    indexed = dict(conn.execute("SELECT path, blob_sha FROM indexed_files").fetchall())

    to_remove = [p for p in indexed if p not in current_paths]
    to_index = [(p, sha) for p, sha in current if indexed.get(p) != sha]

    for p in to_remove:
        conn.execute("DELETE FROM chunks WHERE path=?", (p,))
        conn.execute("DELETE FROM indexed_files WHERE path=?", (p,))
    if to_remove:
        conn.commit()

    total_chunks, done = 0, 0
    for path, sha in to_index:
        fp = d / path
        try:
            text = fp.read_text(encoding="utf-8")[:MAX_FILE_READ]
        except (UnicodeDecodeError, OSError):
            text = None

        conn.execute("DELETE FROM chunks WHERE path=?", (path,))
        if text:
            for i, piece in enumerate(chunk_text(text)):
                vec = embed(piece)
                if not vec:
                    continue
                if get_meta(conn, "dim") is None:
                    set_meta(conn, "dim", str(len(vec)))
                offset = append_vector(d, vec)
                conn.execute(
                    "INSERT INTO chunks(path, chunk_index, content, vec_offset) VALUES (?,?,?,?)",
                    (path, i, piece, offset))
                total_chunks += 1
        conn.execute("INSERT OR REPLACE INTO indexed_files(path, blob_sha) VALUES (?,?)", (path, sha))
        conn.commit()  # durable per-file: a mid-run kill only loses the in-flight file
        done += 1
        if done % PROGRESS_EVERY == 0:
            _notify(f"📚 Indexing '{name}': {done}/{len(to_index)} files, {total_chunks} chunks so far…")
            backup_to_origin(d)  # periodic checkpoint so progress survives a kill

    if to_index or to_remove:
        backup_to_origin(d)
    conn.close()
    return {"changed": len(to_index), "removed": len(to_remove), "chunks": total_chunks, "restored": restored}


def stats(d: Path) -> dict | None:
    if not db_path(d).exists():
        return None
    conn = open_db(d)
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_files = conn.execute("SELECT COUNT(*) FROM indexed_files").fetchone()[0]
    conn.close()
    return {"chunks": n_chunks, "files": n_files} if n_chunks else None


# ── Search ───────────────────────────────────────────────────────────────────
def search(d: Path, query: str, top_k: int = 10) -> list:
    if not db_path(d).exists():
        return []
    conn = open_db(d)
    dim_s = get_meta(conn, "dim")
    if not dim_s:
        conn.close()
        return []
    rows = conn.execute("SELECT id, path, chunk_index, vec_offset FROM chunks").fetchall()
    if not rows:
        conn.close()
        return []
    qvec = embed(query)
    if not qvec:
        conn.close()
        return []

    dim = int(dim_s)
    mat = load_vectors(d, [r[3] for r in rows], dim)
    q = np.asarray(qvec, dtype=np.float32)
    sims = (mat @ q) / (np.linalg.norm(mat, axis=1) * np.linalg.norm(q) + 1e-8)
    top = np.argsort(-sims)[:top_k]

    results = []
    for i in top:
        row_id, path, chunk_index, _ = rows[i]
        content = conn.execute("SELECT content FROM chunks WHERE id=?", (row_id,)).fetchone()[0]
        results.append({"path": path, "chunk_index": chunk_index, "content": content, "score": float(sims[i])})
    conn.close()
    return results
