import streamlit as st
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from io import BytesIO
from PIL import Image
import os
import json
import hashlib
import zipfile
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed


st.set_page_config(
    page_title="漫画画像抽出ツール",
    page_icon="🖼️",
    layout="wide",
)

st.title("🖼️ 漫画画像抽出ツール（抽出だけ）")
st.markdown("URLから漫画画像を抽出し、一覧表示・ZIPダウンロードします（AI解析はしません）。")


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _get_output_base_dir() -> str:
    """保存先（リポジトリ内 output/）"""
    try:
        base = os.path.dirname(__file__)
    except Exception:
        base = os.getcwd()
    return os.path.join(base, "output")


def _ensure_output_dir() -> str:
    base = _get_output_base_dir()
    os.makedirs(base, exist_ok=True)
    return base


def _make_run_id() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    rnd = hashlib.sha256(os.urandom(16)).hexdigest()[:8]
    return f"{ts}_{rnd}"


def _zip_bytes_from_files(file_map: dict[str, bytes]) -> bytes:
    """{zip内パス: bytes} をZIP化して返す"""
    buf = BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_path, data in file_map.items():
            zf.writestr(rel_path, data)
    return buf.getvalue()


def get_request_headers(url: str) -> dict:
    parsed_url = urlparse(url)
    base_domain = f"{parsed_url.scheme}://{parsed_url.netloc}"
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Referer": base_domain,
    }


def get_pagination_urls(url: str, soup: BeautifulSoup, debug: bool = False) -> list[str]:
    """ページネーションのURLを取得（同一記事内の /2 /3... を想定）"""
    urls = [url]

    pagination_selectors = [
        ".pagination a",
        ".page-numbers a",
        ".pager a",
        ".wp-pagenavi a",
        "nav.navigation a",
        ".post-page-numbers",
        "a.page-link",
        ".pages a",
    ]

    pagination_links = []
    for selector in pagination_selectors:
        links = soup.select(selector)
        if links:
            pagination_links.extend(links)
            if debug:
                st.write(f"ページネーション検出: {selector} ({len(links)}件)")
            break

    if not pagination_links:
        # rel=next（同一記事の次ページを指すことが多い）
        rel_next = soup.select_one('a[rel="next"], link[rel="next"]')
        if rel_next and rel_next.get("href"):
            href = rel_next.get("href")
            # 後段の共通処理（hrefを読んでURL化）に乗せるため、擬似的にaとして扱う
            rel_next.name = "a"
            rel_next["href"] = href
            pagination_links.append(rel_next)
            if debug:
                st.write(f"rel=next をページネーション候補として追加: {urljoin(url, href)}")

    if not pagination_links:
        all_links = soup.find_all("a")
        base_path = urlparse(url).path.rstrip("/")
        for link in all_links:
            text = link.get_text(strip=True)
            href = link.get("href", "")
            if not href:
                continue
            if text.isdigit():
                full_href = urljoin(url, href)
                href_path = urlparse(full_href).path.rstrip("/")
                if href_path.startswith(base_path):
                    pagination_links.append(link)
                    if debug:
                        st.write(f"数字リンク検出: {text} -> {full_href}")

    if not pagination_links:
        # 「次のページ」等のテキストリンク（数字リンクが無いサイト向け）
        for link in soup.find_all("a"):
            text = link.get_text(" ", strip=True)
            href = link.get("href", "")
            if not href or not text:
                continue
            if any(k in text for k in ["次のページ", "次ページ", "next page", "Next Page"]):
                full_href = urljoin(url, href)
                if urlparse(full_href).netloc == urlparse(url).netloc:
                    pagination_links.append(link)
                    if debug:
                        st.write(f"次ページテキストリンク検出: {text} -> {full_href}")
                break

    base_path = urlparse(url).path.rstrip("/")
    seen = {url}
    for link in pagination_links:
        href = link.get("href")
        if not href:
            continue
        full_url = urljoin(url, href)
        if urlparse(full_url).netloc != urlparse(url).netloc:
            continue
        if full_url in seen:
            continue
        # 同一記事のページネーションだけに限定（次話/次記事ナビが混ざるのを防ぐ）
        full_path = urlparse(full_url).path.rstrip("/")
        if full_path != base_path and not _looks_like_intra_post_pagination(url, full_url):
            continue

        text = link.get_text(strip=True).lower()
        if text in ["next", "prev", "previous", "»", "«", "›", "‹", "次へ", "前へ"]:
            continue
        urls.append(full_url)
        seen.add(full_url)

    def extract_page_num(u: str) -> int:
        path = urlparse(u).path.rstrip("/")
        if path == base_path:
            return 1
        if path.startswith(base_path + "/"):
            suffix = path[len(base_path) + 1 :]
            if suffix.isdigit():
                return int(suffix)
        return 999

    urls.sort(key=extract_page_num)

    if debug and len(urls) > 1:
        st.write(f"検出されたページ: {len(urls)}ページ")
        for u in urls:
            st.write(f"  - {u}")

    return urls


def get_page_images(url: str, debug: bool = False) -> tuple[list[dict], BeautifulSoup | None]:
    """ページから画像URLを抽出"""
    headers = get_request_headers(url)

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
    except requests.RequestException as e:
        st.error(f"ページの取得に失敗しました: {e}")
        return [], None

    soup = BeautifulSoup(response.content, "html.parser")
    images: list[dict] = []

    if debug:
        st.write(f"HTMLサイズ: {len(response.content)} bytes")

    content_selectors = [
        "article",
        ".entry-content",
        ".post-content",
        ".article-content",
        ".content",
        ".single-content",
        ".post-body",
        ".article-body",
        "main",
        "#content",
        "#main",
        ".post",
        ".entry",
        ".ystd",
        "#ystd",
    ]

    content_area = None
    for selector in content_selectors:
        content_area = soup.select_one(selector)
        if content_area:
            if debug:
                st.write(f"コンテンツエリア検出: {selector}")
            break

    if not content_area:
        content_area = soup.body if soup.body else soup
        if debug:
            st.write("コンテンツエリア: body全体")

    img_tags = content_area.find_all("img")
    if debug:
        st.write(f"検出されたimgタグ数: {len(img_tags)}")

    skip_patterns = [
        "icon",
        "logo",
        "avatar",
        "emoji",
        "button",
        "banner",
        "advertisement",
        "widget",
        "gravatar",
        "favicon",
        "sprite",
        "pixel",
        "tracking",
        "analytics",
        "1x1",
    ]

    for img in img_tags:
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original")
            or img.get("data-full-url")
            or img.get("data-lazy")
            or img.get("data-image")
            or (img.get("data-srcset", "").split()[0] if img.get("data-srcset") else None)
            or (img.get("data-lazy-srcset", "").split()[0] if img.get("data-lazy-srcset") else None)
            or (img.get("srcset", "").split()[0] if img.get("srcset") else None)
        )
        if not src:
            if debug:
                st.write(f"⚠️ src無し: {str(img)[:100]}...")
            continue
        if src.startswith("data:"):
            if debug:
                st.write("⚠️ data URI スキップ")
            continue

        img_url = urljoin(url, src)
        if any(p in img_url.lower() for p in skip_patterns):
            if debug:
                st.write(f"⚠️ スキップパターン: {img_url[:80]}...")
            continue

        img_extensions = [".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"]
        has_img_ext = any(ext in img_url.lower() for ext in img_extensions)

        img_path_patterns = ["/uploads/", "/images/", "/wp-content/", "/img/", "/photo/", "/manga/", "/comic/"]
        has_img_path = any(p in img_url.lower() for p in img_path_patterns)

        has_size_param = any(x in img_url.lower() for x in ["width=", "height=", "w=", "h=", "size=", "resize"])

        if has_img_ext or has_img_path or has_size_param:
            images.append({"url": img_url, "alt": img.get("alt", "")})
            if debug:
                st.write(f"✅ 画像追加: {img_url[:80]}...")
        else:
            if debug:
                st.write(f"❌ 条件不一致でスキップ: {img_url[:80]}...")

    # 重複除去
    seen_urls: set[str] = set()
    unique_images: list[dict] = []
    for item in images:
        u = item.get("url", "")
        if not u or u in seen_urls:
            continue
        seen_urls.add(u)
        unique_images.append(item)

    return unique_images, soup


def _looks_like_intra_post_pagination(current_url: str, candidate_url: str) -> bool:
    """同一記事内ページネーション（/2 /3 ...）っぽいURLかどうか。

    例:
    - current: https://aikatu.jp/archives/1031854
      cand:    https://aikatu.jp/archives/1031854/2
    - current: https://w.grapps.me/original/624941/
      cand:    https://w.grapps.me/original/624941/2/
    """
    try:
        cu = urlparse(current_url)
        nu = urlparse(candidate_url)
        if cu.netloc != nu.netloc:
            return False
        base = cu.path.rstrip("/")
        cand = nu.path.rstrip("/")
        if not cand.startswith(base + "/"):
            return False
        suffix = cand[len(base) + 1 :]
        return suffix.isdigit()
    except Exception:
        return False


def get_next_episode_url(soup: BeautifulSoup, base_url: str, debug: bool = False) -> str | None:
    """「次の話>>」のURLを取得（特定サイト向けの緩い実装）"""
    # 1) 旧ロジック（特定サイト向け）
    next_episode_div = soup.find("div", class_="page-text-body", string=lambda t: t and "次の話" in t)
    if next_episode_div:
        parent = next_episode_div.find_parent("a")
        if parent and parent.get("href"):
            next_url = urljoin(base_url, parent["href"])
            # /2など同一記事内ページネーションは「次話」ではない
            if not _looks_like_intra_post_pagination(base_url, next_url):
                if debug:
                    st.write(f"🔗 次の話を検出(div): {next_url}")
                return next_url
        next_link = next_episode_div.find_next("a")
        if next_link and next_link.get("href"):
            next_url = urljoin(base_url, next_link["href"])
            if not _looks_like_intra_post_pagination(base_url, next_url):
                if debug:
                    st.write(f"🔗 次の話を検出(div-next): {next_url}")
                return next_url

    # 2) WordPress系の「次の記事」ナビ（nav-next）
    for sel in [
        "nav.post-navigation .nav-next a",
        "nav.navigation.post-navigation .nav-next a",
        ".post-navigation .nav-next a",
        ".navigation.post-navigation .nav-next a",
    ]:
        a = soup.select_one(sel)
        if a and a.get("href"):
            next_url = urljoin(base_url, a["href"])
            if _looks_like_intra_post_pagination(base_url, next_url):
                continue
            if debug:
                st.write(f"🔗 次の話を検出(nav-next): {next_url}")
            return next_url

    # 3) テキストで「次の話」を優先して探す（「次のページ」より優先）
    keywords_strong = ["次の話", "次の話＞＞", "次の話>>", "次話", "次のエピソード"]
    for a in soup.find_all("a"):
        tx = a.get_text(" ", strip=True)
        href = a.get("href")
        if not tx or not href:
            continue
        if any(k in tx for k in keywords_strong):
            next_url = urljoin(base_url, href)
            if _looks_like_intra_post_pagination(base_url, next_url):
                continue
            if debug:
                st.write(f"🔗 次の話を検出(text): {tx[:40]} -> {next_url}")
            return next_url

    if debug:
        st.write("ℹ️ 「次の話」リンクは見つかりませんでした")
    return None


def get_episode_images(url: str, episode_num: int = 1, debug: bool = False) -> tuple[list[dict], str | None]:
    """1話分の画像を取得（ページネーション込み）"""
    first_page_images, soup = get_page_images(url, debug)
    if not soup:
        return [], None

    next_episode_url = get_next_episode_url(soup, url, debug)
    page_urls = get_pagination_urls(url, soup, debug)

    all_images: list[dict] = []
    seen_urls: set[str] = set()

    if debug:
        st.write(f"📖 第{episode_num}話の取得開始")

    for img in first_page_images:
        if img["url"] in seen_urls:
            continue
        img["page"] = 1
        img["episode"] = episode_num
        all_images.append(img)
        seen_urls.add(img["url"])

    if len(page_urls) > 1:
        for i, page_url in enumerate(page_urls[1:], start=2):
            if debug:
                st.write(f"  ページ {i} を取得中: {page_url}")
            page_images, page_soup = get_page_images(page_url, debug)
            for img in page_images:
                if img["url"] in seen_urls:
                    continue
                img["page"] = i
                img["episode"] = episode_num
                all_images.append(img)
                seen_urls.add(img["url"])
            if page_soup and not next_episode_url:
                next_episode_url = get_next_episode_url(page_soup, page_url, debug)

    if debug:
        st.write(f"📖 第{episode_num}話: {len(all_images)}枚の画像を取得")

    return all_images, next_episode_url


def get_multiple_episodes_images(url: str, num_episodes: int, debug: bool = False) -> list[dict]:
    """複数話の画像を取得（次の話リンクを辿る）"""
    all_images: list[dict] = []
    current_url: str | None = url

    for episode in range(1, num_episodes + 1):
        if not current_url:
            if debug:
                st.write(f"⚠️ 第{episode}話のURLがありません。取得を終了します。")
            break
        if debug:
            st.write(f"📚 第{episode}話を取得中: {current_url}")
        episode_images, next_url = get_episode_images(current_url, episode_num=episode, debug=debug)
        all_images.extend(episode_images)
        current_url = next_url
        if not next_url and episode < num_episodes:
            if debug:
                st.write(f"ℹ️ 第{episode}話が最終話です。{episode}話分を取得しました。")
            break

    if debug:
        st.write(f"✅ 合計 {len(all_images)}枚の画像を取得")

    return all_images


def download_image(url: str, referer: str = "") -> bytes | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": referer,
    }
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.content
    except requests.RequestException:
        return None


@st.cache_data(show_spinner=False, ttl=60 * 10)
def _cached_download_image(url: str, referer: str = "") -> bytes | None:
    return download_image(url, referer)


def _download_and_validate_image(
    img_info: dict,
    min_size: int,
    referer: str,
) -> dict | None:
    """1枚の画像をダウンロードしてバリデーション（並列処理用）"""
    img_data = download_image(img_info["url"], referer)
    if not img_data:
        return None

    if len(img_data) < min_size:
        return None

    try:
        img = Image.open(BytesIO(img_data))
        width, height = img.size
        aspect_ratio = width / height if height > 0 else 0

        if aspect_ratio > 3:
            return None
        if width < 200 or height < 200:
            return None

        return {
            **img_info,
            "data": img_data,
            "width": width,
            "height": height,
            "size": len(img_data),
        }
    except Exception:
        return None


def filter_manga_images(
    images: list[dict],
    min_size: int = 50_000,
    referer: str = "",
    debug: bool = False,
    max_workers: int = 10,
    progress_callback=None,
) -> list[dict]:
    """漫画画像をフィルタリング（サイズ/縦横/アスペクト比）- 並列ダウンロード対応"""
    manga_images: list[dict] = []
    total = len(images)
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_img = {
            executor.submit(_download_and_validate_image, img_info, min_size, referer): img_info
            for img_info in images
        }

        results_map: dict[str, dict] = {}

        for future in as_completed(future_to_img):
            img_info = future_to_img[future]
            completed += 1

            if progress_callback:
                progress_callback(completed, total)

            try:
                result = future.result()
                if result:
                    results_map[img_info["url"]] = result
                    if debug:
                        st.write(f"✅ 取得成功: {img_info['url'][:60]}...")
                else:
                    if debug:
                        st.write(f"❌ フィルタ除外: {img_info['url'][:60]}...")
            except Exception as e:
                if debug:
                    st.write(f"⚠️ エラー: {img_info['url'][:60]}... - {e}")

    for img_info in images:
        if img_info["url"] in results_map:
            manga_images.append(results_map[img_info["url"]])

    return manga_images


def _guess_ext(img_bytes: bytes, fallback_ext: str = ".jpg") -> str:
    try:
        img = Image.open(BytesIO(img_bytes))
        fmt = (img.format or "").upper()
        if fmt == "JPEG":
            return ".jpg"
        if fmt == "PNG":
            return ".png"
        if fmt == "WEBP":
            return ".webp"
        if fmt == "GIF":
            return ".gif"
        if fmt == "AVIF":
            return ".avif"
    except Exception:
        pass
    return fallback_ext


def build_images_zip(manga_images: list[dict]) -> tuple[bytes, dict[str, str]]:
    """画像をZIP化して返す。戻り値は(zip_bytes, filename_map[url]=zip内パス)"""
    file_map: dict[str, bytes] = {}
    name_map: dict[str, str] = {}

    for idx, img in enumerate(manga_images, start=1):
        ep = int(img.get("episode", 1) or 1)
        page = int(img.get("page", 1) or 1)
        ext = _guess_ext(img.get("data") or b"")
        rel = f"images/ep{ep:02d}_p{page:03d}_{idx:04d}{ext}"
        file_map[rel] = img["data"]
        name_map[img.get("url", f"idx:{idx}")] = rel

    zip_bytes = _zip_bytes_from_files(file_map)
    return zip_bytes, name_map


with st.sidebar:
    st.header("⚙️ 設定")

    debug_mode = st.checkbox("デバッグモード", value=False, help="画像検出の詳細を表示します")
    min_image_size_kb = st.slider(
        "最小画像サイズ (KB)",
        min_value=1,
        max_value=800,
        value=30,
        help="この値より小さい画像は除外されます",
    )
    max_images_total = st.slider(
        "抽出する最大画像枚数（上限）",
        min_value=5,
        max_value=300,
        value=120,
        step=5,
        help="多いほど重くなります（表示/ZIPも大きくなります）",
    )
    parallel_downloads = st.slider(
        "並列ダウンロード数",
        min_value=1,
        max_value=20,
        value=10,
        help="同時にダウンロードする画像数。大きいほど速いですがサーバー負荷が上がります",
    )
    st.divider()
    st.subheader("📚 取得範囲")
    mode = st.radio(
        "取得モード",
        options=["エピ漫画（1話）", "連載漫画（3話）", "連載漫画（10話）", "任意話数"],
        index=0,
    )
    if mode == "任意話数":
        num_episodes = st.number_input("話数", min_value=1, max_value=30, value=1, step=1)
    elif mode == "連載漫画（10話）":
        num_episodes = 10
    elif mode == "連載漫画（3話）":
        num_episodes = 3
    else:
        num_episodes = 1


url = st.text_input(
    "漫画記事URL",
    placeholder="https://example.com/manga/xxxx",
    help="漫画ページ（開始話）のURLを入れてください",
)

col1, col2 = st.columns([1, 4])
with col1:
    extract_button = st.button("🖼️ 抽出開始", type="primary", use_container_width=True)


if extract_button:
    if not url:
        st.error("URLを入力してください")
    else:
        with st.spinner("ページから画像を取得中..."):
            images = get_multiple_episodes_images(url, num_episodes=int(num_episodes), debug=debug_mode)

        if not images:
            st.warning("画像が見つかりませんでした。デバッグモードをONにして詳細を確認してください。")
        else:
            st.info(f"📷 {len(images)}件の画像候補を検出しました。並列ダウンロード中（{parallel_downloads}並列）...")

            progress_bar = st.progress(0, text="画像をダウンロード中...")

            def update_progress(completed: int, total: int):
                progress = completed / total
                progress_bar.progress(progress, text=f"画像をダウンロード中... {completed}/{total}")

            manga_images = filter_manga_images(
                images,
                min_size=int(min_image_size_kb) * 1000,
                referer=url,
                debug=debug_mode,
                max_workers=int(parallel_downloads),
                progress_callback=update_progress,
            )

            progress_bar.empty()

            if not manga_images:
                st.warning("漫画画像が見つかりませんでした。フィルタ設定（最小サイズなど）を調整してください。")
                if debug_mode and images:
                    st.subheader("検出された画像URL一覧（フィルタ前）")
                    for img in images:
                        st.text(img["url"])
            else:
                if len(manga_images) > int(max_images_total):
                    st.warning(f"⚠️ 画像が{len(manga_images)}枚あります。上限により先頭{int(max_images_total)}枚だけ扱います。")
                    manga_images = manga_images[: int(max_images_total)]

                # 話数ごとの枚数
                episode_counts: dict[int, int] = {}
                for img in manga_images:
                    ep = int(img.get("episode", 1) or 1)
                    episode_counts[ep] = episode_counts.get(ep, 0) + 1
                episode_summary = "、".join([f"第{ep}話: {count}枚" for ep, count in sorted(episode_counts.items())])
                st.success(f"✅ {len(manga_images)}件の漫画画像を抽出しました（{episode_summary}）")

                st.divider()
                st.subheader("🖼️ 抽出結果（プレビュー）")

                cols_per_row = 3
                for i in range(0, len(manga_images), cols_per_row):
                    cols = st.columns(cols_per_row)
                    for j, col in enumerate(cols):
                        idx = i + j
                        if idx >= len(manga_images):
                            continue
                        img_info = manga_images[idx]
                        with col:
                            ep = int(img_info.get("episode", 1) or 1)
                            page = int(img_info.get("page", 1) or 1)
                            st.image(
                                img_info["data"],
                                caption=f"第{ep}話 P{page} / {img_info.get('width')}x{img_info.get('height')} / {int(img_info.get('size',0))/1024:.1f}KB",
                                use_container_width=True,
                            )

                st.divider()
                st.subheader("⬇️ ダウンロード")

                zip_bytes, name_map = build_images_zip(manga_images)
                run_id = _make_run_id()
                st.download_button(
                    "画像ZIPをダウンロード",
                    data=zip_bytes,
                    file_name=f"manga_images_{run_id}.zip",
                    mime="application/zip",
                    use_container_width=True,
                )

                # JSON（URLとメタ）
                items = []
                for img in manga_images:
                    items.append(
                        {
                            "episode": int(img.get("episode", 1) or 1),
                            "page": int(img.get("page", 1) or 1),
                            "url": img.get("url", ""),
                            "alt": img.get("alt", ""),
                            "width": int(img.get("width", 0) or 0),
                            "height": int(img.get("height", 0) or 0),
                            "size_bytes": int(img.get("size", 0) or 0),
                            "zip_path": name_map.get(img.get("url", ""), ""),
                        }
                    )

                st.download_button(
                    "画像一覧JSONをダウンロード",
                    data=json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8"),
                    file_name=f"manga_images_{run_id}.json",
                    mime="application/json",
                    use_container_width=True,
                )

                with st.expander("💾 output/ に保存（任意）", expanded=False):
                    st.caption("サーバー上の `output/<run_id>/` に保存します（ローカル運用向け）。")
                    if st.button("保存する", use_container_width=True):
                        base = _ensure_output_dir()
                        run_dir = os.path.join(base, run_id)
                        img_dir = os.path.join(run_dir, "images")
                        os.makedirs(img_dir, exist_ok=True)

                        # 画像ファイル保存
                        for img in manga_images:
                            zp = name_map.get(img.get("url", ""), "")
                            if not zp.startswith("images/"):
                                continue
                            rel_name = zp[len("images/") :]
                            out_path = os.path.join(img_dir, rel_name)
                            with open(out_path, "wb") as f:
                                f.write(img["data"])

                        meta = {
                            "url": url,
                            "num_episodes": int(num_episodes),
                            "min_image_size_kb": int(min_image_size_kb),
                            "max_images_total": int(max_images_total),
                            "total_candidates": len(images),
                            "total_extracted": len(manga_images),
                            "episode_counts": episode_counts,
                        }
                        with open(os.path.join(run_dir, "images.json"), "w", encoding="utf-8") as f:
                            json.dump(items, f, ensure_ascii=False, indent=2)
                        with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as f:
                            json.dump(meta, f, ensure_ascii=False, indent=2)

                        st.success(f"保存しました: output/{run_id}/")

                if debug_mode:
                    st.divider()
                    st.subheader("🔎 デバッグ情報")
                    st.write("候補画像（フィルタ前）:", len(images))
                    st.write("抽出画像（フィルタ後）:", len(manga_images))
                    st.write("入力URLのドメイン:", urlparse(url).netloc)
                    st.write("URLのハッシュ:", _sha256_text(url)[:16])


