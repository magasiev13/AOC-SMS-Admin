#!/usr/bin/env python3
"""Standard-library client for Twinevia's organization-scoped registration API.

Dry run is the default. Secrets are read only from TWINEVIA_SYNC_SECRET; payloads,
phone numbers and signatures are never printed. Retry an interrupted invocation
with exactly the same input and identity. No third-party packages are required.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


@dataclass(frozen=True)
class Delivery:
    identity: str
    body: bytes


def encode_payload(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')


def inventory_deliveries(payload, inventory_id, page_size=50):
    """Build page/completion bodies from one caller-captured consistent snapshot."""
    if not 1 <= page_size <= 500:
        raise ValueError('Page size must be between 1 and 500.')
    if not re.fullmatch(r'[A-Za-z0-9._:-]{1,100}', inventory_id):
        raise ValueError('A stable inventory identity is required.')
    records = payload.get('bookings')
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ValueError('Inventory input requires a bookings array.')
    pages = [records[i:i+page_size] for i in range(0, len(records), page_size)] or [[]]
    manifest = {'id': inventory_id, 'source_scope': payload['source_scope'],
                'watermark': payload['watermark'], 'pages': len(pages), 'records': len(records)}
    base = {'protocol_version': 2, 'source': payload['source'], 'event': payload['event'], 'inventory': manifest}
    hashes = []
    deliveries = []
    for number, records_page in enumerate(pages, 1):
        encoded = encode_payload(records_page).decode('utf-8')
        hashes.append(hashlib.sha256(encoded.encode('utf-8')).hexdigest())
        body = encode_payload({**base, 'action': 'inventory_page', 'page': number,
                               'bookings': records_page, 'records_json': encoded})
        deliveries.append(Delivery(f'{inventory_id}:page:{number}', body))
    proof = hashlib.sha256('\n'.join(hashes).encode('ascii')).hexdigest()
    deliveries.append(Delivery(f'{inventory_id}:complete', encode_payload(
        {**base, 'action': 'inventory_complete', 'completion_proof': proof})))
    return deliveries


def signed_request(url, delivery, secret, timestamp=None):
    parsed = urlparse(url)
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}):
        raise ValueError('Use HTTPS, or loopback HTTP for isolated testing.')
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError('Webhook URL must not contain credentials.')
    if not re.fullmatch(r'[A-Za-z0-9._:-]{1,140}', delivery.identity):
        raise ValueError('Delivery identity must contain 1–140 safe identifier characters.')
    if not secret:
        raise ValueError('TWINEVIA_SYNC_SECRET is required to send.')
    timestamp = str(int(time.time()) if timestamp is None else int(timestamp))
    signature = hmac.new(secret.encode('utf-8'), timestamp.encode('ascii')+b'.'+delivery.body, hashlib.sha256).hexdigest()
    return Request(url, data=delivery.body, method='POST', headers={
        'Content-Type': 'application/json', 'X-Twinevia-Timestamp': timestamp,
        'X-Twinevia-Signature': 'sha256='+signature, 'X-Twinevia-Delivery-ID': delivery.identity,
    })


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def submit(url, delivery, secret, timeout=15):
    request = signed_request(url, delivery, secret)
    try:
        with build_opener(_NoRedirects()).open(request, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f'Webhook returned HTTP {response.status}. Retry the same delivery identity after resolving the response.')
            # Do not echo source contact data or server errors to application logs.
            return response.status
    except HTTPError as exc:
        raise RuntimeError(f'Webhook returned HTTP {exc.code}. Retry the same delivery identity after resolving the response.') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='Organization-specific /webhooks/event-sync/<slug>/app URL.')
    parser.add_argument('--payload', type=Path, required=True, help='JSON mutation or captured inventory input file.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--delivery-id', help='Stable identity for a single mutation; reuse it on retry.')
    mode.add_argument('--inventory-id', help='Stable inventory identity; input must contain source,event,source_scope,watermark,bookings.')
    parser.add_argument('--page-size', type=int, default=50)
    parser.add_argument('--send', action='store_true', help='Submit to the endpoint. Omit for an offline dry run.')
    args = parser.parse_args(argv)
    try:
        raw = args.payload.read_bytes()
        payload = json.loads(raw.decode('utf-8'))
        if not isinstance(payload, dict):
            raise ValueError('Input JSON must be an object.')
        if args.inventory_id:
            deliveries = inventory_deliveries(payload, args.inventory_id, args.page_size)
        else:
            if payload.get('protocol_version') != 2:
                raise ValueError('Application mutations require protocol_version 2.')
            deliveries = [Delivery(args.delivery_id, raw)]
        parsed = urlparse(args.url)
        if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('A valid organization endpoint URL without embedded credentials is required.')
        for delivery in deliveries:
            status = submit(args.url, delivery, os.environ.get('TWINEVIA_SYNC_SECRET', '')) if args.send else None
            print(json.dumps({'mode': 'submitted' if args.send else 'dry_run', 'endpoint': args.url,
                              'delivery_id': delivery.identity, 'sha256': hashlib.sha256(delivery.body).hexdigest(),
                              'bytes': len(delivery.body), 'http_status': status}))
        return 0
    except (OSError, UnicodeDecodeError, ValueError, KeyError, RuntimeError) as exc:
        parser.exit(1, f'{type(exc).__name__}: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
