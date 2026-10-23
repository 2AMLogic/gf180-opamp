"""Write new simulation evidence through the fleet artifact store (issue #271).

New-run evidence bytes (the per-corner `corners/<run-id>/` trees) no longer
land in git. They are staged in git-ignored scratch (`sim/build/`), stored
with `klt artifact put`, and git keeps:

* a manifest, `manifests/artifacts/<bench>-<run-id>.manifest.json`, one entry
  per stored file (`sha256`, `size`, `kind=evidence`, `producer`, `uri`, and
  the logical `name` the file would have had under `sim/`). A `uri` is the
  logical `artifact:<key>` only the broker resolves; a manifest names no
  storage location;
* the run's Markdown record, which gains a "Stored evidence" section.

Fail closed: every failure raises :class:`StoreError` (no broker configured,
no `klt` with `artifact put`, broker unreachable or refusing, a put failing
part-way, a manifest that disagrees with the bytes it claims). Nothing is then
installed under `manifests/`; the staged bytes stay in scratch so the run is
not lost. There is no fallback that commits the bytes instead.

Configuration is environment only (`KLT_ARTIFACT_BROKER`, optionally
`KLT_ARTIFACT_REPO`), read by `klt` itself. The broker URL is never put in
argv, a record or a message, and no credential is held.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ENV_BROKER = "KLT_ARTIFACT_BROKER"
ENV_REPO = "KLT_ARTIFACT_REPO"
MANIFEST_DIR = Path("manifests") / "artifacts"
PROBE_TIMEOUT_S = 30
PUT_TIMEOUT_S = 900

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_URI_RE = re.compile(r"^artifact:([^/\s]\S*)$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REMOTE_RE = re.compile(r"[:/]([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class StoreError(RuntimeError):
    """Evidence could not be stored; nothing was committed (fail closed)."""


def manifest_path(repo_root: Path, bench: str, rid: str) -> Path:
    for part in (bench, rid):
        if not _ID_RE.match(part):
            raise ValueError(f"invalid bench or run id {part!r}")
    return repo_root / MANIFEST_DIR / f"{bench}-{rid}.manifest.json"


def sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


# --------------------------------------------------------------------------
# Preflight: refuse to start a run whose evidence could not be stored
# --------------------------------------------------------------------------


def _repo_name(repo_root: Path, env, run) -> str:
    repo = env.get(ENV_REPO)
    if not repo:
        try:
            p = run(["git", "-C", str(repo_root), "remote", "get-url", "origin"],
                    capture_output=True, text=True, timeout=30, check=False)
            remote = p.stdout.strip() if p.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            remote = ""
        m = _REMOTE_RE.search(remote)
        if m is None:
            raise StoreError(f"cannot tell which repo to store under: set ${ENV_REPO}")
        repo = f"{m.group(1)}/{m.group(2)}"
    return repo


def probe_broker(base: str, repo: str, timeout: float = PROBE_TIMEOUT_S) -> None:
    """One `stat` of the empty digest: proves the broker is reachable and
    admits this caller for `repo` before simulation time is spent. HTTP 200
    or 404 passes; anything else raises."""
    parts = urlsplit(base)
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment):
        raise StoreError(f"${ENV_BROKER} is not a plain http(s) base URL")
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = cls(parts.netloc, timeout=timeout)
    body = {"kind": "evidence", "repo": repo, "sha256": _EMPTY_SHA256}
    try:
        conn.request("POST", parts.path.rstrip("/") + "/v1/stat", body=json.dumps(body).encode(),
                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        resp = conn.getresponse()
        status = resp.status
        resp.read(64 << 10)
    except (OSError, http.client.HTTPException) as exc:
        raise StoreError(f"artifact broker unreachable ({type(exc).__name__})") from None
    finally:
        conn.close()
    if status not in (200, 404):
        raise StoreError(f"artifact broker refused this caller (HTTP {status})")


def preflight(repo_root: Path, *, klt: str = "klt", env=None, which=shutil.which,
              run=subprocess.run, probe=probe_broker) -> None:
    """Raise :class:`StoreError` unless a run's evidence could be stored."""
    env = os.environ if env is None else env
    base = env.get(ENV_BROKER)
    if not base:
        raise StoreError(f"no artifact broker configured (${ENV_BROKER} is unset); "
                         "new evidence is never committed as bytes")
    exe = which(klt)
    if exe is None:
        raise StoreError(f"`{klt}` is not on PATH")
    try:
        ok = run([exe, "artifact", "put", "--help"], capture_output=True, text=True,
                 timeout=60, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        raise StoreError(f"`{klt}` has no `artifact put` verb (needs klayout-tools 7e3561f or later)")
    probe(base, _repo_name(repo_root, env, run))


# --------------------------------------------------------------------------
# Put
# --------------------------------------------------------------------------


def _klt_error(stderr: str) -> str:
    try:
        err = json.loads(stderr)["error"]
        return f"{err.get('code', 'error')}: {err.get('message', '')}".strip()
    except (ValueError, KeyError, TypeError):
        lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
        return lines[-1][:300] if lines else "no diagnostic"


def entry_problem(entry) -> str | None:
    """Why a manifest entry is not acceptable, or None."""
    if not isinstance(entry, dict):
        return "entry is not an object"
    sha = entry.get("sha256")
    if not (isinstance(sha, str) and _SHA_RE.match(sha)):
        return "bad sha256"
    if not (isinstance(entry.get("size"), int) and not isinstance(entry["size"], bool)
            and entry["size"] > 0):
        return "bad size"
    if entry.get("kind") != "evidence":
        return "kind is not evidence"
    if not (isinstance(entry.get("name"), str) and entry["name"]):
        return "no name"
    uri = entry.get("uri")
    m = _URI_RE.match(uri) if isinstance(uri, str) else None
    if m is None:
        return "uri is not artifact:<key>"
    if m.group(1).rsplit("/", 1)[-1] != sha:
        return "uri key does not name the sha256"
    if not isinstance(entry.get("producer"), dict):
        return "no producer"
    return None


def put_files(files: list[tuple[Path, str]], repo_root: Path, *, klt: str = "klt",
              run=None) -> tuple[dict, list[str]]:
    """Store every `(path, name)`; return `(manifest, empty_names)`.

    One `klt artifact put --name NAME` per file, merged into a manifest in a
    private temporary directory (never in the repo). Zero-byte files cannot be
    stored; their names are returned for the summary. Every resulting entry
    is cross-checked against a local re-hash. Any failure raises StoreError.
    """
    run = run or subprocess.run
    names = [n for _, n in files]
    if len(set(names)) != len(names):
        raise StoreError("two staged files map to the same evidence name")
    expected: dict[str, tuple[str, int]] = {}
    empty: list[str] = []
    with tempfile.TemporaryDirectory(prefix="evidence-manifest-") as tmp:
        tmp_manifest = os.path.join(tmp, "manifest.json")
        for path, name in files:
            sha, size = sha256_file(path)
            if size == 0:
                empty.append(name)
                continue
            expected[name] = (sha, size)
            cmd = [klt, "artifact", "put", str(path), "--name", name,
                   "--manifest", tmp_manifest, "--format", "json"]
            try:
                p = run(cmd, cwd=str(repo_root), capture_output=True, text=True,
                        timeout=PUT_TIMEOUT_S, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise StoreError(f"klt artifact put {name}: {type(exc).__name__}") from None
            if p.returncode != 0:
                raise StoreError(f"klt artifact put {name} failed (exit {p.returncode}): "
                                 f"{_klt_error(p.stderr)}")
        if not expected:
            raise StoreError("nothing to store: every staged file is empty")
        try:
            with open(tmp_manifest, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, ValueError) as exc:
            raise StoreError(f"klt wrote no readable manifest: {exc}") from None
    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    if not isinstance(entries, list) or manifest.get("schema_version") != 1:
        raise StoreError("klt manifest is not {schema_version: 1, entries: [...]}")
    by_name = {}
    for e in entries:
        problem = entry_problem(e)
        if problem:
            raise StoreError(f"klt manifest entry invalid: {problem}")
        by_name[e["name"]] = e
    if set(by_name) != set(expected):
        raise StoreError("klt manifest entries do not match the staged files")
    for name, (sha, size) in expected.items():
        if (by_name[name]["sha256"], by_name[name]["size"]) != (sha, size):
            raise StoreError(f"klt manifest entry for {name} does not match the file's "
                             "local sha256/size")
    manifest["entries"] = sorted(entries, key=lambda e: e["name"])
    return manifest, sorted(empty)


def install_manifest(manifest: dict, path: Path) -> None:
    """Create `path` exclusively: an existing manifest is never replaced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")
    except FileExistsError as exc:
        raise StoreError(f"{path.name} already exists; refusing to overwrite") from exc


def stage_files(stage_dir: Path, name_prefix: str) -> list[tuple[Path, str]]:
    """Every regular file under `stage_dir` as `(path, name_prefix/rel)`,
    sorted; symlinks are refused (their bytes are not what is stored)."""
    out = []
    for path in sorted(stage_dir.rglob("*")):
        if path.is_symlink():
            raise StoreError(f"staged evidence contains a symlink: {path.name}")
        if path.is_file():
            out.append((path, f"{name_prefix}/{path.relative_to(stage_dir).as_posix()}"))
    return out


def summary_section(manifest_rel: str, manifest: dict, empty: list[str]) -> str:
    """Markdown appended to a run's record: where its bytes are."""
    total = sum(e["size"] for e in manifest["entries"])
    lines = [
        "",
        "## Stored evidence",
        "",
        f"The per-corner bytes of this run are in the fleet artifact store, not in git "
        f"(issue #271). Manifest: `{manifest_rel}` ({len(manifest['entries'])} objects, "
        f"{total} B). Paths cited above are the manifest `name`s. Fetch one, "
        "hash-verified, with `klt artifact get <manifest>#<name> -o <out>`; check them all "
        "with `klt artifact verify <manifest>`.",
        "",
        "| name | size (B) | sha256 |",
        "|---|---:|---|",
    ]
    for e in manifest["entries"]:
        lines.append(f"| `{e['name']}` | {e['size']} | `{e['sha256']}` |")
    for name in empty:
        lines.append(f"| `{name}` | 0 | `{_EMPTY_SHA256}` (empty, not stored) |")
    return "\n".join(lines) + "\n"


def store_run(stage_dir: Path, name_prefix: str, *, repo_root: Path, bench: str, rid: str,
              record_path: Path, put=None) -> Path:
    """Store the staged tree; commit only its manifest and a record section.

    (1) refuse if the manifest already exists; (2) put every file; (3) install
    the manifest exclusively; (4) append the "Stored evidence" section to the
    already-written record. A failure in (1)-(2) leaves nothing under
    `manifests/`. Returns the manifest path.
    """
    mpath = manifest_path(repo_root, bench, rid)
    if os.path.lexists(mpath):
        raise StoreError(f"{mpath.relative_to(repo_root)} already exists; refusing to overwrite evidence")
    files = stage_files(stage_dir, name_prefix)
    if not files:
        raise StoreError(f"nothing staged under {stage_dir.name}")
    manifest, empty = (put or put_files)(files, repo_root)
    install_manifest(manifest, mpath)
    with open(record_path, "a", encoding="utf-8") as fh:
        fh.write(summary_section(mpath.relative_to(repo_root).as_posix(), manifest, empty))
    return mpath
