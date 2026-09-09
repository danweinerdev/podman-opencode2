#!/usr/bin/env python3
"""Pinned-runtime usage feasibility probe. HTTP only; never opens SQLite.

Run in an isolated image with this repository mounted at /workspace:
  python3 /workspace/tests/usage-stats-probe.py --output /evidence/usage-probe.json
Uses synthetic loopback providers; does not read provider credentials.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
import urllib.request


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    mock = load('usage_mock', 'mock-model-router-provider.py')
    driver = load('usage_driver', 'model-router-dispatch-driver.py')
    result = {'runtime': subprocess.check_output(['opencode2', '--version'], text=True).strip(),
              'cases': [], 'assertions': []}
    with tempfile.TemporaryDirectory(prefix='usage-probe.') as tmp:
        root = Path(tmp)
        state = mock.State(root / 'requests.jsonl')
        original_chunk = mock.completion_chunk
        def chunk(model, delta, finish_reason=None):
            value = original_chunk(model, delta, finish_reason)
            if finish_reason and model != 'no-usage':
                value['usage'] = {'prompt_tokens': 120, 'completion_tokens': 30, 'total_tokens': 150,
                                  'prompt_tokens_details': {'cached_tokens': 20},
                                  'completion_tokens_details': {'reasoning_tokens': 8}}
            if finish_reason and model == 'zero-usage':
                value['usage'] = {'prompt_tokens':0, 'completion_tokens':0, 'total_tokens':0}
            return value
        mock.completion_chunk = chunk
        servers = [ThreadingHTTPServer(('127.0.0.1', 18081+i), mock.handler_for(role, state))
                   for i, role in enumerate(['parent', 'worker'])]
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        config = json.loads(Path('/workspace/container-config.json').read_text())
        config.pop('$schema', None)
        config['mcp'] = {'servers': {}}
        config['agents']['title']['model'] = 'fake-parent/parent-model'
        opts = config['plugins'][0]['options']
        opts['pin_default_agent_model'] = False
        opts['profiles'] = {name: {'model': 'fake-worker/worker-model'} for name in opts['profiles']}
        # Primary route intentionally differs from actual session selection.
        config['provider'] = {}
        for role, npm, port in [('parent', '@ai-sdk/deepseek', 18081),
                                ('worker', '@ai-sdk/openai-compatible', 18082)]:
            config['provider']['fake-'+role] = {
                'npm': npm, 'name': 'Synthetic '+role,
                'options': {'baseURL': f'http://127.0.0.1:{port}/v1', 'apiKey': 'synthetic-only'},
                'models': {role+'-model': {'name': role, 'limit': {'context': 32000, 'output': 4096},
                                          'cost': {'input': 2, 'output': 4, 'cache_read': 1, 'cache_write': 3}}}}
        for name in ['no-usage', 'zero-usage', 'unpriced']:
            config['provider']['fake-worker']['models'][name] = {'name':name, 'limit':{'context':32000,'output':4096}}
        cfg = root / 'config.json'
        cfg.write_text(json.dumps(config))
        env = {**os.environ, 'OPENCODE_CONFIG': str(cfg), 'OPENCODE_SERVER_PASSWORD': 'usage-probe-only',
               'XDG_DATA_HOME': str(root/'data'), 'XDG_STATE_HOME': str(root/'state'),
               'OPENCODE_MODEL_ROUTER_CONFIG': '', 'OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG': ''}
        driver.BASE_URL = 'http://127.0.0.1:18083'
        import base64
        driver.AUTH = 'Basic '+base64.b64encode(b'opencode:usage-probe-only').decode()
        log = (root/'server.log').open('w+')
        def start():
            process = subprocess.Popen(['opencode2', 'serve', '--hostname', '127.0.0.1', '--port', '18083'],
                                       env=env, stdout=log, stderr=log, cwd='/workspace')
            driver.poll_until('server', 400, lambda: driver.api('GET', driver.BASE_URL+'/api/health') is not None)
            return process
        def history_for(sid):
            request = urllib.request.Request(driver.BASE_URL+f'/api/session/{sid}/history?limit=100', headers={'Authorization':driver.AUTH})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                return {'http_error':error.code, 'body':error.read().decode()[:500]}
        process = None
        try:
            process = start()
            for mode in ['foreground', 'background']:
                parent = driver.dispatch(mode)
                children = driver.children_for(parent)
                assert children, 'missing child'
                child = next(c for c in children if c['agent']=='extractor')
                driver.api('POST', driver.BASE_URL+f"/api/session/{child['id']}/wait", body=b'')
                sessions = []
                for sid in [parent, child['id']]:
                    info = driver.api_json('GET', driver.BASE_URL+f'/api/session/{sid}')['data']
                    messages = driver.api_json('GET', driver.BASE_URL+f'/api/session/{sid}/message?limit=100&order=asc')
                    history = history_for(sid)
                    sessions.append({'session': {k: info.get(k) for k in ['id','parentID','projectID','agent','model','tokens','cost']},
                        'messages': [{k:m.get(k) for k in ['id','type','agent','model','time','tokens','cost','finish','error']}
                                     for m in messages.get('data',[])], 'history': history})
                result['cases'].append({'mode': mode, 'sessions': sessions})
            result['edge_cases'] = []
            for model in ['no-usage','zero-usage','unpriced']:
                created = driver.api_json('POST', driver.BASE_URL+'/api/session', body=json.dumps({'agent':'orchestrator','model':{'providerID':'fake-worker','id':model},'location':{'directory':'/workspace'}}).encode(), content_type='application/json')['data']
                sid = created['id']
                driver.api('POST', driver.BASE_URL+f'/api/session/{sid}/prompt', body=b'{"text":"USAGE_ONLY"}', content_type='application/json')
                driver.api('POST', driver.BASE_URL+f'/api/session/{sid}/wait', body=b'')
                msgs = driver.api_json('GET', driver.BASE_URL+f'/api/session/{sid}/message?limit=100&order=asc')['data']
                result['edge_cases'].append({'model':model,'messages':[{k:m.get(k) for k in ['id','type','agent','model','tokens','cost','finish']} for m in msgs if m.get('type')=='assistant']})
            # Finish background follow-ups before taking the restart baseline.
            time.sleep(1)
            fields = ['id','type','agent','model','time','tokens','cost','finish','error']
            for case in result['cases']:
                for session in case['sessions']:
                    sid=session['session']['id']
                    driver.api('POST', driver.BASE_URL+f'/api/session/{sid}/wait', body=b'')
                    msgs=driver.api_json('GET',driver.BASE_URL+f'/api/session/{sid}/message?limit=100&order=asc')['data']
                    session['messages']=[{k:m.get(k) for k in fields} for m in msgs]
            # Retain the server-managed data through restart; only use public HTTP reads.
            before = {s['session']['id']:s['history'] for c in result['cases'] for s in c['sessions']}
            process.terminate(); process.wait(timeout=10)
            process = start()
            after = {sid:history_for(sid) for sid in before}
            result['restart_history_equal'] = before == after and all('http_error' not in h for h in before.values())
            result['restart_messages_equal'] = all(s['messages'] == [{k:m.get(k) for k in ['id','type','agent','model','time','tokens','cost','finish','error']} for m in driver.api_json('GET', driver.BASE_URL+f"/api/session/{s['session']['id']}/message?limit=100&order=asc").get('data',[])] for c in result['cases'] for s in c['sessions'])
            result['pagination'] = []
            for sid in before:
                page=driver.api_json('GET',driver.BASE_URL+f'/api/session/{sid}/message?limit=1&order=asc')
                ids=[m['id'] for m in page.get('data',[])]
                seen=set()
                while page.get('cursor',{}).get('next'):
                    cursor=page['cursor']['next']
                    assert cursor not in seen, 'pagination cursor loop'
                    seen.add(cursor)
                    page=driver.api_json('GET',driver.BASE_URL+f'/api/session/{sid}/message?limit=1&cursor='+urllib.parse.quote(cursor,safe=''))
                    ids.extend(m['id'] for m in page.get('data',[]))
                expected=next(s['messages'] for c in result['cases'] for s in c['sessions'] if s['session']['id']==sid)
                result['pagination'].append({'sessionID':sid,'matches_full_read':ids==[m['id'] for m in expected], 'records':len(ids)})
            result['provider_requests']=[json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
            # Keep only usage-event metadata in the evidence file; never persist event content.
            for case in result['cases']:
                for session in case['sessions']:
                    hist = session.pop('history')
                    session['history_error'] = hist if 'http_error' in hist else None
                    session['history_shape'] = {k: type(v).__name__ for k,v in hist.items()}
                    session['history_usage'] = []
                    for event in hist.get('data', hist.get('events', [])):
                        typ = event.get('type', '')
                        if 'step' in typ:
                            session['history_usage'].append(event)
                    session['history_event_types'] = sorted(set(e.get('type','') for e in hist.get('data',hist.get('events',[]))))
            checks = {
                'runtime_pin': result['runtime'] == 'opencode2 v0.0.0-beta-19234',
                'message_restart_recovery': result['restart_messages_equal'],
                'message_pagination': all(p['matches_full_read'] for p in result['pagination']),
                'history_endpoint_unavailable': all(s['history_error'] and s['history_error']['http_error']==404 for c in result['cases'] for s in c['sessions']),
                'missing_and_zero_collapsed': result['edge_cases'][0]['messages'][0]['tokens'] == result['edge_cases'][1]['messages'][0]['tokens'],
                'unpriced_cost_zero': result['edge_cases'][2]['messages'][0]['cost'] == 0,
                'actual_model_attribution': all(m['model']['providerID']==('fake-parent' if i==0 else 'fake-worker') for c in result['cases'] for i,s in enumerate(c['sessions']) for m in s['messages'] if m['type']=='assistant'),
                'worker_parent_links': all(c['sessions'][1]['session']['parentID']==c['sessions'][0]['session']['id'] for c in result['cases']),
                'compatible_token_normalization': all(m['tokens']=={'input':100,'output':22,'reasoning':8,'cache':{'read':20,'write':0}} for c in result['cases'] for m in c['sessions'][1]['messages'] if m['type']=='assistant'),
            }
            result['assertions'] = checks
            assert all(checks.values()), checks
            Path(args.output).write_text(json.dumps(result, indent=2)+'\n')
            print(json.dumps({'output': args.output, 'runtime': result['runtime'], 'restart_history_equal':result['restart_history_equal'],
                              'sessions':len(before)}))
        except BaseException:
            log.flush(); log.seek(0)
            print(log.read()[-10000:])
            raise
        finally:
            if process is not None:
                process.terminate(); process.wait(timeout=10)
            for server in servers:
                server.shutdown()
            log.close()


if __name__ == '__main__':
    main()
