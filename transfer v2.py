import logging
from simple_salesforce import Salesforce
from collections import defaultdict, deque
import sys
import json
import math

# =====================================================
# CONFIGURATION
# =====================================================

SOURCE_CONFIG = {
    "username": "source_username",
    "password": "source_password",
    "security_token": "source_token",
    "domain": "test"
}

TARGET_CONFIG = {
    "username": "target_username",
    "password": "target_password",
    "security_token": "target_token",
    "domain": "test"
}

OBJECTS_TO_MIGRATE = [
    "Account",
    "Contact",
    "Opportunity",
]

BATCH_SIZE = 5000
MIGRATE_OWNER = False


# =====================================================
# LOGGING
# =====================================================

logging.basicConfig(
    filename="migration.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

failure_logger = logging.getLogger("failures")
failure_handler = logging.FileHandler("migration_failures.log")
failure_logger.addHandler(failure_handler)
failure_logger.setLevel(logging.ERROR)


# =====================================================
# CONNECT
# =====================================================

try:
    source_sf = Salesforce(**SOURCE_CONFIG)
    target_sf = Salesforce(**TARGET_CONFIG)
    print("Connected to both sandboxes.")
except Exception:
    logging.critical("Salesforce connection failed", exc_info=True)
    sys.exit("Connection failed.")


# =====================================================
# FIELD FILTERING
# =====================================================

def get_safe_fields(sf, object_name):
    desc = sf.__getattr__(object_name).describe()
    safe_fields = []

    for field in desc["fields"]:
        if not field["createable"]:
            continue
        if field["calculated"]:
            continue
        if field["autoNumber"]:
            continue
        if field["name"] in [
            "Id", "CreatedDate", "CreatedById",
            "LastModifiedDate", "LastModifiedById",
            "SystemModstamp", "IsDeleted", "IsPartner"
        ]:
            continue
        if not MIGRATE_OWNER and field["name"] == "OwnerId":
            continue

        safe_fields.append(field["name"])

    return safe_fields


def get_lookup_fields(sf, object_name):
    desc = sf.__getattr__(object_name).describe()
    return {
        field["name"]: field["referenceTo"]
        for field in desc["fields"]
        if field["type"] == "reference"
    }


# =====================================================
# DEPENDENCY ORDER
# =====================================================

def build_dependency_graph(sf, objects):
    graph = defaultdict(set)

    for obj in objects:
        lookups = get_lookup_fields(sf, obj)
        for field, parents in lookups.items():
            for parent in parents:
                if parent in objects:
                    graph[obj].add(parent)

    return graph


def topological_sort(graph):
    in_degree = defaultdict(int)

    for node in graph:
        in_degree[node] = 0

    for node in graph:
        for dep in graph[node]:
            in_degree[node] += 1

    queue = deque([n for n in in_degree if in_degree[n] == 0])
    order = []

    while queue:
        node = queue.popleft()
        order.append(node)

        for other in graph:
            if node in graph[other]:
                in_degree[other] -= 1
                if in_degree[other] == 0:
                    queue.append(other)

    return order


# =====================================================
# HELPERS
# =====================================================

def fetch_source_records(object_name, fields):

    if "RecordTypeId" not in fields:
        fields.append("RecordTypeId")

    query = f"""
    SELECT Id,
           {', '.join(fields)},
           RecordType.DeveloperName
    FROM {object_name}
    """

    return source_sf.query_all(query)["records"]

def fetch_target_name_map(object_name):
    query = f"SELECT Id, Name FROM {object_name}"
    records = target_sf.query_all(query)["records"]
    return {r["Name"]: r["Id"] for r in records}


def chunk_list(data, size):
    for i in range(0, len(data), size):
        yield data[i:i + size]


def remap_lookup_ids(record, lookup_fields, id_map):

    for field, parent_objects in lookup_fields.items():

        if not record.get(field):
            continue

        source_parent_id = record[field]

        # If parent created in this run
        if source_parent_id in id_map:
            record[field] = id_map[source_parent_id]
            continue

        # Otherwise resolve via Name matching
        parent_object = parent_objects[0]  # Usually one object type

        # Build target cache
        target_map = build_target_parent_map(parent_object)

        # Get source parent name
        source_name_map = get_source_parent_names(parent_object, [source_parent_id])

        if source_parent_id not in source_name_map:
            logging.warning(f"Source parent not found for {parent_object}: {source_parent_id}")
            continue

        parent_name = source_name_map[source_parent_id]

        if parent_name in target_map:
            record[field] = target_map[parent_name]
        else:
            logging.warning(
                f"Target parent not found: {parent_object} → {parent_name}"
            )
            record.pop(field, None)  # remove invalid lookup

    return record


# =====================================================
# MIGRATION
# =====================================================

def remove_null_fields(record):
    """Remove fields with None values."""
    return {k: v for k, v in record.items() if v is not None}

# =====================================================
# RECORD TYPE HANDLING
# =====================================================

def build_target_recordtype_map():
    """
    Builds a mapping:
    {
        'Account': {
            'Business': '012XXXXXXXXXXXX',
            'PersonAccount': '012YYYYYYYYYYYY'
        }
    }
    """

    recordtype_map = defaultdict(dict)

    try:
        query = """
        SELECT Id, DeveloperName, SObjectType
        FROM RecordType
        WHERE IsActive = true
        """

        results = target_sf.query_all(query)["records"]

        for rt in results:
            sobject = rt["SObjectType"]
            devname = rt["DeveloperName"]
            recordtype_map[sobject][devname] = rt["Id"]

        logging.info("Target RecordType map built successfully.")

    except Exception:
        logging.error("Failed building target RecordType map", exc_info=True)

    return recordtype_map


def remap_recordtype(record, object_name, target_rt_map):
    """
    Replace source RecordTypeId with correct target RecordTypeId
    using DeveloperName match.
    """

    if "RecordType" not in record or not record["RecordType"]:
        return record

    source_devname = record["RecordType"]["DeveloperName"]

    if object_name in target_rt_map and source_devname in target_rt_map[object_name]:
        record["RecordTypeId"] = target_rt_map[object_name][source_devname]
    else:
        logging.warning(
            f"RecordType missing in target for {object_name} → {source_devname}"
        )

    return record

def build_target_parent_map(object_name):

    if object_name in target_parent_cache:
        return target_parent_cache[object_name]

    try:
        query = f"SELECT Id, Name FROM {object_name}"
        records = target_sf.query_all(query)["records"]

        name_map = {r["Name"]: r["Id"] for r in records}
        target_parent_cache[object_name] = name_map

        logging.info(f"Built target parent cache for {object_name}")
        return name_map

    except Exception:
        logging.error(f"Failed building target parent map for {object_name}", exc_info=True)
        return {}

def get_source_parent_names(object_name, ids):

    if not ids:
        return {}

    try:
        id_list = ",".join([f"'{i}'" for i in ids])
        query = f"SELECT Id, Name FROM {object_name} WHERE Id IN ({id_list})"
        records = source_sf.query_all(query)["records"]

        return {r["Id"]: r["Name"] for r in records}

    except Exception:
        logging.error(f"Failed querying source parent names for {object_name}", exc_info=True)
        return {}


def migrate():
    # Cache: { object_name : { source_id : parent_name } }
    source_parent_cache = defaultdict(dict)

    # Cache: { object_name : { parent_name : target_id } }
    target_parent_cache = defaultdict(dict)

    target_recordtype_map = build_target_recordtype_map()

    id_map = {}

    graph = build_dependency_graph(source_sf, OBJECTS_TO_MIGRATE)
    migration_order = topological_sort(graph)

    print("Migration order:", migration_order)

    for obj in migration_order:

        print(f"\nMigrating {obj}...")
        logging.info(f"Starting object: {obj}")

        stats = {
            "processed": 0,
            "inserted": 0,
            "updated": 0,
            "success": 0,
            "failed": 0
        }

        try:
            fields = get_safe_fields(source_sf, obj)

            if "Name" not in fields:
                print(f"Skipping {obj} (no Name field)")
                continue

            lookup_fields = get_lookup_fields(source_sf, obj)
            source_records = fetch_source_records(obj, fields)
            target_name_map = fetch_target_name_map(obj)

            insert_list = []
            update_list = []

            for record in source_records:

                stats["processed"] += 1
		record.pop("attributes", None)
		source_id = record.pop("Id")

		# Remap RecordTypeId
		record = remap_recordtype(record, obj, target_recordtype_map)
		record.pop("RecordType", None)

		# Remap lookups
		record = remap_lookup_ids(record, lookup_fields, id_map)

		# Remove null fields
		record = remove_null_fields(record)

                # Skip empty records
                if not record or "Name" not in record:
                    continue

                if record["Name"] in target_name_map:
                    record["Id"] = target_name_map[record["Name"]]
                    update_list.append((source_id, record))
                else:
                    insert_list.append((source_id, record))

            total_operations = len(insert_list) + len(update_list)
            print(f"Total operations for {obj}: {total_operations}")

            # ================= INSERT =================
            total_batches = math.ceil(len(insert_list) / BATCH_SIZE)
            for batch_num, batch in enumerate(chunk_list(insert_list, BATCH_SIZE), start=1):

                remaining = max(0, len(insert_list) - (batch_num * BATCH_SIZE))

                print(f"[{obj}] INSERT Batch {batch_num}/{total_batches} "
                      f"Size: {len(batch)} Remaining Inserts: {remaining}")

                payload = [r[1] for r in batch]
                results = target_sf.bulk.__getattr__(obj).insert(payload)

                batch_success = 0
                batch_fail = 0

                for i, result in enumerate(results):
                    source_id = batch[i][0]

                    if result["success"]:
                        batch_success += 1
                        stats["inserted"] += 1
                        stats["success"] += 1
                        id_map[source_id] = result["id"]
                    else:
                        batch_fail += 1
                        stats["failed"] += 1
                        failure_logger.error(json.dumps({
                            "object": obj,
                            "operation": "insert",
                            "errors": result["errors"],
                            "record": batch[i][1]
                        }))

                print(f"   → Batch Result: Success={batch_success} Failed={batch_fail}")

            # ================= UPDATE =================
            total_batches = math.ceil(len(update_list) / BATCH_SIZE)
            for batch_num, batch in enumerate(chunk_list(update_list, BATCH_SIZE), start=1):

                remaining = max(0, len(update_list) - (batch_num * BATCH_SIZE))

                print(f"[{obj}] UPDATE Batch {batch_num}/{total_batches} "
                      f"Size: {len(batch)} Remaining Updates: {remaining}")

                payload = [r[1] for r in batch]
                results = target_sf.bulk.__getattr__(obj).update(payload)

                batch_success = 0
                batch_fail = 0

                for i, result in enumerate(results):
                    source_id = batch[i][0]

                    if result["success"]:
                        batch_success += 1
                        stats["updated"] += 1
                        stats["success"] += 1
                        id_map[source_id] = result["id"]
                    else:
                        batch_fail += 1
                        stats["failed"] += 1
                        failure_logger.error(json.dumps({
                            "object": obj,
                            "operation": "update",
                            "errors": result["errors"],
                            "record": batch[i][1]
                        }))

                print(f"   → Batch Result: Success={batch_success} Failed={batch_fail}")

            print(
                f"{obj} COMPLETE → "
                f"Processed={stats['processed']} "
                f"Inserted={stats['inserted']} "
                f"Updated={stats['updated']} "
                f"Success={stats['success']} "
                f"Failed={stats['failed']}"
            )

        except Exception:
            logging.error(f"Unexpected failure for {obj}", exc_info=True)

    print("\nMigration Complete.")



# =====================================================
# RUN
# =====================================================

if __name__ == "__main__":
    migrate()
