#!/usr/bin/env python3
"""Refresh Go DTOs, validation schemas and route metadata in initialized owner repositories. Requires PyYAML."""
from pathlib import Path
import yaml,json,re,subprocess
root=Path(__file__).resolve().parents[1];spec=yaml.safe_load((root/'contracts/openapi.yaml').read_text());defs=spec['components']['schemas']
def field(n):
 parts=re.sub(r'([a-z0-9])([A-Z])',r'\1_\2',n).split('_');return ''.join({'id':'ID','ids':'IDs','url':'URL','urls':'URLs','xp':'XP','uri':'URI','hp':'Hp'}.get(p.lower(),p[:1].upper()+p[1:]) for p in parts)
def typ(s):
 if '$ref' in s:return s['$ref'].split('/')[-1]
 if 'anyOf' in s:
  valid=[v for v in s['anyOf'] if v.get('type')!='null'];return '*'+typ(valid[0])
 t=s.get('type');t=t[0] if isinstance(t,list) else t
 if t=='object':
  if 'properties' not in s:return 'map[string]'+typ(s.get('additionalProperties',{}))
  fields=[]
  for n,v in s['properties'].items():
   ts=typ(v);optional=n not in s.get('required',[])
   if optional and not ts.startswith(('*','[]','map[')):ts='*'+ts
   fields.append(field(n)+' '+ts+' `json:"'+n+(',omitempty' if optional else '')+'"`')
  return 'struct {\n'+'\n'.join(fields)+'\n}'
 if t=='array':return '[]'+typ(s['items'])
 return {'string':'string','integer':'int','number':'float64','boolean':'bool'}.get(t,'any')
for service,tag,extra in [('user-management','User Management',['Package','PackageConfig','CombatRules','Schedule','StarterInput','Pet']),('battle','Battle',['Package','PackageConfig','CombatRules','PetReservation','PetReserveInput','PetBattleResult','PetResult','BattleHoldInput','Hold','BattleMoneyResult','WalletResult','Relationship','ProfileUpdate'])]:
 needed=set(extra+['Error']);routes={}
 for path,ops in spec['paths'].items():
  for method,op in ops.items():
   if not isinstance(op,dict) or tag not in op.get('tags',[]):continue
   needed.update(re.findall(r'#/components/schemas/(\w+)',json.dumps(op)))
   routes[method.upper()+' '+path]={'callers':op.get('x-allowed-callers',[]),'parameters':op.get('parameters',[])}
 pending=list(needed)
 while pending:
  for ref in re.findall(r'#/components/schemas/(\w+)',json.dumps(defs[pending.pop()])):
   if ref not in needed:needed.add(ref);pending.append(ref)
 dest=root/'services'/service/'internal/contract'
 (dest/'types.go').write_text('// Code generated from the shared OpenAPI contract. DO NOT EDIT.\npackage contract\n\n'+'\n\n'.join('type '+n+' = '+typ(s) for n,s in defs.items() if n in needed)+'\n')
 doc={'$schema':'https://json-schema.org/draft/2020-12/schema','$defs':{n:s for n,s in defs.items() if n in needed}}
 (dest/'schema.json').write_text(json.dumps(doc,indent=2).replace('#/components/schemas/','#/$defs/')+'\n')
 (dest/'routes.json').write_text(json.dumps(routes,indent=2)+'\n')

 subprocess.run(["gofmt", "-w", str(dest / "types.go")], check=True)
