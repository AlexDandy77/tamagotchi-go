"""Verify persistence and recovery in a running disposable Lab 1 environment."""
import json,subprocess,sys,time,uuid
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'scripts'))
import lab
from smoke import request,USERS
# Run only against a disposable local lab: briefly interrupts APIs and Kafka.

def sql(db,query):
 return lab.compose('exec','-T','postgres','psql','-U','postgres','-d',db,'-At','-c',query,stdout=subprocess.PIPE).stdout.decode().strip()
def snapshot():
 return [sql(db,"SELECT md5(string_agg(kind||id||body::text,',' ORDER BY kind,id)) FROM records") for db in ['users','battles']]
before=snapshot();lab.provision()
for s in lab.SERVICES:
 lab.compose('run','--rm','--no-deps',s,'migrate')
 lab.compose('run','--rm','--no-deps',s,'seed')
assert before==snapshot(),'Provision/migrate/seed changed existing data'
lab.compose('stop','user-management','battle')
before=snapshot();lab.compose('up','-d','--force-recreate','--wait','postgres');assert before==snapshot(),'Postgres recreation lost data'
lab.compose('up','-d','--wait');lab.compose('stop','kafka')
name='outage'+uuid.uuid4().hex[:12]
request(USERS,'POST','/v1/auth/register',{'username':name,'email':name+'@demo.invalid','password':lab.environment()['SEED_PASSWORD'],'packageId':'11111111-1111-4111-8111-111111111111'},expected=201)
time.sleep(1);assert int(sql('users','SELECT count(*) FROM outbox'))>0,'Outage lost outbox event'
lab.compose('up','-d','--wait','kafka')
for _ in range(40):
 if int(sql('users','SELECT count(*) FROM outbox'))==0:break
 time.sleep(.25)
else:raise AssertionError('outbox did not recover')
# A service role must not connect to the other database.
r=lab.compose('exec','-T','postgres','psql','-U','postgres','-At','-c',"SELECT has_database_privilege('users','battles','CONNECT'),has_database_privilege('battles','users','CONNECT')",stdout=subprocess.PIPE)
assert r.stdout.strip()==b'f|f','Cross-database connection allowed'
print('Verified: repeatable provisioning/migrations/seeds, database recreation, database isolation, Kafka outage recovery.')
