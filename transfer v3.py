#!/usr/bin/env python3
"""
Salesforce Sandbox → Sandbox Data Transfer
- Uses simple_salesforce Bulk API
- Handles lookup relationships automatically
- Does NOT use External IDs
- Skips records whose parents do not exist in target
- Tracks success / skipped / failed
- Avoids system fields
- Fixes topological sort edge cases (never empty graph processing)
"""

import logging
from collections import defaultdict, deque
from simple_salesforce import Salesforce

# ================= CONFIG =================
SOURCE = dict(username="SOURCE_USERNAME", password="SOURCE_PASS", security_token="SOURCE_TOKEN", domain="test")
TARGET = dict(username="TARGET_USERNAME", password="TARGET_PASS", security_token="TARGET_TOKEN", domain="test")

OBJECTS_TO_TRANSFER = [
    "Account",
    "Contact",
    "Opportunity"
]

RECORD_LIMIT = 5000
BATCH_SIZE = 200

SYSTEM_FIELDS = {
    'Id','IsDeleted','CreatedDate','CreatedById','LastModifiedDate','LastModifiedById',
    'SystemModstamp','LastActivityDate','LastViewedDate','LastReferencedDate'
}
# ==========================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')


class TransferLogger:
    def __init__(self):
        self.success = 0
        self.skipped = 0
        self.failed = 0

    def summary(self):
        logging.info(f"SUCCESS: {self.success}  SKIPPED: {self.skipped}  FAILED: {self.failed}")


class SalesforceTransfer:
    def __init__(self):
        self.src = Salesforce(**SOURCE)
        self.tgt = Salesforce(**TARGET)
        self.id_map = defaultdict(dict)  # {object: {sourceId: targetId}}
        self.relationships = defaultdict(set)
        self.logger = TransferLogger()

    # ---------- METADATA ----------
    def build_relationship_graph(self, objects):
        for obj in objects:
            desc = getattr(self.src, obj).describe()
            for field in desc['fields']:
                if field['type'] == 'reference':
                    for ref in field['referenceTo']:
                        if ref in objects and ref != obj:
                            self.relationships[obj].add(ref)

        # ensure graph contains every node (fix zero-indegree bug)
        for obj in objects:
            self.relationships.setdefault(obj, set())

    def topo_sort(self):
        indegree = {o: 0 for o in self.relationships}
        for child, parents in self.relationships.items():
            for p in parents:
                indegree[child] += 1

        q = deque([o for o, d in indegree.items() if d == 0])
        order = []

        while q:
            node = q.popleft()
            order.append(node)
            for child, parents in self.relationships.items():
                if node in parents:
                    indegree[child] -= 1
                    if indegree[child] == 0:
                        q.append(child)

        # fix topological-sort bug: if cycle or empty, fall back to original list
        if not order:
            logging.warning("Topo sort empty — using provided order")
            return list(self.relationships.keys())

        return order

    # ---------- FIELD FILTER ----------
    def transferable_fields(self, obj):
        desc = getattr(self.src, obj).describe()
        fields = []
        for f in desc['fields']:
            if f['name'] not in SYSTEM_FIELDS and not f.get('calculated') and f['createable']:
                fields.append(f['name'])
        return fields

    # ---------- DATA FETCH ----------
    def fetch_records(self, obj, fields):
        soql = f"SELECT {','.join(fields)} FROM {obj} LIMIT {RECORD_LIMIT}"
        return self.src.query_all(soql)['records']

    # ---------- LOOKUP RESOLUTION ----------
    def resolve_lookups(self, obj, record):
        desc = getattr(self.src, obj).describe()
        new_record = {}

        for f in desc['fields']:
            name = f['name']
            if name not in record or name in SYSTEM_FIELDS:
                continue

            if f['type'] == 'reference' and record.get(name):
                parent_obj = f['referenceTo'][0]
                source_parent_id = record[name]

                target_parent_id = self.id_map[parent_obj].get(source_parent_id)
                if not target_parent_id:
                    self.logger.skipped += 1
                    return None  # skip record if parent missing
                new_record[name] = target_parent_id
            else:
                new_record[name] = record[name]

        return new_record

    # ---------- UPSERT (NO EXTERNAL ID) ----------
    def bulk_insert(self, obj, records):
        if not records:
            return

        batches = [records[i:i+BATCH_SIZE] for i in range(0, len(records), BATCH_SIZE)]

        for batch_num, batch in enumerate(batches, start=1):
            logging.info(f"{obj} Batch {batch_num}/{len(batches)} size={len(batch)}")
            results = getattr(self.tgt.bulk, obj).insert(batch, batch_size=BATCH_SIZE)

            for rec, res in zip(batch, results):
                if res['success']:
                    self.logger.success += 1
                    self.id_map[obj][rec['_sourceId']] = res['id']
                else:
                    self.logger.failed += 1

    # ---------- TRANSFER ----------
    def transfer_object(self, obj):
        logging.info(f"Transferring {obj}")
        fields = self.transferable_fields(obj)
        records = self.fetch_records(obj, fields + ['Id'])

        prepared = []
        for r in records:
            clean = self.resolve_lookups(obj, r)
            if clean:
                clean['_sourceId'] = r['Id']
                prepared.append(clean)

        self.bulk_insert(obj, prepared)

    def run(self):
        self.build_relationship_graph(OBJECTS_TO_TRANSFER)
        order = self.topo_sort()
        logging.info(f"Processing Order: {order}")

        for obj in order:
            self.transfer_object(obj)

        self.logger.summary()


if __name__ == "__main__":
    SalesforceTransfer().run()
