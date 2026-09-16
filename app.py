"""Local TikTok Shop profit checker and inventory-sync safety sandbox.

This program intentionally has no cloud account, no password collection, and no
live TikTok write capability until the owner completes official OAuth setup.
"""
from __future__ import annotations

import base64
import cgi
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
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

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
ACCESS_PASSWORD = os.environ.get("APP_ACCESS_PASSWORD", "")
TIKTOK_APP_KEY = os.environ.get("TIKTOK_APP_KEY", "")
TIKTOK_APP_SECRET = os.environ.get("TIKTOK_APP_SECRET", "")
TIKTOK_SERVICE_ID = os.environ.get("TIKTOK_SERVICE_ID", "")
TIKTOK_TOKEN_ENCRYPTION_KEY = os.environ.get("TIKTOK_TOKEN_ENCRYPTION_KEY", "")
TIKTOK_REDIRECT_URL = os.environ.get("TIKTOK_REDIRECT_URL", "https://erp-sistem.onrender.com/tiktok/callback")
LOCAL_NODE = Path(r"C:\Users\Win10\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe")


def default_state():
    return {"costs": {}, "product_costs": {}, "name_replacements": [], "order_overrides": {}, "inventory_mappings": [], "inventory_events": []}


def state():
    DATA.mkdir(exist_ok=True)
    if not STATE_PATH.exists():
        save_state(default_state())
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(value):
    DATA.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def auth_db():
    """Temporary local storage for development only; production needs a persistent DB."""
    DATA.mkdir(exist_ok=True)
    connection = sqlite3.connect(DATA / "tiktok_auth.db")
    connection.execute("CREATE TABLE IF NOT EXISTS oauth_states (value TEXT PRIMARY KEY, created_at INTEGER NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS shop_tokens (shop_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at INTEGER NOT NULL)")
    return connection


def token_cipher():
    if not Fernet or not TIKTOK_TOKEN_ENCRYPTION_KEY:
        return None
    try:
        raw = TIKTOK_TOKEN_ENCRYPTION_KEY.strip().encode("utf-8")
        # Render's Generate action may return a regular secret rather than a
        # Fernet key. Derive a fixed 32-byte key without exposing the secret.
        key = raw if len(raw) == 44 else base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        return Fernet(key)
    except Exception:
        return None


def save_oauth_state(value):
    with auth_db() as connection:
        connection.execute("DELETE FROM oauth_states WHERE created_at < strftime('%s','now') - 900")
        connection.execute("INSERT INTO oauth_states(value, created_at) VALUES (?, strftime('%s','now'))", (value,))


def consume_oauth_state(value):
    with auth_db() as connection:
        row = connection.execute("SELECT value FROM oauth_states WHERE value=? AND created_at >= strftime('%s','now') - 900", (value,)).fetchone()
        if not row:
            return False
        connection.execute("DELETE FROM oauth_states WHERE value=?", (value,))
        return True


def exchange_tiktok_code(code):
    """Exchange a one-time TikTok authorization code without ever exposing the secret."""
    if not TIKTOK_APP_KEY or not TIKTOK_APP_SECRET:
        raise RuntimeError("TikTok App Key 或 App Secret 尚未配置。")
    query = urlencode({"app_key": TIKTOK_APP_KEY, "app_secret": TIKTOK_APP_SECRET, "auth_code": code, "grant_type": "authorized_code"})
    request = Request(f"https://auth.tiktok-shops.com/api/v2/token/get?{query}", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as error:
        raise RuntimeError("TikTok Token 服务暂时不可用，请稍后重试。") from error
    if result.get("code") not in (0, "0", None) or not result.get("data"):
        raise RuntimeError("TikTok 未接受授权码。")
    return result["data"]


def save_shop_token(token_data):
    cipher = token_cipher()
    if not cipher:
        raise RuntimeError("未配置 TIKTOK_TOKEN_ENCRYPTION_KEY，系统拒绝保存 Token。")
    shop_key = text(token_data.get("open_id"))
    if not shop_key:
        raise RuntimeError("TikTok 未返回店铺授权标识。")
    encrypted = cipher.encrypt(json.dumps(token_data, ensure_ascii=False).encode("utf-8")).decode("ascii")
    with auth_db() as connection:
        connection.execute("INSERT OR REPLACE INTO shop_tokens(shop_key, payload, updated_at) VALUES (?, ?, strftime('%s','now'))", (shop_key, encrypted))


def text(value):
    return "" if value is None else str(value).strip()


def money(value):
    raw = text(value).replace("RM", "").replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return 0.0


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


def display_product_name(value, app_state):
    """Local presentation rules only; never alter the TikTok source or cost key."""
    result = text(value)
    for rule in app_state.get("name_replacements", []):
        old = text(rule.get("find"))
        if old:
            result = result.replace(old, text(rule.get("replace")))
    return result


def save_cost_entry(entries, amount, effective_from, note):
    """Replace same-date entry so a correction actually takes effect."""
    entries[:] = [entry for entry in entries if text(entry.get("effective_from")) != effective_from]
    entries.append({"amount": amount, "effective_from": effective_from, "note": note})
    entries.sort(key=lambda item: item["effective_from"])


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


def table(path, sheet, required_header):
    rows = read_xlsx_rows(path, sheet)
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


SYSTEM_COLUMNS = {"商品名称", "Variation", "实际数量", "成本", "纯利润", "核对状态"}


def income_table(path):
    """Load a TikTok export, removing columns from an older tool export."""
    rows = read_xlsx_rows(path, "Order details")
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


def detail_quantity(value):
    return sum(number(qty) for qty in re.findall(r"\*\s*(\d+)", text(value)))


def date_key(value):
    raw = text(value)
    for pattern in ("%Y/%m/%d", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw[:10], pattern).date()
        except ValueError:
            continue
    return None


def choose_cost(cost_entries, order_date):
    order_day = date_key(order_date)
    applicable = [
        entry for entry in cost_entries
        if order_day is None or date_key(entry.get("effective_from")) is None or date_key(entry.get("effective_from")) <= order_day
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
    return any(any(word in text(item).lower() for word in ("refund", "return", "退款", "退货")) for item in types)


def manual_override(app_state, order_id):
    if not app_state.get("active_report_id") or app_state.get("override_report_id") != app_state.get("active_report_id"):
        return {}
    return app_state.get("order_overrides", {}).get(order_id, {})


def analyse(income_path, orders_path, app_state):
    income = income_table(income_path)
    orders = table(orders_path, "OrderSKUList", "Order ID")

    source_columns = list(income[0].keys()) if income else []
    settlements = defaultdict(lambda: {"net": 0.0, "details_units": 0, "types": set(), "rows": 0, "source": defaultdict(list)})
    for row in income:
        transaction_type = text(row.get("Transaction type"))
        key = normalized_id(row.get("Related order ID") or row.get("Order/Adjustment ID"))
        if not key or transaction_type.lower() == "logistics reimbursement":
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
    report, missing = [], {}
    for order_id, settlement in settlements.items():
        order_lines = by_order.get(order_id, [])
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
        flags = []
        lifecycle = order_lifecycle(order_lines)
        override = manual_override(app_state, order_id)
        net_settlement = money(override["net_settlement"]) if "net_settlement" in override else settlement["net"]
        override_cost = money(override["cost"]) if "cost" in override else None
        if lifecycle["cancelled"]:
            report.append({
                "order_id": order_id, "net_settlement": round(net_settlement, 2), "actual_units": 0,
                "details_units": settlement["details_units"], "cost": 0.0, "profit": None,
                "status": "已取消", "flags": "未结算，不计成本或利润", "transaction_types": ", ".join(sorted(settlement["types"])),
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
            cost = choose_cost(app_state["costs"].get(sku_id, []), order_date)
            cost = cost or choose_cost(app_state["product_costs"].get(product_key(name), []), order_date)
            if quantity and cost is None:
                missing[sku_id] = {
                    "sku_id": sku_id,
                    "product_name": name,
                    "variation": text(line.get("Variation")),
                    "quantity": 0,
                }
                missing[sku_id]["quantity"] += quantity
                flags.append(f"缺成本：{sku_id}")
            elif cost:
                cost_total += quantity * money(cost.get("amount"))
        # Full returns have zero net units while Income may list the original
        # item. That is expected and must not hide the refund loss.
        if settlement["details_units"] and actual_units != settlement["details_units"] and not (lifecycle["returned"] and actual_units == 0):
            flags.append(f"数量不一致：订单 {actual_units} 件，结算详情 {settlement['details_units']} 件")
        if override_cost is not None:
            cost_total = override_cost
            flags = [flag for flag in flags if not flag.startswith("找不到订单商品明细")]
            if not order_lines:
                # The merchant explicitly supplied the total historical cost,
                # so a missing old All Orders row must not block the loss result.
                flags = []
        refund_only = lifecycle["returned"] and not any(text(kind).lower().startswith("order") for kind in settlement["types"])
        if refund_only and override_cost is None:
            # The original sale was settled in an earlier report. Its COGS was
            # already recognised then; this report must not deduct it again.
            cost_total = 0.0
            flags = [flag for flag in flags if not flag.startswith("缺成本")]
        profit_value = round(net_settlement - cost_total, 2) if not flags else None
        if lifecycle["returned"]:
            refund_label = "已退货退款" if actual_units == 0 else f"部分退货退款（净售 {actual_units} 件）"
        elif is_refund_transaction(settlement["types"]):
            refund_label = "退款结算影响（请核对商品）"
        else:
            refund_label = ""
        status = "需核对" if flags else (refund_label or "可确认")
        report.append({
            "order_id": order_id,
            "net_settlement": round(net_settlement, 2),
            "actual_units": actual_units,
            "details_units": settlement["details_units"],
            "cost": round(cost_total, 2),
            "profit": profit_value,
            "status": status,
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
            "confirmed_cost": round(sum(row["cost"] for row in confirmed), 2),
            "orders": len(report),
        "needs_review": sum(row["status"] == "需核对" for row in report),
        },
        "orders": report,
        "source_columns": source_columns,
        "missing_costs": sorted(missing.values(), key=lambda item: item["product_name"]),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def write_csv(report):
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / "latest_profit_report.csv"
    columns = ["商品名称", "Variation", "订单", "到账", "订单件数", "结算件数", "总成本", "纯利润", "状态", "核对说明"] + report.get("source_columns", [])
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        for item in report["orders"]:
            row = {
                "商品名称": item.get("product_name", ""), "Variation": item.get("variation", ""),
                "订单": item["order_id"], "到账": item["net_settlement"], "订单件数": item["actual_units"],
                "结算件数": item["details_units"], "总成本": item["cost"],
                "纯利润": "" if item["profit"] is None else item["profit"],
                "状态": item["status"], "核对说明": item["flags"],
            }
            row.update(item.get("source", {}))
            writer.writerow(row)
    return path


def write_augmented_xlsx(income_path, orders_path, app_state):
    income = income_table(income_path)
    orders = table(orders_path, "OrderSKUList", "Order ID")
    original_headers = list(income[0].keys())
    insert_at = original_headers.index("Total Revenue") + 1
    added = ["商品名称", "Variation", "实际数量", "成本", "纯利润", "核对状态"]
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
        lifecycle = order_lifecycle(by_order.get(order_id, []))
        override = manual_override(app_state, order_id)
        net_settlement = money(override["net_settlement"]) if "net_settlement" in override else totals[order_id]["net"]
        if transaction == "logistics reimbursement":
            status = "非订单结算，不计算利润"
            cost = profit = ""
        elif order_id in seen:
            status = "同订单调整行；成本已在首次订单行计算"
            cost = profit = ""
        elif lifecycle["cancelled"]:
            seen.add(order_id)
            status = "已取消"
            cost = profit = ""
        elif not by_order.get(order_id):
            seen.add(order_id)
            cost = money(override["cost"]) if "cost" in override else ""
            profit = round(net_settlement - cost, 2) if isinstance(cost, float) else ""
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
                item_cost = choose_cost(app_state.get("costs", {}).get(normalized_id(line.get("SKU ID")), []), date)
                item_cost = item_cost or choose_cost(app_state.get("product_costs", {}).get(product_key(name), []), date)
                if item_cost is None:
                    missing = True
                else:
                    cost_total += units * money(item_cost.get("amount"))
            if "cost" in override:
                cost_total = money(override["cost"])
                missing = False
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
            profit_value = None if flags else round(net_settlement - cost_total, 2)
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
        extra = ["\n".join(names), "\n".join(variations), quantity if names else "", cost, profit, status]
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
    numeric_headers = {"Total settlement amount", "Total Revenue", "成本", "纯利润"}
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
    cost_index = headers.index("成本")
    profit_index = headers.index("纯利润")
    status_index = headers.index("核对状态")
    for row_index, row in enumerate(rows, start=1):
        status = str(row[status_index] or "")
        profit_value = row[profit_index]
        if profit_value == "" or not any(status.startswith(label) for label in ("可确认", "已退货退款", "部分退货退款", "退款结算影响", "手动成本已用于计算")):
            continue
        excel_row = row_index + 1
        override_net = profit_net_overrides.get(str(row_index - 1))
        if override_net is not None:
            net_expression = str(float(override_net))
        else:
            net_expression = f'SUMIF(${excel_column(order_index)}$2:${excel_column(order_index)}${len(rows) + 1},{excel_column(order_index)}{excel_row},${excel_column(settlement_index)}$2:${excel_column(settlement_index)}${len(rows) + 1})'
        worksheet.write_formula(row_index, profit_index, f"={net_expression}-{excel_column(cost_index)}{excel_row}", number_format)

    summary_row = len(rows) + 2
    worksheet.write(summary_row, order_index, "总计", total_label_format)
    for index in (settlement_index, headers.index("Total Revenue"), cost_index, profit_index):
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
    income_path, orders_path = DATA / "latest_income.xlsx", DATA / "latest_orders.xlsx"
    if not income_path.exists() or not orders_path.exists():
        return None
    result = analyse(income_path, orders_path, app_state)
    write_augmented_xlsx(income_path, orders_path, app_state)
    write_csv(result)
    return result


def cost_catalog(app_state):
    """All products in the latest All Orders file, including already-priced ones."""
    path = DATA / "latest_orders.xlsx"
    if not path.exists():
        return []
    grouped = {}
    for line in table(path, "OrderSKUList", "Order ID"):
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
        """Keep a deployed single-user test instance private when configured."""
        if not ACCESS_PASSWORD:
            return True
        received = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"merchant:{ACCESS_PASSWORD}".encode("utf-8")).decode("ascii")
        if secrets.compare_digest(received, expected):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Merchant Ops Tool"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

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
        if parsed.path == "/tiktok/authorize":
            if not TIKTOK_APP_KEY or not TIKTOK_APP_SECRET or not TIKTOK_SERVICE_ID or not token_cipher():
                self.json({"error": "TikTok API 尚未完成安全配置。"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            auth_state = secrets.token_urlsafe(32)
            save_oauth_state(auth_state)
            query = urlencode({"service_id": TIKTOK_SERVICE_ID, "state": auth_state})
            location = f"https://services.tiktokshop.com/open/authorize?{query}"
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if parsed.path == "/tiktok/callback":
            params = parse_qs(parsed.query)
            code = text((params.get("code") or params.get("auth_code") or [""])[0])
            auth_state = text((params.get("state") or [""])[0])
            error = text((params.get("error") or [""])[0])
            if error or not code:
                result, status = "<h2>TikTok 授权已取消或失败</h2><p>请返回系统重试。</p>", HTTPStatus.BAD_REQUEST
            elif not consume_oauth_state(auth_state):
                result, status = "<h2>授权请求已失效</h2><p>请回到系统重新开始授权。</p>", HTTPStatus.BAD_REQUEST
            else:
                try:
                    save_shop_token(exchange_tiktok_code(code))
                    result, status = "<h2>TikTok 店铺已安全连接</h2><p>可以关闭此页并返回系统。</p>", HTTPStatus.OK
                except RuntimeError:
                    result, status = "<h2>连接未完成</h2><p>系统没有保存任何 Token。请检查配置后重新授权。</p>", HTTPStatus.SERVICE_UNAVAILABLE
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            raw = ("<!doctype html><meta charset='utf-8'><title>TikTok 授权</title>" + result).encode("utf-8")
            self.send_header("Content-Length", len(raw))
            self.end_headers()
            self.wfile.write(raw)
            return
        if parsed.path == "/api/state":
            current = state()
            self.json({"costs": current["costs"], "product_costs": current.get("product_costs", {}), "name_replacements": current.get("name_replacements", []), "inventory_mappings": current["inventory_mappings"], "inventory_events": current["inventory_events"][-20:]})
            return
        if parsed.path == "/api/cost-catalog":
            self.json({"items": cost_catalog(state())})
            return
        if parsed.path == "/api/download/latest":
            path = REPORTS / "TikTok_Income_with_Profit.xlsx"
            if not path.exists():
                self.json({"error": "尚未生成报告"}, HTTPStatus.NOT_FOUND)
                return
            raw = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", 'attachment; filename="TikTok_Income_with_Profit.xlsx"')
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
            self.send_header("Content-Disposition", 'attachment; filename="TikTok_Income_with_Profit.csv"')
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
            if self.path == "/api/analyse":
                form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers["Content-Type"]})
                if not form.getfirst("income") or not form.getfirst("orders"):
                    raise ValueError("请选择 Income 与 All Orders 两个 Excel 文件。")
                with tempfile.TemporaryDirectory() as directory:
                    paths = {}
                    for name in ("income", "orders"):
                        field = form[name]
                        path = Path(directory) / f"{name}.xlsx"
                        with path.open("wb") as output:
                            shutil.copyfileobj(field.file, output)
                        paths[name] = path
                        DATA.mkdir(exist_ok=True)
                        shutil.copyfile(path, DATA / f"latest_{name}.xlsx")
                    report_id = hashlib.sha256(paths["income"].read_bytes() + paths["orders"].read_bytes()).hexdigest()
                    current = state()
                    current["active_report_id"] = report_id
                    if current.get("override_report_id") != report_id:
                        current["order_overrides"] = {}
                    save_state(current)
                    result = analyse(paths["income"], paths["orders"], current)
                    write_augmented_xlsx(paths["income"], paths["orders"], current)
                    write_csv(result)
                self.json(result)
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
                for field in ("net_settlement", "cost"):
                    if field in payload:
                        value = text(payload[field])
                        if value == "":
                            override.pop(field, None)
                        elif money(value) < 0 and field == "cost":
                            raise ValueError("成本不能小于 0。")
                        else:
                            override[field] = money(value)
                if not override:
                    current["order_overrides"].pop(order_id, None)
                save_state(current)
                income_path, orders_path = DATA / "latest_income.xlsx", DATA / "latest_orders.xlsx"
                if not income_path.exists() or not orders_path.exists():
                    raise ValueError("请先重新分析两份 Excel。")
                result = analyse(income_path, orders_path, current)
                write_augmented_xlsx(income_path, orders_path, current)
                write_csv(result)
                self.json(result)
                return
            if self.path == "/api/cost":
                sku_id = normalized_id(payload.get("sku_id"))
                if not sku_id or money(payload.get("amount")) < 0:
                    raise ValueError("请提供 TikTok SKU ID 与非负成本。")
                current = state()
                entries = current["costs"].setdefault(sku_id, [])
                save_cost_entry(entries, money(payload["amount"]), text(payload.get("effective_from")) or "1900/01/01", text(payload.get("note")))
                save_state(current)
                self.json({"ok": True, "result": refresh_current_report(current)})
                return
            if self.path == "/api/product-cost":
                name = text(payload.get("product_name"))
                if not name or money(payload.get("amount")) < 0:
                    raise ValueError("请提供商品名称与非负成本。")
                current = state()
                entries = current.setdefault("product_costs", {}).setdefault(product_key(name), [])
                save_cost_entry(entries, money(payload["amount"]), text(payload.get("effective_from")) or "1900/01/01", "商品名默认成本")
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
                income_path, orders_path = DATA / "latest_income.xlsx", DATA / "latest_orders.xlsx"
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
            if self.path == "/api/inventory/mapping":
                required = ["canonical_id", "shop", "product_id", "sku_id"]
                if any(not text(payload.get(key)) for key in required):
                    raise ValueError("库存映射必须有货品编号、店铺、商品 ID、平台 SKU ID。")
                current = state()
                mapping = {key: text(payload.get(key)) for key in required}
                mapping["buffer"] = max(0, number(payload.get("buffer")))
                mapping["shared_stock"] = max(0, number(payload.get("shared_stock")))
                current["inventory_mappings"] = [item for item in current["inventory_mappings"] if not (item["shop"] == mapping["shop"] and item["sku_id"] == mapping["sku_id"])] + [mapping]
                save_state(current)
                self.json({"ok": True, "mapping": mapping})
                return
            if self.path == "/api/inventory/simulate-sale":
                canonical_id = text(payload.get("canonical_id"))
                quantity = number(payload.get("quantity"))
                if not canonical_id or quantity < 1:
                    raise ValueError("请输入货品编号和售出数量。")
                current = state()
                targets = [item for item in current["inventory_mappings"] if item["canonical_id"] == canonical_id]
                if not targets:
                    raise ValueError("没有这个货品的库存映射。")
                before = min(number(item["shared_stock"]) for item in targets)
                after = max(0, before - quantity)
                for item in targets:
                    item["shared_stock"] = after
                    item["target_available_stock"] = max(0, after - number(item["buffer"]))
                event = {"at": datetime.now().isoformat(timespec="seconds"), "mode": "SIMULATION", "canonical_id": canonical_id, "sold": quantity, "stock_before": before, "stock_after": after, "targets": targets}
                current["inventory_events"].append(event)
                save_state(current)
                self.json(event)
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
