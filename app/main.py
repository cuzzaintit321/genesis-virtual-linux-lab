"""GENESIS: local-first libvirt lab control plane.
No arbitrary command execution or unmanaged resource mutation.
"""
from __future__ import annotations
import json, os, re, secrets, sqlite3, subprocess, time, uuid
from contextlib import contextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.getenv('GENESIS_DB', str(ROOT / 'data' / 'genesis.db')))
LIBVIRT_URI = os.getenv('GENESIS_LIBVIRT_URI', 'qemu:///system')
TOKEN = os.getenv('GENESIS_TOKEN', '')
app = FastAPI(title='GENESIS Virtual Linux Lab', version='1.0.0')

@contextmanager
def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS labs(id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
            created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS machines(id TEXT PRIMARY KEY, lab_id TEXT NOT NULL, name TEXT NOT NULL UNIQUE,
            memory_mb INTEGER NOT NULL, vcpus INTEGER NOT NULL, disk_gb INTEGER NOT NULL,
            iso_path TEXT NOT NULL, state TEXT NOT NULL, created INTEGER NOT NULL,
            FOREIGN KEY(lab_id) REFERENCES labs(id));
        CREATE TABLE IF NOT EXISTS challenges(id TEXT PRIMARY KEY, lab_id TEXT NOT NULL, title TEXT NOT NULL,
            description TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER NOT NULL,
            action TEXT NOT NULL, detail TEXT NOT NULL);
        ''')

init()

def auth(x_genesis_token: str | None = Header(default=None)):
    if TOKEN and not secrets.compare_digest(x_genesis_token or '', TOKEN):
        raise HTTPException(401, 'Invalid GENESIS token')

def audit(c, action, detail):
    c.execute('INSERT INTO events(timestamp, action, detail) VALUES(?,?,?)', (int(time.time()), action, detail))

def virt(args: list[str], timeout=15):
    try:
        p = subprocess.run(['virsh', '-c', LIBVIRT_URI, *args], capture_output=True,
                           text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HTTPException(503, f'libvirt unavailable: {e}')
    if p.returncode:
        raise HTTPException(409, (p.stderr or p.stdout).strip()[:400])
    return p.stdout.strip()

class LabIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default='', max_length=500)

class MachineIn(BaseModel):
    lab_id: str
    name: str = Field(min_length=3, max_length=60, pattern=r'^[a-zA-Z][a-zA-Z0-9_-]*$')
    memory_mb: int = Field(ge=512, le=32768, default=2048)
    vcpus: int = Field(ge=1, le=16, default=2)
    disk_gb: int = Field(ge=5, le=200, default=20)
    iso_path: str = Field(min_length=1, max_length=500)

class ChallengeIn(BaseModel):
    lab_id: str
    title: str = Field(min_length=3, max_length=100)
    description: str = Field(min_length=3, max_length=1200)

@app.get('/')
def index():
    return FileResponse(ROOT / 'app' / 'static' / 'index.html')

@app.get('/api/overview', dependencies=[Depends(auth)])
def overview():
    with db() as c:
        counts = {t: c.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                  for t in ['labs', 'machines', 'challenges', 'events']}
    try:
        virt(['list', '--all', '--name'])
        connected = True
    except HTTPException:
        connected = False
    return {'counts': counts, 'libvirt_connected': connected, 'uri': LIBVIRT_URI}

@app.get('/api/labs', dependencies=[Depends(auth)])
def labs():
    with db() as c:
        return [dict(r) for r in c.execute('SELECT * FROM labs ORDER BY created DESC')]

@app.post('/api/labs', dependencies=[Depends(auth)])
def create_lab(body: LabIn):
    uid = str(uuid.uuid4())
    with db() as c:
        c.execute('INSERT INTO labs VALUES(?,?,?,?)', (uid, body.name, body.description, int(time.time())))
        audit(c, 'lab.created', body.name)
    return {'id': uid}

@app.get('/api/machines', dependencies=[Depends(auth)])
def machines():
    with db() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM machines ORDER BY created DESC')]
    for r in rows:
        if r['state'] != 'defined':
            continue
        try:
            r['runtime_state'] = virt(['domstate', r['name']])
        except HTTPException:
            r['runtime_state'] = 'unavailable'
    return rows

@app.post('/api/machines', dependencies=[Depends(auth)])
def plan_machine(body: MachineIn):
    """Plan only: no VM/disk is created until explicit provision."""
    p = Path(body.iso_path)
    if not p.is_absolute() or not str(p).startswith(('/var/lib/libvirt/images/', '/home/')) or p.suffix.lower() != '.iso':
        raise HTTPException(422, 'Use an absolute .iso path under /var/lib/libvirt/images or /home')
    uid = str(uuid.uuid4())
    with db() as c:
        if not c.execute('SELECT 1 FROM labs WHERE id=?', (body.lab_id,)).fetchone():
            raise HTTPException(404, 'Lab not found')
        if c.execute('SELECT 1 FROM machines WHERE name=?', (body.name,)).fetchone():
            raise HTTPException(409, 'Machine name already planned')
        c.execute('INSERT INTO machines VALUES(?,?,?,?,?,?,?,?,?)',
                  (uid, body.lab_id, body.name, body.memory_mb, body.vcpus,
                   body.disk_gb, str(p), 'planned', int(time.time())))
        audit(c, 'machine.planned', body.name)
    return {'id': uid, 'state': 'planned'}

@app.post('/api/machines/{machine_id}/provision', dependencies=[Depends(auth)])
def provision(machine_id: str, confirm: bool = False):
    if not confirm:
        raise HTTPException(400, 'Explicit confirm=true required')
    with db() as c:
        m = c.execute('SELECT * FROM machines WHERE id=?', (machine_id,)).fetchone()
        if not m: raise HTTPException(404, 'Machine not found')
        if m['state'] != 'planned': raise HTTPException(409, 'Machine already provisioned')
        if not Path(m['iso_path']).is_file(): raise HTTPException(409, 'ISO not found on this host')
        # virt-install provisions a VM with its own qcow2 disk; no host filesystem access beyond iso.
        disk = f'/var/lib/libvirt/images/genesis-{machine_id}.qcow2'
        args = ['virt-install', '--connect', LIBVIRT_URI, '--name', m['name'],
                '--memory', str(m['memory_mb']), '--vcpus', str(m['vcpus']),
                '--disk', f'path={disk},size={m["disk_gb"]},format=qcow2,bus=virtio',
                '--cdrom', m['iso_path'], '--network', 'network=default,model=virtio',
                '--graphics', 'vnc,listen=127.0.0.1', '--noautoconsole', '--wait', '0']
        try:
            created = subprocess.run(args, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise HTTPException(503, f'virt-install unavailable or timed out: {e}')
        if created.returncode:
            raise HTTPException(409, (created.stderr or created.stdout)[-500:])
        c.execute('UPDATE machines SET state=? WHERE id=?', ('defined', machine_id))
        audit(c, 'machine.provisioned', m['name'])
    return {'state':'defined', 'note':'VM provisioned with new qcow2 disk and ISO installer.'}

@app.post('/api/machines/{machine_id}/power/{action}', dependencies=[Depends(auth)])
def power(machine_id: str, action: str, confirm: bool = False):
    if action not in {'start', 'shutdown'}:
        raise HTTPException(400, 'Only start or graceful shutdown supported')
    if not confirm: raise HTTPException(400, 'Explicit confirmation required')
    with db() as c:
        m = c.execute('SELECT name, state FROM machines WHERE id=?', (machine_id,)).fetchone()
        if not m: raise HTTPException(404, 'Machine not found')
        if m['state'] != 'defined': raise HTTPException(409, 'VM not defined')
        virt([action, m['name']])
        audit(c, f'machine.{action}', m['name'])
    return {'ok': True}

@app.get('/api/challenges', dependencies=[Depends(auth)])
def challenges():
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM challenges ORDER BY created DESC')]

@app.post('/api/challenges', dependencies=[Depends(auth)])
def create_challenge(body: ChallengeIn):
    uid = str(uuid.uuid4())
    with db() as c:
        if not c.execute('SELECT 1 FROM labs WHERE id=?', (body.lab_id,)).fetchone():
            raise HTTPException(404, 'Lab not found')
        c.execute('INSERT INTO challenges VALUES(?,?,?,?,?,?)',
                  (uid, body.lab_id, body.title, body.description, 'open', int(time.time())))
        audit(c, 'challenge.created', body.title)
    return {'id': uid}

@app.post('/api/challenges/{challenge_id}/complete', dependencies=[Depends(auth)])
def complete(challenge_id: str):
    with db() as c:
        r = c.execute('UPDATE challenges SET status="completed" WHERE id=?', (challenge_id,))
        if r.rowcount == 0: raise HTTPException(404, 'Challenge not found')
        audit(c, 'challenge.completed', challenge_id)
    return {'ok': True, 'note': 'Self-reported completion; automatic grading is not implemented.'}

@app.get('/api/events', dependencies=[Depends(auth)])
def events():
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')]
