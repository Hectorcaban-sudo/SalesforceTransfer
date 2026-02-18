import logging
from simple_salesforce import Salesforce
from simple_salesforce.exceptions import SalesforceMalformedRequest
from collections import defaultdict, deque
import sys
import json

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

# Optional: Set to False if you do NOT want OwnerId migrated
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
except Exception as e:
    logging.critical("Connection failed", exc_info=True)
    print("Connection failed:", e)
    sys.exit(1)


# =====================================================
# METADATA SAFE FIELD FILTER
# =====================================================

def get_safe_fields(sf, object_name):
    """
    Returns ONLY fields that are safe to insert/update.
    Uses describe metadata flags.
    """

    try:
        desc = sf.__getattr__(object_name).describe()
        safe_fields = []

        for field in desc["fields"]:

            # Skip system / calculated / read-only fields
            if not field["createable"]:
                continue

            if field["calculated"]:
                continue

            if field["autoNumber"]:
                continue

            if field["type"] in ["location", "address"]:
                continue

            # Explicit system exclusions
            if field["name"] in [
                "Id",
                "CreatedDate",
                "CreatedById",
                "LastModifiedDate",
                "LastModifiedById",
                "SystemModstamp",
                "IsDeleted",
                "IsPartner",
            ]:
                continue

            if not MIGRATE_OWNER and field["name"] == "OwnerId":
                continue

            safe_fields.append(field["name"])

        return safe_fields

    except Exception:
        logging.error(f"Describe failed for {object_name}", exc_info=True)
        return []


def get_lookup_fields(sf, object_name):
    try:
        desc = sf.__getattr__(object_name).describe()
        return {
            field["name"]: field["referenceTo"]
            for field in desc["fields"]
            if field["type"] == "reference"
        }
    except Exception:
        logging.error(f"Lookup detection failed for {object_name}", exc_info=True)
        return {}


# =====================================================
# DEPENDENCY GRAPH
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

def fetch_records(sf, object_name, fields):
    try:
        query = f"SELECT Id, {', '.join(fields)} FROM {object_name}"
        return sf.query_all(query)["records"]
    except SalesforceMalformedRequest as e:
        logging.error(f"Query failed for {object_name}: {e.content}")
        return []
    except Exception:
        logging.error(f"Unexpected query error for {object_name}", exc_info=True)
        return []


def chunk_list(data, size):
    for i in range(0, len(data), size):
        yield data[i:i + size]


def remap_lookup_ids(record, lookup_fields, id_map):
    for field in lookup_fields:
        if record.get(field) and record[field] in id_map:
            record[field] = id_map[record[field]]
    return record


# =====================================================
# MIGRATION
# =====================================================

def migrate():

    id_map = {}

    print("Building dependency graph...")
    graph = build_dependency_graph(source_sf, OBJECTS_TO_MIGRATE)
    migration_order = topological_sort(graph)

    print("Migration order:", migration_order)

    for obj in migration_order:

        print(f"\nMigrating {obj}...")

        try:
            fields = get_safe_fields(source_sf, obj)

            if "Name" not in fields:
                print(f"Skipping {obj} (no Name field)")
                continue

            lookup_fields = get_lookup_fields(source_sf, obj)
            records = fetch_records(source_sf, obj, fields)

            if not records:
                print(f"No records found for {obj}")
                continue

            prepared_records = []

            for record in records:
                source_id = record["Id"]
                record.pop("attributes", None)
                record.pop("Id", None)

                record = remap_lookup_ids(record, lookup_fields, id_map)

                prepared_records.append((source_id, record))

            for batch in chunk_list(prepared_records, BATCH_SIZE):

                batch_payload = [r[1] for r in batch]

                results = target_sf.bulk.__getattr__(obj).upsert(
                    batch_payload,
                    external_id_field="Name"
                )

                for i, result in enumerate(results):

                    source_id = batch[i][0]

                    if result["success"]:
                        id_map[source_id] = result["id"]
                    else:
                        failure_detail = {
                            "object": obj,
                            "source_id": source_id,
                            "errors": result["errors"]
                        }
                        failure_logger.error(json.dumps(failure_detail))

            print(f"Finished {obj}")

        except Exception:
            logging.error(f"Unexpected failure for {obj}", exc_info=True)

    print("\nMigration Complete.")


# =====================================================
# RUN
# =====================================================

if __name__ == "__main__":
    migrate()
