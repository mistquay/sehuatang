"""解析 x1080x 普通 Discuz 页面，保留旧 archiver 样本的详情兼容。

选择器同时覆盖 archiver 简化页和普通页两套 DOM，
以兼容站点在不同镜像/版本间的差异。
"""
import re
from dataclasses import dataclass
from html import unescape
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from scrapers.core.contracts import DetailValidationError
from scrapers.core.cf_challenge import is_cf_blocked, is_cf_challenge, is_rate_limited

from util.javbee_code import normalize_code_key, resolve_x1080x_code

_TID_QUERY_RE = re.compile(r"(?:^|[?&])tid=(\d+)")
_TID_PATH_RE = re.compile(r"(?:^|/)thread-(\d+)-")
_PUBLISH_DATE_RE = re.compile(
    r"(?:发表于|發表於)\s*(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})"
)
_MAGNET_RE = re.compile(r"""magnet:\?[^\s<>"'\[]+""", re.I)
_CODE_BLOCK_RE = re.compile(r"\[code\](.*?)\[/code\]", re.S | re.I)
_IMG_BBCODE_RE = re.compile(r"\[img\](.*?)\[/img\]", re.I)
_UNAVAILABLE_MARKERS = (
    "database error",
    "archiver 功能没有开",
    "archiver 功能沒有開",
)
_GIF_MARKERS = (".gif", "format=gif", "ext=gif", "type=gif", "image/gif")


class ListValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ListPage:
    tids: List[int]
    last_page: Optional[int] = None


_EMPTY_LIST_MARKERS = ("本版块或指定的范围内尚无主题", "本版塊或指定的範圍內尚無主題",
                       "没有找到符合条件的主题", "沒有找到符合條件的主題")


def _ordered_unique(values) -> list:
    return list(dict.fromkeys(values))


def _is_gif_url(url: str) -> bool:
    candidate = str(url or "").strip().lower()
    return bool(candidate) and any(marker in candidate for marker in _GIF_MARKERS)


class X1080XParser:
    """x1080x 普通页面解析器。"""

    def __init__(self, type_map: Optional[Dict[str, str]] = None):
        self.type_map = dict(type_map or {})

    def is_unavailable(self, soup: BeautifulSoup, text: str) -> bool:
        title = (soup.title.get_text(strip=True) if soup.title else "").lower()
        body_text = text.lower()
        return any(
            marker in title or marker in body_text
            for marker in _UNAVAILABLE_MARKERS
        )

    def parse_list(self, html_content: bytes) -> List[int]:
        """从列表页提取帖子 tid（保持页面顺序、去重）。"""
        soup = BeautifulSoup(html_content, "html.parser")
        if self.is_unavailable(soup, soup.get_text(" ", strip=True)[:2000]):
            return []

        scope = (
            soup.select_one("#threadlist")
            or soup.select_one("#content")
            or soup
        )
        tids = []
        for anchor in scope.select('a[href*="viewthread"], a[href*="thread-"]'):
            href = anchor.get("href") or ""
            match = _TID_QUERY_RE.search(urlparse(href).query or href)
            if match:
                tids.append(int(match.group(1)))
                continue
            match = _TID_PATH_RE.search(href)
            if match:
                tids.append(int(match.group(1)))
        return _ordered_unique(tids)

    def parse_list_page(self, html_content: bytes, expected_page: int) -> ListPage:
        """仅明确末页/空列表可结束扫描，验证页与重定向页不算空页。"""
        if is_cf_blocked(html_content) or is_cf_challenge(html_content, 200) or is_rate_limited(html_content):
            raise ListValidationError("列表响应为验证或受限页面")
        soup = BeautifulSoup(html_content, "html.parser")
        scope = soup.select_one("#threadlist")
        if scope is None or self.is_unavailable(soup, soup.get_text(" ", strip=True)[:2000]):
            raise ListValidationError("缺少普通列表结构或站点不可用")
        tids = self.parse_list(html_content)
        current_pages, last_pages = set(), set()
        for pager in soup.select(".pg"):
            current = pager.select_one("strong")
            label = current.get_text(strip=True) if current else ""
            if not label.isdigit():
                raise ListValidationError("分页栏缺少当前页码")
            current_page = int(label)
            current_pages.add(current_page)
            has_next = bool(pager.select_one(".nxt, [rel~=next]"))
            linked_pages = []
            for anchor in pager.select("a[href]"):
                values = parse_qs(urlparse(anchor.get("href", "")).query)
                number = values.get("page", [""])[0]
                if number.isdigit():
                    linked_pages.append(int(number))
                    label = anchor.get_text(strip=True)
                    if "last" in anchor.get("class", []) or label.startswith("...") or re.search(r"末[页頁]|尾[页頁]", label):
                        last_pages.add(int(number))
                if re.search(r"下一[页頁]|next", anchor.get_text(strip=True), re.I):
                    has_next = True
            if not has_next and all(number <= current_page for number in linked_pages):
                last_pages.add(current_page)
        if current_pages and current_pages != {expected_page}:
            raise ListValidationError(f"响应页码与请求第 {expected_page} 页不一致")
        if len(last_pages) > 1:
            raise ListValidationError("分页栏末页不一致")
        last_page = next(iter(last_pages), None)
        if last_page is not None and last_page < expected_page:
            raise ListValidationError("请求页码超过末页")
        if not tids:
            if not any(marker in scope.get_text(" ", strip=True) for marker in _EMPTY_LIST_MARKERS):
                raise ListValidationError("列表为空但缺少明确空列表提示")
            if last_page is not None and last_page > expected_page:
                raise ListValidationError("末页之前出现空列表")
        return ListPage(tids, last_page)

    def parse_detail(
        self,
        html_content: bytes,
        detail_url: str,
        *,
        tid: int,
        fid: int,
        typeid: str = "",
        section: str = "",
        strict: bool = False,
    ) -> Optional[Dict[str, Any]]:
        def invalid(reason):
            if strict:
                raise DetailValidationError(reason)
            return None

        soup = BeautifulSoup(html_content, "html.parser")
        page_text = soup.get_text(" ", strip=True)
        if self.is_unavailable(soup, page_text[:2000]):
            return invalid("page_unavailable")

        title = self._extract_title(soup)
        if not title:
            return invalid("missing_title")

        date_value = self._extract_publish_date(soup)
        if not date_value:
            return invalid("missing_date")

        content_node = self._content_node(soup)
        if content_node is None:
            return invalid("missing_content")
        content_html = content_node.decode_contents()
        content_text = content_node.get_text("\n", strip=True)

        magnets = self._extract_magnets(content_html, content_text)
        if not magnets and self._requires_membership(content_node):
            return invalid("membership_required")
        page_typeid, page_section = self._extract_section(soup)
        resolved_typeid = page_typeid or str(typeid or "")
        resolved_section = (
            page_section
            or self.type_map.get(resolved_typeid, "")
            or str(section or "")
        )

        code_resolution = resolve_x1080x_code(title, magnets)
        return {
            "source_key": str(tid),
            "tid": int(tid),
            "fid": int(fid),
            "typeid": resolved_typeid,
            "section": resolved_section,
            "title": title,
            "code": code_resolution.code,
            "code_normalized": normalize_code_key(code_resolution.code),
            "code_source": code_resolution.source,
            "code_confidence": code_resolution.confidence,
            "date": date_value,
            "url": detail_url,
            "magnet": magnets[0] if magnets else None,
            "magnets": magnets,
            "img": self._extract_images(content_node, content_html, detail_url),
        }

    @staticmethod
    def _requires_membership(content_node) -> bool:
        for node in content_node.select(".locked"):
            text = node.get_text(" ", strip=True)
            if "VIP" in text.upper() and any(marker in text for marker in (
                "免费", "免費", "下载", "下載", "加入", "会员", "會員",
            )):
                return True
        return False

    @staticmethod
    def _extract_title(soup: BeautifulSoup) -> str:
        node = soup.select_one("#thread_subject")
        if node:
            title = node.get_text(strip=True)
            if title:
                return title

        nav = soup.select_one("#nav") or soup.select_one("#pt")
        if nav:
            nav_text = nav.get_text(" ", strip=True)
            parts = [
                part.strip()
                for part in re.split(r"[›»>]+", nav_text)
                if part.strip()
            ]
            if parts:
                return parts[-1]
        return ""

    @staticmethod
    def _extract_publish_date(soup: BeautifulSoup) -> Optional[str]:
        candidates = []
        for node in soup.select('em[id^="authorposton"]'):
            # 普通页可能显示“几分钟前”，完整日期位于子节点 title 属性。
            candidates.extend("发表于 " + child.get("title", "") for child in node.select("[title]"))
            candidates.append(node.get_text(" ", strip=True))
        for node in soup.select("p.author"):
            candidates.append(node.get_text(" ", strip=True))
        for text in candidates:
            match = _PUBLISH_DATE_RE.search(text)
            if match:
                raw = match.group(1).replace("/", "-").replace(".", "-")
                parts = raw.split("-")
                if len(parts) == 3:
                    return f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        return None

    @staticmethod
    def _content_node(soup: BeautifulSoup):
        node = soup.select_one('[id^="postmessage_"]')
        if node is not None:
            return node
        return soup.select_one("#content")

    @staticmethod
    def _extract_magnets(content_html: str, content_text: str) -> List[str]:
        candidates = []
        decoded_html = unescape(content_html or "")
        decoded_text = unescape(content_text or "")

        for block in _CODE_BLOCK_RE.findall(decoded_html):
            block = block.strip()
            if block.lower().startswith("magnet:?"):
                candidates.append(block)

        for source in (decoded_html, decoded_text):
            candidates.extend(
                match.rstrip(".,;，。")
                for match in _MAGNET_RE.findall(source)
            )
        return _ordered_unique(
            value.strip()
            for value in candidates
            if value.strip().lower().startswith("magnet:?")
        )

    @staticmethod
    def _extract_images(content_node, content_html: str, base_url: str) -> List[str]:
        candidates = list(_IMG_BBCODE_RE.findall(content_html or ""))
        for image in content_node.select("img"):
            for attr in ("file", "zoomfile", "src"):
                value = (image.get(attr) or "").strip()
                if value:
                    candidates.append(value)

        urls = []
        for candidate in candidates:
            url = urljoin(base_url, candidate.strip())
            if urlparse(url).scheme in ("http", "https") and not _is_gif_url(url):
                urls.append(url)
        return _ordered_unique(urls)

    def _extract_section(self, soup: BeautifulSoup) -> tuple[str, str]:
        """优先从 typeid 链接取分类；其次从面包屑匹配已知分类名。"""
        for anchor in soup.select('a[href*="typeid="]'):
            href = anchor.get("href") or ""
            type_id = parse_qs(urlparse(href).query).get("typeid", [""])[0]
            if type_id in self.type_map:
                text = anchor.get_text(strip=True)
                section = (
                    text
                    if text in self.type_map.values()
                    else self.type_map[type_id]
                )
                return type_id, section

        nav = soup.select_one("#nav") or soup.select_one("#pt")
        if nav:
            parts = [
                part.strip()
                for part in re.split(r"[›»>]+", nav.get_text(" ", strip=True))
                if part.strip()
            ]
            known = {name: type_id for type_id, name in self.type_map.items()}
            for part in reversed(parts):
                if part in known:
                    return known[part], part
        return "", ""
