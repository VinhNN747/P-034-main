"""Two-app functional embedding audit. Credentials come from stdin only.

Run interactively; prompts disable echo. Reports contain assertions/IDs only.
"""
from __future__ import annotations

import base64
import json
import struct
import sys
import termios
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from openai import OpenAI

from src.scripts.openai_audit_session import AuditSession


def main():
    attr = termios.tcgetattr(sys.stdin.fileno())
    quiet = attr.copy()
    quiet[3] &= ~termios.ECHO
    termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, quiet)
    print('Credential input ready', flush=True)
    credentials = json.loads(sys.stdin.readline())
    termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, attr)
    cfg = dotenv_values('.env')
    model_id = json.loads(Path('docs/openai-sdk/evidence/2026-10-08-embedding-config.json').read_text())['model_id']
    report = {'evidence_kind': 'live', 'strict_sdk_base64_limitation': 'Explicit base64 is a string on the wire; openai2.54 strict validates embedding as list[float]', 'sdk_version': '2.54.0', 'cases': [], 'cleanup': []}
    with AuditSession(cfg['SUPABASE_URL'], cfg['SUPABASE_PUBLISHABLE_KEY'],
                      'http://127.0.0.1:18000/api/v1') as session:
        session.login(credentials['email'], credentials['password'])
        apps = {label: session.resolve_application(label, key) for label, key in credentials['keys'].items()}
        for label, aid in apps.items():
            conf = session.management('GET', f'/applications/{aid}/gateway-config').json()
            if conf['model_pool_id']:
                pool_id = conf['model_pool_id']
                pool = session.management('GET', f'/gateway/model-pools/{pool_id}').json()
                snapshot = {k: pool[k] for k in ('name', 'purpose', 'status')}
                snapshot['members'] = [{'model_id': m['model_id']} for m in pool['members']]
                if model_id not in {m['model_id'] for m in snapshot['members']}:
                    session.register_cleanup('embedding-pool-membership',
                        lambda pool_id=pool_id, snapshot=snapshot: session.management('PUT', f'/gateway/model-pools/{pool_id}', json=snapshot))
                    session.management('PUT', f'/gateway/model-pools/{pool_id}',
                        json={**snapshot, 'members': snapshot['members'] + [{'model_id': model_id}]})
        clients = {label: OpenAI(base_url='http://127.0.0.1:18001/v1', api_key=key,
            timeout=60, max_retries=0, _strict_response_validation=True) for label, key in credentials['keys'].items()}
        normal_clients = {label: OpenAI(base_url='http://127.0.0.1:18001/v1', api_key=key, timeout=60, max_retries=0) for label, key in credentials['keys'].items()}
        try:
            for label, client in clients.items():
                for encoding, dimension, input_value in [('float', 256, ['one', 'two']), ('base64', 256, ['one', 'two']),
                    ('float', None, 'native'), ('float', 256, [[15339]])]:
                    key = 'embedding-audit-' + uuid4().hex
                    args = {'model': 'text-embedding-3-small', 'input': input_value, 'encoding_format': encoding,
                        'extra_headers': {'Idempotency-Key': key}}
                    if dimension is not None:
                        args['dimensions'] = dimension
                    try:
                        chosen = normal_clients[label] if encoding == 'base64' else client
                        response = chosen.embeddings.create(**args)
                        replay = chosen.embeddings.create(**args)
                        expected_count = len(input_value) if isinstance(input_value, list) else 1
                        actual = [len(struct.unpack('<' + 'f' * (len(base64.b64decode(item.embedding)) // 4), base64.b64decode(item.embedding)))
                                  if isinstance(item.embedding, str) else len(item.embedding) for item in response.data]
                        assert actual == [dimension or 1536] * expected_count
                        assert [item.index for item in response.data] == list(range(expected_count))
                        assert replay.model_dump(warnings=False) == response.model_dump(warnings=False)
                        assert replay._request_id == response._request_id
                        assert response.usage.prompt_tokens > 0
                        assert response.usage.total_tokens == response.usage.prompt_tokens
                        report['cases'].append({'app': label, 'encoding': encoding, 'dimensions': dimension or 1536,
                            'request_id': response._request_id, 'status': 'pass', 'replay': True, 'strict_validation': encoding != 'base64',
                            'usage': {'prompt_tokens': response.usage.prompt_tokens}})
                    except Exception as exc:
                        report['cases'].append({'app': label, 'encoding': encoding, 'dimensions': dimension or 1536,
                            'status': 'fail', 'error_class': type(exc).__name__, 'http_status': getattr(exc, 'status_code', None), 'error_code': getattr(exc, 'code', None),
                            'request_id': getattr(exc, 'request_id', None)})
        finally:
            for client in [*clients.values(), *normal_clients.values()]:
                client.close()
    report['cleanup'] = session.cleanup_results
    Path('docs/openai-sdk/evidence/2026-10-08-two-app-embeddings.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
