"""Read product request rows; never execute document code or legacy crawlers."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET

from .links import link_kind

MAX_INPUT = 20 * 1024 * 1024
MAX_ROWS = 10000
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
URL_PATTERN = re.compile(r"https?://[^\s<>\"\[\]|]+")
HEADER_NAMES = {
    "name": {"ten", "ten san pham", "san pham", "product", "product name", "product_name", "name", "title"},
    "category": {"danh muc", "nganh hang", "category", "nhom san pham"},
    "price": {"gia", "khoang gia", "gia ban", "price", "price range", "gia tham khao", "gia ban tham khao"},
    "url": {"url", "link", "product_url", "source_url", "pdp_url", "link san pham", "link san pham hoac nguon phat hien"},
}


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.lower().replace("đ", "d"))
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split()).strip(" :")


def _urls(text: str) -> list[str]:
    return list(dict.fromkeys(match.rstrip(").,;") for match in URL_PATTERN.findall(text)))


def _price_range(text: str) -> tuple[int | float | None, int | float | None]:
    matches = re.findall(r"(\d[\d.,]*)(?:\s*(triệu|nghìn|ngàn|tr|k)\b)?", text, re.I)
    if not 1 <= len(matches) <= 2 or "%" in text:
        return None, None
    amounts = []
    for number, unit in matches:
        if unit:
            numeric = float(number.replace(",", "."))
            multiplier = 1000000 if unit.lower() in ("tr", "triệu") else 1000
            numeric *= multiplier
        elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", number):
            numeric = float(re.sub(r"[.,]", "", number))
        else:
            try:
                numeric = float(number.replace(",", "."))
            except ValueError:
                return None, None
        amounts.append(int(numeric) if numeric.is_integer() else numeric)
    minimum, maximum = amounts[0], amounts[-1]
    return (minimum, maximum) if minimum <= maximum else (None, None)


def _looks_price(text: str) -> bool:
    return bool(re.search(r"\d.*(?:₫|VNĐ|VND|đ\b|\btriệu\b|\bnghìn\b|\bngàn\b)", text, re.I)
                or re.search(r"\d+(?:[.,]\d+)?\s*(?:k|tr)\b", text, re.I)) and "http" not in text


def _text_rows(text: str, suffix: str) -> list[tuple[int, list[str], list[str]]]:
    if suffix == ".csv":
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        return [(i, row, []) for i, row in enumerate(csv.reader(io.StringIO(text), dialect, strict=True), 1)]
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            cells = re.split(r"(?<!\\)\|", line.strip("|"))
            cells = [cell.replace("\\|", "|").strip() for cell in cells]
            if cells and all(not cell or re.fullmatch(r":?-+:?", cell) for cell in cells):
                continue
        elif "\t" in line:
            cells = line.split("\t")
        else:
            cells = [re.sub(r"^(?:[-*]\s+|\d+[.)]\s+)", "", line)]
        rows.append((number, cells, []))
    return rows


def _checked_zip(path: Path) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(path)
    if (len(archive.infolist()) > 3000
            or sum(entry.file_size for entry in archive.infolist()) > 100 * 1024 * 1024):
        archive.close()
        raise ValueError("Document ZIP exceeds intake limits.")
    return archive


def _docx_rows(path: Path) -> list[tuple[int, list[str], list[str]]]:
    with _checked_zip(path) as archive:
        if archive.getinfo("word/document.xml").file_size > MAX_INPUT:
            raise ValueError("Word document XML is too large.")
        tree = ET.fromstring(archive.read("word/document.xml"))
        relationships = {}
        if "word/_rels/document.xml.rels" in archive.namelist():
            for rel in ET.fromstring(archive.read("word/_rels/document.xml.rels")):
                if rel.get("Type", "").endswith("/hyperlink"):
                    relationships[rel.get("Id")] = rel.get("Target", "")

    def cell(node):
        text = " ".join(t.text or "" for t in node.iter(W + "t")).strip()
        links = [relationships[link.get(R + "id")] for link in node.iter(W + "hyperlink")
                 if link.get(R + "id") in relationships]
        return text, links

    rows = []
    body = tree.find(W + "body")
    if body is None:
        return rows
    for child in body:
        if child.tag == W + "tbl":
            for row in child.findall(W + "tr"):
                values = [cell(c) for c in row.findall(W + "tc")]
                rows.append((len(rows) + 1, [v[0] for v in values], [u for v in values for u in v[1]]))
        elif child.tag == W + "p":
            text, links = cell(child)
            style = child.find("./" + W + "pPr/" + W + "pStyle")
            if not text or (style is not None and style.get(W + "val", "").lower().startswith(("heading", "title"))):
                continue
            field = re.match(r"^(?:Giá|Khoảng giá|Danh mục|Ngành hàng|Link|URL)\s*:", text, re.I)
            if field and rows:
                rows[-1][1].append(text)
                rows[-1][2].extend(links)
            else:
                rows.append((len(rows) + 1, [re.sub(r"^\d+[.)]\s+", "", text)], links))
    return rows


def _xlsx_rows(path: Path) -> list[tuple[int, list[str], list[str], str]]:
    from openpyxl import load_workbook

    with _checked_zip(path):
        pass
    workbook = load_workbook(path, data_only=False, read_only=False, keep_links=False)
    rows = []
    try:
        for sheet in workbook.worksheets:
            if sheet.max_row > MAX_ROWS or sheet.max_column > 300:
                raise ValueError("Excel sheet exceeds intake row/column limits.")
            for row in sheet.iter_rows():
                cells, links, cell_links = [], [], []
                for item in row:
                    value = "" if item.value is None else str(item.value)
                    m = re.match(r'=HYPERLINK\(\s*(.*?)\s*[,;]\s*"([^"]*)"\s*\)$', value, re.I)
                    item_links = []
                    if m:
                        parts = re.findall(r'"([^"]*)"', m.group(1))
                        if parts:
                            item_links.append("".join(parts))
                        value = m.group(2)
                    elif value.startswith("="):
                        value = "[unsupported formula]"
                    if item.hyperlink and item.hyperlink.target:
                        item_links.append(item.hyperlink.target)
                    cell_links.append(list(dict.fromkeys(item_links)))
                    links.extend(item_links)
                    cells.append(value)
                rows.append((row[0].row, cells, links, sheet.title, cell_links))
    finally:
        workbook.close()
    return rows


def parse_input(path: Path) -> list[dict]:
    if not path.is_file() or path.stat().st_size > MAX_INPUT:
        raise ValueError("Input file is missing or exceeds 20 MiB.")
    suffix = path.suffix.lower()
    try:
        if suffix in (".txt", ".md", ".csv"):
            rows = _text_rows(path.read_text(encoding="utf-8-sig"), suffix)
        elif suffix == ".docx":
            rows = _docx_rows(path)
        elif suffix == ".xlsx":
            rows = _xlsx_rows(path)
        else:
            raise ValueError("Supported input: TXT, Markdown, CSV, XLSX, DOCX.")
    except (csv.Error, ET.ParseError, zipfile.BadZipFile, KeyError) as error:
        raise ValueError(f"Cannot read {suffix} input: {error}") from error
    if len(rows) > MAX_ROWS:
        raise ValueError("Input exceeds 10000 rows.")
    results, headers, seen = [], {}, {}
    previous_sheet = None
    for raw in rows:
        number, cells, hyperlink_targets = raw[:3]
        sheet = raw[3] if len(raw) >= 4 else None
        if sheet != previous_sheet:
            headers = {}
            previous_sheet = sheet
        cells = [str(c).strip() for c in cells]
        while cells and not cells[-1]:
            cells.pop()
        if not any(cells):
            continue
        candidate_headers = {kind: next((i for i, value in enumerate(cells) if _fold(value) in aliases), None)
                             for kind, aliases in HEADER_NAMES.items()}
        if candidate_headers["name"] is not None or (
                candidate_headers["url"] is not None and not hyperlink_targets
                and not any(_urls(c) for c in cells)):
            headers = {kind: i for kind, i in candidate_headers.items() if i is not None}
            continue
        links = list(dict.fromkeys([*hyperlink_targets, *[u for c in cells for u in _urls(c)]]))
        index = headers.get("name", 1 if len(cells) > 1 and cells[0].isdigit() else 0)
        name = cells[index] if index < len(cells) else ""
        name = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", name).strip("*")
        # Link-only rows remain usable without inventing a product identity.
        if not name and links:
            name = links[0]
        if len(cells) == 1 and links and name == links[0]:
            query = parse_qs(urlsplit(links[0]).query).get("keyword", [])
            name = query[0] if query else name
        price_index = headers.get("price")
        price_text = (cells[price_index] if price_index is not None and price_index < len(cells)
                      else next((c for i, c in enumerate(cells) if i != index and _looks_price(c)), ""))
        minimum, maximum = _price_range(price_text) if price_text else (None, None)
        category_index = headers.get("category", index + 1)
        category = cells[category_index] if category_index < len(cells) else ""
        category_field = next((c.split(":", 1)[1].strip() for c in cells
                               if re.match(r"^(?:Danh mục|Ngành hàng)\s*:", c, re.I)), None)
        if category_field is not None:
            category = category_field
        if category == price_text or "http" in category or _looks_price(category):
            category = ""
        parse_warnings = []
        if not name:
            parse_warnings.append("missing_product_name")
        if price_text and minimum is None and re.search(r"\d", price_text):
            parse_warnings.append("ambiguous_price_constraint")
        fingerprint = hashlib.sha256(repr((cells, links)).encode("utf-8")).hexdigest()
        order = len(results) + 1
        row_id = f"row_{order:04d}"
        results.append({"row_id": row_id, "order": order, "source_row": number, "source_sheet": sheet,
                        "name": name, "category": category, "price_text": price_text,
                        "price_min": minimum, "price_max": maximum,
                        "currency": "VND" if price_text and re.search(r"VNĐ|VND|₫|đ\b", price_text, re.I) else None,
                        "links": [{"url": u, "kind": link_kind(u)} for u in links],
                        "raw_cells": cells, "duplicate_of": seen.get(fingerprint),
                        "parse_warnings": parse_warnings,
                        "alternatives": next(([part.strip() for part in group.split("/")]
                                              for group in re.findall(r"\(([^)]+)\)", name) if "/" in group), []),
                        "metadata_verification": "user_supplied_unverified"})
        seen.setdefault(fingerprint, row_id)
    if not results:
        raise ValueError("No product rows found in input.")
    return results
