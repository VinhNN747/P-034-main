"""Two-app functional checks on the original DB; credentials stay in stdin/memory."""
from __future__ import annotations

import json
from decimal import ROUND_UP, Decimal
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from openai import APIStatusError, AuthenticationError, OpenAI

from src.scripts.openai_audit_session import AuditSession
from src.scripts.openai_staging_live_audit import credentials_from_stdin


def main():
    credentials = credentials_from_stdin()
    cfg = dotenv_values('.env')
    run = uuid4().hex
    report = {'run_id': run, 'evidence_kind': 'original-database-real-provider',
              'production_schema_modified': False, 'sdk_python': '2.54.0', 'cases': [], 'cleanup': []}
    with AuditSession(cfg['SUPABASE_URL'], cfg['SUPABASE_PUBLISHABLE_KEY'],
                      'http://127.0.0.1:18000/api/v1') as management:
        management.login(credentials['email'], credentials['password'])
        apps = {label: management.resolve_application(label, key) for label, key in credentials['keys'].items()}
        clients = {label: OpenAI(base_url='http://127.0.0.1:18001/v1', api_key=key, max_retries=0,
                   timeout=60, _strict_response_validation=True) for label, key in credentials['keys'].items()}
        raw = httpx.Client(timeout=30)
        def case(label, name, action):
            try:
                result = action() or {}
                value = {'app': label, 'case_id': name, 'status': 'pass', **result}
            except Exception as error:
                value = {'app': label, 'case_id': name, 'status': 'fail', 'error_class': type(error).__name__,
                         'http_status': getattr(error, 'status_code', None)}
            report['cases'].append(value)
            print(json.dumps(value), flush=True)
        def receipt(label, request_id):
            response = raw.get('http://127.0.0.1:18001/api/v1/runtime/requests/' + request_id,
                              headers={'Authorization': 'Bearer ' + credentials['keys'][label]})
            assert response.status_code == 200
            return response.json()
        def accounting(label, request_id, value):
            usage = management.management('GET', '/gateway/usage',
                params={'application_id': apps[label], 'page_size': 100}).json()['items']
            matching = [row for row in usage if row['request_id'] == request_id]
            assert len(matching) == 1
            cost = Decimal(str(value['cost']['estimated_cost']))
            assert abs(Decimal(str(matching[0]['cost'])) - cost) < Decimal('0.000000000001')
            transactions = management._items('/billing/transactions')
            debits = [row for row in transactions if row.get('reference') == request_id]
            if cost > 0:
                assert len(debits) == 1
                assert abs(Decimal(str(debits[0]['amount']))) == cost.quantize(Decimal('0.000001'), rounding=ROUND_UP)
            return {'request_id': request_id, 'usage_events': len(matching), 'debits': len(debits), 'cost_usd': str(cost)}
        try:
            embedding_id = json.loads(Path('docs/openai-sdk/evidence/2026-10-08-embedding-config.json').read_text())['model_id']
            for label, app_id in apps.items():
                config = management.management('GET', f'/applications/{app_id}/gateway-config').json()
                if config['model_pool_id']:
                    pool_id = config['model_pool_id']
                    pool = management.management('GET', f'/gateway/model-pools/{pool_id}').json()
                    snapshot = {key: pool[key] for key in ('name', 'purpose', 'status')}
                    snapshot['members'] = [{'model_id': member['model_id']} for member in pool['members']]
                    if embedding_id not in {member['model_id'] for member in snapshot['members']}:
                        management.register_cleanup(label + '-embedding-membership',
                            lambda pool_id=pool_id, snapshot=snapshot: management.management('PUT', f'/gateway/model-pools/{pool_id}', json=snapshot))
                        management.management('PUT', f'/gateway/model-pools/{pool_id}', json={
                            **snapshot, 'members': snapshot['members'] + [{'model_id': embedding_id}]})
                client = clients[label]
                other = next(other for other in apps if other != label)
                def auth_and_revoke():
                    created = management.management('POST', f'/applications/{app_id}/api-keys', json={
                        'name': 'sdk-audit-' + run[:12]}).json()
                    key_id = created['id']
                    path = f'/applications/{app_id}/api-keys/{key_id}/revoke'
                    management.register_cleanup(label + '-temporary-key-revoked',
                        lambda path=path: management.management('POST', path))
                    with OpenAI(base_url='http://127.0.0.1:18001/v1', api_key=created['raw_key'], max_retries=0) as temporary:
                        assert temporary.models.list().data
                        management.management('POST', path)
                        try:
                            temporary.models.list()
                        except AuthenticationError as error:
                            assert error.status_code == 401 and error.request_id
                        else:
                            raise AssertionError('Revoked key still authorized')
                    assert client.models.list().data
                    return {'temporary_key_revoked': True, 'original_key_active': True}
                case(label, 'temporary-key-revocation', auth_and_revoke)
                def chat():
                    model = 'gpt-5.4-nano' if label == 'app_1' else 'gpt-4o-mini'
                    args = {'model': model, 'messages': [{'role': 'user', 'content': 'Say hello.'}],
                            'max_completion_tokens': 48, 'extra_headers': {'Idempotency-Key': 'live-chat-' + run}}
                    response = client.chat.completions.create(**args)
                    replay = client.chat.completions.create(**args)
                    assert response.model_dump() == replay.model_dump() and response.usage.completion_tokens <= 48
                    try:
                        client.chat.completions.create(**{**args, 'max_completion_tokens': 24})
                    except APIStatusError as error:
                        assert error.status_code == 409
                    else:
                        raise AssertionError('Changed-body replay did not conflict')
                    value = receipt(label, response.id)
                    assert value['usage']['final_output']['total_tokens'] == response.usage.total_tokens
                    foreign = raw.get('http://127.0.0.1:18001/api/v1/runtime/requests/' + response.id,
                        headers={'Authorization': 'Bearer ' + credentials['keys'][other]})
                    assert foreign.status_code == 404
                    return {**accounting(label, response.id, value), 'replay': True, 'conflict': True,
                            'foreign_receipt_404': True, 'output_tokens': response.usage.completion_tokens,
                            'finish_reason': response.choices[0].finish_reason}
                case(label, 'chat-cap-conflict-isolation-accounting', chat)
                def embedding():
                    args = {'model': 'text-embedding-3-small', 'input': 'one', 'dimensions': 256,
                            'encoding_format': 'float', 'extra_headers': {'Idempotency-Key': 'live-embedding-' + run}}
                    response = client.embeddings.create(**args)
                    replay = client.embeddings.create(**args)
                    assert response.model_dump() == replay.model_dump() and len(response.data[0].embedding) == 256
                    try:
                        client.embeddings.create(**{**args, 'input': 'two'})
                    except APIStatusError as error:
                        assert error.status_code == 409
                    else:
                        raise AssertionError('Changed embedding did not conflict')
                    value = receipt(label, response._request_id)
                    result = accounting(label, response._request_id, value)
                    return {**result, 'replay': True, 'conflict': True}
                case(label, 'embedding-shared-key-conflict-accounting', embedding)
        finally:
            for client in clients.values():
                client.close()
            raw.close()
    report['cleanup'] = management.cleanup_results
    # Assert different applications using the same key string were not merged.
    for name in ('chat-cap-conflict-isolation-accounting', 'embedding-shared-key-conflict-accounting'):
        ids = [case['request_id'] for case in report['cases'] if case['case_id'] == name and case['status'] == 'pass']
        report['cases'].append({'case_id': name + '-cross-app-key-scope',
                                'status': 'pass' if len(ids) == 2 and len(set(ids)) == 2 else 'fail'})
    Path('docs/openai-sdk/evidence/2026-10-08-original-live-security.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'cases': len(report['cases']), 'failures': sum(c['status'] != 'pass' for c in report['cases']),
                      'cleanup': report['cleanup']}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'setup_status': 'fail', 'error_class': type(error).__name__}), flush=True)
        raise SystemExit(1) from None
