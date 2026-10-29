import os, tempfile
from pathlib import Path
os.environ['GENESIS_DB']=str(Path(tempfile.mkdtemp())/'test.db')
from fastapi.testclient import TestClient
from app.main import app

def test_lab_and_vm_planning():
    c=TestClient(app)
    assert c.get('/api/overview').status_code==200
    lab=c.post('/api/labs',json={'name':'Sandbox','description':'Disposable lab'})
    assert lab.status_code==200
    lid=lab.json()['id']
    vm=c.post('/api/machines',json={'lab_id':lid,'name':'ubuntu-test','iso_path':'/var/lib/libvirt/images/test.iso'})
    assert vm.status_code==200
    assert vm.json()['state']=='planned'
    assert len(c.get('/api/machines').json())==1
    assert c.post(f"/api/machines/{vm.json()['id']}/provision").status_code==400
    ch=c.post('/api/challenges',json={'lab_id':lid,'title':'Fix broken DNS','description':'Investigate service resolution'})
    assert ch.status_code==200
    assert c.post('/api/challenges/'+ch.json()['id']+'/complete').status_code==200
    assert c.get('/api/events').json()
