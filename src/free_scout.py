"""Isolated, current-source-verified Space Bunny scout launcher. No paid fallback."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

SOURCE_URL = 'https://opencode.ai/docs/go/'
CHAT_URL = 'https://opencode.ai/zen/go/v1/chat/completions'
BASE_URL = 'https://opencode.ai/zen/go/v1'
MODEL = 'space-bunny-free'
PROFILE = 'ps679-opencode-space-bunny-free'
ENDPOINT = 'ps679-opencode-go'


class Tables(HTMLParser):
    def __init__(self):
        super().__init__(); self.rows = []; self.row = None; self.cell = None
    def handle_starttag(self, tag, attrs):
        if tag == 'tr': self.row = []
        elif tag in ('td', 'th') and self.row is not None: self.cell = []
    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split())); self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row); self.row = None


def verify_terms(raw):
    table = Tables(); table.feed(raw.decode('utf-8'))
    price_rows = [r for r in table.rows if r and r[0] == 'Space Bunny Free' and len(r) == 6]
    prices = [r for r in price_rows if r[1:4] == ['Free'] * 3 and r[4] == '-']
    routes = [r for r in table.rows if len(r) >= 3 and r[:3] == ['Space Bunny Free', MODEL, CHAT_URL]]
    if len(price_rows) != 1 or len(prices) != 1 or not routes:
        raise ValueError('official free price and exact model route could not be established')


def private_dir(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink(): raise ValueError('symlinked directory refused')
    path = path.resolve()
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise ValueError('runtime directory must be private (0700)')
    return path


def write_json(path, value):
    raw = (json.dumps(value, indent=2) + '\n').encode()
    write_bytes(path, raw)


def write_bytes(path, raw):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream: stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try: temp.unlink()
        except FileNotFoundError: pass


def read_json(path):
    if Path(path).is_symlink(): raise ValueError('symlinked configuration refused')
    with open(path) as stream: return json.load(stream)


def claim_database(root):
    data = private_dir(root / 'data'); database = data / 'app.db'; marker = root / 'database-owner.json'
    if database.is_symlink(): raise ValueError('symlinked database refused')
    if marker.exists():
        owned = read_json(marker)
        if not database.exists() or owned.get('database_identity') != [database.stat().st_dev, database.stat().st_ino]:
            raise ValueError('runtime database ownership is uncertain')
    else:
        if any(data.iterdir()): raise ValueError('refusing existing data directory or database')
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'schema_version': 1, 'database_identity': None}, stream); stream.flush(); os.fsync(stream.fileno())
    return database, marker


def start_attempt(root):
    marker = root / 'attempt-started.json'
    fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'schema_version': 1, 'started_at': datetime.now(timezone.utc).isoformat(), 'model': MODEL}, stream)
        stream.flush(); os.fsync(stream.fileno())
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def configure_environment(root, manifest):
    data = private_dir(root / 'data')
    os.environ['ODYSSEUS_DATA_DIR'] = str(data)
    os.environ['DATABASE_URL'] = 'sqlite:///' + str(data / 'app.db')
    os.environ['ODYSSEUS_OPENCODE_SESSION'] = manifest['session_id']
    os.environ.pop('ODYSSEUS_OFFER_CONFIG', None)
    os.environ.pop('ODYSSEUS_FREE_OFFER_CONFIG', None)
    os.environ['ODYSSEUS_USAGE_EXPORT_CONFIG'] = str(root / 'usage-export.json')


def refresh(root, manifest):
    # Fixed official HTTPS URL, bounded response, no redirects and no credentials.
    import requests
    with requests.get(SOURCE_URL, timeout=15, allow_redirects=False, stream=True) as response:
        if response.status_code != 200: raise ValueError('official terms unavailable')
        chunks = []; size = 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 2 * 1024 * 1024: raise ValueError('official terms too large')
            chunks.append(chunk)
        source_bytes = b''.join(chunks)
    verify_terms(source_bytes)
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    source = root / (source_hash + '.source.html'); write_bytes(source, source_bytes)
    configure_environment(root, manifest)
    database, owner_marker = claim_database(root)
    from core.database import SessionLocal, ModelEndpoint, RoutingModelProfile
    bound_database = getattr(getattr(SessionLocal.kw.get('bind'), 'url', None), 'database', None)
    if not bound_database or Path(bound_database).resolve() != database:
        raise ValueError('existing process database binding is not the isolated runtime')
    write_json(owner_marker, {'schema_version': 1, 'database_identity': [database.stat().st_dev, database.stat().st_ino]})
    from src.capacity_collector import collect_endpoint_capacity
    from src.provider_capacity_store import ProviderCapacityStore
    from src.promotional_dispatch import configure_verified_offer
    from src.endpoint_resolver import resolve_endpoint_by_id
    from src.llm_core import _detect_provider
    auth = read_json(manifest['auth_file'])
    key = auth.get('opencode-go', {}).get('key')
    if not isinstance(key, str) or not key.strip(): raise ValueError('existing OpenCode Go credential missing')
    db = SessionLocal()
    try:
        if any(ep.id != ENDPOINT for ep in db.query(ModelEndpoint).all()) or any(p.id != PROFILE for p in db.query(RoutingModelProfile).all()):
            raise ValueError('isolated runtime contains unrelated endpoints or profiles')
        endpoint = db.get(ModelEndpoint, ENDPOINT)
        if endpoint is None: endpoint = ModelEndpoint(id=ENDPOINT, name='OpenCode Go free scout', base_url=BASE_URL); db.add(endpoint)
        endpoint.base_url = BASE_URL; endpoint.api_key = key; endpoint.is_enabled = True; endpoint.provider_auth_id = None
        db.flush()
        profile = db.get(RoutingModelProfile, PROFILE)
        if profile is None: profile = RoutingModelProfile(id=PROFILE, model=MODEL, roles='[]'); db.add(profile)
        profile.model_endpoint_id = ENDPOINT; profile.model = MODEL
        profile.roles = json.dumps(['scout', 'reviewer', 'planner', 'debugger'])
        profile.context_window = 32768; profile.max_output_tokens = 256
        profile.is_free = True; profile.is_premium = False; profile.enabled = True
        profile.input_cost_per_mtok = 0; profile.output_cost_per_mtok = 0
        db.commit()
    finally: db.close()
    capacity_dir = private_dir(root / 'capacity')
    capacity = collect_endpoint_capacity(ProviderCapacityStore(str(capacity_dir)), ENDPOINT, MODEL)
    chat, model, headers = resolve_endpoint_by_id(ENDPOINT, MODEL)
    now = datetime.now(timezone.utc)
    terms = {'verified_by': 'official-opencode-go-table-parser-v1', 'provider': 'opencode-go',
        'credential_sha256': capacity.credential_sha256, 'endpoint_id': ENDPOINT, 'transport_provider': _detect_provider(chat),
        'pool_id': capacity.pool_id, 'native_model': MODEL, 'harness': 'odysseus-scout', 'usage_path': 'api-key',
        'tariff_id': 'opencode-go-space-bunny-free', 'tariff_version': source_hash,
        'source_url': SOURCE_URL, 'source_sha256': source_hash, 'observed_at': now.isoformat(), 'ttl_seconds': 300,
        'valid_from': now.isoformat(), 'valid_until': (now+timedelta(minutes=5)).isoformat(),
        'unit': 'request', 'offered_rate_usd': 0}
    terms_path = root / 'verified-terms.json'; write_json(terms_path, terms)
    config = configure_verified_offer(terms_path=terms_path, source_path=source, capacity_store=capacity_dir,
        profile_id=PROFILE, chat_url=chat, directory=root / ('offer-' + uuid.uuid4().hex))
    os.environ['ODYSSEUS_FREE_OFFER_CONFIG'] = config
    write_json(root / 'last-preflight.json', {'observed_at': now.isoformat(), 'model': model, 'source_sha256': source_hash,
        'capacity_ref': capacity.ref, 'offer_config': config, 'cash_price_usd': 0, 'callability': 'not_established_by_preflight'})


def prepare(root, task_path, auth_file, export_directory):
    if (root / 'runtime.json').exists(): raise ValueError('runtime already exists; use preview or run')
    task = read_json(task_path)
    if task.get('dataSensitivity') != 'public': raise ValueError('this free scout runtime requires an explicitly public task')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', task.get('id', '')): raise ValueError('explicit stable task ID required')
    task['routing'] = {'allowFreeModels': True, 'allowPaidModels': False, 'allowPremiumModels': False, 'maxAttempts': 1, 'maxCostUsd': 0}
    if not Path(task.get('repoPath', '')).is_absolute(): raise ValueError('absolute repository path required')
    write_json(root / 'task.json', task)
    manifest = {'schema_version': 1, 'session_id': 'odysseus-' + hashlib.sha256(task['id'].encode()).hexdigest(),
        'auth_file': str(Path(auth_file).expanduser().resolve()), 'task_sha256': hashlib.sha256((root/'task.json').read_bytes()).hexdigest()}
    write_json(root / 'usage-export.json', {'directory': str(Path(export_directory).expanduser().resolve()),
        'cohort': 'routing-connectivity', 'profiles': {PROFILE: 'opencode-go'}})
    refresh(root, manifest); write_json(root / 'runtime.json', manifest)


def main(argv=None):
    previous_umask = os.umask(0o077)
    try:
        return _main(argv)
    finally:
        os.umask(previous_umask)


def _main(argv=None):
    parser = argparse.ArgumentParser(description='Isolated public Space Bunny scout; verifies current free terms and quota, never paid fallback')
    parser.add_argument('action', choices=['prepare', 'preview', 'run'])
    parser.add_argument('--directory', required=True)
    parser.add_argument('--task')
    parser.add_argument('--auth-file', default='~/.local/share/opencode/auth.json')
    parser.add_argument('--export-directory', default='~/.local/state/tmos-ai-usage/outcome-events')
    args = parser.parse_args(argv)
    root = private_dir(args.directory)
    if args.action == 'prepare':
        if not args.task: raise ValueError('--task is required for prepare')
        prepare(root, args.task, args.auth_file, args.export_directory)
        print(json.dumps({'status': 'prepared', 'runtime': str(root), 'model': MODEL, 'inference_requests': 0})); return 0
    manifest = read_json(root / 'runtime.json')
    if hashlib.sha256((root / 'task.json').read_bytes()).hexdigest() != manifest['task_sha256']: raise ValueError('prepared task changed')
    if (root / 'blocked.json').exists(): raise ValueError('previous run failed; inspect the private result before preparing a new runtime')
    if args.action == 'run' and (root / 'attempt-started.json').exists(): raise ValueError('one-shot attempt already started; inspect results before preparing a new runtime')
    refresh(root, manifest)
    repo = Path(__file__).resolve().parents[1]
    if args.action == 'preview':
        command = [sys.executable, str(repo/'scripts/odysseus-route'), 'preview', '--task', str(root/'task.json')]
    else:
        command = [sys.executable, str(repo/'scripts/odysseus-run'), '--task', str(root/'task.json'), '--mode', 'scout', '--models', PROFILE, '--max-attempts', '1', '--budget', '0']
    if args.action == 'run': start_attempt(root)
    result = subprocess.run(command, capture_output=True, text=True, timeout=150, env=dict(os.environ))
    write_bytes(root / 'last-cli.stdout', result.stdout.encode()); write_bytes(root / 'last-cli.stderr', result.stderr.encode())
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    status = payload.get('status', 'previewed' if args.action == 'preview' and result.returncode == 0 else 'failed')
    if args.action == 'run' and status != 'succeeded': write_json(root/'blocked.json', {'reason': 'run_did_not_succeed', 'exit_code': result.returncode})
    print(json.dumps({'status': status, 'runtime': str(root), 'model': MODEL,
        'candidate_count': len(payload.get('candidates', [])) if args.action == 'preview' else None,
        'result_path': str(root/'last-cli.stdout'), 'paid_fallback': False}))
    return 0 if status in ('succeeded', 'previewed') else 1


if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'status': 'refused', 'reason': type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
