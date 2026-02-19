#!/usr/bin/env python3
"""
Salesforce Sandbox Data Migrator using simple_salesforce Bulk API
CONFIG-BASED VERSION (no CLI args)
"""

from collections import defaultdict, deque
from simple_salesforce import Salesforce

# ---------------- CONFIG ----------------
CONFIG = {
    "objects": ["Account", "Contact"],
    "limit": 200,

    "source": {
        "username": "SRC_USERNAME",
        "password": "SRC_PASSWORD",
        "token": "SRC_TOKEN",
        "domain": "test"
    },

    "target": {
        "username": "TGT_USERNAME",
        "password": "TGT_PASSWORD",
        "token": "TGT_TOKEN",
        "domain": "test"
    }
}

SYSTEM_FIELDS = {
    'Id','CreatedDate','CreatedById','LastModifiedDate','LastModifiedById',
    'SystemModstamp','IsDeleted','LastActivityDate','LastViewedDate','LastReferencedDate'
}

RESULTS = {'success':0,'skipped':0,'failed':0}

# ---------------- CONNECTION ----------------

def connect(cfg):
    return Salesforce(
        username=cfg['username'],
        password=cfg['password'],
        security_token=cfg['token'],
        domain=cfg['domain']
    )

# ---------------- DESCRIBE METADATA ----------------

def get_fields_and_lookups(sf, obj):
    meta = sf.__getattr__(obj).describe()
    fields = []
    lookups = {}

    for f in meta['fields']:
        if f['name'] in SYSTEM_FIELDS or f['calculated'] or f['autoNumber']:
            continue

        fields.append(f['name'])
        if f['type'] == 'reference' and f['referenceTo']:
            lookups[f['name']] = f['referenceTo'][0]

    return fields, lookups

# ---------------- DEPENDENCY ORDER ----------------

def build_dependency_graph(sf, objects):
    graph = defaultdict(set)
    for obj in objects:
        _, lookups = get_fields_and_lookups(sf, obj)
        for parent in lookups.values():
            if parent in objects:
                graph[obj].add(parent)
    return graph


def topo_sort(graph):
    indeg = defaultdict(int)
    for node in graph:
        for dep in graph[node]:
            indeg[dep]+=1

    q = deque([n for n in graph if indeg[n]==0])
    ordered=[]
    while q:
        n=q.popleft()
        ordered.append(n)
        for m in graph:
            if n in graph[m]:
                indeg[m]-=1
                if indeg[m]==0:
                    q.append(m)
    return ordered

# ---------------- QUERY SOURCE ----------------

def fetch_records(sf, obj, fields, limit):
    soql = f"SELECT {', '.join(fields)} FROM {obj} LIMIT {limit}"
    data = sf.query_all(soql)['records']
    for r in data:
        r.pop('attributes',None)
    return data

# ---------------- TARGET MATCHING ----------------

def find_existing(sf, obj, record):
    if 'Name' not in record or not record['Name']:
        return None
    name = record['Name'].replace("'","\'")
    soql=f"SELECT Id FROM {obj} WHERE Name = '{name}' LIMIT 1"
    res=sf.query(soql)['records']
    return res[0]['Id'] if res else None

# ---------------- LOOKUP RESOLUTION ----------------

def resolve_lookups(target_sf, lookups, record):
    for field,parent_obj in lookups.items():
        if field not in record or not record[field]:
            continue

        parent_id = record[field]
        parent = target_sf.query(f"SELECT Id FROM {parent_obj} WHERE Id='{parent_id}' LIMIT 1")['records']
        if parent:
            record[field]=parent[0]['Id']
        else:
            RESULTS['skipped']+=1
            return None
    return record

# ---------------- BULK UPSERT ----------------

def bulk_upsert(sf, obj, records):
    if not records:
        return

    to_insert=[]
    to_update=[]

    for r in records:
        existing=find_existing(sf,obj,r)
        if existing:
            r['Id']=existing
            to_update.append(r)
        else:
            to_insert.append(r)

    if to_insert:
        res=sf.bulk.__getattr__(obj).insert(to_insert,batch_size=200,use_serial=True)
        for r in res:
            if r['success']: RESULTS['success']+=1
            else: RESULTS['failed']+=1

    if to_update:
        res=sf.bulk.__getattr__(obj).update(to_update,batch_size=200,use_serial=True)
        for r in res:
            if r['success']: RESULTS['success']+=1
            else: RESULTS['failed']+=1

# ---------------- MAIN TRANSFER ----------------

def transfer(src_sf, tgt_sf, objects, limit):
    graph=build_dependency_graph(src_sf,objects)
    order=topo_sort(graph)

    for obj in order:
        print(f"Transferring {obj}")
        fields,lookups=get_fields_and_lookups(src_sf,obj)
        records=fetch_records(src_sf,obj,fields,limit)

        processed=[]
        for r in records:
            resolved=resolve_lookups(tgt_sf,lookups,r)
            if resolved:
                processed.append(resolved)

        bulk_upsert(tgt_sf,obj,processed)

    print('RESULTS:',RESULTS)

# ---------------- RUN ----------------

if __name__=='__main__':
    src=connect(CONFIG['source'])
    tgt=connect(CONFIG['target'])
    transfer(src,tgt,CONFIG['objects'],CONFIG['limit'])
