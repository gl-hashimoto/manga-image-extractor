# 🚀 漫画画像抽出ツール（8516）をStreamlitでWeb公開する手順

このツールは **AI/APIキー不要** です（画像の抽出・ZIP化のみ）。

> 注意: 対象サイトの利用規約やrobots.txt、直リンク制限、Cloudflare等のブロックにより、Web環境（Streamlit Cloud）では取得できない場合があります。

---

## 方式A（推奨）: 8516だけを単体リポジトリにしてStreamlit Cloudにデプロイ

### ステップ1: GitHubリポジトリ作成＆push

```bash
cd "/Users/s-hashimoto/Documents/CURSOR/#biz_制作ツール/8516_漫画画像抽出ツール"

git init
git add .
git commit -m "Initial commit: 漫画画像抽出ツール（8516）"

# gh CLIが入っている場合（おすすめ）
gh repo create manga-image-extractor --private --source=. --push
```

（`gh` が無い場合）
- GitHubで新規リポジトリを作成 → 表示される手順に従って `git remote add origin ...` → `git push`

### ステップ2: Streamlit Cloudでデプロイ

1. Streamlit Cloud（`https://share.streamlit.io/`）にアクセス → GitHubでログイン
2. **New app** をクリック
3. 以下を設定:
   - **Repository**: `（上で作成したリポジトリ）`
   - **Branch**: `main`
   - **Main file path**: `app.py`
4. **Advanced settings**:
   - **Python version**: `3.11`（`runtime.txt` があれば概ね合わせて動きます）
   - **Secrets**: 不要（空でOK）
5. **Deploy!**

---

## 方式B: モノレポからデプロイ（Main file pathでサブディレクトリ指定）

Streamlit Cloudでは「Main file path」をサブディレクトリにできます。

例（モノレポをGitHubにpushしている前提）:
- **Main file path**: `#biz_制作ツール/8516_漫画画像抽出ツール/app.py`

> 注意: `#` や日本語パスを含むため、環境によってはパス解決で詰まる可能性があります。詰まった場合は方式A（単体リポジトリ）がおすすめです。

---

## デプロイ後の確認

- アクセスURLは `https://<app-name>.streamlit.app/`
- URLを入れて「🖼️ 抽出開始」→「画像ZIPをダウンロード」まで動けばOKです。

---

## よくある詰まり

### 取得が失敗する（ローカルはOK、WebはNG）

- サイト側が **クラウドIP** をブロックしている
- **Referer必須/直リンク禁止** の仕様になっている
- **Cloudflare** 等でBot判定されている

この場合は、
- ローカル運用に切り替える
- 対象サイト専用の取得方法（ヘッダ/待機/リトライ/セレクタ）を調整する
などが必要です。


