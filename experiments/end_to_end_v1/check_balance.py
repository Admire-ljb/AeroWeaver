"""Read the configured provider balance remotely without generating model tokens."""

import argparse
import json
import os
from pathlib import Path
import shlex
import paramiko


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-models", action="store_true", help="Also read the provider model catalog; no generation")
    args = parser.parse_args()
    script = """
import datetime,json,sys,urllib.request
sys.path.insert(0, '/home/runner/AeroWeaver/backend')
from llm_client import get_client
client=get_client(module='planner')
assert client._base_url.rstrip('/') in {'https://api.deepseek.com','https://api.deepseek.com/v1'}
request=urllib.request.Request('https://api.deepseek.com/user/balance',headers={'Authorization':'Bearer '+client._api_key})
with urllib.request.urlopen(request,timeout=30) as response:
    balance=json.load(response)
record={'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'configured_planner_model':client.model,'balance':balance}
if INCLUDE_MODELS:
    request=urllib.request.Request('https://api.deepseek.com/models',headers={'Authorization':'Bearer '+client._api_key})
    with urllib.request.urlopen(request,timeout=30) as response:
        record['models']=json.load(response)
print(json.dumps(record))
"""
    script = script.replace("INCLUDE_MODELS", repr(args.include_models))
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect("192.0.2.10", username="runner", password=os.environ["AEROWEAVER_DEPLOY_PASSWORD"], timeout=15)
    try:
        _, stdout, stderr = ssh.exec_command(shlex.join(["/home/runner/.venvs/aeroweaver/bin/python", "-c", script]), timeout=45)
        body = stdout.read().decode("utf-8")
        code = stdout.channel.recv_exit_status()
        if code:
            raise RuntimeError(stderr.read().decode("utf-8"))
        record = json.loads(body)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(record))
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
