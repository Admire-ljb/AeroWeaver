"""Stage scoped fixes, check provider, update text models and run a bounded recheck."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

import paramiko

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from run_closed_loop_activation_remote import execute, download_tree, save
from stage_mock_parser_deploy import transplant

MODEL = 'deepseek-v4.1-flash-expires-on-0910'
REMOTE_ROOT = '/home/lsh/AeroWeaver'
PYTHON = '/home/lsh/.venvs/aeroweaver/bin/python'


def main():
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    local = ROOT / 'results/private-communication-recheck' / stamp
    local.mkdir(parents=True, exist_ok=False)
    remote = '/home/lsh/.aeroweaver-experiments/private-recheck/' + stamp
    stage = remote + '/stage'
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect('10.61.3.7', username='lsh', password=os.environ['AEROWEAVER_DEPLOY_PASSWORD'], timeout=15)
    ssh.get_transport().set_keepalive(30)
    save(local / 'launch.json', {'remote': remote, 'stage': stage, 'model': MODEL, 'host': '10.61.3.7'})
    print('Artifacts: ' + str(local), flush=True)
    def run(name, command):
        print('Stage: ' + name, flush=True)
        code = execute(ssh, command, local / (name + '.log'))
        if code:
            raise RuntimeError(f'{name} exited {code}; no automatic retry')
    try:
        status = "import json,urllib.request;s=json.load(urllib.request.urlopen('http://127.0.0.1:5001/api/status'));print(json.dumps(s));assert not any(s.get(k) for k in ['mission_active','is_executing','executing_robots']),'Online mission active'"
        run('status-before', shlex.join([PYTHON, '-c', status]))
        run('stage-copy', ' && '.join([
            'mkdir -p ' + shlex.quote(remote + '/inputs'),
            'mkdir -p ' + shlex.quote(stage),
            'cp -a ' + shlex.quote(REMOTE_ROOT + '/backend') + ' ' + shlex.quote(stage + '/backend'),
            'cp -a ' + shlex.quote(REMOTE_ROOT + '/tests') + ' ' + shlex.quote(stage + '/tests'),
            'cp ' + shlex.quote(REMOTE_ROOT + '/pyproject.toml') + ' ' + shlex.quote(stage + '/pyproject.toml')]))
        with ssh.open_sftp() as sftp:
            entries = []
            for relative in ['backend/sim/mock_tasks.py', 'backend/config.py', 'backend/skills/docs/mock_tasks/skill.md']:
                with sftp.open(REMOTE_ROOT + '/' + relative, 'rb') as handle:
                    before = handle.read()
                live = before.decode('utf-8-sig').replace('\r\n', '\n')
                source = (ROOT / relative).read_text(encoding='utf-8-sig')
                if relative.endswith('mock_tasks.py'):
                    after = transplant(live, source, ['observe', '_options'], 'MockTask')
                elif relative.endswith('config.py'):
                    old = '_env("DEEPSEEK_MODEL", "deepseek-v4-flash")'
                    assert live.count(old) == 1, 'Unexpected live model default'
                    after = live.replace(old, '_env("DEEPSEEK_MODEL", "' + MODEL + '")')
                else:
                    paragraph = source.split('Private communication uses two-bit XOR')[1]
                    after = live.rstrip() + '\n\nPrivate communication uses two-bit XOR' + paragraph
                target = local / 'patch' / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(after, encoding='utf-8', newline='\n')
                backup = local / 'before' / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(before)
                sftp.put(str(target), stage + '/' + relative)
                entries.append({'path': relative, 'before_sha256': hashlib.sha256(before).hexdigest(),
                                'after_sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
            manifest = {'stage': stage, 'root': REMOTE_ROOT, 'files': entries}
            save(local / 'manifest.json', manifest)
            sftp.put(str(local / 'manifest.json'), stage + '/manifest.json')
            for test in ['test_private_protocol.py', 'test_mock_rewards.py', 'test_mock_tasks.py']:
                sftp.put(str(ROOT / 'tests' / test), stage + '/tests/' + test)
            files = {name: ROOT / 'experiments/end_to_end_v1' / name for name in
                     ['pilot.py','run_all_tasks.py','test_pilot.py','test_all_tasks.py','run_private_recheck.py']}
            files.update({'activation_support.py': ROOT / 'experiments/skill_activation_closed_loop_v1/run.py',
                          'catalog.json': ROOT / 'experiments/skill_activation_v1/catalog.json',
                          'install.py': ROOT / 'scripts/install_trajectory_memory.py'})
            for name, path in files.items():
                sftp.put(str(path), remote + '/inputs/' + name)
            with sftp.open('/home/lsh/.venvs/aeroweaver-skill-eval/lib/python3.10/site-packages/rank_bm25.py', 'rb') as src:
                with sftp.open(remote + '/inputs/rank_bm25.py', 'wb') as dest:
                    dest.write(src.read())
            activation = '/home/lsh/.aeroweaver-experiments/end-to-end-pilot/20260910-125042/outputs/shared_activations/private_communication.json'
            with sftp.open(activation, 'rb') as src, sftp.open(remote + '/inputs/activation.json', 'wb') as dest:
                dest.write(src.read())
        run('protocol-tests', 'cd ' + shlex.quote(stage) + ' && ' + shlex.join(
            [PYTHON, '-m', 'pytest', '-q', 'tests/test_private_protocol.py', '--tb=short']))
        run('reward-tests', 'cd ' + shlex.quote(stage) + ' && ' + shlex.join(
            [PYTHON, '-m', 'pytest', '-q', 'tests/test_mock_rewards.py', '--tb=short']))
        run('experiment-tests', shlex.join(['env', 'AEROWEAVER_REPO_ROOT=' + stage, PYTHON, '-m', 'pytest', '-q',
            remote + '/inputs/test_pilot.py', remote + '/inputs/test_all_tasks.py', '--tb=short']))
        def phase(name):
            run(name, shlex.join(['timeout','600','env','AEROWEAVER_REPO_ROOT=' + REMOTE_ROOT,
                PYTHON,'-u',remote + '/inputs/run_private_recheck.py','--phase',name,
                '--catalog',remote + '/inputs/catalog.json','--activation',remote + '/inputs/activation.json',
                '--output',remote + '/' + name]))
        phase('interface')
        # Use the application's persistent configuration API; keys never leave the host.
        change_model = """import json,urllib.request
base='http://127.0.0.1:5001'
def get(path):
 return json.load(urllib.request.urlopen(base+path))
def post(path,data,method):
 req=urllib.request.Request(base+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json'},method=method)
 result=json.load(urllib.request.urlopen(req));assert result['ok'];return result
cfg=get('/api/llm/config')
p=cfg['providers']['deepseek']
post('/api/llm/provider',dict(name='deepseek',base_url=p['base_url'],default_model=MODEL,timeout=p['timeout']), 'POST')
for name,m in cfg['modules'].items():
 if m['resolved_provider']=='deepseek':
  post('/api/llm/module/'+name,dict(provider='deepseek',model=MODEL),'PUT')
after=get('/api/llm/config')
print(json.dumps({'default_model':after['providers']['deepseek']['default_model'],'modules':after['modules']}))
assert all(m['resolved_model']==MODEL for m in after['modules'].values() if m['resolved_provider']=='deepseek')
"""
        run('online-model-update', shlex.join([PYTHON, '-c', 'MODEL=' + repr(MODEL) + '\n' + change_model]))
        phase('legacy')
        run('deploy', shlex.join([PYTHON, remote + '/inputs/install.py', stage]))
        phase('fixed')
        run('status-after', shlex.join([PYTHON, '-c', status]))
        run('model-after-restart', shlex.join([PYTHON, '-c',
            "import json,urllib.request;c=json.load(urllib.request.urlopen('http://127.0.0.1:5001/api/llm/config'));print(json.dumps(c['modules']));assert c['providers']['deepseek']['default_model']==" + repr(MODEL)]))
        save(local / 'exit.json', {'status': 'complete'})
    except Exception as exc:
        save(local / 'exit.json', {'status': 'failed', 'error': str(exc)})
        raise
    finally:
        with ssh.open_sftp() as sftp:
            for name in ['interface','legacy','fixed']:
                try:
                    download_tree(sftp, remote + '/' + name, local / name)
                except FileNotFoundError:
                    pass
            try:
                sftp.get(stage + '/deployment-result.json', str(local / 'deployment-result.json'))
            except FileNotFoundError:
                pass
        ssh.close()
        print('Collected: ' + str(local), flush=True)


if __name__ == '__main__':
    main()
