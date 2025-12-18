#!/bin/bash

# 漫画画像抽出ツール起動スクリプト

echo "🚀 漫画画像抽出ツールを起動します..."
echo "ポート: 8516"
echo "URL: http://localhost:8516"
echo ""

# プロジェクトディレクトリに移動
cd "$(dirname "$0")"

# Streamlit をヘッドレスモードで実行
export STREAMLIT_SERVER_HEADLESS=true

# 8516ポートでアプリを起動
/Users/s-hashimoto/Documents/CURSOR/.venv/bin/streamlit run app.py --server.port 8516 --server.address 0.0.0.0 --server.headless=true


