"""Verify persistence and recovery in a running disposable Lab 1 environment."""
import json,subprocess,sys,time,urllib.error,urllib.request,uuid
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'scripts'))
import lab
from smoke import request,USERS
# Run only against a disposable local lab: briefly interrupts APIs and the Message Broker.

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
def status(path,token):
 try:
  with urllib.request.urlopen(urllib.request.Request(USERS+path,headers={'Authorization':'Bearer '+token}),timeout=15) as r:return r.status
 except urllib.error.HTTPError as e:return e.code
# The broker carries Gateway's requests and every event. While it is down, a request ends within
# the task deadline (504) until Gateway's breaker opens, then fails at once (503); none hangs.
lab.compose('up','-d','--wait')
token=request(USERS,'POST','/v1/auth/login',{'email':'alice@demo.invalid','password':lab.environment()['SEED_PASSWORD']})['accessToken']
lab.compose('stop','message-broker')
answers=[]
for _ in range(5):
 started=time.monotonic();answers.append(status('/v1/users/me',token))
 assert answers[-1] in (503,504) and time.monotonic()-started<8,f'With the broker down Gateway answered {answers}'
 if answers[-1]==503:break
assert answers[-1]==503,f'Gateway never failed fast with the broker down: {answers}'
lab.compose('up','-d','--wait','message-broker')
name='outage'+uuid.uuid4().hex[:12]
request(USERS,'POST','/v1/auth/register',{'username':name,'email':name+'@demo.invalid','password':lab.environment()['SEED_PASSWORD'],'packageId':'11111111-1111-4111-8111-111111111111'},expected=201)
for _ in range(40):
 if int(sql('users','SELECT count(*) FROM outbox'))==0:break
 time.sleep(.25)
else:raise AssertionError('outbox did not recover')
# A service role must not connect to the other database.
r=lab.compose('exec','-T','postgres','psql','-U','postgres','-At','-c',"SELECT has_database_privilege('users','battles','CONNECT'),has_database_privilege('battles','users','CONNECT')",stdout=subprocess.PIPE)
assert r.stdout.strip()==b'f|f','Cross-database connection allowed'
print('Verified: repeatable provisioning/migrations/seeds, database recreation, database isolation, Message Broker outage recovery.')
