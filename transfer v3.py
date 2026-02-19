#!/usr/bin/env python3
"""
Salesforce Sandbox → Sandbox Data Transfer (Option B cache)
- Bulk API batching
- Lookup remap with parent ordering
- No External IDs
- Persistent ID cache (for lookups only; records still re-evaluated)
- Persistent automation cache (validation rules, triggers, flows)
- Separate info and error logs
"""

import json
import os
import logging
from collections import defaultdict, deque
from simple_salesforce import Salesforce

# ================= CONFIG =================
SOURCE = dict(username="SOURCE_USERNAME", password="SOURCE_PASS", security_token="SOURCE_TOKEN", domain="test")
TARGET = dict(username="TARGET_USERNAME", password="TARGET_PASS", security_token="TARGET_TOKEN", domain="test")
OBJECTS_TO_TRANSFER = ["Account","Contact","Opportunity"]
RECORD_LIMIT = 5000
BATCH_SIZE = 200
SYSTEM_FIELDS = {'Id','IsDeleted','CreatedDate','CreatedById','LastModifiedDate','LastModifiedById','SystemModstamp','LastActivityDate','LastViewedDate','LastReferencedDate'}
CACHE_FILE = 'id_cache.json'
AUTOMATION_CACHE_FILE = 'automation_cache.json'
# ==========================================

# ---- logging ----
logger = logging.getLogger()
logger.setLevel(logging.INFO)
fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
info_h = logging.FileHandler('migration.log'); info_h.setFormatter(fmt); info_h.setLevel(logging.INFO)
err_h = logging.FileHandler('migration_errors.log'); err_h.setFormatter(fmt); err_h.setLevel(logging.ERROR)
console = logging.StreamHandler(); console.setFormatter(fmt)
logger.addHandler(info_h); logger.addHandler(err_h); logger.addHandler(console)

class TransferLogger:
    def __init__(self):
        self.success=self.inserted=self.updated=self.skipped=self.failed=0
    def summary(self):
        logging.info(f"SUCCESS:{self.success} INSERTED:{self.inserted} UPDATED:{self.updated} SKIPPED:{self.skipped} FAILED:{self.failed}")

class SalesforceTransfer:
    def __init__(self):
        self.src=Salesforce(**SOURCE)
        self.tgt=Salesforce(**TARGET)
        self.id_map=defaultdict(dict)
        self.relationships=defaultdict(set)
        self.logger=TransferLogger()
        self.automation_cache={}
        self.load_cache()

    # ---------- cache ----------
    def load_cache(self):
        if os.path.exists(CACHE_FILE):
            self.id_map.update(json.load(open(CACHE_FILE)))
            logging.info('Loaded ID cache')
        if os.path.exists(AUTOMATION_CACHE_FILE):
            self.automation_cache=json.load(open(AUTOMATION_CACHE_FILE))
            logging.info('Loaded automation cache')
    def save_cache(self):
        json.dump(self.id_map,open(CACHE_FILE,'w'))
        json.dump(self.automation_cache,open(AUTOMATION_CACHE_FILE,'w'))

    # ---------- metadata ----------
    def build_relationship_graph(self,objects):
        for obj in objects:
            desc=getattr(self.src,obj).describe()
            for f in desc['fields']:
                if f['type']=='reference':
                    for ref in f['referenceTo']:
                        if ref in objects and ref!=obj:
                            self.relationships[obj].add(ref)
        for o in objects:self.relationships.setdefault(o,set())

    def topo_sort(self):
        indeg={o:0 for o in self.relationships}
        for c,ps in self.relationships.items():
            for p in ps: indeg[c]+=1
        q=deque([o for o,d in indeg.items() if d==0])
        order=[]
        while q:
            n=q.popleft(); order.append(n)
            for c,ps in self.relationships.items():
                if n in ps:
                    indeg[c]-=1
                    if indeg[c]==0:q.append(c)
        return order or list(self.relationships.keys())

    def transferable_fields(self,obj):
        desc=getattr(self.src,obj).describe()
        return [f['name'] for f in desc['fields'] if f['name'] not in SYSTEM_FIELDS and not f.get('calculated') and f['createable']]

    def fetch_records(self,obj,fields):
        try:return self.src.query_all(f"SELECT {','.join(fields)} FROM {obj} LIMIT {RECORD_LIMIT}")['records']
        except Exception as e: logging.exception(f"Fetch failed {obj}:{e}"); return []

    def resolve_lookups(self,obj,record):
        desc=getattr(self.src,obj).describe(); out={}
        for f in desc['fields']:
            n=f['name']
            if n not in record or n in SYSTEM_FIELDS:continue
            if f['type']=='reference' and record.get(n):
                parent=f['referenceTo'][0]
                if parent not in self.relationships:continue
                tgt=self.id_map[parent].get(record[n])
                if not tgt:self.logger.skipped+=1; return None
                out[n]=tgt
            else: out[n]=record[n]
        return out

    # ---------- bulk ----------
    def bulk_insert(self,obj,records):
        inserts=[];updates=[]
        for r in records:
            sid=r['_sourceId'];payload=dict(r);payload.pop('_sourceId',None)
            name=payload.get('Name');tid=None
            if name:
                try:
                    q=self.tgt.query(f"SELECT Id FROM {obj} WHERE Name='{name.replace("'","\'")}' LIMIT 1")
                    if q['records']:tid=q['records'][0]['Id']
                except:pass
            (updates if tid else inserts).append((sid,payload,tid))

        def run_batches(action,data):
            batches=[data[i:i+BATCH_SIZE] for i in range(0,len(data),BATCH_SIZE)]
            for i,b in enumerate(batches,1):
                logging.info(f"{obj} {action} batch {i}/{len(batches)} size={len(b)}")
                try:
                    payload=[x[1] if action=='INSERT' else {**x[1],'Id':x[2]} for x in b]
                    res=getattr(self.tgt.bulk,obj).insert(payload,batch_size=BATCH_SIZE) if action=='INSERT' else getattr(self.tgt.bulk,obj).update(payload,batch_size=BATCH_SIZE)
                    for x,r in zip(b,res):
                        if r['success']:
                            self.logger.success+=1
                            if action=='INSERT':self.logger.inserted+=1;self.id_map[obj][x[0]]=r['id']
                            else:self.logger.updated+=1;self.id_map[obj][x[0]]=x[2]
                        else:self.logger.failed+=1
                except Exception as e:
                    logging.exception(f"Bulk {action} failure {obj}:{e}"); self.logger.failed+=len(b)
                self.save_cache()
        run_batches('INSERT',[x for x in inserts if not x[2]])
        run_batches('UPDATE',[x for x in updates if x[2]])

    # ---------- automation ----------
    def _tool(self,method,url,json_body=None):
        import requests
        h={"Authorization":f"Bearer {self.tgt.session_id}","Content-Type":"application/json"}
        r=requests.request(method,f"https://{self.tgt.sf_instance}{url}",headers=h,json=json_body,timeout=60)
        return r.json() if r.text else {}

    def disable_automation(self,obj):
        if obj in self.automation_cache:
            ids=self.automation_cache[obj]
        else:
            ids={'vr':[], 'tr':[], 'fl':[]}
            vr=self._tool('GET',f"/services/data/v59.0/tooling/query/?q=SELECT+Id+FROM+ValidationRule+WHERE+EntityDefinition.QualifiedApiName='{obj}'+AND+Active=true")
            ids['vr']=[r['Id'] for r in vr.get('records',[])]
            tr=self._tool('GET',f"/services/data/v59.0/tooling/query/?q=SELECT+Id+FROM+ApexTrigger+WHERE+TableEnumOrId='{obj}'+AND+Status='Active'")
            ids['tr']=[r['Id'] for r in tr.get('records',[])]
            fl=self._tool('GET',f"/services/data/v59.0/tooling/query/?q=SELECT+Id+FROM+FlowDefinition+WHERE+DeveloperName='{obj}'")
            ids['fl']=[r['Id'] for r in fl.get('records',[])]
            self.automation_cache[obj]=ids; self.save_cache()
        for i in ids['vr']:self._tool('PATCH',f"/services/data/v59.0/tooling/sobjects/ValidationRule/{i}",{"Metadata":{"active":False}})
        for i in ids['tr']:self._tool('PATCH',f"/services/data/v59.0/tooling/sobjects/ApexTrigger/{i}",{"Status":"Inactive"})
        for i in ids['fl']:self._tool('PATCH',f"/services/data/v59.0/tooling/sobjects/FlowDefinition/{i}",{"ActiveVersionNumber":0})

    def enable_automation(self,obj):
        ids=self.automation_cache.get(obj,{'vr':[],'tr':[],'fl':[]})
        for i in ids['vr']:self._tool('PATCH',f"/services/data/v59.0/tooling/sobjects/ValidationRule/{i}",{"Metadata":{"active":True}})
        for i in ids['tr']:self._tool('PATCH',f"/services/data/v59.0/tooling/sobjects/ApexTrigger/{i}",{"Status":"Active"})
        for i in ids['fl']:self._tool('PATCH',f"/services/data/v59.0/tooling/sobjects/FlowDefinition/{i}",{"ActiveVersionNumber":1})

    # ---------- transfer ----------
    def transfer_object(self,obj):
        logging.info(f"Transferring {obj}")
        self.disable_automation(obj)
        try:
            fields=self.transferable_fields(obj)
            recs=self.fetch_records(obj,fields+['Id'])
            prepared=[]
            for r in recs:
                c=self.resolve_lookups(obj,r)
                if c:c['_sourceId']=r['Id'];prepared.append(c)
            self.bulk_insert(obj,prepared)
        finally:
            self.enable_automation(obj); self.save_cache()

    def run(self):
        self.build_relationship_graph(OBJECTS_TO_TRANSFER)
        for obj in self.topo_sort():
            try:self.transfer_object(obj)
            except Exception as e:logging.exception(f"Object failed {obj}:{e}")
        self.logger.summary()

if __name__=='__main__':
    SalesforceTransfer().run()
