"""Reviewed namespace observations shared by the world and trusted host observer.

These literal programs inspect only the harness surface. No HTTP caller can add
an executable, shell command or environment variable to this plan.
"""

from __future__ import annotations

POLICY_ID = "preflight-host-observer@1"
EVALUATION_PATHS = (
    "/opt/landingparty/probes",
    "/opt/landingparty/prereg",
    "/opt/landingparty/analysis",
    "/opt/landingparty/goals",
    "/opt/landingparty/tests",
    "/opt/landingparty/worldd/skill_tests.py",
    "/data/private",
    "/data/banks",
)


def environment_command():
    script = (
        "import hashlib,json,os,ssl; from pathlib import Path; from urllib.parse import urlsplit,urlunsplit; "
        "keys=['WORLD_RUN_ID','ANTHROPIC_BASE_URL','OPENAI_BASE_URL','HTTP_PROXY','HTTPS_PROXY','SSL_CERT_FILE']; "
        "values={key:os.environ[key] for key in keys if key in os.environ}; "
        "path=Path(values.get('SSL_CERT_FILE','/nonexistent')); "
        "values['WORLD_CA_SHA256']=hashlib.sha256(ssl.PEM_cert_to_DER_cert(path.read_text())).hexdigest() if path.is_file() else None; "
        "private=[key for key in os.environ if key=='WORLD_CONTROL_TOKEN' or (key.startswith('WORLD_') and key.endswith('_KEY'))]; "
        "values['WORLD_PRIVATE_ENV_PRESENT']=any(os.environ.get(key) for key in private); "
        "marker=Path('/world/.preflight-observer.json'); values['WORLD_OBSERVER_MARKER']=json.loads(marker.read_text()) if marker.is_file() else None; "
        "\nfor key in ('HTTP_PROXY','HTTPS_PROXY'):\n"
        " if key in values:\n"
        "  parsed=urlsplit(values[key]); host=parsed.hostname or ''; host='['+host+']' if ':' in host else host\n"
        "  values[key]=urlunsplit((parsed.scheme,host+(':'+str(parsed.port) if parsed.port else ''),parsed.path,parsed.query,parsed.fragment))\n"
        "print(json.dumps(values))"
    )
    return ["python3", "-c", script]


def surface_command():
    script = (
        "import hashlib,json,pathlib,sys\n"
        "def tree(name):\n"
        " root=pathlib.Path(name)\n"
        " if root.is_symlink(): raise ValueError('symlink root')\n"
        " if not root.exists(): return None\n"
        " files={}; directories=[]; links={}\n"
        " for path in sorted(root.rglob('*')):\n"
        "  relative=path.relative_to(root).as_posix()\n"
        "  if path.is_symlink(): links[relative]=str(path.readlink())\n"
        "  elif path.is_dir(): directories.append(relative)\n"
        "  elif path.is_file():\n"
        "   hashed=hashlib.sha256()\n"
        "   with path.open('rb') as stream:\n"
        "    for block in iter(lambda:stream.read(1048576),b''): hashed.update(block)\n"
        "   files[relative]={'sha256':'sha256:'+hashed.hexdigest(),'executable':bool(path.stat().st_mode&0o111)}\n"
        "  else: raise ValueError('special file')\n"
        " return {'files':files,'directories':directories,'links':links}\n"
        "print(json.dumps({'work':tree(sys.argv[1]),'beliefs':tree(sys.argv[2])}))"
    )
    return ["python3", "-c", script, "/work", "/world/beliefs"]


def evaluation_command(paths=EVALUATION_PATHS):
    script = (
        "import os,sys; sys.exit(1 if any(os.access(p,os.R_OK) for p in "
        + repr(list(paths))
        + ") else 0)"
    )
    return ["python3", "-c", script]


def isolation_commands():
    return {
        "control": [
            "curl",
            "--noproxy",
            "*",
            "--max-time",
            "2",
            "--connect-timeout",
            "1",
            "http://172.30.77.2:7777/",
        ],
        "curl": [
            "curl",
            "--noproxy",
            "*",
            "--max-time",
            "3",
            "--connect-timeout",
            "2",
            "-I",
            "https://1.1.1.1/",
        ],
        "python": [
            "python3",
            "-c",
            "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('https://1.1.1.1/',timeout=2).read(1)",
        ],
        "dns": [
            "python3",
            "-c",
            "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(2); s.sendto(bytes.fromhex('123401000001000000000000076578616d706c6503636f6d0000010001'),('1.1.1.1',53)); s.recvfrom(512)",
        ],
    }


def harness_command(harness_id):
    script = (
        "import hashlib,importlib.metadata,json,os,pathlib,shutil,subprocess,sys\n"
        "identity=sys.argv[1]; lower=identity.lower(); result={'declared_id':identity}\n"
        "distributions={'openhands':'openhands-ai','harbor':'harbor','inspect':'inspect-ai','openenv':'openenv-core'}\n"
        "if 'claude' in lower:\n"
        " path=shutil.which('claude')\n"
        " if not path: raise ValueError('Claude harness binary is unavailable')\n"
        " child=subprocess.run([path,'--version'],capture_output=True,text=True,timeout=5,env={k:v for k,v in os.environ.items() if k in {'PATH','HOME','LANG','LC_ALL'}})\n"
        " if child.returncode: raise ValueError('Harness version failed')\n"
        " result.update(family='claude',observed_version=child.stdout.strip()[:300],binary_sha256='sha256:'+hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest())\n"
        "else:\n"
        " family=next((name for name in distributions if name in lower),None)\n"
        " if family: result.update(family=family,observed_version=importlib.metadata.version(distributions[family]))\n"
        " elif lower.startswith(('manual','lp','mcp')):\n"
        "  import worldd; result.update(family='world-client',observed_version=worldd.__version__)\n"
        " else: raise ValueError('No reviewed version observer for this harness family')\n"
        "print(json.dumps(result))"
    )
    return ["python3", "-c", script, harness_id]


def reviewed_plan(evaluation_paths=(), *, harness_id="manual@0.1"):
    paths = list(dict.fromkeys([*EVALUATION_PATHS, *evaluation_paths]))
    if any(
        not isinstance(path, str) or not path.startswith("/") or "\x00" in path for path in paths
    ):
        raise ValueError("Evaluation paths must be absolute literal paths")
    return {
        "policy_id": POLICY_ID,
        "harness_id": harness_id,
        "evaluation_paths": paths,
        "commands": {
            "environment": environment_command(),
            "surfaces": surface_command(),
            "evaluation": evaluation_command(paths),
            "harness": harness_command(harness_id),
            **isolation_commands(),
        },
    }
