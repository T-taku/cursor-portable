#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

HOME_TOKEN = "__HOME__"
MANIFEST_VERSION = 1
USER_RULE_KEY = "aicontext.personalContext"
SECRET_KEY = re.compile(
    r"(api[-_]?key|token|secret|password|passwd|authorization|credential|private[-_]?key)",
    re.IGNORECASE,
)
SECRET_LINE = re.compile(
    r'("(?P<key>[^"]*(?:api[-_]?key|token|secret|password|passwd|authorization|credential|private[-_]?key)[^"]*)"\s*:\s*")(?P<val>[^"]+)(")',
    re.IGNORECASE,
)
JSON_STRING = re.compile(r'"((?:\\.|[^"\\])*)"')
IGNORE_PATHS = {
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
}
_LANG: str | None = None


def detect_language() -> str:
    forced = os.environ.get("CURSOR_PACK_LANG", "").lower()
    if forced in {"ja", "en"}:
        return forced
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(key, "").lower()
        if value.startswith("ja"):
            return "ja"
        if value.startswith("en"):
            return "en"
    return "ja"


def take_lang(argv: list[str]) -> tuple[list[str], str | None]:
    rest: list[str] = []
    chosen: str | None = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--lang":
            if index + 1 >= len(argv):
                chosen = ""
                index += 1
                continue
            chosen = argv[index + 1]
            index += 2
            continue
        if arg.startswith("--lang="):
            chosen = arg.split("=", 1)[1]
            index += 1
            continue
        rest.append(arg)
        index += 1
    return rest, chosen


def set_language(argv: list[str]) -> list[str]:
    global _LANG
    rest, chosen = take_lang(argv)
    _LANG = detect_language()
    if chosen is None:
        return rest
    if chosen not in {"ja", "en"}:
        raise SystemExit(t("--lang には ja か en を指定してください。", "--lang must be ja or en."))
    _LANG = chosen
    return rest


def language() -> str:
    if _LANG is None:
        set_language(sys.argv[1:])
    assert _LANG is not None
    return _LANG


def t(ja: str, en: str) -> str:
    return ja if language() == "ja" else en


def missing_user_rules(text: str) -> bool:
    body = text.lstrip()
    return body.startswith("このパソコンの設定データベースから") or body.startswith(
        "Could not read user rules from this computer"
    )


def home_dir() -> Path:
    override = os.environ.get("CURSOR_PACK_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home()


def user_dir() -> Path:
    home = home_dir()
    system = platform.system()
    if system == "Darwin":
        return home / "Library" / "Application Support" / "Cursor" / "User"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata and not os.environ.get("CURSOR_PACK_HOME"):
            return Path(appdata) / "Cursor" / "User"
        return home / "AppData" / "Roaming" / "Cursor" / "User"
    return home / ".config" / "Cursor" / "User"


def dot_cursor() -> Path:
    return home_dir() / ".cursor"


def state_db() -> Path:
    return user_dir() / "globalStorage" / "state.vscdb"


def relocate_out(text: str) -> str:
    home = str(home_dir())
    if len(home) < 2:
        return text
    return text.replace(home, HOME_TOKEN)


def relocate_in(text: str) -> str:
    return text.replace(HOME_TOKEN, str(home_dir()))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def redact_text(text: str) -> tuple[str, list[str]]:
    found: list[str] = []

    def replace(match: re.Match[str]) -> str:
        found.append(match.group("key"))
        return f"{match.group(1)}{match.group(4)}"

    return SECRET_LINE.sub(replace, text), found


def redact_json(node: object, path: str = "") -> tuple[object, list[str]]:
    found: list[str] = []
    if isinstance(node, dict):
        new: dict[str, object] = {}
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(value, str) and SECRET_KEY.search(str(key)) and value:
                new[key] = ""
                found.append(child)
            else:
                cleaned, nested = redact_json(value, child)
                new[key] = cleaned
                found.extend(nested)
        return new, found
    if isinstance(node, list):
        new_list = []
        for index, value in enumerate(node):
            cleaned, nested = redact_json(value, f"{path}[{index}]")
            new_list.append(cleaned)
            found.extend(nested)
        return new_list, found
    return node, found


def extension_ids() -> list[str]:
    root = dot_cursor() / "extensions"
    if not root.is_dir():
        return []
    found: set[str] = set()
    for child in root.iterdir():
        if not child.is_dir():
            continue
        ext_id = extension_id(child)
        if ext_id and not ext_id.startswith("anysphere."):
            found.add(ext_id)
    return sorted(found)


def extension_id(path: Path) -> str | None:
    package = path / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        publisher = data.get("publisher")
        name = data.get("name")
        if isinstance(publisher, str) and isinstance(name, str):
            return f"{publisher}.{name}".lower()
    match = re.match(r"^([A-Za-z0-9-]+)\.([A-Za-z0-9-]+)", path.name)
    if match:
        return f"{match.group(1)}.{match.group(2)}".lower()
    return None


def read_user_rules() -> str | None:
    db = state_db()
    if not db.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                (USER_RULE_KEY,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return ""
    value = row[0]
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def cursor_is_running() -> bool:
    system = platform.system()
    if system == "Windows":
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Cursor.exe"],
            capture_output=True,
            text=True,
        )
        return "Cursor.exe" in result.stdout
    result = subprocess.run(["ps", "-ax", "-o", "comm="], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        name = line.strip()
        if name == "Cursor" or name.endswith("/Contents/MacOS/Cursor") or name.endswith("/cursor"):
            return True
    return False


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    dest = path.with_name(f"{path.name}.bak-{stamp}")
    number = 2
    while dest.exists():
        dest = path.with_name(f"{path.name}.bak-{stamp}-{number}")
        number += 1
    if path.is_dir():
        shutil.copytree(path, dest)
    else:
        shutil.copy2(path, dest)
    return dest


def copy_text_file(src: Path, dest: Path, rewrite_home: bool) -> list[str]:
    text = read_text(src)
    text, redacted = redact_text(text)
    if rewrite_home:
        text = relocate_out(text)
    write_text(dest, text)
    return redacted


def is_text_file(path: Path) -> bool:
    chunk = path.read_bytes()[:8192]
    return b"\0" not in chunk


def path_pieces(raw: str) -> list[str]:
    text = raw.replace('\\"', '"').strip()
    quoted = re.findall(r'"([^"]+)"', text)
    candidates = quoted or [text.strip('"')]
    pieces: list[str] = []
    for candidate in candidates:
        if re.match(r"^[A-Za-z]:\\", candidate):
            pieces.append(candidate)
            continue
        if candidate.startswith(("/", "__HOME__")) and ":" in candidate:
            pieces.extend(part for part in candidate.split(":") if part)
            continue
        pieces.append(candidate)
    return pieces


def interesting_paths(text: str) -> tuple[list[str], list[str]]:
    rewritten: list[str] = []
    absolute: list[str] = []
    for raw in JSON_STRING.findall(text):
        for piece in path_pieces(raw):
            if piece in IGNORE_PATHS or not piece:
                continue
            if piece.startswith("__HOME__"):
                rewritten.append(piece)
            elif piece.startswith(("/Users/", "/home/", "/opt/", "/Applications/")) or re.match(
                r"^[A-Za-z]:\\", piece
            ):
                absolute.append(piece)
    return rewritten, absolute


def collect_review(bundle: Path, included: list[str], redacted: list[str], rules: str | None) -> str:
    rewritten: list[str] = []
    absolute: list[str] = []
    for relative in included:
        path = bundle / relative
        if path.is_file() and is_text_file(path):
            home_paths, other_paths = interesting_paths(read_text(path))
            rewritten.extend(home_paths)
            absolute.extend(other_paths)
        elif path.is_dir():
            for child in path.rglob("*"):
                if child.is_file() and is_text_file(child):
                    home_paths, other_paths = interesting_paths(read_text(child))
                    rewritten.extend(home_paths)
                    absolute.extend(other_paths)
    lines = [
        "このパックに入っているファイル / Files in this pack",
        *[f"- {name}" for name in included if name not in {"review.txt", "README.txt", "cursor_pack.py"}],
        "",
        "入れていないもの / Not included",
        "- Cursorのログイン情報とチャット履歴 / Cursor login information and chat history",
        "- 拡張機能のプログラム本体（extensions.txt のIDから入れ直す） / The extensions themselves (reinstall them from the IDs in extensions.txt)",
        "- トークンやパスワードの値 / Token and password values",
        "- アカウント側だけにあり、このパソコンのファイルに無いルール / Rules that exist only on the account and are not in a file on this computer",
        "",
    ]
    if redacted:
        lines.append("空にした秘密情報 / Cleared secrets")
        for name in redacted:
            lines.append(f"- {name}")
        lines.append("新しいパソコンで、必要な値を入れ直してください。")
        lines.append("On the new computer, fill in the values you still need.")
        lines.append("")
    unique_rewritten = sorted(set(rewritten))
    if unique_rewritten:
        lines.append("新しいパソコンのホームフォルダに合わせて戻すパス / Paths restored to the new computer's home folder")
        for item in unique_rewritten:
            lines.append(f"- {item.replace(HOME_TOKEN, '~')}")
        lines.append("")
    unique_absolute = sorted(set(absolute))
    if unique_absolute:
        lines.append("このままでは届かないパス。新しいパソコンに合わせて書き換えてください")
        lines.append("Paths that will not work as-is. Rewrite them for the new computer.")
        for item in unique_absolute:
            lines.append(f"- {item}")
        lines.append("")
    if rules is None:
        lines.append("ユーザールールは、このパソコンの設定データベースから読めませんでした。")
        lines.append("User rules could not be read from this computer's settings database.")
    elif rules == "":
        lines.append("このパソコンのファイルにはユーザールールがありませんでした。")
        lines.append("This computer's files had no user rules.")
    else:
        lines.append(f"ユーザールールを user-rules.txt に書き出しました（{len(rules)}文字）。")
        lines.append(f"Exported user rules to user-rules.txt ({len(rules)} characters).")
        lines.append("設定画面にだけ書いているルールは、ここには入っていません。")
        lines.append("Rules that exist only in the settings screen are not included here.")
    lines.append("")
    lines.append("フック（sage-hook など）は自動では戻しません。")
    lines.append("Hooks (such as sage-hook) are not restored automatically.")
    lines.append("拡張機能を入れ直すと、その拡張機能がフックを作り直します。")
    lines.append("Reinstalling an extension recreates that extension's hooks.")
    lines.append("")
    return "\n".join(lines)


def bundle_readme() -> str:
    return """Cursor設定パック

新しいパソコンでの戻し方:

1. Cursorをインストールする
2. 一度起動して終了する
3. このフォルダで次を実行する

   python3 cursor_pack.py import . --install-extensions --apply-user-rules

4. review.txt を読み、残っているパスを直す
5. Cursorを起動する

--install-extensions は拡張機能を入れます。cursor コマンドが必要です。
--apply-user-rules はユーザールールを戻します。Cursorは終了した状態で実行してください。

フックは戻りません。必要な拡張機能を入れ直してください。

English

How to restore on the new computer:

1. Install Cursor
2. Launch it once, then quit
3. Run the following in this folder

   python3 cursor_pack.py import . --install-extensions --apply-user-rules

4. Read review.txt and fix any remaining paths
5. Launch Cursor

--install-extensions installs extensions. The cursor command is required.
--apply-user-rules restores user rules. Quit Cursor before you run it.

Hooks are not restored. Reinstall the extensions that need them.
"""


def export_bundle(out: Path) -> int:
    staging = Path(tempfile.mkdtemp(prefix="cursor-pack-"))
    redacted: list[str] = []
    included: list[str] = []
    try:
        user = user_dir()
        for name in ("settings.json", "keybindings.json"):
            src = user / name
            if src.is_file():
                redacted.extend(copy_text_file(src, staging / name, True))
                included.append(name)

        snippets = user / "snippets"
        if snippets.is_dir() and any(snippets.iterdir()):
            dest = staging / "snippets"
            shutil.copytree(snippets, dest)
            for path in dest.rglob("*"):
                if path.is_file() and is_text_file(path):
                    text, found = redact_text(read_text(path))
                    write_text(path, relocate_out(text))
                    redacted.extend(found)
            included.append("snippets")

        mcp = dot_cursor() / "mcp.json"
        if mcp.is_file():
            try:
                data = json.loads(read_text(mcp))
                cleaned, found = redact_json(data)
                text = json.dumps(cleaned, indent=2, ensure_ascii=False) + "\n"
                write_text(staging / "mcp.json", relocate_out(text))
                redacted.extend(found)
            except json.JSONDecodeError:
                redacted.extend(copy_text_file(mcp, staging / "mcp.json", True))
            included.append("mcp.json")

        optional = staging / "optional"
        hooks = dot_cursor() / "hooks.json"
        if hooks.is_file():
            copy_text_file(hooks, optional / "hooks.json", True)
            included.append("optional/hooks.json")
        hooks_dir = dot_cursor() / "hooks"
        if hooks_dir.is_dir():
            for path in hooks_dir.iterdir():
                if path.is_file() and is_text_file(path):
                    target = optional / "hooks" / path.name
                    write_text(target, relocate_out(read_text(path)))
                    included.append(f"optional/hooks/{path.name}")

        for folder_name in ("skills", "agents"):
            folder = dot_cursor() / folder_name
            if folder.is_dir() and any(p.is_file() for p in folder.rglob("*")):
                dest = staging / folder_name
                shutil.copytree(
                    folder,
                    dest,
                    ignore=shutil.ignore_patterns("*.png", "*.jpg", "*.jpeg", "*.webp"),
                )
                for path in dest.rglob("*"):
                    if path.is_file() and is_text_file(path):
                        write_text(path, relocate_out(read_text(path)))
                included.append(folder_name)

        ids = extension_ids()
        write_text(staging / "extensions.txt", "\n".join(ids) + ("\n" if ids else ""))
        included.append("extensions.txt")
        write_text(
            staging / "install-extensions.sh",
            "#!/bin/sh\nwhile IFS= read -r id; do\n"
            "  [ -z \"$id\" ] && continue\n"
            "  cursor --install-extension \"$id\" || exit 1\n"
            "done < extensions.txt\n",
        )
        write_text(
            staging / "install-extensions.ps1",
            "Get-Content extensions.txt | Where-Object { $_ -ne '' } | ForEach-Object {\n"
            "  cursor --install-extension $_\n"
            "  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
            "}\n",
        )

        rules = read_user_rules()
        if rules is None:
            write_text(
                staging / "user-rules.txt",
                "このパソコンの設定データベースからユーザールールを読めませんでした。\n"
                "Could not read user rules from this computer's settings database.\n",
            )
        else:
            write_text(staging / "user-rules.txt", rules)
        included.append("user-rules.txt")

        write_text(staging / "review.txt", collect_review(staging, included, redacted, rules))
        write_text(staging / "README.txt", bundle_readme())
        script = Path(__file__).resolve()
        shutil.copy2(script, staging / "cursor_pack.py")
        included.extend(["review.txt", "README.txt", "cursor_pack.py"])

        manifest = {
            "version": MANIFEST_VERSION,
            "exportedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "os": platform.system(),
            "files": included,
            "extensionCount": len(ids),
            "redacted": redacted,
        }
        write_text(staging / "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(staging).as_posix())
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    print(t(f"書き出しました: {out}", f"Exported: {out}"))
    print(t("ログイン情報とチャット履歴は入れていません。", "Login information and chat history were not included."))
    print(t(
        "中の review.txt に、新しいパソコンで直すパスを書いてあります。",
        "review.txt inside the archive lists paths to fix on the new computer.",
    ))
    return 0


def bundle_root(path: Path) -> Path:
    if (path / "manifest.json").is_file():
        return path
    children = [child for child in path.iterdir() if (child / "manifest.json").is_file()]
    if len(children) == 1:
        return children[0]
    raise SystemExit(t("manifest.json が見つかりません。", "manifest.json was not found."))


def open_bundle(path: Path) -> tuple[Path, Path | None]:
    if path.is_dir():
        return bundle_root(path), None
    if not path.is_file() or path.suffix.lower() != ".zip":
        raise SystemExit(t("フォルダか zip を指定してください。", "Specify a folder or a zip file."))
    extracted = Path(tempfile.mkdtemp(prefix="cursor-pack-import-"))
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            parts = Path(info.filename).parts
            if Path(info.filename).is_absolute() or ".." in parts:
                raise SystemExit(t("パックの中身が不正です。", "The pack contents are invalid."))
        archive.extractall(extracted)
    return bundle_root(extracted), extracted


def load_manifest(root: Path) -> dict[str, object]:
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(t(
            "manifest.json がありません。この道具で書き出したパックを指定してください。",
            "manifest.json is missing. Specify a pack exported by this tool.",
        ))
    data = json.loads(read_text(manifest_path))
    if data.get("version") != MANIFEST_VERSION:
        raise SystemExit(t("このパックの形式には対応していません。", "This pack format is not supported."))
    return data


def install_file(src: Path, dest: Path, dry_run: bool) -> None:
    text = relocate_in(read_text(src)) if src.is_file() else ""
    if dry_run:
        print(t(f"戻す予定: {dest}", f"Would restore: {dest}"))
        return
    saved = backup(dest)
    if saved:
        print(t(f"今のファイルを退避しました: {saved}", f"Backed up the current file: {saved}"))
    write_text(dest, text)
    if dest.name == "mcp.json":
        os.chmod(dest, 0o600)
    print(t(f"戻しました: {dest}", f"Restored: {dest}"))


def install_tree(src: Path, dest: Path, dry_run: bool) -> None:
    if dry_run:
        print(t(f"戻す予定: {dest}", f"Would restore: {dest}"))
        return
    saved = backup(dest)
    if saved:
        print(t(f"今のフォルダを退避しました: {saved}", f"Backed up the current folder: {saved}"))
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    for path in dest.rglob("*"):
        if path.is_file() and is_text_file(path):
            write_text(path, relocate_in(read_text(path)))
    print(t(f"戻しました: {dest}", f"Restored: {dest}"))


def apply_user_rules(root: Path, dry_run: bool) -> int:
    rules_path = root / "user-rules.txt"
    if not rules_path.is_file():
        print(t("user-rules.txt がありません。", "user-rules.txt is missing."))
        return 1
    rules = read_text(rules_path)
    if missing_user_rules(rules):
        print(t(
            "パックにユーザールールが入っていないので、戻していません。",
            "The pack has no user rules, so none were restored.",
        ))
        return 0
    db = state_db()
    if dry_run:
        print(t(f"ユーザールールを戻す予定: {db}", f"Would restore user rules: {db}"))
        return 0
    if cursor_is_running():
        print(t(
            "Cursorが起動中です。終了してから --apply-user-rules を付けてもう一度実行してください。",
            "Cursor is running. Quit it, then run again with --apply-user-rules.",
        ))
        return 1
    if not db.is_file():
        print(t(
            "Cursorの設定データベースがありません。Cursorを一度起動して終了してから、もう一度実行してください。",
            "Cursor's settings database is missing. Launch Cursor once, quit it, and run again.",
        ))
        print(t(f"ルールの文面はここにあります: {rules_path}", f"The rule text is here: {rules_path}"))
        return 1
    backup_text = user_dir() / f"user-rules.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}.txt"
    previous = read_user_rules()
    if previous is not None:
        write_text(backup_text, previous)
        print(t(f"今のユーザールールを退避しました: {backup_text}", f"Backed up the current user rules: {backup_text}"))
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ItemTable(key, value) VALUES (?, ?)",
            (USER_RULE_KEY, rules),
        )
        conn.commit()
    print(t("ユーザールールを戻しました。", "Restored user rules."))
    return 0


def install_extensions(root: Path, dry_run: bool) -> int:
    listing = root / "extensions.txt"
    if not listing.is_file():
        print(t("extensions.txt がありません。", "extensions.txt is missing."))
        return 1
    ids = [line.strip() for line in read_text(listing).splitlines() if line.strip()]
    if not ids:
        print(t("入れる拡張機能はありません。", "There are no extensions to install."))
        return 0
    exe = shutil.which("cursor")
    if not exe:
        print(t("cursor コマンドが見つかりません。", "The cursor command was not found."))
        print(t(
            "Cursorのコマンドパレットで Install 'cursor' command in PATH を実行してください。",
            "Run Install 'cursor' command in PATH from Cursor's command palette.",
        ))
        print(t(
            "そのあと、このフォルダの install-extensions.sh を実行できます。",
            "Then you can run install-extensions.sh in this folder.",
        ))
        return 1
    if dry_run:
        for ext_id in ids:
            print(t(f"入れる予定: {ext_id}", f"Would install: {ext_id}"))
        return 0
    failed: list[str] = []
    for ext_id in ids:
        print(t(f"拡張機能を入れています: {ext_id}", f"Installing extension: {ext_id}"))
        result = subprocess.run([exe, "--install-extension", ext_id])
        if result.returncode != 0:
            failed.append(ext_id)
    if failed:
        print(t("入れられなかった拡張機能:", "Extensions that failed to install:"))
        for ext_id in failed:
            print(f"- {ext_id}")
        return 1
    print(t(f"拡張機能を {len(ids)} 個入れました。", f"Installed {len(ids)} extensions."))
    return 0


def import_bundle(source: Path, install_ext: bool, apply_rules: bool, with_hooks: bool, dry_run: bool) -> int:
    root, cleanup = open_bundle(source)
    try:
        return import_bundle_at(root, install_ext, apply_rules, with_hooks, dry_run)
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)


def import_bundle_at(root: Path, install_ext: bool, apply_rules: bool, with_hooks: bool, dry_run: bool) -> int:
    load_manifest(root)
    status = 0
    mapping = {
        "settings.json": user_dir() / "settings.json",
        "keybindings.json": user_dir() / "keybindings.json",
        "mcp.json": dot_cursor() / "mcp.json",
    }
    for name, dest in mapping.items():
        src = root / name
        if src.is_file():
            install_file(src, dest, dry_run)
    snippets = root / "snippets"
    if snippets.is_dir():
        install_tree(snippets, user_dir() / "snippets", dry_run)
    for folder_name in ("skills", "agents"):
        src = root / folder_name
        if src.is_dir():
            install_tree(src, dot_cursor() / folder_name, dry_run)
    if with_hooks:
        hooks = root / "optional" / "hooks.json"
        if hooks.is_file():
            install_file(hooks, dot_cursor() / "hooks.json", dry_run)
        hooks_dir = root / "optional" / "hooks"
        if hooks_dir.is_dir():
            install_tree(hooks_dir, dot_cursor() / "hooks", dry_run)
    else:
        print(t(
            "フックは戻していません。拡張機能を入れ直すと作り直されます。",
            "Hooks were not restored. Reinstalling an extension recreates them.",
        ))
    if apply_rules:
        status = max(status, apply_user_rules(root, dry_run))
    else:
        print(t(
            "ユーザールールは戻していません。戻すときは --apply-user-rules を付けて、Cursorを終了した状態で実行してください。",
            "User rules were not restored. To restore them, quit Cursor and run again with --apply-user-rules.",
        ))
    if install_ext:
        status = max(status, install_extensions(root, dry_run))
    else:
        print(t(
            "拡張機能は入れていません。入れるときは --install-extensions を付けてください。",
            "Extensions were not installed. To install them, run again with --install-extensions.",
        ))
    print(t("review.txt に、手で直すパスが書いてあります。", "review.txt lists paths to fix by hand."))
    return status


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=t(
        "Cursorの設定を別のパソコンへ運ぶ",
        "Move Cursor settings to another computer",
    ))
    parser.add_argument("--lang", choices=["ja", "en"], help=t("表示言語（ja または en）", "Message language (ja or en)"))
    commands = parser.add_subparsers(dest="command", required=True)

    export_cmd = commands.add_parser("export", help=t("このパソコンの設定をzipにまとめる", "Pack this computer's settings into a zip"))
    export_cmd.add_argument("--lang", choices=["ja", "en"], help=t("表示言語（ja または en）", "Message language (ja or en)"))
    export_cmd.add_argument("--out", type=Path, help=t("書き出すzipのパス", "Path of the zip to write"))

    import_cmd = commands.add_parser("import", help=t("zipまたはフォルダから設定を戻す", "Restore settings from a zip or folder"))
    import_cmd.add_argument("--lang", choices=["ja", "en"], help=t("表示言語（ja または en）", "Message language (ja or en)"))
    import_cmd.add_argument("bundle", type=Path, help=t("書き出したzipか、展開したフォルダ", "Exported zip, or the unzipped folder"))
    import_cmd.add_argument("--install-extensions", action="store_true", help=t("拡張機能を入れる", "Install extensions"))
    import_cmd.add_argument("--apply-user-rules", action="store_true", help=t("ユーザールールを戻す", "Restore user rules"))
    import_cmd.add_argument("--with-hooks", action="store_true", help=t("フックも戻す", "Also restore hooks"))
    import_cmd.add_argument("--dry-run", action="store_true", help=t("書き込まず、戻す内容だけ表示する", "Show what would be restored without writing"))
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    argv = set_language(argv)
    args = parse_args(argv)
    if args.command == "export":
        if args.out:
            out = args.out
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M")
            out = Path(__file__).resolve().parent / f"cursor-settings-{stamp}.zip"
        return export_bundle(out)
    return import_bundle(
        args.bundle,
        args.install_extensions,
        args.apply_user_rules,
        args.with_hooks,
        args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
