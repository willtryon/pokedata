#!/usr/bin/env python3
"""
fix_windows_image_paths.py

Rename every file/folder under a pokedata images tree whose name would be
INVALID on Windows, while keeping the images usable by:

  * the Java consumer (PokeImageComp) -- its CardIndex.resolveImage() falls back
    to normalizeForMatch(), which strips everything except [a-z0-9], so any name
    with the illegal characters removed still resolves to the same card; and
  * data.sqlite -- which is deliberately NOT touched. The DB's `img` column is a
    remote URL, and the local path is derived from `cardId` (also the prices join
    key). Renaming files on disk does not invalidate anything the DB stores.

The transform here is intentionally IDENTICAL to the `sanitizeWinPath()` patch
for pokedata/src/common.ts, so a future scrape lands on the exact same names.

What counts as invalid on Windows (all handled):
  * reserved characters:  < > : " / \\ | ? *
  * ASCII control chars 0x00-0x1F
  * a trailing space or dot on any path component
  * reserved device names: CON PRN AUX NUL COM1-9 LPT1-9 (with or without ext)

Usage:
    # dry run (default) - shows what WOULD change, writes nothing
    python3 fix_windows_image_paths.py --root ./images

    # actually rename
    python3 fix_windows_image_paths.py --root ./images --apply

    # rename via `git mv` so history is preserved (run inside the repo)
    python3 fix_windows_image_paths.py --root ./images --apply --use-git

    # also write an undo map you can keep or reverse with --undo
    python3 fix_windows_image_paths.py --root ./images --apply --map-out rename_map.json
    python3 fix_windows_image_paths.py --undo rename_map.json --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

# --- the canonical Windows-safe transform (keep in sync with common.ts) -------

ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# a maximal run of dashes and/or illegal chars; collapsed only if it CONTAINS an
# illegal char, so pre-existing valid runs like "Platinum---Arceus" are preserved
DASH_OR_ILLEGAL_RUN = re.compile(r'[-<>:"/\\|?*\x00-\x1f]+')
RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_component(name: str, is_file: bool) -> str:
    """Make a single path component (one file or folder name) Windows-safe.

    Only names that are actually invalid on Windows change. Legal characters --
    including & ' ( ) [ ] and runs of dashes like 'Platinum---Arceus' -- are left
    exactly as they are. Deterministic: no collision suffixes here (collisions are
    handled by the caller so the scraper, which cannot add suffixes, stays in sync)."""
    if is_file and "." in name:
        stem, _, ext = name.rpartition(".")
        if stem == "":               # e.g. ".gitignore" -- treat whole thing as the stem
            stem, ext = name, ""
    else:
        stem, ext = name, ""

    def collapse(run: re.Match) -> str:
        r = run.group(0)
        return "-" if ILLEGAL.search(r) else r   # touch a run only if it holds an illegal char

    stem = DASH_OR_ILLEGAL_RUN.sub(collapse, stem)
    stem = stem.rstrip(" .")         # Windows forbids a trailing space or dot
    stem = stem.lstrip(" ")          # a leading space is legal but pointless; a leading dot is fine
    if stem == "":
        stem = "_"
    if stem.upper() in RESERVED:
        stem = "_" + stem

    if ext:
        ext = ILLEGAL.sub("", ext).strip(" .")
        return f"{stem}.{ext}" if ext else stem
    return stem


def needs_fix(name: str, is_file: bool) -> bool:
    return sanitize_component(name, is_file) != name


# --- collision-safe target naming --------------------------------------------

def unique_target(dirpath: str, desired: str, is_file: bool, claimed: set[str]) -> str:
    """Return a target name that does not clobber an existing DIFFERENT file.

    On case-insensitive filesystems a pure case change is allowed through."""
    src_lower_map = {e.lower() for e in claimed}
    candidate = desired
    if is_file and "." in desired:
        base, _, ext = desired.rpartition(".")
        make = lambda n: f"{base}-{n}.{ext}"
    else:
        make = lambda n: f"{desired}-{n}"

    n = 1
    while True:
        full = os.path.join(dirpath, candidate)
        # free if: nothing there, or the thing there is exactly our source rename target
        if candidate.lower() not in src_lower_map and not os.path.exists(full):
            return candidate
        candidate = make(n)
        n += 1


# --- rename engine ------------------------------------------------------------

def git_mv(src: str, dst: str, repo: str) -> None:
    # two-step through a temp name handles case-only renames on case-insensitive FS
    tmp = dst + ".winfix.tmp"
    subprocess.run(["git", "-C", repo, "mv", "-f", src, tmp], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    subprocess.run(["git", "-C", repo, "mv", "-f", tmp, dst], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def os_mv(src: str, dst: str) -> None:
    tmp = dst + ".winfix.tmp"
    os.rename(src, tmp)
    os.rename(tmp, dst)


def find_git_root(start: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", start, "rev-parse", "--show-toplevel"],
                             check=True, capture_output=True, text=True)
        return out.stdout.strip()
    except Exception:
        return None


def run(root: str, apply: bool, use_git: bool, map_out: str | None) -> int:
    if not os.path.isdir(root):
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    repo = find_git_root(root) if use_git else None
    if use_git and not repo:
        print("warning: --use-git set but no git repo found; falling back to os.rename")
        use_git = False

    renames: list[dict] = []
    conflicts: list[str] = []

    # bottom-up so renaming a directory never invalidates paths we still have to visit
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        claimed: set[str] = set(os.listdir(dirpath)) if os.path.isdir(dirpath) else set()

        for entries, is_file in ((sorted(filenames), True), (sorted(dirnames), False)):
            for name in entries:
                if name in (".git",):
                    continue
                if not needs_fix(name, is_file):
                    continue
                desired = sanitize_component(name, is_file)
                target = unique_target(dirpath, desired, is_file, claimed)
                if target != desired:
                    conflicts.append(os.path.join(dirpath, name)
                                     + f"  (wanted '{desired}', used '{target}')")
                src = os.path.join(dirpath, name)
                dst = os.path.join(dirpath, target)
                renames.append({"src": src, "dst": dst, "is_file": is_file})
                claimed.discard(name)
                claimed.add(target)

    if not renames:
        print(f"Nothing to fix under {root}. All names are already Windows-safe.")
        return 0

    kind = "APPLYING" if apply else "DRY RUN (no changes written)"
    print(f"== {kind} == {len(renames)} name(s) to fix under {root}\n")
    for r in renames:
        rel_src = os.path.relpath(r["src"], root)
        rel_dst = os.path.relpath(r["dst"], root)
        tag = "dir " if not r["is_file"] else "file"
        print(f"  [{tag}] {rel_src}\n         -> {rel_dst}")

    if conflicts:
        print("\n-- name collisions resolved with a numeric suffix "
              "(review these; the scraper will NOT add suffixes) --")
        for c in conflicts:
            print("  " + c)

    if apply:
        done = 0
        for r in renames:
            try:
                if use_git:
                    git_mv(r["src"], r["dst"], repo)  # type: ignore[arg-type]
                else:
                    os_mv(r["src"], r["dst"])
                done += 1
            except Exception as e:
                print(f"  FAILED: {r['src']} -> {r['dst']}: {e}", file=sys.stderr)
        print(f"\nDone. Renamed {done}/{len(renames)}.")
        if map_out:
            with open(map_out, "w", encoding="utf-8") as fh:
                json.dump(renames, fh, ensure_ascii=False, indent=2)
            print(f"Wrote undo map to {map_out}")
        print("\nNote: if PokeImageComp has a built ORB/meta cache (cache_orb.dat / "
              "cache_meta.dat), delete it and re-scan so cached paths pick up the new names.")
    else:
        print("\nRe-run with --apply to perform these renames.")
    return 0


def undo(map_file: str, apply: bool, use_git: bool) -> int:
    with open(map_file, encoding="utf-8") as fh:
        renames = json.load(fh)
    repo = find_git_root(os.path.dirname(os.path.abspath(map_file))) if use_git else None
    print(f"== {'APPLYING' if apply else 'DRY RUN'} UNDO == {len(renames)} rename(s)\n")
    for r in reversed(renames):
        print(f"  {r['dst']}  ->  {r['src']}")
        if apply:
            try:
                if repo:
                    git_mv(r["dst"], r["src"], repo)
                else:
                    os_mv(r["dst"], r["src"])
            except Exception as e:
                print(f"  FAILED: {e}", file=sys.stderr)
    if not apply:
        print("\nRe-run with --apply to perform the undo.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Fix Windows-invalid image path names in pokedata.")
    p.add_argument("--root", default="./images",
                   help="images tree to fix (default: ./images, run from the pokedata repo root)")
    p.add_argument("--apply", action="store_true", help="actually rename (default is a dry run)")
    p.add_argument("--use-git", action="store_true", help="rename via `git mv` to preserve history")
    p.add_argument("--map-out", metavar="FILE", help="write a JSON undo map of everything renamed")
    p.add_argument("--undo", metavar="FILE", help="reverse a previously written map instead of scanning")
    args = p.parse_args()

    if args.undo:
        return undo(args.undo, args.apply, args.use_git)
    return run(args.root, args.apply, args.use_git, args.map_out)


if __name__ == "__main__":
    raise SystemExit(main())
