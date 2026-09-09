#!/usr/bin/env python3
"""Standalone-mode collector transport and lifecycle probe. HTTP only; never opens SQLite.

Answers the blocking open question in Specs/ModelRouterUsage:
"Verify collector transport and lifecycle in the actual standalone launcher,
or explicitly design a launcher integration that preserves its isolation."

Phase A -- as-shipped `opencode2 --standalone` (baked image config):
  - records the private server child's exact transport (cmdline + fds)
  - discovers the loopback TCP listener the server child opens, attributes
    it to the child process, and checks whether the port is stable/known
    (run twice; record whether a separate process can reach the API and
    what discovery mechanism, if any, exists)
  - checks where the server password lives (child environ vs. disk)
  - checks whether the server child exits with the CLI

Phase B -- launcher integration (the supported-path candidate):
  - launches `opencode2 serve --hostname 127.0.0.1 --port 18083` with
    OPENCODE_SERVER_PASSWORD and XDG_DATA_HOME at the launcher's data-volume
    path (/var/lib/opencode-data, a named volume)
  - a separate non-interactive `opencode2 run --server URL` client drives a
    real (mock-provider) dispatch: orchestrator -> extractor worker
  - a separate interactive TUI `opencode2 --server URL` client stays connected
  - a separate collector client (this script, plain HTTP) reads the same
    session's messages: model, agent, tokens, parent links
  - verifies auth enforcement and records the data layout

Phase C -- lifecycle:
  - the server survives client exit
  - restarting the server on the same data dir preserves the records

Run in the dev image with this repository mounted at /workspace and a fresh
named volume at the launcher's data path:
  podman run --rm --pull=never --userns=keep-id --security-opt label=disable \
    -v "$PWD:/workspace:ro" \
    -v "$PWD/.plans/Research/evidence/model-router-usage:/evidence" \
    -v opencode2-data-standalone-probe:/var/lib/opencode-data:U \
    -w /workspace --entrypoint python3 \
    localhost/ark-services-opencode2-dev:latest \
    /workspace/tests/usage-stats-standalone-probe.py --output /evidence/usage-standalone-probe.json

Uses synthetic loopback providers; does not read provider credentials.
"""
import argparse
import base64
import importlib.util
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

CANDIDATE_PORTS = (4096, 18083, 18081, 18082, 3000, 8080, 8000)


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sh(args, env=None, cwd='/workspace', timeout=120, stdin=subprocess.DEVNULL):
    """Run a command, return (exit_code, combined_output_tail)."""
    try:
        proc = subprocess.run(args, env=env, cwd=cwd, timeout=timeout,
                              stdin=stdin, capture_output=True, text=True)
        return proc.returncode, (proc.stdout + proc.stderr)[-4000:]
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b'') + (e.stderr or b'')).decode(errors='replace')
        return 'timeout', out[-4000:]


def http_status(url, headers=None, timeout=15):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors='replace')
    except OSError:
        return None, None


def http_json(url, headers=None, timeout=15):
    status, body = http_status(url, headers=headers, timeout=timeout)
    if status is None:
        return None, None
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


def process_cmdlines():
    """Map pid -> cmdline for every process in the container."""
    found = {}
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            cmdline = Path(f'/proc/{entry}/cmdline').read_bytes().replace(b'\0', b' ').decode().strip()
        except OSError:
            continue
        if cmdline:
            found[int(entry)] = cmdline
    return found


def ppid_of(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
    except OSError:
        return None
    return int(stat.rsplit(')', 1)[1].split()[1])


def find_server_child(ppid):
    """Find the `serve --stdio` child of the given CLI pid."""
    for pid, cmd in process_cmdlines().items():
        if 'serve --stdio' in cmd and ppid_of(pid) == ppid:
            return pid, cmd
    return None, None


def fd_map(pid):
    fds = {}
    for fd in os.listdir(f'/proc/{pid}/fd'):
        try:
            fds[fd] = os.readlink(f'/proc/{pid}/fd/{fd}')
        except OSError:
            pass
    return fds


def listen_entries():
    """All LISTEN sockets: list of (addr, port, inode)."""
    entries = []
    for path in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            text = Path(path).read_text()
        except OSError:
            continue
        for line in text.splitlines()[1:]:
            fields = line.split()
            if len(fields) > 9 and fields[3] == '0A':
                addr, _, port = fields[1].rpartition(':')
                entries.append((addr, int(port, 16), fields[9]))
    return entries


def owner_of_inode(inode):
    for pid in process_cmdlines():
        try:
            for fd in os.listdir(f'/proc/{pid}/fd'):
                try:
                    if os.readlink(f'/proc/{pid}/fd/{fd}') == f'socket:[{inode}]':
                        return pid
                except OSError:
                    pass
        except OSError:
            continue
    return None


def environ_of(pid, key):
    try:
        raw = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
    except OSError:
        return None
    for item in raw:
        if item.startswith(key.encode() + b'='):
            return item.split(b'=', 1)[1].decode()
    return None


def try_ports(ports):
    result = {}
    for port in ports:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=2):
                result[port] = 'connected'
        except OSError as e:
            result[port] = type(e).__name__
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()

    result = {'phases': {}, 'assertions': {}}
    result['runtime'] = subprocess.check_output(['opencode2', '--version'], text=True).strip()

    # ------------------------------------------------------------------ #
    # Phase A: as-shipped `opencode2 --standalone`                        #
    # ------------------------------------------------------------------ #
    phase_a = {'config': 'baked /etc/opencode/container-config.json (as-shipped image config)',
               'env_overrides': ['XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME'],
               'runs': [], 'well_known_port_connects': None,
               'api_attempts': {}, 'service_status': None,
               'password_on_disk_files': None, 'server_child_exits_with_cli': None}

    def start_standalone(tmpdir):
        root = Path(tmpdir)
        env = {**os.environ, 'XDG_DATA_HOME': str(root / 'data'),
               'XDG_STATE_HOME': str(root / 'state'), 'XDG_CACHE_HOME': str(root / 'cache')}
        log = (root / 'cli.log').open('w+')
        cli = subprocess.Popen(['opencode2', '--standalone', '--auto'],
                               env=env, stdout=log, stderr=log, cwd='/workspace')
        return cli, env, log

    def discover_run(tmpdir, run_index):
        """Start one standalone CLI, attribute the loopback listener, probe auth."""
        cli, env, log = start_standalone(tmpdir)
        run = {'run': run_index, 'port': None, 'port_addr': None, 'server_child': None,
               'server_child_fds': None, 'auth': {}, 'child_environ_password': None,
               'cli_environ_password': None, 'server_exited_with_cli': None}
        server_pid, server_cmd = None, None
        try:
            for _ in range(240):
                if cli.poll() is not None:
                    run['events'] = [f'CLI exited early with code {cli.returncode}']
                    break
                server_pid, server_cmd = find_server_child(cli.pid)
                if server_pid:
                    break
                time.sleep(0.25)
            run['server_child'] = {'pid': server_pid, 'cmdline': server_cmd}
            if server_pid:
                run['server_child_fds'] = fd_map(server_pid)
                time.sleep(2)
                for addr, port, inode in listen_entries():
                    if owner_of_inode(inode) == server_pid:
                        run['port'], run['port_addr'] = port, addr
                        break
                if run['port']:
                    base = f'http://127.0.0.1:{run["port"]}'
                    status, _ = http_status(base + '/api/health')
                    run['auth']['no_credentials'] = status
                    wrong = {'Authorization': 'Basic ' + base64.b64encode(b'opencode:wrong').decode()}
                    status, _ = http_status(base + '/api/health', headers=wrong)
                    run['auth']['wrong_password'] = status
                    child_pw = environ_of(server_pid, 'OPENCODE_PASSWORD')
                    run['child_environ_password'] = {'present': child_pw is not None,
                                                     'length': len(child_pw) if child_pw else 0}
                    if child_pw:
                        headers = {'Authorization': 'Basic '
                                   + base64.b64encode(f'opencode:{child_pw}'.encode()).decode()}
                        status, _ = http_status(base + '/api/health', headers=headers)
                        run['auth']['child_environ_password'] = status
                    run['cli_environ_password'] = {
                        'OPENCODE_PASSWORD': environ_of(cli.pid, 'OPENCODE_PASSWORD') is not None,
                        'OPENCODE_SERVER_PASSWORD': environ_of(cli.pid, 'OPENCODE_SERVER_PASSWORD') is not None}
            # Lifecycle: server child must die when the CLI is killed.
            if server_pid and cli.poll() is None:
                cli.send_signal(signal.SIGTERM)
                for _ in range(40):
                    if not Path(f'/proc/{server_pid}').exists():
                        run['server_exited_with_cli'] = True
                        break
                    time.sleep(0.25)
                if run['server_exited_with_cli'] is None:
                    cli.kill()
            cli.wait(timeout=15)
            time.sleep(2)  # let the api --standalone leftovers settle
        finally:
            if cli.poll() is None:
                cli.kill()
                cli.wait(timeout=10)
            log.close()
        return run

    with tempfile.TemporaryDirectory(prefix='standalone-probe-a.') as tmp:
        run1 = discover_run(tmp, 1)
        phase_a['runs'].append(run1)

        # Where does the password live? Scan both XDG trees for anything that
        # looks like credential/service metadata.
        suspects = []
        for base in (Path(tmp) / 'data', Path(tmp) / 'state'):
            for p in base.rglob('*'):
                name = p.name.lower()
                if any(s in name for s in ('password', 'secret', 'token', 'auth', 'service')):
                    suspects.append(str(p.relative_to(base)))
        phase_a['password_on_disk_files'] = suspects

        # Can a separate shipped client reach the running instance at all?
        env = {**os.environ, 'XDG_DATA_HOME': str(Path(tmp) / 'data')}
        code, out = sh(['opencode2', 'api', 'GET', '/api/health'], env=env, timeout=60)
        phase_a['api_attempts']['opencode2 api (background service default)'] = {
            'exit': code, 'output_tail': out[-800:]}
        code, out = sh(['opencode2', 'api', '--standalone', 'GET', '/api/health'], env=env, timeout=60)
        own_pid = None
        try:
            own_pid = json.loads(out).get('pid')
        except (ValueError, AttributeError):
            pass
        phase_a['api_attempts']['opencode2 api --standalone'] = {
            'exit': code, 'server_pid_in_response': own_pid,
            'output_tail': out[-800:]}
        code, out = sh(['opencode2', 'service', 'status'], env=env, timeout=30)
        phase_a['service_status'] = {'exit': code, 'output_tail': out[-400:]}

        run2 = discover_run(tmp, 2)
        phase_a['runs'].append(run2)
        phase_a['well_known_port_connects'] = try_ports(CANDIDATE_PORTS)
        phase_a['server_child_exits_with_cli'] = (
            run1['server_exited_with_cli'] is True and run2['server_exited_with_cli'] is True)

    # ------------------------------------------------------------------ #
    # Phase B: launcher integration (serve + --server client + collector) #
    # ------------------------------------------------------------------ #
    phase_b = {'config': 'container-config.json with mock loopback providers',
               'data_layout': None, 'auth': {}, 'clients': {},
               'collector_read': None, 'sessions': []}

    tmp2 = tempfile.mkdtemp(prefix='standalone-probe-b.')
    root = Path(tmp2)
    mock = load('usage_mock', 'mock-model-router-provider.py')
    state = mock.State(root / 'requests.jsonl')
    original_chunk = mock.completion_chunk

    def chunk(model, delta, finish_reason=None):
        value = original_chunk(model, delta, finish_reason)
        if finish_reason and model != 'no-usage':
            value['usage'] = {'prompt_tokens': 120, 'completion_tokens': 30, 'total_tokens': 150,
                              'prompt_tokens_details': {'cached_tokens': 20},
                              'completion_tokens_details': {'reasoning_tokens': 8}}
        return value
    mock.completion_chunk = chunk
    mock_server = ThreadingHTTPServer(('127.0.0.1', 18081), mock.handler_for('parent', state))
    threading.Thread(target=mock_server.serve_forever, daemon=True).start()
    mock_server2 = ThreadingHTTPServer(('127.0.0.1', 18082), mock.handler_for('worker', state))
    threading.Thread(target=mock_server2.serve_forever, daemon=True).start()

    config = json.loads(Path('/workspace/container-config.json').read_text())
    config.pop('$schema', None)
    config['mcp'] = {'servers': {}}
    config['agents']['title']['model'] = 'fake-parent/parent-model'
    opts = config['plugins'][0]['options']
    opts['pin_default_agent_model'] = False
    opts['profiles'] = {name: {'model': 'fake-worker/worker-model'} for name in opts['profiles']}
    config['provider'] = {
        'fake-parent': {
            'npm': '@ai-sdk/deepseek', 'name': 'Synthetic parent',
            'options': {'baseURL': 'http://127.0.0.1:18081/v1', 'apiKey': 'synthetic-only'},
            'models': {'parent-model': {'name': 'parent', 'limit': {'context': 32000, 'output': 4096},
                                        'cost': {'input': 2, 'output': 4, 'cache_read': 1, 'cache_write': 3}}}},
        'fake-worker': {
            'npm': '@ai-sdk/openai-compatible', 'name': 'Synthetic worker',
            'options': {'baseURL': 'http://127.0.0.1:18082/v1', 'apiKey': 'synthetic-only'},
            'models': {'worker-model': {'name': 'worker', 'limit': {'context': 32000, 'output': 4096},
                                        'cost': {'input': 2, 'output': 4, 'cache_read': 1, 'cache_write': 3}}}}}
    cfg = root / 'config.json'
    cfg.write_text(json.dumps(config))

    PASSWORD = 'standalone-probe-only'
    env = {**os.environ, 'OPENCODE_CONFIG': str(cfg), 'OPENCODE_SERVER_PASSWORD': PASSWORD,
           'XDG_DATA_HOME': '/var/lib/opencode-data', 'XDG_STATE_HOME': '/var/lib/opencode-data/state',
           'XDG_CACHE_HOME': str(root / 'cache'),  # launcher leaves cache in the container home
           'OPENCODE_MODEL_ROUTER_CONFIG': '', 'OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG': ''}
    BASE = 'http://127.0.0.1:18083'
    AUTH = {'Authorization': 'Basic ' + base64.b64encode(f'opencode:{PASSWORD}'.encode()).decode()}

    os.makedirs('/var/lib/opencode-data', exist_ok=True)
    log = (root / 'server.log').open('w+')
    server = subprocess.Popen(['opencode2', 'serve', '--hostname', '127.0.0.1', '--port', '18083'],
                              env=env, stdout=log, stderr=log, cwd='/workspace')
    try:
        def healthy():
            status, _ = http_json(BASE + '/api/health', headers=AUTH)
            return status == 200
        ok = False
        for _ in range(240):
            if healthy():
                ok = True
                break
            time.sleep(0.25)
        if not ok:
            raise RuntimeError('serve did not become healthy')

        # Auth enforcement: no auth, wrong password, correct password.
        status, _ = http_status(BASE + '/api/health')
        phase_b['auth']['no_credentials'] = status
        wrong = {'Authorization': 'Basic ' + base64.b64encode(b'opencode:wrong').decode()}
        status, _ = http_status(BASE + '/api/health', headers=wrong)
        phase_b['auth']['wrong_password'] = status
        status, _ = http_status(BASE + '/api/health', headers=AUTH)
        phase_b['auth']['correct_password'] = status

        # Which client auth forms does the shipped CLI accept against --server?
        code, out = sh(['opencode2', 'api', '--server', BASE, 'GET', '/api/health'],
                       env={k: v for k, v in env.items() if k != 'OPENCODE_SERVER_PASSWORD'}, timeout=60)
        phase_b['clients']['api --server, no auth env'] = {'exit': code, 'output_tail': out[-500:]}
        code, out = sh(['opencode2', 'api', '--server', BASE, 'GET', '/api/health'], env=env, timeout=60)
        phase_b['clients']['api --server, OPENCODE_SERVER_PASSWORD env'] = {'exit': code, 'output_tail': out[-500:]}
        cred_url = BASE.replace('://', f'://opencode:{PASSWORD}@')
        code, out = sh(['opencode2', 'api', '--server', cred_url, 'GET', '/api/health'],
                       env={k: v for k, v in env.items() if k != 'OPENCODE_SERVER_PASSWORD'}, timeout=60)
        phase_b['clients']['api --server, URL credentials'] = {'exit': code, 'output_tail': out[-500:]}

        # Interactive TUI against the external server.
        tui_log = (root / 'tui.log').open('w+')
        tui = subprocess.Popen(['opencode2', '--server', BASE, '--auto'],
                               env=env, stdout=tui_log, stderr=tui_log, cwd='/workspace')
        time.sleep(12)
        tui_alive = tui.poll() is None
        tui_log.seek(0)
        tui_text = tui_log.read()
        phase_b['clients']['tui --server'] = {
            'alive_after_12s': tui_alive,
            'connection_errors': sorted(set(re.findall(r'ECONNREFUSED|401|Unauthorized|ECONNRESET', tui_text)))}
        if tui_alive:
            tui.kill()
            tui.wait(timeout=10)
        tui_log.close()

        # Non-interactive client drives a real dispatch (mock providers).
        code, out = sh(['opencode2', 'run', '--server', BASE, '--auto',
                        '--agent', 'orchestrator', '--model', 'fake-parent/parent-model',
                        '--title', 'STANDALONE_PROBE_SESSION', 'MODEL_ROUTER_FOREGROUND'],
                       env=env, timeout=300)
        phase_b['clients']['run --server dispatch'] = {'exit': code, 'output_tail': out[-1500:]}

        # Collector client (plain HTTP, separate process from any opencode CLI):
        # find the session and read messages/children with auth.
        status, listing = http_json(BASE + '/api/session?directory=%2Fworkspace&limit=50', headers=AUTH)
        sessions = (listing or {}).get('data', []) if status == 200 else []
        probe = [s for s in sessions if 'STANDALONE_PROBE_SESSION' in json.dumps(s)]
        if not probe:
            probe = [s for s in sessions if s.get('agent') == 'orchestrator'
                     and (s.get('model') or {}).get('providerID') == 'fake-parent']
        if probe:
            parent = probe[-1]
            pid = parent['id']
            status, child_list = http_json(BASE + f'/api/session?parentID={pid}&directory=%2Fworkspace&limit=20', headers=AUTH)
            children = (child_list or {}).get('data', []) if status == 200 else []
            child = next((c for c in children if c.get('agent') == 'extractor'), None)
            status, parent_msgs = http_json(BASE + f'/api/session/{pid}/message?limit=100&order=asc', headers=AUTH)
            parent_msgs = (parent_msgs or {}).get('data', []) if status == 200 else []
            child_msgs = []
            if child:
                status, child_msgs = http_json(BASE + f"/api/session/{child['id']}/message?limit=100&order=asc", headers=AUTH)
                child_msgs = (child_msgs or {}).get('data', []) if status == 200 else []
            keep = ['id', 'type', 'agent', 'model', 'tokens', 'finish', 'error']
            phase_b['collector_read'] = {
                'collector_process': 'this probe script (plain HTTP client, not an opencode CLI)',
                'parent': {k: parent.get(k) for k in ['id', 'agent', 'model', 'parentID', 'projectID', 'title']},
                'parent_messages': [{k: m.get(k) for k in keep} for m in parent_msgs],
                'child': {k: child.get(k) for k in ['id', 'agent', 'model', 'parentID', 'projectID']} if child else None,
                'child_messages': [{k: m.get(k) for k in keep} for m in child_msgs]}
            phase_b['sessions'] = [
                {k: s.get(k) for k in ['id', 'agent', 'model', 'parentID']} for s in sessions]

        # Data layout at the launcher's data-volume path (skip the bun cache).
        volume = Path('/var/lib/opencode-data')
        tree = [str(p.relative_to(volume))
                for p in sorted(volume.rglob('*'))
                if p.relative_to(volume).parts[0] != 'cache']
        phase_b['data_layout'] = tree[:120]

        # ---------------------------------------------------------------- #
        # Phase C: lifecycle                                                #
        # ---------------------------------------------------------------- #
        phase_c = {'server_survives_client_exit': None, 'restart_recovery': None,
                   'messages_before_restart': None, 'messages_after_restart': None}

        # All `run`/TUI clients are already gone; the server should still serve.
        status, _ = http_json(BASE + '/api/health', headers=AUTH)
        phase_c['server_survives_client_exit'] = (status == 200)

        def read_all_messages():
            status, listing = http_json(BASE + '/api/session?directory=%2Fworkspace&limit=50', headers=AUTH)
            if status != 200:
                return None
            snapshot = {}
            for s in listing.get('data', []):
                status, msgs = http_json(BASE + f"/api/session/{s['id']}/message?limit=100&order=asc", headers=AUTH)
                if status == 200:
                    snapshot[s['id']] = [
                        {k: m.get(k) for k in ['id', 'type', 'agent', 'model', 'tokens', 'finish']}
                        for m in msgs.get('data', [])]
            return snapshot

        before = read_all_messages()
        phase_c['messages_before_restart'] = before

        server.send_signal(signal.SIGTERM)
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        time.sleep(2)
        status, _ = http_json(BASE + '/api/health', headers=AUTH)
        if status is not None:
            raise RuntimeError(f'server still responding after kill (status {status})')
        log.close()

        log = (root / 'server2.log').open('w+')
        server = subprocess.Popen(['opencode2', 'serve', '--hostname', '127.0.0.1', '--port', '18083'],
                                  env=env, stdout=log, stderr=log, cwd='/workspace')
        try:
            ok = False
            for _ in range(240):
                if healthy():
                    ok = True
                    break
                time.sleep(0.25)
            if not ok:
                raise RuntimeError('serve did not become healthy after restart')
            after = read_all_messages()
            phase_c['messages_after_restart'] = after
            phase_c['restart_recovery'] = (before is not None and before == after)
        finally:
            if server.poll() is None:
                server.kill()
                server.wait(timeout=10)
            log.close()

        result['phases'] = {'A_as_shipped_standalone': phase_a,
                            'B_launcher_integration': phase_b,
                            'C_lifecycle': phase_c}

        parent_msgs = (phase_b.get('collector_read') or {}).get('parent_messages', [])
        child = (phase_b.get('collector_read') or {}).get('child') or {}
        child_msgs = (phase_b.get('collector_read') or {}).get('child_messages', [])
        parent_assist = [m for m in parent_msgs if m.get('type') == 'assistant']
        child_assist = [m for m in child_msgs if m.get('type') == 'assistant']
        run1, run2 = phase_a['runs'][0], phase_a['runs'][1]
        result['assertions'] = {
            # Phase A: what the as-shipped standalone server actually exposes
            'standalone_spawns_stdio_server_child': (run1.get('server_child') or {}).get('cmdline', '')
                                                      .endswith('serve --stdio --port 0'),
            'standalone_loopback_listener_owned_by_server_child': bool(run1.get('port'))
                                                      and (run1.get('port_addr') == '0100007F'),
            'standalone_listener_port_ephemeral': bool(run1.get('port'))
                                                      and run1['port'] not in CANDIDATE_PORTS
                                                      and run2.get('port') not in (None, run1.get('port')),
            'standalone_listener_requires_auth': (run1.get('auth') or {}).get('no_credentials') == 401
                                                      and (run1.get('auth') or {}).get('wrong_password') == 401,
            'standalone_password_only_in_child_environ': (run1.get('child_environ_password') or {}).get('present') is True
                                                      and (run1.get('auth') or {}).get('child_environ_password') == 200
                                                      and bool(run1.get('cli_environ_password'))
                                                      and all(v is False for v in run1['cli_environ_password'].values()),
            'standalone_password_not_on_disk': not phase_a['password_on_disk_files'],
            'standalone_no_supported_discovery': (phase_a['api_attempts'].get('opencode2 api (background service default)') or {}).get('exit') != 0
                                                      and (phase_a['api_attempts'].get('opencode2 api --standalone') or {}).get('server_pid_in_response') not in (None, run1.get('server_child', {}).get('pid'))
                                                      and 'stopped' in (phase_a['service_status'] or {}).get('output_tail', ''),
            'standalone_well_known_ports_closed': all(v != 'connected' for v in phase_a['well_known_port_connects'].values()),
            'standalone_server_exits_with_cli': phase_a['server_child_exits_with_cli'] is True,
            # Phase B: the launcher-integration transport works
            'server_requires_auth': phase_b['auth'].get('no_credentials') == 401
                                   and phase_b['auth'].get('wrong_password') == 401
                                   and phase_b['auth'].get('correct_password') == 200,
            'client_auth_via_password_env_works': (phase_b['clients'].get('api --server, OPENCODE_SERVER_PASSWORD env') or {}).get('exit') == 0,
            'client_auth_url_credentials_not_supported': (phase_b['clients'].get('api --server, URL credentials') or {}).get('exit') != 0,
            'tui_connects_external_server': (phase_b['clients'].get('tui --server') or {}).get('alive_after_12s') is True
                                             and not (phase_b['clients'].get('tui --server') or {}).get('connection_errors'),
            'run_client_dispatch_completed': (phase_b['clients'].get('run --server dispatch') or {}).get('exit') == 0,
            'collector_read_parent_model': all(
                (m.get('model') or {}).get('providerID') == 'fake-parent'
                and (m.get('model') or {}).get('id') == 'parent-model'
                for m in parent_assist) and bool(parent_assist),
            'collector_read_worker_attribution': bool(child.get('id'))
                and child.get('parentID') == (phase_b['collector_read'].get('parent') or {}).get('id')
                and (child.get('model') or {}).get('providerID') == 'fake-worker'
                and all((m.get('model') or {}).get('providerID') == 'fake-worker' for m in child_assist) and bool(child_assist),
            'collector_read_token_shape': all(
                m.get('tokens') == {'input': 100, 'output': 22, 'reasoning': 8,
                                    'cache': {'read': 20, 'write': 0}}
                for m in child_assist) and bool(child_assist),
            'data_under_launcher_data_path': any(p.rsplit('/', 1)[-1] == 'opencode.db'
                                                 for p in (phase_b.get('data_layout') or [])),
            # Phase C: lifecycle
            'server_survives_client_exit': phase_c.get('server_survives_client_exit') is True,
            'restart_recovery': phase_c.get('restart_recovery') is True,
        }
        failed = [k for k, v in result['assertions'].items() if not v]
        if failed:
            print('FAILED assertions: ' + json.dumps(failed, indent=2), file=sys.stderr)
        Path(args.output).write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({'output': args.output, 'runtime': result['runtime'],
                          'assertions_passed': sum(result['assertions'].values()),
                          'assertions_failed': failed}))
        if failed:
            sys.exit(1)
    finally:
        if server.poll() is None:
            server.kill()
            server.wait(timeout=10)
        mock_server.shutdown()
        mock_server2.shutdown()
        Path(root / 'requests.jsonl').unlink(missing_ok=True)


if __name__ == '__main__':
    main()
