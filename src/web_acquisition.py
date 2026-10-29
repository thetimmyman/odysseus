"""Opt-in client for operator-configured, owner-specific acquisition instances.

No company database or credential is wired into Odysseus. Install a separate
personal instance of the same package/contract. Configuration is read only from
the native data directory and is never exposed as a model tool parameter.
"""
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from src.constants import WEB_ACQUISITION_OWNERS_FILE


class WebAcquisitionUnavailable(RuntimeError):
    pass


class WebAcquisitionClient:
    def __init__(self, owner):
        try:
            if not isinstance(owner, str) or not owner.strip():
                raise ValueError('owner required')
            config = json.loads(Path(WEB_ACQUISITION_OWNERS_FILE).read_text())
            row = config[owner]  # no global/default owner or token fallback
            endpoint = row['endpoint']
            if not isinstance(endpoint, str):
                raise ValueError('endpoint must be a URL')
            self.endpoint = endpoint.rstrip('/')
            parsed = urlsplit(self.endpoint)
            parsed.port  # Reject malformed or out-of-range ports before loading credentials.
            if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1', 'localhost'}
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError('local transport required')
            self.token = Path(row['token_file']).read_text().strip()
            if not self.token:
                raise ValueError('credential missing')
        except (OSError, ValueError, KeyError, TypeError):
            raise WebAcquisitionUnavailable('owner acquisition instance is not configured') from None

    def request(self, action, payload):
        if action not in {'discover', 'acquire', 'retrieve'}:
            raise WebAcquisitionUnavailable('unsupported acquisition action')
        try:
            response = httpx.post(self.endpoint + '/v1/' + action, json=payload,
                headers={'Authorization': 'Bearer ' + self.token}, timeout=120,
                trust_env=False, follow_redirects=False)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise WebAcquisitionUnavailable('acquisition unavailable; no cloud fallback') from None

    def discover(self, query_id):
        return self.request('discover', {'query': query_id})

    def acquire(self, source_id):
        return self.request('acquire', {'source': source_id})

    def retrieve(self, query, semantic=False):
        return self.request('retrieve', {'query': query, 'semantic': bool(semantic)})
