#!/usr/bin/env python3
"""Cursorの設定を別のパソコンへ運ぶ。

運ぶもの: settings.json、keybindings.json、スニペット、MCP設定、
拡張機能の一覧、このパソコンのファイルに入っているユーザールール。

運ばないもの: ログイン情報、チャット履歴、拡張機能の実体、
秘密情報（トークンやパスワードは空にする）。
"""

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
        "このパックに入っているファイル",
        *[f"- {name}" for name in included if name not in {"review.txt", "README.txt", "cursor_pack.py"}],
        "",
        "入れていないもの",
        "- Cursorのログイン情報とチャット履歴",
        "- 拡張機能のプログラム本体（extensions.txt のIDから入れ直す）",
        "- トークンやパスワードの値",
        "- アカウント側だけにあり、このパソコンのファイルに無いルール",
        "",
    ]
    if redacted:
        lines.append("空にした秘密情報")
        for name in redacted:
            lines.append(f"- {name}")
        lines.append("新しいパソコンで、必要な値を入れ直してください。")
        lines.append("")
    unique_rewritten = sorted(set(rewritten))
    if unique_rewritten:
        lines.append("新しいパソコンのホームフォルダに合わせて戻すパス")
        for item in unique_rewritten:
            lines.append(f"- {item.replace(HOME_TOKEN, '~')}")
        lines.append("")
    unique_absolute = sorted(set(absolute))
    if unique_absolute:
        lines.append("このままでは届かないパス。新しいパソコンに合わせて書き換えてください")
        for item in unique_absolute:
            lines.append(f"- {item}")
        lines.append("")
    if rules is None:
        lines.append("ユーザールールは、このパソコンの設定データベースから読めませんでした。")
    elif rules == "":
        lines.append("このパソコンのファイルにはユーザールールがありませんでした。")
    else:
        lines.append(f"ユーザールールを user-rules.txt に書き出しました（{len(rules)}文字）。")
        lines.append("設定画面にだけ書いているルールは、ここには入っていません。")
    lines.append("")
    lines.append("フック（sage-hook など）は自動では戻しません。")
    lines.append("拡張機能を入れ直すと、その拡張機能がフックを作り直します。")
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
                "このパソコンの設定データベースからユーザールールを読めませんでした。\n",
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

    print(f"書き出しました: {out}")
    print("ログイン情報とチャット履歴は入れていません。")
    print("中の review.txt に、新しいパソコンで直すパスを書いてあります。")
    return 0


def bundle_root(path: Path) -> Path:
    if (path / "manifest.json").is_file():
        return path
    children = [child for child in path.iterdir() if (child / "manifest.json").is_file()]
    if len(children) == 1:
        return children[0]
    raise SystemExit("manifest.json が見つかりません。")


def open_bundle(path: Path) -> tuple[Path, Path | None]:
    if path.is_dir():
        return bundle_root(path), None
    if not path.is_file() or path.suffix.lower() != ".zip":
        raise SystemExit("フォルダか zip を指定してください。")
    extracted = Path(tempfile.mkdtemp(prefix="cursor-pack-import-"))
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            parts = Path(info.filename).parts
            if Path(info.filename).is_absolute() or ".." in parts:
                raise SystemExit("パックの中身が不正です。")
        archive.extractall(extracted)
    return bundle_root(extracted), extracted


def load_manifest(root: Path) -> dict[str, object]:
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit("manifest.json がありません。この道具で書き出したパックを指定してください。")
    data = json.loads(read_text(manifest_path))
    if data.get("version") != MANIFEST_VERSION:
        raise SystemExit("このパックの形式には対応していません。")
    return data


def install_file(src: Path, dest: Path, dry_run: bool) -> None:
    text = relocate_in(read_text(src)) if src.is_file() else ""
    if dry_run:
        print(f"戻す予定: {dest}")
        return
    saved = backup(dest)
    if saved:
        print(f"今のファイルを退避しました: {saved}")
    write_text(dest, text)
    if dest.name == "mcp.json":
        os.chmod(dest, 0o600)
    print(f"戻しました: {dest}")


def install_tree(src: Path, dest: Path, dry_run: bool) -> None:
    if dry_run:
        print(f"戻す予定: {dest}")
        return
    saved = backup(dest)
    if saved:
        print(f"今のフォルダを退避しました: {saved}")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    for path in dest.rglob("*"):
        if path.is_file() and is_text_file(path):
            write_text(path, relocate_in(read_text(path)))
    print(f"戻しました: {dest}")


def apply_user_rules(root: Path, dry_run: bool) -> int:
    rules_path = root / "user-rules.txt"
    if not rules_path.is_file():
        print("user-rules.txt がありません。")
        return 1
    rules = read_text(rules_path)
    if rules.startswith("このパソコンの設定データベースから"):
        print("パックにユーザールールが入っていないので、戻していません。")
        return 0
    db = state_db()
    if dry_run:
        print(f"ユーザールールを戻す予定: {db}")
        return 0
    if cursor_is_running():
        print("Cursorが起動中です。終了してから --apply-user-rules を付けてもう一度実行してください。")
        return 1
    if not db.is_file():
        print("Cursorの設定データベースがありません。Cursorを一度起動して終了してから、もう一度実行してください。")
        print(f"ルールの文面はここにあります: {rules_path}")
        return 1
    backup_text = user_dir() / f"user-rules.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}.txt"
    previous = read_user_rules()
    if previous is not None:
        write_text(backup_text, previous)
        print(f"今のユーザールールを退避しました: {backup_text}")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ItemTable(key, value) VALUES (?, ?)",
            (USER_RULE_KEY, rules),
        )
        conn.commit()
    print("ユーザールールを戻しました。")
    return 0


def install_extensions(root: Path, dry_run: bool) -> int:
    listing = root / "extensions.txt"
    if not listing.is_file():
        print("extensions.txt がありません。")
        return 1
    ids = [line.strip() for line in read_text(listing).splitlines() if line.strip()]
    if not ids:
        print("入れる拡張機能はありません。")
        return 0
    exe = shutil.which("cursor")
    if not exe:
        print("cursor コマンドが見つかりません。")
        print("Cursorのコマンドパレットで Install 'cursor' command in PATH を実行してください。")
        print("そのあと、このフォルダの install-extensions.sh を実行できます。")
        return 1
    if dry_run:
        for ext_id in ids:
            print(f"入れる予定: {ext_id}")
        return 0
    failed: list[str] = []
    for ext_id in ids:
        print(f"拡張機能を入れています: {ext_id}")
        result = subprocess.run([exe, "--install-extension", ext_id])
        if result.returncode != 0:
            failed.append(ext_id)
    if failed:
        print("入れられなかった拡張機能:")
        for ext_id in failed:
            print(f"- {ext_id}")
        return 1
    print(f"拡張機能を {len(ids)} 個入れました。")
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
        print("フックは戻していません。拡張機能を入れ直すと作り直されます。")
    if apply_rules:
        status = max(status, apply_user_rules(root, dry_run))
    else:
        print("ユーザールールは戻していません。戻すときは --apply-user-rules を付けて、Cursorを終了した状態で実行してください。")
    if install_ext:
        status = max(status, install_extensions(root, dry_run))
    else:
        print("拡張機能は入れていません。入れるときは --install-extensions を付けてください。")
    print("review.txt に、手で直すパスが書いてあります。")
    return status


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cursorの設定を別のパソコンへ運ぶ")
    commands = parser.add_subparsers(dest="command", required=True)

    export_cmd = commands.add_parser("export", help="このパソコンの設定をzipにまとめる")
    export_cmd.add_argument("--out", type=Path, help="書き出すzipのパス")

    import_cmd = commands.add_parser("import", help="zipまたはフォルダから設定を戻す")
    import_cmd.add_argument("bundle", type=Path, help="書き出したzipか、展開したフォルダ")
    import_cmd.add_argument("--install-extensions", action="store_true", help="拡張機能を入れる")
    import_cmd.add_argument("--apply-user-rules", action="store_true", help="ユーザールールを戻す")
    import_cmd.add_argument("--with-hooks", action="store_true", help="フックも戻す")
    import_cmd.add_argument("--dry-run", action="store_true", help="書き込まず、戻す内容だけ表示する")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
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
