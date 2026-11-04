"""免费附件领取及 v1 torrent 转磁链，不保存临时下载票据。"""
import hashlib
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from scrapers.core.contracts import DetailValidationError


def torrent_magnet(raw):
    if not raw or len(raw) > 8 * 1024 * 1024:
        raise DetailValidationError("invalid_torrent")
    info_bytes = None

    def read(pos, depth=0):
        nonlocal info_bytes
        if depth > 64 or pos >= len(raw):
            raise ValueError()
        start = pos
        kind = raw[pos:pos + 1]
        if kind == b"i":
            end = raw.index(b"e", pos)
            return int(raw[pos + 1:end]), end + 1
        if kind in (b"d", b"l"):
            value = {} if kind == b"d" else []
            pos += 1
            while raw[pos:pos + 1] != b"e":
                key, pos = read(pos, depth + 1)
                if kind == b"l":
                    value.append(key)
                    continue
                if not isinstance(key, bytes) or key in value:
                    raise ValueError()
                value_start = pos
                child, pos = read(pos, depth + 1)
                value[key] = child
                if start == 0 and key == b"info":
                    info_bytes = raw[value_start:pos]
            return value, pos + 1
        colon = raw.index(b":", pos)
        size = int(raw[pos:colon])
        end = colon + 1 + size
        if size < 0 or end > len(raw):
            raise ValueError()
        return raw[colon + 1:end], end

    try:
        document, end = read(0)
        info = document[b"info"]
        if end != len(raw) or not isinstance(info, dict) or not info_bytes:
            raise ValueError()
        pieces = info.get(b"pieces")
        if not isinstance(pieces, bytes) or not pieces or len(pieces) % 20:
            raise ValueError()
        name = info.get(b"name.utf-8", info.get(b"name", b""))
        magnet = "magnet:?xt=urn:btih:" + hashlib.sha1(info_bytes).hexdigest()
        if isinstance(name, bytes) and name:
            magnet += "&dn=" + quote(name.decode("utf-8", "replace"), safe="")
        return magnet
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
        raise DetailValidationError("invalid_torrent") from None


def free_preview(preview):
    if preview.get("status") != "claimable" or preview.get("file_ready") is not True:
        raise DetailValidationError("attachment_unavailable")
    try:
        price = Decimal(str(preview["price_points"]))
        payable = Decimal(str(preview["payable_points"]))
        quota = Decimal(str(preview["quota_units"]))
        free = all(n.is_finite() and n == 0 for n in (price, payable, quota))
    except (KeyError, InvalidOperation, ValueError):
        raise DetailValidationError("invalid_price_preview") from None
    if not free:
        raise DetailValidationError("paid_resource")
    if not preview.get("viewer_id") or not preview.get("version_id"):
        raise DetailValidationError("invalid_price_preview")
    return preview


def collect_free_torrents(client, resource_id, slots, *, dry_run=False):
    client.ensure_authenticated(resource_id)
    magnets = []
    denied = []
    for slot in slots:
        preview = client.api_json("GET", f"/api/slots/{slot}/preview")
        if preview.get("slot_id") != slot:
            raise DetailValidationError("invalid_price_preview")
        try:
            free_preview(preview)
        except DetailValidationError as exc:
            denied.append(exc.reason)
            continue
        if dry_run:
            continue
        # 此 body 在 HTTP 重试时保持不变，不生成新的领取操作。
        claim = client.api_json("POST", "/api/downloads/claim", data={
            "expected_user_id": preview["viewer_id"], "slot_id": slot,
            "expected_version": preview["version_id"],
            "expected_price": preview["price_points"], "expected_payable": preview["payable_points"],
            "idempotency_key": str(uuid.uuid4()),
        })
        receipt = claim.get("receipt_id")
        if claim.get("requires_new_claim") or not isinstance(receipt, str) or not receipt:
            raise DetailValidationError("invalid_download_claim")
        magnets.append(torrent_magnet(client.fetch_torrent("/api/downloads/file/" + quote(receipt, safe=""))))
    if not magnets and denied and len(denied) == len(slots):
        raise DetailValidationError(denied[0])
    return list(dict.fromkeys(magnets))
