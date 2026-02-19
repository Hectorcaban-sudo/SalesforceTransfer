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
        self.inserted = 0
        self.updated = 0

    def summary(self):
        logging.info(f"SUCCESS: {self.success}  INSERTED: {self.inserted}  UPDATED: {self.updated}  SKIPPED: {self.skipped}  FAILED: {self.failed}")


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
        try:
            return self.src.query_all(soql)['records']
        except Exception as e:
            logging.exception(f"Failed fetching {obj}: {e}")
            return []

    # ---------- LOOKUP RESOLUTION ----------
    def resolve_lookups(self, obj, record):
        desc = getattr(self.src, obj).describe()
        new_record = {}

        for f in desc['fields']:
            name = f['name']
            if name not in record or name in SYSTEM_FIELDS:
                continue

            # Only enforce lookup remap if the parent object is part of the transfer set
            if f['type'] == 'reference' and record.get(name):
                parent_obj = f['referenceTo'][0]
                source_parent_id = record[name]

                # If parent object is NOT part of transfer, leave value empty (avoid invalid cross‑org ids)
                if parent_obj not in self.relationships:
                    continue

                target_parent_id = self.id_map[parent_obj].get(source_parent_id)
                if not target_parent_id:
                    # parent not migrated yet → skip record safely
                    self.logger.skipped += 1
                    return None
                new_record[name] = target_parent_id
            else:
                new_record[name] = record[name]

        return new_record

    # ---------- UPSERT (NO EXTERNAL ID) ----------
    def bulk_insert(self, obj, records):
        if not records:
            return

        # split insert vs update based on Name match (natural key fallback since no external id allowed)
        inserts = []
        updates = []

        for r in records:
            source_id = r['_sourceId']
            payload = dict(r)
            payload.pop('_sourceId', None)

            name = payload.get('Name')
            target_id = None
            if name:
                try:
                    result = self.tgt.query(f"SELECT Id FROM {obj} WHERE Name = '{name.replace("'","\'")}' LIMIT 1")
                    if result['records']:
                        target_id = result['records'][0]['Id']
                except Exception:
                    pass

            if target_id:
                payload['Id'] = target_id
                updates.append((source_id, payload, target_id))
            else:
                inserts.append((source_id, payload))

        # ----- INSERTS -----
        if inserts:
            insert_batches = [inserts[i:i+BATCH_SIZE] for i in range(0, len(inserts), BATCH_SIZE)]
            for i, batch in enumerate(insert_batches, start=1):
                logging.info(f"{obj} INSERT batch {i}/{len(insert_batches)} size={len(batch)}")
                batch_records = [p for _, p in batch]
                try:
                results = getattr(self.tgt.bulk, obj).insert(batch_records, batch_size=BATCH_SIZE)
            except Exception as e:
                logging.exception(f"Bulk INSERT failure {obj}: {e}")
                self.logger.failed += len(batch)
                continue
                for (source_id, _), res in zip(batch, results):
                    if res['success']:
                        self.logger.success += 1
                        self.logger.inserted += 1
                        self.id_map[obj][source_id] = res['id']
                    else:
                        self.logger.failed += 1

        # ----- UPDATES -----
        if updates:
            update_batches = [updates[i:i+BATCH_SIZE] for i in range(0, len(updates), BATCH_SIZE)]
            for i, batch in enumerate(update_batches, start=1):
                logging.info(f"{obj} UPDATE batch {i}/{len(update_batches)} size={len(batch)}")
                batch_records = [p for _, p, _ in batch]
                try:
                results = getattr(self.tgt.bulk, obj).update(batch_records, batch_size=BATCH_SIZE)
            except Exception as e:
                logging.exception(f"Bulk UPDATE failure {obj}: {e}")
                self.logger.failed += len(batch)
                continue
                for (source_id, _, target_id), res in zip(batch, results):
                    if res['success']:
                        self.logger.success += 1
                        self.logger.updated += 1
                        self.id_map[obj][source_id] = target_id
                    else:
                        self.logger.failed += 1
                else:
                    self.logger.failed += 1

    # ---------- TRANSFER ----------
    def transfer_object(self, obj):
        logging.info(f"Transferring {obj}")
        self.disable_automation(obj)
        try:
            fields = self.transferable_fields(obj)
            records = self.fetch_records(obj, fields + ['Id'])

            prepared = []
            for r in records:
                clean = self.resolve_lookups(obj, r)
                if clean:
                    clean['_sourceId'] = r['Id']
                    prepared.append(clean)

            self.bulk_insert(obj, prepared)
        finally:
            self.enable_automation(obj)

    def run(self):
        try:
            self.build_relationship_graph(OBJECTS_TO_TRANSFER)
            order = self.topo_sort()
            logging.info(f"Processing Order: {order}")

            for obj in order:
                try:
                    self.transfer_object(obj)
                except Exception as e:
                    logging.exception(f"Object failed: {obj} -> {e}")
            self.logger.summary()
        except Exception as e:
            logging.exception(f"Fatal migration error: {e}")
            self.logger.summary()


    # ---------- AUTOMATION CONTROL (Tooling API) ----------
    def _tooling(self, method, url, json=None):
        import requests
        base = self.tgt.sf_instance
        sid = self.tgt.session_id
        full = f"https://{base}{url}"
        headers = {"Authorization": f"Bearer {sid}", "Content-Type": "application/json"}
        try:
            r = requests.request(method, full, headers=headers, json=json, timeout=60)
            if not r.ok:
                logging.warning(f"Tooling API call failed {method} {url}: {r.text}")
            return r.json() if r.text else {}
        except Exception as e:
            logging.exception(f"Tooling API exception {method} {url}: {e}")
            return {}

    def disable_automation(self, obj):
        logging.info(f"Disabling automation for {obj}")
        # Validation Rules
        q = f"/services/data/v59.0/tooling/query/?q=SELECT+Id,Active+FROM+ValidationRule+WHERE+EntityDefinition.QualifiedApiName='{obj}'+AND+Active=true"
        res = self._tooling('GET', q)
        for rec in res.get('records', []):
            self._tooling('PATCH', f"/services/data/v59.0/tooling/sobjects/ValidationRule/{rec['Id']}", {"Metadata": {"active": False}})
        # Triggers
        q = f"/services/data/v59.0/tooling/query/?q=SELECT+Id,Status+FROM+ApexTrigger+WHERE+TableEnumOrId='{obj}'+AND+Status='Active'"
        res = self._tooling('GET', q)
        for rec in res.get('records', []):
            self._tooling('PATCH', f"/services/data/v59.0/tooling/sobjects/ApexTrigger/{rec['Id']}", {"Status": "Inactive"})
        # Flows
        q = f"/services/data/v59.0/tooling/query/?q=SELECT+Id,ActiveVersionNumber+FROM+FlowDefinition+WHERE+DeveloperName='{obj}'"
        res = self._tooling('GET', q)
        for rec in res.get('records', []):
            self._tooling('PATCH', f"/services/data/v59.0/tooling/sobjects/FlowDefinition/{rec['Id']}", {"ActiveVersionNumber": 0})

    def enable_automation(self, obj):
        logging.info(f"Re-enabling automation for {obj}")
        # Validation Rules
        q = f"/services/data/v59.0/tooling/query/?q=SELECT+Id,Active+FROM+ValidationRule+WHERE+EntityDefinition.QualifiedApiName='{obj}'"
        res = self._tooling('GET', q)
        for rec in res.get('records', []):
            self._tooling('PATCH', f"/services/data/v59.0/tooling/sobjects/ValidationRule/{rec['Id']}", {"Metadata": {"active": True}})
        # Triggers
        q = f"/services/data/v59.0/tooling/query/?q=SELECT+Id,Status+FROM+ApexTrigger+WHERE+TableEnumOrId='{obj}'"
        res = self._tooling('GET', q)
        for rec in res.get('records', []):
            self._tooling('PATCH', f"/services/data/v59.0/tooling/sobjects/ApexTrigger/{rec['Id']}", {"Status": "Active"})
        # Flows (cannot restore exact version easily — reactivate latest)
        q = f"/services/data/v59.0/tooling/query/?q=SELECT+Id,LatestVersionNumber+FROM+FlowDefinition+WHERE+DeveloperName='{obj}'"
        res = self._tooling('GET', q)
        for rec in res.get('records', []):
            self._tooling('PATCH', f"/services/data/v59.0/tooling/sobjects/FlowDefinition/{rec['Id']}", {"ActiveVersionNumber": rec.get('LatestVersionNumber', 0)})

if __name__ == "__main__":
    SalesforceTransfer().run()
