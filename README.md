# Cursor設定移行アシスタント

Cursor の設定を、別のパソコンへ移すためのツールです。

コマンド実行における表示言語は、パソコンの設定言語に依存します。言語の変更は `--lang ja` か `--lang en` を用いることで変更可能です。

[English](#english)

## このパソコンで書き出す

```sh
python3 cursor_pack.py export
```

実行したフォルダに `cursor-settings-日付.zip` ができます。この zip を USB、AirDrop、クラウドなどで新しいパソコンへコピーしてください。

## 新しいパソコンで戻す

1. Cursor をインストールしてください
2. 一度起動してから終了してください
3. zip を展開してください
4. 展開したフォルダで次を実行してください（`--apply-user-rules` は、Cursor を終了した状態で実行してください）

```sh
python3 cursor_pack.py import . --install-extensions --apply-user-rules
```

展開したフォルダの `review.txt` には、新しいパソコンではそのまま使えないパスが書いてあります。

## 含まれるもの

- エディタ設定（`settings.json`）
- キーバインド
- スニペット
- MCP の設定
- 入れている拡張機能の一覧
- このパソコンのファイルに保存されているユーザールール

ホームフォルダのパスは `__HOME__` に置き換えてあります。戻すときに、新しいパソコンのホームフォルダのパスへ戻します。

## 含まれないもの

- ログイン情報、メールアドレス、チャット履歴
- 拡張機能そのもの（新しいパソコンで入れ直します）
- トークンやパスワード（設定に含まれていれば、値を空にします）
- フック（拡張機能を入れ直すと、その拡張機能がフックを作り直します）

設定画面にだけあり、このパソコンのファイルには保存されていないルールは含まれません。書き出した `user-rules.txt` を確認し、足りないものがあれば設定画面からコピーしてください。

## English

A tool for moving Cursor settings to another computer.

Messages follow this computer's language: Japanese if it is Japanese, English otherwise. Choose with `--lang ja` or `--lang en`. The exported `README.txt` and `review.txt` are written in both languages.

### Export on this computer

```sh
python3 cursor_pack.py export
```

This creates `cursor-settings-date.zip` in the folder where you ran the command. Copy that zip to the new computer with a USB drive, AirDrop, or cloud storage.

### Import on the new computer

1. Install Cursor
2. Launch it once, then quit
3. Unzip the archive
4. Run the following in the unzipped folder (`--apply-user-rules` must be run while Cursor is quit)

```sh
python3 cursor_pack.py import . --install-extensions --apply-user-rules
```

`review.txt` in the unzipped folder lists paths that will not work as-is on the new computer.

### Included

- Editor settings (`settings.json`)
- Keybindings
- Snippets
- MCP settings
- A list of installed extensions
- User rules saved in files on this computer

Home-folder paths are replaced with `__HOME__`. On import, they are restored to the new computer's home folder.

### Not included

- Login information, email address, and chat history
- The extensions themselves (reinstall them on the new computer)
- Tokens and passwords (if they appear in settings, the values are cleared)
- Hooks (reinstalling an extension recreates that extension's hooks)

Rules that exist only in the settings screen, and are not saved in a file on this computer, are not included. Check the exported `user-rules.txt`, and copy anything missing from the settings screen.
