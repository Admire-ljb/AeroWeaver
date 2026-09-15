"""Read existing private-communication evidence and remote deployment metadata."""
import json
import os
from pathlib import Path
import paramiko

ROOT = Path(__file__).resolve().parents[2]


def main():
    output = ROOT / 'results/end-to-end-pilot/20260910-125042/remote-artifacts/outputs'
    for folder in sorted((output / 'episodes').glob('private_communication-*')):
        initial = json.loads((folder / 'initial_state.json').read_text())
        print(folder.name, initial['private_state_for_pairing_only'])
        for line in (folder / 'trace.jsonl').read_text().splitlines():
            row = json.loads(line)
            print(json.dumps({'step': row['step'], 'choices': {k: v['choice'] for k, v in row['decisions'].items()},
                              'metrics': row['metrics']}))
    for line in (output / 'calls.jsonl').open(encoding='utf-8'):
        call = json.loads(line)
        if call.get('scenario') == 'private_communication' and call.get('agent_id') == 'UAV_2' and call.get('step') == 1:
            print('RECEIVER REQUEST', json.dumps(call['request'], ensure_ascii=False))
            break
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect('192.0.2.10', username='runner', password=os.environ['AEROWEAVER_DEPLOY_PASSWORD'], timeout=15)
    try:
        command = "cd /home/runner/AeroWeaver && /home/runner/.venvs/aeroweaver/bin/python -c "
        import shlex
        code = """import json, urllib.request, hashlib
from pathlib import Path
for endpoint in ['/api/status','/api/llm/config']:
 data=json.load(urllib.request.urlopen('http://127.0.0.1:5001'+endpoint))
 if endpoint.endswith('config'):
  for p in data.get('providers',{}).values():
   p.pop('api_key',None);p.pop('api_key_masked',None)
 print(endpoint,json.dumps(data))
for name in ['backend/sim/mock_tasks.py','backend/config.py','backend/llm_client.py']:
 print(name,hashlib.sha256(Path(name).read_bytes()).hexdigest())
"""
        _, out, err = ssh.exec_command(command + shlex.quote(code), timeout=30)
        print(out.read().decode()); print(err.read().decode())
        if out.channel.recv_exit_status():
            raise RuntimeError('Remote inspection failed')
    finally:
        ssh.close()


if __name__ == '__main__':
    main()
