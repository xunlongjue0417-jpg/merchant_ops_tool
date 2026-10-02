"""Multi-marketplace merchant profit checker.

The application is intentionally report-driven: users upload marketplace
income/settlement and order exports, then the shared profit engine matches
orders to product costs.  Live marketplace authorization, token storage and
inventory writes are not part of this product.
"""
from __future__ import annotations

import base64
import math
try:
    import cgi  # Python <=3.12
except ModuleNotFoundError:  # Python 3.13+
    # cgi.FieldStorage was removed from the standard library.  Keep the small
    # multipart subset needed by the two upload endpoints without adding a
    # third-party web framework.
    from email import policy
    from email.parser import BytesParser
    from types import SimpleNamespace

    class _UploadField:
        def __init__(self, name, filename, value):
            self.name, self.filename, self.value = name, filename, value
            self.file = io.BytesIO(value)

    class _FieldStorage:
        def __init__(self, fp, headers, environ=None):
            length = int(headers.get("Content-Length", "0"))
            body = fp.read(length)
            content_type = headers.get("Content-Type", "")
            message = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
            )
            self._fields = {}
            for part in message.iter_parts():
                disposition = part.get("Content-Disposition", "")
                name = part.get_param("name", header="content-disposition")
                if not name:
                    continue
                payload = part.get_payload(decode=True) or b""
                filename = part.get_filename()
                self._fields[name] = _UploadField(name, filename, payload) if filename else _UploadField(name, None, payload)

        def getfirst(self, name):
            field = self._fields.get(name)
            if not field:
                return None
            return field.filename if field.filename else field.value.decode("utf-8", "replace")

        def __getitem__(self, name):
            return self._fields[name]

    cgi = SimpleNamespace(FieldStorage=_FieldStorage)
import csv
import os
import hashlib
import io
import json
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    import xlsxwriter
except ModuleNotFoundError:
    # The existing Windows launcher includes the Node workbook generator.
    # Render installs XlsxWriter from requirements.txt instead.
    xlsxwriter = None

try:
    from cryptography.fernet import Fernet
except ModuleNotFoundError:
    Fernet = None

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
STATIC = ROOT / "static"
STATE_PATH = DATA / "state.json"
STATE_LOCK = threading.Lock()
ACCESS_PASSWORD = os.environ.get("APP_ACCESS_PASSWORD", "")
LOCAL_NODE = Path(r"C:\Users\Win10\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe")
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_REQUEST_BYTES = 110 * 1024 * 1024


def default_state():
    return {"costs": {}, "product_costs": {}, "platform_costs": {}, "name_replacements": [], "order_overrides": {}}


def state():
    DATA.mkdir(exist_ok=True)
    # The old OAuth database belongs to the retired authorization/inventory
    # feature.  It is intentionally not migrated or backed up: this product
    # is report-driven and must never keep obsolete seller tokens.
    legacy_auth_db = DATA / "tiktok_auth.db"
    if legacy_auth_db.exists():
        try:
            legacy_auth_db.unlink()
        except OSError:
            pass
    if not STATE_PATH.exists():
        save_state(default_state())
    value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    changed = False
    for legacy_key in ("inventory_mappings", "inventory_events", "shops", "shop_tokens", "oauth_states", "sessions"):
        if legacy_key in value:
            value.pop(legacy_key, None)
            changed = True
    value.setdefault("platform_costs", {})
    value.setdefault("order_overrides", {})
    if changed:
        save_state(value)
    return value


def save_state(value):
    DATA.mkdir(exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    temp_path = STATE_PATH.with_suffix(".json.tmp")
    with STATE_LOCK:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, STATE_PATH)


def text(value):
    return "" if value is None else str(value).strip()


def money(value):
    raw = text(value).replace("RM", "").replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def strict_money(value):
    """Parse a finite, non-negative monetary value or reject it."""
    raw = text(value).replace("RM", "").replace(",", "").strip()
    if not raw or not re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)", raw):
        raise ValueError("金额必须是非负数字，例如 12.50。")
    amount = float(raw)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("金额必须是非负数字。")
    return amount


def strict_signed_money(value):
    """Parse a finite monetary amount; settlements may legitimately be negative."""
    raw = text(value).replace("RM", "").replace(",", "").strip()
    if not raw or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)", raw):
        raise ValueError("金额必须是数字，例如 -12.50 或 12.50。")
    amount = float(raw)
    if not math.isfinite(amount):
        raise ValueError("金额必须是有限数字。")
    return amount


def number(value):
    try:
        return int(float(text(value)))
    except ValueError:
        return 0


def normalized_id(value):
    value = text(value).lstrip("'‘’")
    return value[:-2] if value.endswith(".0") and value[:-2].isdigit() else value


def product_key(value):
    return re.sub(r"\s+", " ", text(value).lower()).strip()


def variation_key(value):
    """Stable natural ordering for numeric/word/color/size variations."""
    words = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    sizes = {"2xs": 10, "xs": 20, "s": 30, "m": 40, "l": 50, "xl": 60, "2xl": 70, "3xl": 80, "4xl": 90, "5xl": 100}
    colors = {"cream": 10, "white": 20, "black": 30, "red": 40, "blue": 50, "green": 60, "yellow": 70, "pink": 80, "purple": 90, "brown": 100, "grey": 110, "gray": 110}
    result = []
    for token in re.split(r"[,/|_-]+", text(value).lower()):
        token = token.strip()
        if not token:
            continue
        if token.isdigit():
            result.append((0, int(token)))
        elif token in words:
            result.append((0, words[token]))
        elif token in colors:
            result.append((1, colors[token]))
        elif token in sizes:
            result.append((2, sizes[token]))
        else:
            result.append((3, token))
    ordered = [item for rank in range(4) for item in result if item[0] == rank]
    return tuple(ordered)


def display_product_name(value, app_state):
    """Local presentation rules only; never alter the TikTok source or cost key."""
    result = text(value)
    for rule in app_state.get("name_replacements", []):
        old = text(rule.get("find"))
        if old:
            result = result.replace(old, text(rule.get("replace")))
    return result


def save_cost_entry(entries, amount, effective_from, note, currency="MYR"):
    """Replace same-date entry so a correction actually takes effect."""
    entries[:] = [entry for entry in entries if text(entry.get("effective_from")) != effective_from]
    entries.append({"amount": amount, "effective_from": effective_from, "note": note, "currency": text(currency).upper() or "MYR"})
    entries.sort(key=lambda item: item["effective_from"])


def cost_currency(entry):
    return text(entry.get("currency")) or "MYR"


def column_number(cell_ref):
    letters = re.match(r"([A-Z]+)", cell_ref).group(1)
    result = 0
    for letter in letters:
        result = result * 26 + ord(letter) - 64
    return result - 1


def shared_strings(archive):
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    return ["".join(node.itertext()) for node in root.findall(f"{namespace}si")]


def worksheet_path(archive, desired_name):
    book = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    package_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    rel_map = {item.attrib["Id"]: item.attrib["Target"] for item in rels.findall(f"{package_ns}Relationship")}
    sheets = book.find(f"{ns}sheets").findall(f"{ns}sheet")
    chosen = next((s for s in sheets if s.attrib.get("name") == desired_name), sheets[0])
    target = rel_map[chosen.attrib[f"{rel_ns}id"]].lstrip("/")
    return target if target.startswith("xl/") else f"xl/{target}"


def read_xlsx_rows(path, preferred_sheet):
    """Read XLSX XML directly so malformed export dimensions cannot hide columns."""
    with zipfile.ZipFile(path) as archive:
        strings = shared_strings(archive)
        sheet_xml = archive.read(worksheet_path(archive, preferred_sheet))
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    # Some TikTok/WPS exports write every cell as a separate <row r="N"> node.
    # Merge by the declared row number instead of trusting XML node grouping.
    grouped = defaultdict(dict)
    for row in ET.fromstring(sheet_xml).findall(f".//{ns}row"):
        row_number = int(row.attrib.get("r", "0"))
        cells = grouped[row_number]
        for cell in row.findall(f"{ns}c"):
            index = column_number(cell.attrib["r"])
            cell_type = cell.attrib.get("t")
            value_node = cell.find(f"{ns}v")
            if cell_type == "s" and value_node is not None:
                value = strings[int(value_node.text)]
            elif cell_type == "inlineStr":
                value = "".join(cell.itertext())
            elif value_node is not None:
                value = value_node.text or ""
            else:
                value = ""
            cells[index] = value
    rows = []
    for row_number in sorted(grouped):
        cells = grouped[row_number]
        if not cells:
            continue
        result = [""] * (max(cells) + 1)
        for index, value in cells.items():
            result[index] = value
        rows.append(result)
    return rows


def read_tabular_rows(path, preferred_sheet="Order details"):
    """Read CSV/TSV or XLSX rows for merchant-maintained cost tables."""
    suffix = Path(path).suffix.lower()
    if suffix in (".csv", ".tsv"):
        raw = Path(path).read_bytes()
        content = raw.decode("utf-8-sig")
        sample = content[:4096]
        delimiter = "\t" if suffix == ".tsv" or sample.count("\t") > sample.count(",") else ","
        return list(csv.reader(io.StringIO(content), delimiter=delimiter))
    return read_xlsx_rows(path, preferred_sheet)


def parse_cost_table(path):
    """Parse a flexible merchant cost table into normalized cost records."""
    rows = read_tabular_rows(path, "Costs")
    if not rows:
        raise ValueError("商品成本表为空。")
    header_index = next((i for i, row in enumerate(rows) if any(text(cell) for cell in row)), None)
    headers = [re.sub(r"\s+", " ", text(value).lower()) for value in rows[header_index]]

    aliases = {
        "platform": {"platform", "平台", "marketplace", "channel"},
        "sku": {"sku", "platform sku", "platform_sku", "平台sku", "平台 sku", "sku id", "sku_id"},
        "product_id": {"product id", "product_id", "商品id", "商品 id", "productid"},
        "product_name": {"product name", "product_name", "商品名称", "商品名", "name"},
        "variation": {"variation", "variant", "规格", "变体", "option"},
        "amount": {"unit cost", "unit_cost", "cost", "成本", "单位成本", "cost per unit", "purchase cost", "进货成本"},
        "currency": {"currency", "币种", "货币"},
        "effective_from": {"effective date", "effective_from", "生效日期", "生效日", "cost date"},
        "note": {"note", "备注", "notes"},
    }
    indexes = {}
    for key, names in aliases.items():
        indexes[key] = next((i for i, header in enumerate(headers) if header in names), None)
    if indexes["amount"] is None:
        raise ValueError("商品成本表找不到成本栏位。可使用：单位成本、Unit Cost 或 Cost。")
    if indexes["sku"] is None and indexes["product_name"] is None:
        raise ValueError("商品成本表至少需要 SKU 或商品名称栏位。")

    def value(row, key):
        index = indexes.get(key)
        return text(row[index]) if index is not None and index < len(row) else ""

    records, skipped = [], 0
    for raw in rows[header_index + 1:]:
        if not any(text(value) for value in raw):
            continue
        amount = value(raw, "amount")
        if not amount:
            skipped += 1
            continue
        try:
            amount_value = strict_money(amount)
        except ValueError:
            skipped += 1
            continue
        sku, name = normalized_id(value(raw, "sku")), value(raw, "product_name")
        if not sku and not name:
            skipped += 1
            continue
        currency = value(raw, "currency").upper() or "MYR"
        if not re.fullmatch(r"[A-Z]{3}", currency):
            skipped += 1
            continue
        records.append({
            "platform": value(raw, "platform") or "TikTok",
            "sku": sku,
            "product_id": value(raw, "product_id"),
            "product_name": name,
            "variation": value(raw, "variation"),
            "amount": amount_value,
            "currency": currency,
            "effective_from": value(raw, "effective_from") or "1900/01/01",
            "note": value(raw, "note") or "Imported cost table",
        })
    if not records:
        raise ValueError("商品成本表没有可导入的有效记录。")
    return records, skipped


def table(path, sheet, required_header):
    rows = read_tabular_rows(path, sheet) if Path(path).suffix.lower() in (".csv", ".tsv") else read_xlsx_rows(path, sheet)
    header_index = next((i for i, row in enumerate(rows) if required_header in row), None)
    if header_index is None:
        raise ValueError(f"找不到栏位：{required_header}。请确认上传的是正确的 TikTok 导出表。")
    headers = rows[header_index]
    output = []
    for row in rows[header_index + 1:]:
        row = row + [""] * max(0, len(headers) - len(row))
        item = {headers[i]: row[i] for i in range(len(headers)) if headers[i]}
        # TikTok's All Orders export has a second explanatory header row.
        if text(item.get(required_header)).lower().startswith("platform unique"):
            continue
        if any(text(value) for value in item.values()):
            output.append(item)
    return output


FIELD_ALIASES = {
    "order_id": ("Order ID", "OrderId", "OrderID", "order-id", "order_sn", "order number", "OrderNumber", "Order/Adjustment ID", "Related order ID"),
    "order_item_id": ("Order line item ID", "OrderLineItemID", "Order Item ID", "order-item-id", "order_item_id"),
    "product_id": ("Product ID", "ProductId", "product-id", "ItemID", "ASIN", "asin"),
    "product_name": ("Product Name", "ProductName", "item_name", "product-name", "Item title", "Title", "item-name", "ProductName"),
    "variation": ("Variation", "model_name", "Model", "variant", "variation_name", "Item variation"),
    "sku": ("SKU ID", "SKU", "item_sku", "model_sku", "seller-sku", "SellerSKU", "seller_sku", "merchant-sku"),
    "quantity": ("Quantity", "quantity", "QuantityOrdered", "quantity-ordered", "Qty", "Units"),
    "return_quantity": ("Sku Quantity of return", "return_quantity", "Return Quantity", "refund-quantity"),
    "order_date": ("Order created time", "Created Time", "Order Creation Date", "order_date", "Sale date", "purchase-date", "CreatedAt", "created_at"),
    "settlement": ("Total settlement amount", "Total Released Amount (RM)", "Total Released Amount", "netPayout", "Net Payout", "Net amount", "net_amount", "Amount", "amount", "total-amount", "payout_amount"),
    "transaction_type": ("Transaction type", "Transaction Type", "View By", "transaction-type", "Type", "type"),
    "detail_items": ("Details of items sold", "details_of_items_sold", "Items", "item-details"),
    "currency": ("Currency", "Currency Code", "currency-code", "Settlement Currency", "币种", "货币"),
    "status": ("Order Status", "Order status", "order_status", "Status", "status", "Order Substatus", "Return Status", "Refund Status"),
    "refund": ("Refund Amount", "refund_amount", "Refund amount", "refund-amount"),
}


def rows_as_dicts(path, preferred_sheet="Order details"):
    rows = read_tabular_rows(path, preferred_sheet)
    if not rows:
        return [], []
    known = {product_key(alias) for values in FIELD_ALIASES.values() for alias in values}
    header_index = next((i for i, row in enumerate(rows) if sum(product_key(cell) in known for cell in row) >= 1), 0)
    headers = [text(value) for value in rows[header_index]]
    output = []
    for row in rows[header_index + 1:]:
        row = row + [""] * max(0, len(headers) - len(row))
        item = {headers[i]: row[i] for i in range(len(headers)) if headers[i]}
        if any(text(value) for value in item.values()):
            output.append(item)
    return output, headers


def detect_platform(income_path, orders_path):
    """Infer the report family from distinctive headers, without guessing silently."""
    _, income_headers = rows_as_dicts(income_path, "Income")
    _, order_headers = rows_as_dicts(orders_path, "Orders")
    income = {product_key(header) for header in income_headers}
    orders = {product_key(header) for header in order_headers}
    if {product_key("Order/Adjustment ID"), product_key("Total settlement amount")} <= income:
        return "tiktok"
    signatures = {
        "shopee": ({"order_sn", "order_id"}, {"order_item_id"}, {"net_income", "net_payout", "amount"}),
        "lazada": ({"order_id"}, {"seller_sku", "product_name"}, {"amount", "net_amount", "payout_amount"}),
        "ebay": ({"order_id"}, {"item_id", "title"}, {"amount", "net_amount"}),
        "amazon": ({"order_id"}, {"asin", "product_name"}, {"amount", "net_amount"}),
    }
    candidates = []
    for platform, (income_markers, order_markers, amount_markers) in signatures.items():
        score = len(income & income_markers) + len(orders & order_markers) + len(income & amount_markers)
        if score >= 2:
            candidates.append((score, platform))
    if len(candidates) == 1:
        return candidates[0][1]
    if candidates:
        candidates.sort(reverse=True)
        if len(candidates) == 1 or candidates[0][0] > candidates[1][0]:
            return candidates[0][1]
    raise ValueError("无法可靠识别上传文件的平台报表格式。请确认上传的是同一平台的结算表和订单明细表。")


def alias_value(row, key):
    candidates = {product_key(name): name for name in row}
    for alias in FIELD_ALIASES.get(key, ()):
        actual = candidates.get(product_key(alias))
        if actual is not None and text(row.get(actual)):
            return text(row.get(actual))
    return ""


def normalized_platform_reports(platform, income_path, orders_path):
    """Translate non-TikTok exports to the shared order/settlement model."""
    income_rows, income_headers = rows_as_dicts(income_path, "Income")
    order_rows, order_headers = rows_as_dicts(orders_path, "Orders")
    normalized_income = []
    for row in income_rows:
        order_id = normalized_id(alias_value(row, "order_id"))
        if not order_id:
            continue
        transaction = alias_value(row, "transaction_type") or "Order"
        if platform == "shopee" and transaction.lower() in ("order", "sku"):
            transaction = "Order"
        amount = alias_value(row, "settlement")
        detail = alias_value(row, "detail_items")
        qty = alias_value(row, "quantity")
        if not detail and qty:
            detail = f"item * {qty}"
        normalized_income.append({
            "Related order ID": order_id,
            "Order/Adjustment ID": order_id,
            "Total settlement amount": amount,
            "Transaction type": transaction,
            "Details of items sold": detail,
            "Currency": alias_value(row, "currency") or ("MYR" if platform in ("shopee", "lazada") else "USD"),
            "_raw": row,
        })
    normalized_orders = []
    for row in order_rows:
        order_id = normalized_id(alias_value(row, "order_id"))
        if not order_id:
            continue
        normalized_orders.append({
            "Order ID": order_id,
            "Order Item ID": alias_value(row, "order_item_id"),
            "Product ID": alias_value(row, "product_id"),
            "Product Name": alias_value(row, "product_name"),
            "Variation": alias_value(row, "variation"),
            "SKU ID": normalized_id(alias_value(row, "sku")),
            "Quantity": alias_value(row, "quantity"),
            "Sku Quantity of return": alias_value(row, "return_quantity"),
            "Order created time": alias_value(row, "order_date"),
            "Order Status": alias_value(row, "status"),
            "_raw": row,
        })
    return normalized_income, normalized_orders, income_headers, order_headers


SYSTEM_COLUMNS = {"商品名称", "Variation", "实际数量", "成本", "商品成本", "其他", "总成本", "纯利润", "核对状态"}


def income_table(path):
    """Load a TikTok export, removing columns from an older tool export."""
    rows = read_tabular_rows(path, "Order details") if Path(path).suffix.lower() in (".csv", ".tsv") else read_xlsx_rows(path, "Order details")
    header_index = next((i for i, row in enumerate(rows) if "Order/Adjustment ID" in row), None)
    if header_index is None:
        raise ValueError("找不到栏位：Order/Adjustment ID。请确认上传的是正确的 TikTok 导出表。")
    raw_headers = rows[header_index]
    keep = [index for index, header in enumerate(raw_headers) if header and header not in SYSTEM_COLUMNS]
    headers = [raw_headers[index] for index in keep]
    output = []
    for raw in rows[header_index + 1:]:
        item = {headers[pos]: (raw[index] if index < len(raw) else "") for pos, index in enumerate(keep)}
        if any(text(value) for value in item.values()):
            output.append(item)
    return output


def detect_currency(rows):
    """Detect a single ISO currency from explicit report columns."""
    candidate_names = {
        "currency", "currency code", "settlement currency", "payout currency",
        "currency_code", "币种", "货币", "结算币种", "金额币种",
    }
    values = []
    for row in rows:
        for key, value in row.items():
            key_text = text(key).upper()
            if "(RM)" in key_text or "MYR" in key_text:
                values.append("MYR")
            elif "(USD)" in key_text or "USD" in key_text:
                values.append("USD")
            if product_key(key) in candidate_names:
                raw = text(value).upper()
                if re.fullmatch(r"[A-Z]{3}", raw):
                    values.append(raw)
                elif raw in ("RM", "RM$", "MYR"):
                    values.append("MYR")
    unique = sorted(set(values))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        raise ValueError(f"报表包含多个币种：{', '.join(unique)}。请分开分析。")
    return "MYR"


def detail_quantity(value):
    raw = text(value)
    matches = re.findall(r"(?:\*|x|×|qty\s*[:：]?|quantity\s*[:：]?|数量\s*[:：]?)\s*(\d+)", raw, flags=re.IGNORECASE)
    if matches:
        return sum(number(qty) for qty in matches)
    return number(raw) if re.fullmatch(r"\d+(?:\.0+)?", raw) else 0


def date_key(value):
    raw = text(value)
    if re.fullmatch(r"\d+(?:\.0+)?", raw):
        try:
            serial = int(float(raw))
            # Excel's default 1900 date system, including its historical leap-year bug.
            return datetime(1899, 12, 30).date().fromordinal(datetime(1899, 12, 30).date().toordinal() + serial)
        except (ValueError, OverflowError):
            pass
    iso = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso).date()
    except ValueError:
        pass
    for pattern in ("%Y/%m/%d", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw[:19], pattern).date()
        except ValueError:
            continue
    for pattern in ("%Y/%m/%d", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            parts = re.split(r"[/\-]", raw[:10])
            if len(parts) == 3:
                if len(parts[0]) == 4:
                    return datetime(int(parts[0]), int(parts[1]), int(parts[2])).date()
                return datetime(int(parts[2]), int(parts[1]), int(parts[0])).date()
        except (ValueError, IndexError):
            continue
    return None


def choose_cost(cost_entries, order_date, currency="MYR"):
    order_day = date_key(order_date)
    if order_day is None:
        return None
    applicable = [
        entry for entry in cost_entries
        if cost_currency(entry).upper() == currency.upper()
        and date_key(entry.get("effective_from")) is not None
        and date_key(entry.get("effective_from")) <= order_day
    ]
    if not applicable:
        return None
    return max(applicable, key=lambda entry: date_key(entry.get("effective_from")) or datetime.min.date())


def joined_line_text(lines):
    """Use lifecycle columns only, never product-name keywords, for order statuses."""
    fields = ("Order Status", "Order Substatus", "Cancellation/Return Type", "Cancellation / Return Type")
    return " ".join(text(line.get(field)).lower() for line in lines for field in fields)


def order_lifecycle(lines):
    statuses = joined_line_text(lines)
    gross = sum(max(0, number(line.get("Quantity"))) for line in lines)
    returned = sum(max(0, number(line.get("Sku Quantity of return"))) for line in lines)
    # A cancelled order often records its quantity as returned too. Status is
    # authoritative: cancellation happens before delivery, not after a return.
    has_cancel = any(word in statuses for word in ("cancelled", "canceled", "cancel", "取消"))
    has_return = not has_cancel and (returned > 0 or any(word in statuses for word in ("return", "refund", "退款", "退货")))
    return {"cancelled": has_cancel, "returned": has_return, "gross_units": gross, "returned_units": returned}


def is_refund_transaction(types):
    return any(any(word in text(item).lower() for word in ("refund", "return", "chargeback", "reversal", "adjustment", "退款", "退货", "拒付", "冲正")) for item in types)


def manual_override(app_state, order_id):
    if not app_state.get("active_report_id") or app_state.get("override_report_id") != app_state.get("active_report_id"):
        return {}
    return app_state.get("order_overrides", {}).get(order_id, {})


def analyse(income_path, orders_path, app_state, platform="tiktok"):
    if product_key(platform) in ("", "tiktok", "tiktok shop"):
        income = income_table(income_path)
        orders = table(orders_path, "OrderSKUList", "Order ID")
    else:
        income, orders, _, _ = normalized_platform_reports(product_key(platform), income_path, orders_path)
    report_currency = detect_currency(income)

    source_columns = [key for key in (list(income[0].keys()) if income else []) if not key.startswith("_")]
    settlements = defaultdict(lambda: {"net": 0.0, "details_units": 0, "types": set(), "rows": 0, "source": defaultdict(list)})
    for row in income:
        transaction_type = text(row.get("Transaction type"))
        key = normalized_id(row.get("Related order ID") or row.get("Order/Adjustment ID"))
        if not key:
            continue
        bucket = settlements[key]
        bucket["net"] += money(row.get("Total settlement amount"))
        bucket["details_units"] += detail_quantity(row.get("Details of items sold"))
        bucket["types"].add(transaction_type or "Unknown")
        bucket["rows"] += 1
        for field in source_columns:
            value = text(row.get(field))
            if value and value not in bucket["source"][field]:
                bucket["source"][field].append(value)

    by_order = defaultdict(list)
    for row in orders:
        key = normalized_id(row.get("Order ID"))
        if key:
            by_order[key].append(row)

    app_state.setdefault("product_costs", {})
    report, missing, catalog = [], {}, {}
    for order_id, settlement in settlements.items():
        order_lines = by_order.get(order_id, [])
        logistics_only = settlement["types"] and all(text(kind).lower() == "logistics reimbursement" for kind in settlement["types"])
        product_names = []
        variations = []
        for line in order_lines:
            name, variation = text(line.get("Product Name")), text(line.get("Variation"))
            shown_name = display_product_name(name, app_state)
            if shown_name and shown_name not in product_names:
                product_names.append(shown_name)
            if variation and variation not in variations:
                variations.append(variation)
        presentation = {
            "product_name": "\n".join(product_names),
            "variation": "\n".join(variations),
            "source": {field: "\n".join(values) for field, values in settlement["source"].items()},
        }
        if logistics_only:
            report.append({
                "order_id": order_id, "net_settlement": round(settlement["net"], 2), "actual_units": 0,
                "details_units": 0, "cost": 0.0, "other_cost": 0.0, "other_cost_set": False, "total_cost": 0.0, "profit": round(settlement["net"], 2),
                "status": "物流补偿，已计入", "status_code": "logistics_reimbursement",
                "flags": "物流补偿不扣商品成本", "transaction_types": ", ".join(sorted(settlement["types"])),
                "editable": True, **presentation,
            })
            continue
        flags = []
        lifecycle = order_lifecycle(order_lines)
        override = manual_override(app_state, order_id)
        net_settlement = money(override["net_settlement"]) if "net_settlement" in override else settlement["net"]
        override_cost = money(override["cost"]) if "cost" in override else None
        other_cost = money(override.get("other_cost", 0))
        other_cost_set = "other_cost" in override
        if lifecycle["cancelled"]:
            report.append({
                "order_id": order_id, "net_settlement": round(net_settlement, 2), "actual_units": 0,
                "details_units": settlement["details_units"], "cost": 0.0, "other_cost": 0.0, "other_cost_set": False, "total_cost": 0.0, "profit": None,
                "status": "已取消", "status_code": "cancelled", "flags": "未结算，不计成本或利润", "transaction_types": ", ".join(sorted(settlement["types"])),
                "editable": False, **presentation,
            })
            continue
        if not order_lines and override_cost is None:
            flags.append("找不到订单商品明细；可手动填总成本")
        actual_units = 0
        cost_total = 0.0
        for line in order_lines:
            sku_id = normalized_id(line.get("SKU ID"))
            name = text(line.get("Product Name"))
            quantity = max(0, number(line.get("Quantity")) - number(line.get("Sku Quantity of return")))
            actual_units += quantity
            # Income exports name this field "Order created time"; All Orders
            # exports call the same business date "Created Time".
            order_date = text(line.get("Order created time") or line.get("Created Time"))
            if not date_key(order_date):
                flags.append("缺订单日期，无法选择历史成本")
            cost = choose_cost(app_state["costs"].get(sku_id, []), order_date, report_currency)
            cost = cost or choose_cost(app_state["product_costs"].get(product_key(name), []), order_date, report_currency)
            catalog_key = "|".join((sku_id, product_key(name), product_key(line.get("Variation"))))
            catalog_item = catalog.setdefault(catalog_key, {"sku_id": sku_id, "product_name": name, "variation": text(line.get("Variation")), "quantity": 0, "amount": ""})
            catalog_item["quantity"] += quantity
            if cost is not None:
                catalog_item["amount"] = round(money(cost.get("amount")), 2)
            if quantity and cost is None:
                missing[sku_id] = {
                    "sku_id": sku_id,
                    "product_name": name,
                    "variation": text(line.get("Variation")),
                    "quantity": 0,
                }
                missing[sku_id]["quantity"] += quantity
                available = app_state["costs"].get(sku_id, []) or app_state["product_costs"].get(product_key(name), [])
                if available and not any(cost_currency(entry).upper() == report_currency.upper() for entry in available):
                    flags.append(f"成本币种不匹配：{sku_id}")
                elif not date_key(order_date):
                    flags.append(f"缺成本日期：{sku_id}")
                else:
                    flags.append(f"缺成本：{sku_id}")
            elif cost:
                cost_total += quantity * money(cost.get("amount"))
        # Full returns have zero net units while Income may list the original
        # item. That is expected and must not hide the refund loss.
        if settlement["details_units"] and actual_units != settlement["details_units"] and not (lifecycle["returned"] and actual_units == 0):
            flags.append(f"数量不一致：订单 {actual_units} 件，结算详情 {settlement['details_units']} 件")
        if override_cost is not None:
            cost_total = override_cost
            # A merchant-entered product cost is the final confirmation for a
            # special order (for example a gift bundle). Do not keep it in
            # Needs review merely because automatic line matching is absent.
            flags = []
        refund_only = lifecycle["returned"] and not any(text(kind).lower().startswith("order") for kind in settlement["types"])
        if refund_only and override_cost is None:
            # The original sale was settled in an earlier report. Its COGS was
            # already recognised then; this report must not deduct it again.
            cost_total = 0.0
            flags = [flag for flag in flags if not flag.startswith("缺成本")]
        total_cost = round(cost_total + other_cost, 2)
        profit_value = round(net_settlement - total_cost, 2) if not flags else None
        if lifecycle["returned"]:
            refund_label = "已退货退款" if actual_units == 0 else f"部分退货退款（净售 {actual_units} 件）"
        elif is_refund_transaction(settlement["types"]):
            refund_label = "退款结算影响（请核对商品）"
        else:
            refund_label = ""
        status = "需核对" if flags else (refund_label or "可确认")
        status_code = "needs_review" if flags else ("returned" if lifecycle["returned"] else ("refund_adjustment" if is_refund_transaction(settlement["types"]) else "confirmed"))
        report.append({
            "order_id": order_id,
            "net_settlement": round(net_settlement, 2),
            "actual_units": actual_units,
            "details_units": settlement["details_units"],
            "cost": round(cost_total, 2),
            "other_cost": round(other_cost, 2),
            "other_cost_set": other_cost_set,
            "total_cost": total_cost,
            "profit": profit_value,
            "status": status,
            "status_code": status_code,
            "flags": "；".join(dict.fromkeys(flags)),
            "transaction_types": ", ".join(sorted(settlement["types"])),
            "editable": not lifecycle["cancelled"], **presentation,
        })
    report.sort(key=lambda row: (row["status"] != "需核对", row["order_id"]), reverse=True)
    confirmed = [row for row in report if row["profit"] is not None]
    return {
        "summary": {
            "settlement_total": round(sum(row["net_settlement"] for row in report), 2),
            "confirmed_profit": round(sum(row["profit"] for row in confirmed), 2),
            "confirmed_cost": round(sum(row.get("total_cost", row["cost"]) for row in confirmed), 2),
            "orders": len(report),
            "needs_review": sum(row["status"] == "需核对" for row in report),
            "currency": report_currency,
        },
        "orders": report,
        "source_columns": source_columns,
        "cost_catalog": sorted(catalog.values(), key=lambda item: (product_key(item["product_name"]), variation_key(item["variation"]))),
        "missing_costs": sorted(missing.values(), key=lambda item: item["product_name"]),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def write_csv(report):
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / "latest_profit_report.csv"
    columns = ["商品名称", "Variation", "订单", "币种", "到账", "订单件数", "结算件数", "商品成本", "其他", "总成本", "纯利润", "状态", "核对说明"] + report.get("source_columns", [])
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        for item in report["orders"]:
            row = {
                "商品名称": item.get("product_name", ""), "Variation": item.get("variation", ""),
                "订单": item["order_id"], "币种": report.get("summary", {}).get("currency", ""), "到账": item["net_settlement"], "订单件数": item["actual_units"],
                "结算件数": item["details_units"], "商品成本": item["cost"], "其他": item.get("other_cost", 0), "总成本": item.get("total_cost", item["cost"]),
                "纯利润": "" if item["profit"] is None else item["profit"],
                "状态": item["status"], "核对说明": item["flags"],
            }
            row.update(item.get("source", {}))
            writer.writerow(row)
    return path


def write_generic_augmented_xlsx(income_path, orders_path, app_state, platform):
    if xlsxwriter is None:
        raise RuntimeError("服务器缺少 Excel 导出组件。")
    income, orders, income_headers, _ = normalized_platform_reports(product_key(platform), income_path, orders_path)
    result = analyse(income_path, orders_path, app_state, platform)
    by_order = {item["order_id"]: item for item in result["orders"]}
    headers = [header for header in income_headers if header] + ["商品名称", "Variation", "SKU", "实际数量", "商品成本", "其他", "总成本", "纯利润", "核对状态"]
    output = REPORTS / f"{platform.title()}_Income_with_Profit.xlsx"
    workbook = xlsxwriter.Workbook(output)
    sheet = workbook.add_worksheet("Income with Profit")
    header_format = workbook.add_format({"bg_color": "#4A37B8", "font_color": "#FFFFFF", "bold": True, "text_wrap": True, "border": 1})
    cell_format = workbook.add_format({"text_wrap": True, "border": 1})
    number_format = workbook.add_format({"num_format": "0.00", "border": 1})
    for col, header in enumerate(headers):
        sheet.write(0, col, header, header_format)
    normalized_by_order = defaultdict(list)
    for line in orders:
        normalized_by_order[line["Order ID"]].append(line)
    for index, source in enumerate(income, start=1):
        order_id = source["Related order ID"]
        item = by_order.get(order_id, {})
        lines = normalized_by_order.get(order_id, [])
        names = "\n".join(dict.fromkeys(text(line.get("Product Name")) for line in lines if text(line.get("Product Name"))))
        variations = "\n".join(dict.fromkeys(text(line.get("Variation")) for line in lines if text(line.get("Variation"))))
        skus = "\n".join(dict.fromkeys(text(line.get("SKU ID")) for line in lines if text(line.get("SKU ID"))))
        values = [source.get(header, source.get("_raw", {}).get(header, "")) for header in income_headers]
        values += [names, variations, skus, item.get("actual_units", ""), item.get("cost", ""), item.get("other_cost", 0), item.get("total_cost", item.get("cost", "")), item.get("profit", ""), item.get("status", "")]
        for col, value in enumerate(values):
            if headers[col] in ("商品成本", "其他", "总成本", "纯利润") and value not in ("", None):
                sheet.write_number(index, col, float(value), number_format)
            else:
                sheet.write(index, col, value, cell_format)
    sheet.freeze_panes(1, 0)
    sheet.set_column(0, len(headers) - 1, 18)
    workbook.close()
    return output


def write_augmented_xlsx(income_path, orders_path, app_state, platform="tiktok"):
    if product_key(platform) not in ("", "tiktok", "tiktok shop"):
        return write_generic_augmented_xlsx(income_path, orders_path, app_state, platform)
    income = income_table(income_path)
    orders = table(orders_path, "OrderSKUList", "Order ID")
    report_currency = detect_currency(income)
    original_headers = list(income[0].keys())
    # TikTok changes report columns between exports/regions. When the
    # optional Total Revenue column is absent, append calculated columns.
    insert_at = original_headers.index("Total Revenue") + 1 if "Total Revenue" in original_headers else len(original_headers)
    added = ["商品名称", "Variation", "实际数量", "商品成本", "其他", "总成本", "纯利润", "核对状态"]
    headers = original_headers[:insert_at] + added + original_headers[insert_at:]
    by_order = defaultdict(list)
    for line in orders:
        by_order[normalized_id(line.get("Order ID"))].append(line)
    totals = defaultdict(lambda: {"net": 0.0, "types": set(), "details": 0})
    for source in income:
        order_id = normalized_id(source.get("Related order ID") or source.get("Order/Adjustment ID"))
        totals[order_id]["net"] += money(source.get("Total settlement amount"))
        totals[order_id]["types"].add(text(source.get("Transaction type")))
        totals[order_id]["details"] += detail_quantity(source.get("Details of items sold"))
    rows, seen, profit_net_overrides = [], set(), {}
    for source in income:
        order_id = normalized_id(source.get("Related order ID") or source.get("Order/Adjustment ID"))
        transaction = text(source.get("Transaction type")).lower()
        names, variations, quantity, cost_total, flags = [], [], 0, 0.0, []
        total_cost = ""
        lifecycle = order_lifecycle(by_order.get(order_id, []))
        override = manual_override(app_state, order_id)
        net_settlement = money(override["net_settlement"]) if "net_settlement" in override else totals[order_id]["net"]
        other = money(override.get("other_cost", 0))
        if transaction == "logistics reimbursement":
            status = "物流补偿，已计入"
            cost = 0.0
            total_cost = other
            profit = round(net_settlement - total_cost, 2)
        elif order_id in seen:
            status = "同订单调整行；成本已在首次订单行计算"
            cost = other = total_cost = profit = ""
        elif lifecycle["cancelled"]:
            seen.add(order_id)
            status = "已取消"
            cost = other = total_cost = profit = ""
        elif not by_order.get(order_id):
            seen.add(order_id)
            cost = money(override["cost"]) if "cost" in override else ""
            total_cost = round(cost + other, 2) if isinstance(cost, float) else ""
            profit = round(net_settlement - total_cost, 2) if isinstance(total_cost, float) else ""
            status = "退款/订单缺商品明细；请在网页手动填总成本" if cost == "" else "手动成本已用于计算"
        else:
            seen.add(order_id)
            missing = False
            for line in by_order[order_id]:
                units = max(0, number(line.get("Quantity")) - number(line.get("Sku Quantity of return")))
                if not units:
                    continue
                name, variation = text(line.get("Product Name")), text(line.get("Variation"))
                names.append(f"{display_product_name(name, app_state)} ×{units}")
                variations.append(f"{variation} ×{units}")
                quantity += units
                date = text(line.get("Order created time") or line.get("Created Time"))
                item_cost = choose_cost(app_state.get("costs", {}).get(normalized_id(line.get("SKU ID")), []), date, report_currency)
                item_cost = item_cost or choose_cost(app_state.get("product_costs", {}).get(product_key(name), []), date, report_currency)
                if item_cost is None:
                    missing = True
                else:
                    cost_total += units * money(item_cost.get("amount"))
            if "cost" in override:
                cost_total = money(override["cost"])
                missing = False
                flags = []
            if missing:
                flags.append("缺成本")
            details = totals[order_id]["details"]
            if details and details != quantity and not (lifecycle["returned"] and quantity == 0):
                flags.append(f"数量不一致：订单 {quantity} 件，结算详情 {details} 件")
            refund_only = lifecycle["returned"] and not any(text(kind).lower().startswith("order") for kind in totals[order_id]["types"])
            if refund_only and "cost" not in override:
                cost_total = 0.0
                flags = [flag for flag in flags if flag != "缺成本"]
            cost = "" if missing else round(cost_total, 2)
            total_cost = round(cost_total + other, 2)
            profit_value = None if flags else round(net_settlement - total_cost, 2)
            profit = "" if profit_value is None else profit_value
            if flags:
                status = "；".join(flags)
            elif lifecycle["returned"]:
                status = "已退货退款" if quantity == 0 else f"部分退货退款（净售 {quantity} 件）"
            elif is_refund_transaction(totals[order_id]["types"]):
                status = "退款结算影响"
            else:
                status = "可确认"
        base = [
            normalized_id(source.get(header, "")) if header in {"Order/Adjustment ID", "Related order ID"} else source.get(header, "")
            for header in original_headers
        ]
        if "net_settlement" in override and profit != "" and status != "已取消":
            status = f"{status}；手动到账已用于利润"
        if total_cost == "":
            total_cost = "" if cost == "" else round(money(cost) + money(other), 2)
        extra = ["\n".join(names), "\n".join(variations), quantity if names else "", cost, other, total_cost, profit, status]
        rows.append(base[:insert_at] + extra + base[insert_at:])
        if "net_settlement" in override and profit != "":
            profit_net_overrides[str(len(rows) - 1)] = net_settlement
    REPORTS.mkdir(exist_ok=True)
    output = REPORTS / "TikTok_Income_with_Profit.xlsx"
    if xlsxwriter is None:
        if not LOCAL_NODE.exists():
            raise RuntimeError("缺少 Excel 导出组件。请重新运行启动工具，或在服务器安装 requirements.txt。")
        payload = REPORTS / "augmented_payload.json"
        payload.write_text(json.dumps({"headers": headers, "rows": rows, "profitNetOverrides": profit_net_overrides}, ensure_ascii=False), encoding="utf-8")
        subprocess.run([str(LOCAL_NODE), str(ROOT / "build_augmented_income.mjs"), str(payload), str(output)], check=True, capture_output=True, text=True)
        return output
    workbook = xlsxwriter.Workbook(output)
    worksheet = workbook.add_worksheet("Order details")
    worksheet.hide_gridlines(2)
    worksheet.freeze_panes(1, 0)

    header_format = workbook.add_format({"bg_color": "#4A37B8", "font_color": "#FFFFFF", "bold": True, "font_name": "Arial", "align": "center", "valign": "vcenter", "text_wrap": True, "border": 1, "border_color": "#D9D9E6"})
    detail_header_format = workbook.add_format({"bg_color": "#176B87", "font_color": "#FFFFFF", "bold": True, "font_name": "Arial", "align": "center", "valign": "vcenter", "text_wrap": True, "border": 1, "border_color": "#D9D9E6"})
    cell_format = workbook.add_format({"font_name": "Arial", "font_size": 10, "valign": "vcenter", "text_wrap": True, "border": 1, "border_color": "#D9D9E6"})
    number_format = workbook.add_format({"font_name": "Arial", "font_size": 10, "valign": "vcenter", "text_wrap": True, "border": 1, "border_color": "#D9D9E6", "num_format": "0.00"})
    text_id_format = workbook.add_format({"font_name": "Arial", "font_size": 10, "valign": "vcenter", "text_wrap": True, "border": 1, "border_color": "#D9D9E6", "num_format": "@"})
    total_format = workbook.add_format({"bg_color": "#EEF2FF", "font_color": "#172554", "bold": True, "font_name": "Arial", "valign": "vcenter", "border": 1, "border_color": "#D9D9E6", "num_format": "0.00"})
    total_label_format = workbook.add_format({"bg_color": "#EEF2FF", "font_color": "#172554", "bold": True, "font_name": "Arial", "valign": "vcenter", "border": 1, "border_color": "#D9D9E6"})

    start = headers.index("商品名称")
    for index, header in enumerate(headers):
        worksheet.write(0, index, header, detail_header_format if start <= index < start + 6 else header_format)
    numeric_headers = {"Total settlement amount", "Total Revenue", "商品成本", "其他", "总成本", "纯利润"}
    for row_index, row in enumerate(rows, start=1):
        for column_index, value in enumerate(row):
            header = headers[column_index]
            if header == "Order/Adjustment ID":
                worksheet.write_string(row_index, column_index, str(value or ""), text_id_format)
            elif header in numeric_headers and value != "":
                try:
                    worksheet.write_number(row_index, column_index, float(str(value).replace(",", "")), number_format)
                except ValueError:
                    worksheet.write(row_index, column_index, value, cell_format)
            else:
                worksheet.write(row_index, column_index, value, cell_format)

    def excel_column(index):
        result = ""
        number = index + 1
        while number:
            number, remainder = divmod(number - 1, 26)
            result = chr(65 + remainder) + result
        return result

    order_index = headers.index("Order/Adjustment ID")
    settlement_index = headers.index("Total settlement amount")
    cost_index = headers.index("商品成本")
    other_index = headers.index("其他")
    total_cost_index = headers.index("总成本")
    profit_index = headers.index("纯利润")
    status_index = headers.index("核对状态")
    for row_index, row in enumerate(rows, start=1):
        status = str(row[status_index] or "")
        profit_value = row[profit_index]
        if profit_value == "" or not any(status.startswith(label) for label in ("可确认", "已退货退款", "部分退货退款", "退款结算影响", "物流补偿", "手动成本已用于计算")):
            continue
        excel_row = row_index + 1
        override_net = profit_net_overrides.get(str(row_index - 1))
        if override_net is not None:
            net_expression = str(float(override_net))
        else:
            net_expression = f'SUMIF(${excel_column(order_index)}$2:${excel_column(order_index)}${len(rows) + 1},{excel_column(order_index)}{excel_row},${excel_column(settlement_index)}$2:${excel_column(settlement_index)}${len(rows) + 1})'
        worksheet.write_formula(row_index, total_cost_index, f"={excel_column(cost_index)}{excel_row}+{excel_column(other_index)}{excel_row}", number_format)
        worksheet.write_formula(row_index, profit_index, f"={net_expression}-{excel_column(total_cost_index)}{excel_row}", number_format)

    summary_row = len(rows) + 2
    worksheet.write(summary_row, order_index, "总计", total_label_format)
    revenue_index = headers.index("Total Revenue") if "Total Revenue" in headers else None
    summary_indexes = [settlement_index, cost_index, profit_index]
    if revenue_index is not None:
        summary_indexes.insert(1, revenue_index)
    for index in summary_indexes:
        column = excel_column(index)
        worksheet.write_formula(summary_row, index, f"=SUM({column}2:{column}{len(rows) + 1})", total_format)

    worksheet.set_column(0, 0, 23, text_id_format)
    worksheet.set_column(start, start, 34)
    worksheet.set_column(start + 1, start + 1, 20)
    worksheet.set_column(start + 2, start + 4, 14)
    worksheet.set_column(start + 5, start + 5, 32)
    workbook.close()
    return output


def refresh_current_report(app_state):
    """Rebuild the currently open report after a cost change, if one exists."""
    income_path = next((path for path in DATA.glob("latest_income.*") if path.suffix.lower() in (".xlsx", ".csv", ".tsv")), DATA / "latest_income.xlsx")
    orders_path = next((path for path in DATA.glob("latest_orders.*") if path.suffix.lower() in (".xlsx", ".csv", ".tsv")), DATA / "latest_orders.xlsx")
    if not income_path.exists() or not orders_path.exists():
        return None
    platform = app_state.get("active_platform", "tiktok")
    result = analyse(income_path, orders_path, app_state, platform)
    write_augmented_xlsx(income_path, orders_path, app_state, platform)
    write_csv(result)
    return result


def latest_report_paths():
    income_path = next((path for path in DATA.glob("latest_income.*") if path.suffix.lower() in (".xlsx", ".csv", ".tsv")), DATA / "latest_income.xlsx")
    orders_path = next((path for path in DATA.glob("latest_orders.*") if path.suffix.lower() in (".xlsx", ".csv", ".tsv")), DATA / "latest_orders.xlsx")
    return income_path, orders_path


def cost_catalog(app_state):
    """All products in the latest All Orders file, including already-priced ones."""
    income_path, path = latest_report_paths()
    if not path.exists() or not income_path.exists():
        return []
    active_platform = product_key(app_state.get("active_platform", "tiktok"))
    if active_platform not in ("", "tiktok", "tiktok shop"):
        try:
            generic_income, generic_orders, _, _ = normalized_platform_reports(active_platform, income_path, path)
        except Exception:
            return []
        income_ids = {normalized_id(row.get("Order ID") or row.get("Related order ID")) for row in generic_income}
        lines = generic_orders
        grouped = {}
        platform_costs = app_state.get("platform_costs", {}).get(active_platform, {})
        for line in lines:
            if normalized_id(line.get("Order ID")) not in income_ids:
                continue
            name = text(line.get("Product Name"))
            sku_id = normalized_id(line.get("SKU ID"))
            if not name and not sku_id:
                continue
            group_key = product_key(name or sku_id)
            group = grouped.setdefault(group_key, {"product_name": display_product_name(name or sku_id, app_state), "cost_product_name": name or sku_id, "rows": []})
            row_key = sku_id or product_key(name)
            if any(row["sku_id"] == row_key for row in group["rows"]):
                continue
            variant = choose_cost(platform_costs.get(row_key, []), "2099/12/31")
            group["rows"].append({
                "sku_id": row_key, "variation": text(line.get("Variation")), "quantity": 0,
                "product_name": group["product_name"], "cost_product_name": name or sku_id,
                "default_amount": "", "sku_amount": "" if variant is None else money(variant.get("amount")),
            })
        return [row for group in grouped.values() for row in group["rows"]]
    income_rows, _ = rows_as_dicts(income_path, "Income")
    income_ids = {normalized_id(row.get("Related order ID") or row.get("Order/Adjustment ID")) for row in income_rows}
    grouped = {}
    for line in table(path, "OrderSKUList", "Order ID"):
        if normalized_id(line.get("Order ID")) not in income_ids:
            continue
        name = text(line.get("Product Name"))
        if not name:
            continue
        key = product_key(name)
        group = grouped.setdefault(key, {"product_name": display_product_name(name, app_state), "cost_product_name": name, "rows": []})
        sku_id = normalized_id(line.get("SKU ID"))
        if not sku_id or any(row["sku_id"] == sku_id for row in group["rows"]):
            continue
        default = choose_cost(app_state.get("product_costs", {}).get(key, []), "2099/12/31")
        variant = choose_cost(app_state.get("costs", {}).get(sku_id, []), "2099/12/31")
        group["rows"].append({
            "sku_id": sku_id, "variation": text(line.get("Variation")), "quantity": 0,
            "product_name": group["product_name"], "cost_product_name": name,
            "default_amount": "" if default is None else money(default.get("amount")),
            "sku_amount": "" if variant is None else money(variant.get("amount")),
        })
    return [row for group in grouped.values() for row in group["rows"]]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {format % args}")

    def json(self, value, status=HTTPStatus.OK):
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(raw))
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def require_access(self):
        # Marketplace authorization/security is deliberately out of scope.
        # Keep this hook so local deployments can still use APP_ACCESS_PASSWORD
        # without coupling it to a seller OAuth account.
        if not ACCESS_PASSWORD:
            return True
        received = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"merchant:{ACCESS_PASSWORD}".encode("utf-8")).decode("ascii")
        if secrets.compare_digest(received, expected):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Merchant Profit Checker"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/healthz"):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if not self.require_access():
            return
        if parsed.path == "/api/state":
            current = state()
            self.json({"costs": current["costs"], "product_costs": current.get("product_costs", {}), "name_replacements": current.get("name_replacements", [])})
            return
        if parsed.path == "/api/cost-catalog":
            self.json({"items": cost_catalog(state())})
            return
        if parsed.path == "/api/download/latest":
            current_platform = product_key(state().get("active_platform", "tiktok"))
            filename = f"{current_platform.title()}_Income_with_Profit.xlsx"
            path = REPORTS / filename
            if not path.exists():
                self.json({"error": "尚未生成报告"}, HTTPStatus.NOT_FOUND)
                return
            raw = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", len(raw))
            self.end_headers()
            self.wfile.write(raw)
            return
        if parsed.path == "/api/download/cost-template":
            path = ROOT / "cost_table_template.csv"
            raw = path.read_bytes() if path.is_file() else (
                "Platform,SKU,Product Name,Variation,Unit Cost,Currency,Effective Date,Note\n"
                "TikTok,,,,,MYR,2026/01/01,\n"
            ).encode("utf-8-sig")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="cost_table_template.csv"')
            self.send_header("Content-Length", len(raw))
            self.end_headers()
            self.wfile.write(raw)
            return
        if parsed.path == "/api/download/csv":
            path = REPORTS / "latest_profit_report.csv"
            if not path.exists():
                self.json({"error": "尚未生成报告"}, HTTPStatus.NOT_FOUND)
                return
            raw = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            current_platform = product_key(state().get("active_platform", "tiktok"))
            self.send_header("Content-Disposition", f'attachment; filename="{current_platform.title()}_Income_with_Profit.csv"')
            self.send_header("Content-Length", len(raw))
            self.end_headers()
            self.wfile.write(raw)
            return
        filename = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        file = STATIC / filename
        if not file.is_file() or file.parent != STATIC:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = "text/html; charset=utf-8" if file.suffix == ".html" else "application/javascript; charset=utf-8" if file.suffix == ".js" else "text/css; charset=utf-8"
        raw = file.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(raw))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if not self.require_access():
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length > MAX_REQUEST_BYTES:
                raise ValueError("上传内容过大。单次上传上限为 110 MB。")
            if self.path == "/api/analyse":
                form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers["Content-Type"]})
                if not form.getfirst("income") or not form.getfirst("orders"):
                    raise ValueError("请选择 Income 与 All Orders 两个 Excel 文件。")
                requested_platform = product_key(form.getfirst("platform") or "auto")
                if requested_platform not in ("auto", "tiktok", "shopee", "ebay", "lazada", "amazon"):
                    raise ValueError("不支持这个平台。")
                with tempfile.TemporaryDirectory() as directory:
                    paths = {}
                    for name in ("income", "orders"):
                        field = form[name]
                        suffix = Path(field.filename or "").suffix.lower()
                        if not field.filename or suffix not in (".xlsx", ".csv", ".tsv"):
                            raise ValueError("只支持 XLSX、CSV 或 TSV 文件。")
                        if field.file.seek(0, os.SEEK_END) > MAX_UPLOAD_BYTES:
                            raise ValueError("单个文件过大。每个文件上限为 50 MB。")
                        field.file.seek(0)
                        path = Path(directory) / f"{name}{suffix}"
                        with path.open("wb") as output:
                            shutil.copyfileobj(field.file, output)
                        paths[name] = path
                    detected_platform = detect_platform(paths["income"], paths["orders"])
                    if requested_platform != "auto" and requested_platform != detected_platform:
                        raise ValueError(f"文件检测为 {detected_platform} 报表，与选择的平台不一致。")
                    platform = detected_platform
                    DATA.mkdir(exist_ok=True)
                    for name, path in paths.items():
                        for old in DATA.glob(f"latest_{name}.*"):
                            if old.suffix.lower() in (".xlsx", ".csv", ".tsv"):
                                old.unlink()
                        shutil.copyfile(path, DATA / f"latest_{name}{path.suffix.lower()}")
                    report_id = hashlib.sha256(paths["income"].read_bytes() + paths["orders"].read_bytes()).hexdigest()
                    current = state()
                    current["active_report_id"] = report_id
                    current["active_platform"] = platform
                    if current.get("override_report_id") != report_id:
                        current["order_overrides"] = {}
                    save_state(current)
                    result = analyse(paths["income"], paths["orders"], current, platform)
                    write_augmented_xlsx(paths["income"], paths["orders"], current, platform)
                    write_csv(result)
                self.json(result)
                return
            if self.path == "/api/cost-table":
                form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers["Content-Type"]})
                if not form.getfirst("cost_table"):
                    raise ValueError("请选择商品成本表。")
                field = form["cost_table"]
                if not field.filename or Path(field.filename).suffix.lower() not in (".csv", ".tsv", ".xlsx"):
                    raise ValueError("成本表只支持 CSV、TSV 或 XLSX 文件。")
                if field.file.seek(0, os.SEEK_END) > MAX_UPLOAD_BYTES:
                    raise ValueError("成本表过大。单个文件上限为 50 MB。")
                field.file.seek(0)
                suffix = Path(field.filename or "costs.csv").suffix.lower()
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / f"costs{suffix if suffix in ('.csv', '.tsv', '.xlsx') else '.csv'}"
                    with path.open("wb") as output:
                        shutil.copyfileobj(field.file, output)
                    records, skipped = parse_cost_table(path)
                current = state()
                current.setdefault("platform_costs", {})
                imported = applied = stored_for_later = conflicts = 0
                for record in records:
                    imported += 1
                    platform = product_key(record["platform"])
                    # TikTok is the currently active importer. Other platform
                    # rows are retained separately so they cannot accidentally
                    # change TikTok's result before their adapter is enabled.
                    target = current["costs"] if platform in ("", "tiktok", "tiktok shop") else current["platform_costs"].setdefault(platform, {})
                    key = record["sku"] or product_key(record["product_name"])
                    entries = target.setdefault(key, [])
                    if any(text(entry.get("effective_from")) == record["effective_from"] and cost_currency(entry).upper() == record["currency"].upper() for entry in entries):
                        conflicts += 1
                        continue
                    save_cost_entry(entries, record["amount"], record["effective_from"], record["note"], record["currency"])
                    if platform in ("", "tiktok", "tiktok shop"):
                        if record["sku"]:
                            applied += 1
                        else:
                            product_entries = current.setdefault("product_costs", {}).setdefault(product_key(record["product_name"]), [])
                            if not any(text(entry.get("effective_from")) == record["effective_from"] and cost_currency(entry).upper() == record["currency"].upper() for entry in product_entries):
                                save_cost_entry(product_entries, record["amount"], record["effective_from"], record["note"], record["currency"])
                            applied += 1
                    else:
                        stored_for_later += 1
                save_state(current)
                result = refresh_current_report(current)
                self.json({"ok": True, "imported": imported, "applied": applied, "stored_for_later": stored_for_later, "skipped": skipped, "conflicts": conflicts, "result": result})
                return
            payload = self.body()
            if self.path == "/api/order-override":
                order_id = normalized_id(payload.get("order_id"))
                if not order_id:
                    raise ValueError("找不到订单号。")
                current = state()
                if not current.get("active_report_id"):
                    raise ValueError("请先分析当前两份 Excel，再修改订单。")
                current["override_report_id"] = current["active_report_id"]
                override = current.setdefault("order_overrides", {}).setdefault(order_id, {})
                for field in ("net_settlement", "cost", "other_cost"):
                    if field in payload:
                        value = text(payload[field])
                        if value == "":
                            override.pop(field, None)
                        elif field in ("cost", "other_cost"):
                            override[field] = strict_money(value)
                        else:
                            override[field] = strict_signed_money(value)
                if not override:
                    current["order_overrides"].pop(order_id, None)
                save_state(current)
                income_path, orders_path = latest_report_paths()
                if not income_path.exists() or not orders_path.exists():
                    raise ValueError("请先重新分析两份 Excel。")
                result = analyse(income_path, orders_path, current)
                write_augmented_xlsx(income_path, orders_path, current)
                write_csv(result)
                self.json(result)
                return
            if self.path == "/api/cost":
                sku_id = normalized_id(payload.get("sku_id"))
                amount = strict_money(payload.get("amount"))
                currency = text(payload.get("currency")) or "MYR"
                if not sku_id or not re.fullmatch(r"[A-Za-z]{3}", currency):
                    raise ValueError("请提供 TikTok SKU ID 与非负成本。")
                current = state()
                entries = current["costs"].setdefault(sku_id, [])
                save_cost_entry(entries, amount, text(payload.get("effective_from")) or "1900/01/01", text(payload.get("note")), currency)
                save_state(current)
                self.json({"ok": True, "result": refresh_current_report(current)})
                return
            if self.path == "/api/product-cost":
                name = text(payload.get("product_name"))
                amount = strict_money(payload.get("amount"))
                currency = text(payload.get("currency")) or "MYR"
                if not name or not re.fullmatch(r"[A-Za-z]{3}", currency):
                    raise ValueError("请提供商品名称与非负成本。")
                current = state()
                effective_from = text(payload.get("effective_from")) or "1900/01/01"
                entries = current.setdefault("product_costs", {}).setdefault(product_key(name), [])
                save_cost_entry(entries, amount, effective_from, "商品名默认成本", currency)
                # Product-level cost is the “apply to all variations” default.
                # Remove stale SKU entries for this product/date/currency so
                # they cannot mask the new value in the special-cost fields.
                _, orders_path = latest_report_paths()
                if orders_path.exists():
                    try:
                        order_lines = table(orders_path, "OrderSKUList", "Order ID")
                    except Exception:
                        order_lines = []
                    matching_skus = {
                        normalized_id(line.get("SKU ID"))
                        for line in order_lines
                        if normalized_id(line.get("SKU ID")) and (
                            product_key(line.get("Product Name")) == product_key(name)
                            or product_key(display_product_name(line.get("Product Name"), current)) == product_key(name)
                        )
                    }
                    for sku_id in matching_skus:
                        # Applying a product cost intentionally resets every
                        # existing per-SKU override for that product. A later
                        # “special cost” save can create a new exception.
                        current["costs"][sku_id] = []
                save_state(current)
                self.json({"ok": True, "result": refresh_current_report(current)})
                return
            if self.path == "/api/name-replace":
                find, replace = text(payload.get("find")), text(payload.get("replace"))
                if not find:
                    raise ValueError("请填写要查找的商品名称文字。")
                current = state()
                rules = [rule for rule in current.setdefault("name_replacements", []) if text(rule.get("find")) != find]
                rules.append({"find": find, "replace": replace})
                current["name_replacements"] = rules
                save_state(current)
                income_path, orders_path = latest_report_paths()
                if income_path.exists() and orders_path.exists():
                    result = analyse(income_path, orders_path, current)
                    write_augmented_xlsx(income_path, orders_path, current)
                    write_csv(result)
                    self.json({"ok": True, "rules": rules, "result": result})
                else:
                    self.json({"ok": True, "rules": rules})
                return
            if self.path == "/api/retime-costs":
                effective_from = text(payload.get("effective_from"))
                if date_key(effective_from) is None:
                    raise ValueError("请填写有效日期，例如 2026/01/01。")
                current, count = state(), 0
                for groups in (current.get("costs", {}), current.get("product_costs", {})):
                    for entries in groups.values():
                        for entry in entries:
                            entry["effective_from"] = effective_from
                            count += 1
                save_state(current)
                self.json({"ok": True, "count": count, "result": refresh_current_report(current)})
                return
            self.json({"error": "未知请求"}, HTTPStatus.NOT_FOUND)
        except Exception as error:
            self.json({"error": str(error)}, HTTPStatus.BAD_REQUEST)


if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)
    port = int(os.environ.get("PORT", "8765"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Merchant Ops Tool is running on port {port}")
    server.serve_forever()
