"""
Salesforce Data Transfer Script
Transfers data from one sandbox to another with proper lookup field handling
"""

from simple_salesforce import Salesforce
import json
import time
from typing import Dict, List, Any
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SalesforceDataTransfer:
    """Handles data transfer between Salesforce sandboxes"""
    
    def __init__(self, source_config: Dict, target_config: Dict):
        """
        Initialize connections to source and target Salesforce instances
        
        Args:
            source_config: Dictionary with source Salesforce credentials
            target_config: Dictionary with target Salesforce credentials
        """
        self.source = self._connect(source_config, 'Source')
        self.target = self._connect(target_config, 'Target')
        
        # Store ID mappings for lookup field resolution
        self.id_mappings = {}  # Format: {object_name: {source_id: target_id}}
        
        # Store RecordType mappings between source and target
        self.record_type_mappings = {}  # Format: {object_name: {developer_name: target_id}}
        
    def _connect(self, config: Dict, instance_name: str) -> Salesforce:
        """Establish connection to Salesforce instance"""
        logger.info(f"Connecting to {instance_name} sandbox...")
        
        try:
            sf = Salesforce(
                username=config['username'],
                password=config['password'],
                security_token=config['security_token'],
                domain=config.get('domain', 'test')  # 'test' for sandbox, 'login' for production
            )
            logger.info(f"Successfully connected to {instance_name} sandbox")
            return sf
        except Exception as e:
            logger.error(f"Failed to connect to {instance_name}: {str(e)}")
            raise
    
    def get_all_records(self, sf: Salesforce, object_name: str, 
                       query_fields: List[str] = None) -> List[Dict]:
        """
        Retrieve all records from an object
        
        Args:
            sf: Salesforce connection object
            object_name: Name of the Salesforce object (e.g., 'Account', 'Contact')
            query_fields: List of fields to query (default: all fields)
        
        Returns:
            List of record dictionaries
        """
        logger.info(f"Retrieving records from {object_name}...")
        
        if query_fields is None:
            # Get all fields for the object
            describe = getattr(sf, object_name).describe()
            query_fields = [field['name'] for field in describe['fields']]
            logger.info(f"Found {len(query_fields)} fields in {object_name}")
        
        # Build SOQL query
        fields_str = ', '.join(query_fields)
        soql = f"SELECT {fields_str} FROM {object_name}"
        
        try:
            result = sf.query_all(soql)
            records = result['records']
            
            # Remove 'attributes' field from records
            for record in records:
                if 'attributes' in record:
                    del record['attributes']
            
            logger.info(f"Retrieved {len(records)} records from {object_name}")
            return records
        except Exception as e:
            logger.error(f"Error retrieving records from {object_name}: {str(e)}")
            raise
    
    def get_lookup_fields(self, sf: Salesforce, object_name: str) -> Dict:
        """
        Identify lookup and master-detail relationship fields in an object
        
        Args:
            sf: Salesforce connection object
            object_name: Name of the Salesforce object
        
        Returns:
            Dictionary of lookup fields with their referenced objects
        """
        logger.info(f"Analyzing lookup fields for {object_name}...")
        
        try:
            describe = getattr(sf, object_name).describe()
            lookup_fields = {}
            
            for field in describe['fields']:
                # Check for lookup or master-detail relationships
                if field['type'] in ['reference', 'masterdetail']:
                    relationship_name = field['name']
                    referenced_object = field['referenceTo'][0] if field['referenceTo'] else None
                    lookup_fields[relationship_name] = referenced_object
            
            logger.info(f"Found {len(lookup_fields)} lookup fields in {object_name}")
            return lookup_fields
        except Exception as e:
            logger.error(f"Error analyzing lookup fields: {str(e)}")
            return {}
    
    def discover_all_objects(self) -> List[str]:
        """
        Discover all available objects in the Salesforce org
        
        Returns:
            List of object names that are accessible and creatable
        """
        logger.info("Discovering all objects in Salesforce...")
        
        try:
            objects = []
            global_describe = self.source.describe()
            
            for sobject in global_describe['sobjects']:
                obj_name = sobject['name']
                
                # Filter for objects that are:
                # - Accessible (can be queried)
                # - Creatable (can insert records)
                # - Not standard metadata objects
                # - Not system objects
                if (sobject['queryable'] and 
                    sobject['createable'] and
                    not sobject['customSetting'] and
                    not obj_name.endswith('__Feed') and
                    not obj_name.endswith('__History') and
                    not obj_name.endswith('__Share') and
                    not obj_name.endswith('__Tag') and
                    not obj_name.endswith('__Layout') and
                    not obj_name in ['Attachment', 'Event', 'Task', 'Note', 'ActivityHistory']):
                    
                    objects.append(obj_name)
            
            logger.info(f"Discovered {len(objects)} transferable objects")
            return objects
        except Exception as e:
            logger.error(f"Error discovering objects: {str(e)}")
            return []
    
    def build_dependency_graph(self, objects: List[str]) -> Dict:
        """
        Build a dependency graph showing parent-child relationships
        
        Args:
            objects: List of object names to analyze
        
        Returns:
            Dictionary showing dependencies: {child: [parent1, parent2, ...]}
        """
        logger.info("Building dependency graph...")
        
        dependency_graph = {}
        
        for obj_name in objects:
            lookup_fields = self.get_lookup_fields(self.source, obj_name)
            dependencies = []
            
            for field_name, referenced_obj in lookup_fields.items():
                if referenced_obj and referenced_obj in objects:
                    dependencies.append(referenced_obj)
            
            if dependencies:
                dependency_graph[obj_name] = dependencies
                logger.info(f"{obj_name} depends on: {', '.join(dependencies)}")
        
        return dependency_graph
    
    def topological_sort(self, dependency_graph: Dict) -> List:
        """
        Perform topological sort to determine transfer order
        (parents must be transferred before children)
        
        Args:
            dependency_graph: Dictionary of object dependencies
        
        Returns:
            Ordered list of objects for transfer (parents first)
        """
        logger.info("Determining optimal transfer order...")
        
        # Get all objects involved in dependencies
        all_objects = set(dependency_graph.keys())
        for deps in dependency_graph.values():
            all_objects.update(deps)
        
        # Initialize in-degree counts and adjacency list
        in_degree = {obj: 0 for obj in all_objects}
        adjacency = {obj: [] for obj in all_objects}
        
        # Build graph
        for child, parents in dependency_graph.items():
            for parent in parents:
                adjacency[parent].append(child)
                in_degree[child] += 1
        
        # Queue for objects with no dependencies
        queue = [obj for obj in all_objects if in_degree[obj] == 0]
        result = []
        
        # Process nodes
        while queue:
            queue.sort()  # Process in alphabetical order for consistency
            current = queue.pop(0)
            result.append(current)
            
            # Reduce in-degree for dependent objects
            for neighbor in adjacency[current]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        
        # Check for cycles
        if len(result) != len(all_objects):
            logger.warning("Circular dependencies detected! Some objects may have dependency issues.")
            remaining = all_objects - set(result)
            result.extend(remaining)  # Add remaining objects at the end
            logger.warning(f"Remaining objects added to end: {remaining}")
        
        logger.info(f"Optimal transfer order determined: {' -> '.join(result)}")
        return result
    
    def _update_id_mappings_after_batch(self, object_name: str, batch_records: List[Dict]):
        """
        Update ID mappings after successful batch creation by querying created records
        
        Args:
            object_name: Name of the Salesforce object
            batch_records: Records that were just created with source IDs
        """
        try:
            # Get target IDs from the batch results
            source_ids = [rec['source_id'] for rec in batch_records if rec.get('source_id')]
            target_ids = [rec['target_id'] for rec in batch_records if rec.get('target_id')]
            
            # Create mappings
            for rec in batch_records:
                if rec.get('source_id') and rec.get('target_id'):
                    self.id_mappings[object_name][rec['source_id']] = rec['target_id']
            
        except Exception as e:
            logger.error(f"Error updating ID mappings after batch: {str(e)}")
    
    def get_record_types(self, sf: Salesforce, object_name: str) -> Dict:
        """
        Get all RecordTypes for a specific object from Salesforce
        
        Args:
            sf: Salesforce connection object
            object_name: Name of the Salesforce object
        
        Returns:
            Dictionary mapping DeveloperName to RecordTypeId
        """
        logger.info(f"Retrieving RecordTypes for {object_name}...")
        
        try:
            soql = f"""
                SELECT Id, DeveloperName, Name, SobjectType, IsActive 
                FROM RecordType 
                WHERE SobjectType = '{object_name}' AND IsActive = true
            """
            result = sf.query(soql)
            
            record_types = {}
            for rt in result['records']:
                record_types[rt['DeveloperName']] = {
                    'id': rt['Id'],
                    'name': rt['Name'],
                    'developer_name': rt['DeveloperName']
                }
            
            logger.info(f"Found {len(record_types)} active RecordTypes for {object_name}")
            return record_types
            
        except Exception as e:
            logger.error(f"Error retrieving RecordTypes for {object_name}: {str(e)}")
            return {}
    
    def build_record_type_mapping(self, object_name: str):
        """
        Build RecordType ID mapping between source and target systems
        
        Maps source RecordTypes to target RecordTypes using DeveloperName as the key
        
        Args:
            object_name: Name of the Salesforce object
        """
        logger.info(f"Building RecordType mapping for {object_name}...")
        
        try:
            # Get RecordTypes from both systems
            source_record_types = self.get_record_types(self.source, object_name)
            target_record_types = self.get_record_types(self.target, object_name)
            
            if not source_record_types or not target_record_types:
                logger.warning(f"No RecordTypes found for {object_name}")
                return
            
            # Map by DeveloperName
            mapping = {}
            matched = 0
            missing = []
            
            for dev_name, source_rt in source_record_types.items():
                if dev_name in target_record_types:
                    mapping[dev_name] = target_record_types[dev_name]['id']
                    matched += 1
                    logger.debug(f"Mapped RecordType {dev_name}: {source_rt['id']} -> {target_record_types[dev_name]['id']}")
                else:
                    missing.append(dev_name)
                    logger.warning(f"RecordType '{dev_name}' not found in target system for {object_name}")
            
            self.record_type_mappings[object_name] = mapping
            
            logger.info(f"RecordType mapping complete for {object_name}: {matched} matched, {len(missing)} missing")
            
            if missing:
                logger.warning(f"Missing RecordTypes in target: {', '.join(missing)}")
            
        except Exception as e:
            logger.error(f"Error building RecordType mapping: {str(e)}")
    
    def get_target_record_type_id(self, object_name: str, source_record_type_id: str) -> str:
        """
        Get the target RecordType ID for a source RecordType ID
        
        Args:
            object_name: Name of the Salesforce object
            source_record_type_id: Source RecordType ID
        
        Returns:
            Target RecordType ID or None if not found
        """
        try:
            if object_name not in self.record_type_mappings:
                logger.debug(f"No RecordType mapping found for {object_name}")
                return None
            
            # Find the source RecordType by ID
            source_record_types = self.get_record_types(self.source, object_name)
            source_dev_name = None
            
            for dev_name, rt_info in source_record_types.items():
                if rt_info['id'] == source_record_type_id:
                    source_dev_name = dev_name
                    break
            
            if not source_dev_name:
                logger.warning(f"Source RecordType ID {source_record_type_id} not found in {object_name}")
                return None
            
            # Get target RecordType ID
            target_mapping = self.record_type_mappings[object_name]
            if source_dev_name in target_mapping:
                return target_mapping[source_dev_name]
            else:
                logger.warning(f"RecordType '{source_dev_name}' not mapped in target for {object_name}")
                return None
                
        except Exception as e:
            logger.error(f"Error getting target RecordType ID: {str(e)}")
            return None
    
    def get_unique_identifier_fields(self, sf: Salesforce, object_name: str) -> List[str]:
        """
        Identify unique identifier fields for an object
        
        Args:
            sf: Salesforce connection object
            object_name: Name of the Salesforce object
        
        Returns:
            List of unique identifier field names
        """
        try:
            describe = getattr(sf, object_name).describe()
            unique_fields = []
            
            # Priority order for unique identifiers
            for field in describe['fields']:
                if field['type'] == 'id' or field['name'] == 'Id':
                    continue
                
                # Check for unique constraints
                if field['unique']:
                    unique_fields.append(field['name'])
                
                # Add Name field if it exists
                if field['name'] == 'Name':
                    if 'Name' not in unique_fields:
                        unique_fields.append('Name')
                
                # Add common identifier fields
                if field['name'] in ['Email', 'ExternalId', 'External_ID__c']:
                    if field['name'] not in unique_fields:
                        unique_fields.append(field['name'])
            
            return unique_fields
            
        except Exception as e:
            logger.error(f"Error getting unique identifier fields: {str(e)}")
            return ['Name']  # Default to Name field
    
    def build_existing_id_mappings(self, object_name: str, source_records: List[Dict]):
        """
        Build ID mappings for existing records in target system
        
        Queries target system for records that already exist and creates mappings
        based on unique identifiers like Name, Email, etc.
        
        Args:
            object_name: Name of the Salesforce object
            source_records: Source records to build mappings for
        """
        logger.info(f"Building ID mappings for existing {object_name} records in target...")
        
        try:
            # Get unique identifier fields for this object
            unique_fields = self.get_unique_identifier_fields(self.source, object_name)
            
            if not unique_fields:
                logger.warning(f"No unique identifier fields found for {object_name}")
                return
            
            # Use the first available unique field (usually Name or Email)
            identifier_field = unique_fields[0]
            logger.info(f"Using '{identifier_field}' as identifier for {object_name}")
            
            # Extract unique values from source records
            source_values = {}
            for record in source_records:
                value = record.get(identifier_field)
                if value:
                    source_values[value] = record.get('Id')
            
            if not source_values:
                logger.info(f"No {identifier_field} values found in source records for {object_name}")
                return
            
            logger.info(f"Found {len(source_values)} unique {identifier_field} values in source")
            
            # Query target system for matching records
            # Handle up to 500 values per query (Salesforce SOQL limit)
            all_values = list(source_values.keys())
            chunk_size = 500
            
            mappings_found = 0
            for i in range(0, len(all_values), chunk_size):
                chunk = all_values[i:i + chunk_size]
                
                # Build IN clause for SOQL
                values_str = "', '".join([str(v).replace("'", "\\'") for v in chunk])
                soql = f"SELECT Id, {identifier_field} FROM {object_name} WHERE {identifier_field} IN ('{values_str}')"
                
                try:
                    result = self.target.query(soql)
                    
                    for target_record in result['records']:
                        target_value = target_record.get(identifier_field)
                        target_id = target_record.get('Id')
                        
                        if target_value and target_value in source_values:
                            source_id = source_values[target_value]
                            
                            # Store mapping
                            if object_name not in self.id_mappings:
                                self.id_mappings[object_name] = {}
                            
                            self.id_mappings[object_name][source_id] = target_id
                            mappings_found += 1
                            logger.debug(f"Mapped existing {object_name}: {target_value} -> {target_id}")
                
                except Exception as e:
                    logger.error(f"Error querying target for {object_name} chunk {i}: {str(e)}")
            
            logger.info(f"Built {mappings_found} ID mappings for existing {object_name} records")
            
        except Exception as e:
            logger.error(f"Error building existing ID mappings: {str(e)}")
    
    def _find_target_record_by_source_id(self, object_name: str, source_id: str) -> str:
        """
        Find a target record ID by querying the target system with source record data
        
        This is used when a lookup field references a record that wasn't in the initial batch
        or when we need to resolve lookups for records that already exist in target.
        
        Args:
            object_name: Name of the Salesforce object
            source_id: Source record ID
        
        Returns:
            Target record ID or None if not found
        """
        try:
            # Query source system to get the record's unique identifiers
            unique_fields = self.get_unique_identifier_fields(self.source, object_name)
            
            if not unique_fields:
                logger.debug(f"No unique fields to query for {object_name}")
                return None
            
            # Query source record
            fields_to_query = ', '.join(unique_fields)
            soql = f"SELECT {fields_to_query} FROM {object_name} WHERE Id = '{source_id}' LIMIT 1"
            
            try:
                result = self.source.query(soql)
                
                if not result['records']:
                    logger.debug(f"Source record {source_id} not found in {object_name}")
                    return None
                
                source_record = result['records'][0]
                
                # Now query target system using these identifiers
                for field_name in unique_fields:
                    field_value = source_record.get(field_name)
                    if field_value:
                        # Query target for matching record
                        safe_value = str(field_value).replace("'", "\\'")
                        target_soql = f"SELECT Id FROM {object_name} WHERE {field_name} = '{safe_value}' LIMIT 1"
                        
                        try:
                            target_result = self.target.query(target_soql)
                            if target_result['records']:
                                target_id = target_result['records'][0]['Id']
                                logger.debug(f"Found target record for {object_name} using {field_name}: {field_value}")
                                return target_id
                        except Exception as e:
                            logger.debug(f"Error querying target for {field_name}={field_value}: {str(e)}")
                            continue
                
                logger.debug(f"Could not find matching target record for source {source_id}")
                return None
                
            except Exception as e:
                logger.error(f"Error querying source record {source_id}: {str(e)}")
                return None
                
        except Exception as e:
            logger.error(f"Error finding target record by source ID: {str(e)}")
            return None
    
    def prepare_record_for_insert(self, record: Dict, object_name: str, 
                                 lookup_fields: Dict) -> Dict:
        """
        Prepare a record for insertion by handling lookup fields, RecordType, and filtering system fields
        
        Args:
            record: Source record dictionary
            object_name: Name of the Salesforce object
            lookup_fields: Dictionary of lookup fields and their referenced objects
        
        Returns:
            Prepared record dictionary with updated lookup references and RecordType mapping
        """
        prepared_record = {}
        
        # Comprehensive list of system fields to exclude
        system_fields = [
            'Id', 'CreatedDate', 'CreatedById', 
            'LastModifiedDate', 'LastModifiedById',
            'IsDeleted', 'SystemModstamp', 'LastViewedDate',
            'LastReferencedDate', 'JigsawContactId', 'JigsawCompanyId',
            'IsPartner', 'IsAccountDeleted', 'IsPersonAccount',
            'MasterRecordId', 'OwnerChangeOption',
            'PhotoUrl', 'IndividualId', 'BillingGeocodeAccuracy',
            'ShippingGeocodeAccuracy', 'EmailBouncedReason',
            'EmailBouncedDate', 'LastActivityDate', 'LastCURequestDate',
            'LastCUUpdateDate', 'LastReferencedDate', 'LastViewedDate',
            'CleanStatus', 'CurrencyIsoCode'
        ]
        
        for field, value in record.items():
            if value is None:
                continue
                
            # Skip system fields
            if field in system_fields:
                continue
            
            # Skip fields ending with common system patterns
            if any(field.endswith(suffix) for suffix in ['__s', '__pc', '__History', '__Feed', '__Share', '__Tag', '__Layout', '__Track']):
                continue
            
            # Handle RecordType field specially - map to target RecordType ID
            if field == 'RecordTypeId':
                target_record_type_id = self.get_target_record_type_id(object_name, value)
                if target_record_type_id:
                    prepared_record[field] = target_record_type_id
                    logger.debug(f"Mapped RecordTypeId: {value} -> {target_record_type_id}")
                else:
                    logger.warning(f"Could not map RecordTypeId {value} for {object_name}, skipping field")
                continue
            
            # Handle lookup fields
            if field in lookup_fields:
                referenced_object = lookup_fields[field]
                
                # Check if we have an ID mapping for this reference
                if referenced_object in self.id_mappings and value in self.id_mappings[referenced_object]:
                    # Use mapped ID
                    prepared_record[field] = self.id_mappings[referenced_object][value]
                    logger.debug(f"Mapped {field}: {value} -> {prepared_record[field]}")
                else:
                    # No mapping found - try to find the record in target system
                    target_id = self._find_target_record_by_source_id(referenced_object, value)
                    if target_id:
                        prepared_record[field] = target_id
                        # Store the mapping for future use
                        if referenced_object not in self.id_mappings:
                            self.id_mappings[referenced_object] = {}
                        self.id_mappings[referenced_object][value] = target_id
                        logger.debug(f"Resolved {field}: {value} -> {target_id} from target system")
                    else:
                        # No mapping found - skip the lookup field
                        logger.warning(f"No ID mapping found for {field} -> {value} (referencing {referenced_object})")
                        continue
            else:
                # Regular field
                prepared_record[field] = value
        
        return prepared_record
    
    def transfer_records(self, object_name: str, batch_size: int = 50,
                        query_fields: List[str] = None) -> Dict:
        """
        Transfer records from source to target sandbox using batch API
        
        Args:
            object_name: Name of the Salesforce object to transfer
            batch_size: Number of records to process in each batch
            query_fields: Specific fields to query (optional)
        
        Returns:
            Dictionary with transfer statistics
        """
        logger.info(f"Starting data transfer for {object_name} using batch API...")
        
        # Initialize ID mappings for this object
        self.id_mappings[object_name] = {}
        
        stats = {
            'total': 0,
            'success': 0,
            'failed': 0,
            'batches_processed': 0,
            'errors': []
        }
        
        try:
            # Get lookup fields for this object
            lookup_fields = self.get_lookup_fields(self.source, object_name)
            
            # Build RecordType mappings for this object
            self.build_record_type_mapping(object_name)
            
            # Build ID mappings for existing records in target system
            self.build_existing_id_mappings(object_name, source_records)
            
            # Retrieve source records
            source_records = self.get_all_records(
                self.source, object_name, query_fields
            )
            
            stats['total'] = len(source_records)
            
            if not source_records:
                logger.info(f"No records found in {object_name}")
                return stats
            
            # Process records in batches
            for i in range(0, len(source_records), batch_size):
                batch = source_records[i:i + batch_size]
                batch_num = i // batch_size + 1
                total_batches = (len(source_records) + batch_size - 1) // batch_size
                
                logger.info(f"Processing batch {batch_num}/{total_batches} ({len(batch)} records)")
                
                # Prepare all records in the batch
                prepared_records = []
                for record in batch:
                    try:
                        prepared_record = self.prepare_record_for_insert(
                            record, object_name, lookup_fields
                        )
                        prepared_records.append({
                            'prepared': prepared_record,
                            'source_id': record.get('Id'),
                            'name': record.get('Name')
                        })
                    except Exception as e:
                        logger.error(f"Error preparing record: {str(e)}")
                        stats['failed'] += 1
                        stats['errors'].append({
                            'record': record,
                            'error': f"Preparation failed: {str(e)}"
                        })
                
                # Process batch using composite API for efficiency
                if prepared_records:
                    batch_results = self._process_batch(object_name, prepared_records)
                    
                    # Update statistics
                    stats['success'] += batch_results['success']
                    stats['failed'] += batch_results['failed']
                    stats['errors'].extend(batch_results['errors'])
                    
                    # Store ID mappings for this batch
                    for result in batch_results['id_mappings']:
                        source_id = result['source_id']
                        target_id = result['target_id']
                        if source_id and target_id:
                            self.id_mappings[object_name][source_id] = target_id
                
                stats['batches_processed'] += 1
                logger.info(f"Batch {batch_num} completed: {batch_results['success']} success, {batch_results['failed']} failed")
            
            logger.info(f"Transfer complete for {object_name}")
            logger.info(f"Total: {stats['total']}, Success: {stats['success']}, "
                       f"Failed: {stats['failed']}, Batches: {stats['batches_processed']}")
            
            return stats
            
        except Exception as e:
            logger.error(f"Fatal error during transfer: {str(e)}")
            raise
    
    def _process_batch(self, object_name: str, prepared_records: List[Dict]) -> Dict:
        """
        Process a batch of records using Salesforce Composite API
        
        Args:
            object_name: Name of the Salesforce object
            prepared_records: List of prepared records with metadata
        
        Returns:
            Dictionary with batch processing results
        """
        results = {
            'success': 0,
            'failed': 0,
            'errors': [],
            'id_mappings': []
        }
        
        # For better performance, we'll process records in smaller chunks
        # Salesforce Composite API has limits on request size
        chunk_size = 25  # Process 25 records at a time
        
        for i in range(0, len(prepared_records), chunk_size):
            chunk = prepared_records[i:i + chunk_size]
            
            # Build composite request body
            composite_requests = []
            for idx, record_data in enumerate(chunk):
                request_body = {
                    'method': 'POST',
                    'url': f'/services/data/v56.0/sobjects/{object_name}/',
                    'referenceId': f'record_{i + idx}',
                    'body': record_data['prepared']
                }
                composite_requests.append(request_body)
            
            try:
                # Execute composite request
                composite_result = self.target.restful(
                    'composite',
                    method='POST',
                    data={
                        'allOrNone': False,
                        'compositeRequest': composite_requests
                    }
                )
                
                # Process results
                for idx, response in enumerate(composite_result.get('compositeResponse', [])):
                    original_record = chunk[idx]
                    
                    if response['httpStatusCode'] in [200, 201]:
                        # Success - store ID mapping
                        results['success'] += 1
                        results['id_mappings'].append({
                            'source_id': original_record['source_id'],
                            'target_id': response['body'].get('id'),
                            'name': original_record['name']
                        })
                    else:
                        # Failure - log error
                        results['failed'] += 1
                        error_info = response.get('body', {})
                        results['errors'].append({
                            'record': original_record['prepared'],
                            'error': error_info,
                            'source_id': original_record['source_id'],
                            'name': original_record['name']
                        })
                        logger.error(f"Record {original_record.get('name', 'Unknown')} failed: {error_info}")
                
            except Exception as e:
                # If composite API fails, fall back to individual creates
                logger.warning(f"Composite API failed, falling back to individual creates: {str(e)}")
                
                for record_data in chunk:
                    try:
                        result = getattr(self.target, object_name).create(record_data['prepared'])
                        results['success'] += 1
                        results['id_mappings'].append({
                            'source_id': record_data['source_id'],
                            'target_id': result.get('id'),
                            'name': record_data['name']
                        })
                    except Exception as individual_error:
                        results['failed'] += 1
                        results['errors'].append({
                            'record': record_data['prepared'],
                            'error': str(individual_error),
                            'source_id': record_data['source_id'],
                            'name': record_data['name']
                        })
        
        return results
    
    def _update_id_mappings(self, object_name: str, source_records: List[Dict]):
        """
        Update ID mappings after successful record creation using Name field
        
        Args:
            object_name: Name of the Salesforce object
            source_records: Original source records
        """
        logger.info(f"Updating ID mappings for {object_name} using Name field...")
        
        try:
            # Check if object has a Name field
            describe = getattr(self.source, object_name).describe()
            name_field = None
            
            # Priority order for name-like fields
            for field in describe['fields']:
                if field['name'] == 'Name':
                    name_field = 'Name'
                    break
                elif field['name'] == 'FirstName':
                    name_field = 'FirstName'
                elif field['name'] == 'LastName':
                    name_field = 'LastName'
            
            if not name_field:
                logger.warning(f"No Name field found for {object_name}, using ID mappings from batch results")
                return
            
            # Get names from source records
            names = [rec.get(name_field) for rec in source_records if rec.get(name_field)]
            
            if not names:
                logger.warning(f"No names found in source records for {object_name}")
                return
            
            # Build SOQL query to get target records by Name
            names_str = "', '".join([str(name) for name in names])
            soql = f"SELECT Id, {name_field} FROM {object_name} WHERE {name_field} IN ('{names_str}')"
            
            result = self.target.query(soql)
            target_records = result['records']
            
            # Create mapping: name -> target_id
            name_mapping = {
                rec[name_field]: rec['Id'] 
                for rec in target_records
            }
            
            # Map source IDs to target IDs
            mappings_created = 0
            for source_record in source_records:
                source_id = source_record.get('Id')
                name_value = source_record.get(name_field)
                
                if source_id and name_value in name_mapping:
                    self.id_mappings[object_name][source_id] = name_mapping[name_value]
                    mappings_created += 1
            
            logger.info(f"Created {mappings_created} ID mappings for {object_name} using {name_field}")
            
        except Exception as e:
            logger.error(f"Error updating ID mappings: {str(e)}")
    
    def transfer_objects_in_order(self, object_dependencies: List[Dict],
                                batch_size: int = 50) -> Dict:
        """
        Transfer multiple objects respecting dependencies (parent before child)
        
        Args:
            object_dependencies: List of dictionaries with object transfer info
                Format: [
                    {'object_name': 'Account'},
                    {'object_name': 'Contact'},
                ]
            batch_size: Number of records to process in each batch
        
        Returns:
            Dictionary with overall transfer statistics
        """
        logger.info("Starting multi-object data transfer...")
        
        overall_stats = {
            'objects': {},
            'total_records': 0,
            'total_success': 0,
            'total_failed': 0,
            'batches_processed': 0
        }
        
        for obj_config in object_dependencies:
            object_name = obj_config['object_name']
            query_fields = obj_config.get('query_fields')
            
            logger.info(f"\n{'='*60}")
            logger.info(f"Transferring {object_name}")
            logger.info(f"{'='*60}")
            
            try:
                stats = self.transfer_records(
                    object_name=object_name,
                    batch_size=batch_size,
                    query_fields=query_fields
                )
                
                overall_stats['objects'][object_name] = stats
                overall_stats['total_records'] += stats['total']
                overall_stats['total_success'] += stats['success']
                overall_stats['total_failed'] += stats['failed']
                overall_stats['batches_processed'] += stats.get('batches_processed', 0)
                
            except Exception as e:
                logger.error(f"Failed to transfer {object_name}: {str(e)}")
                overall_stats['objects'][object_name] = {
                    'error': str(e),
                    'success': 0,
                    'failed': 0
                }
        
        logger.info("\n" + "="*60)
        logger.info("MULTI-OBJECT TRANSFER COMPLETE")
        logger.info(f"Total Records: {overall_stats['total_records']}")
        logger.info(f"Total Success: {overall_stats['total_success']}")
        logger.info(f"Total Failed: {overall_stats['total_failed']}")
        logger.info(f"Total Batches: {overall_stats['batches_processed']}")
        logger.info("="*60)
        
        return overall_stats
    
    def auto_transfer_all_objects(self, object_filter: List[str] = None,
                                 include_custom: bool = True,
                                 include_standard: bool = True,
                                 batch_size: int = 50) -> Dict:
        """
        Automatically discover dependencies and transfer all objects in correct order
        
        Args:
            object_filter: List of specific objects to transfer (None = all available)
            include_custom: Include custom objects (__c suffix)
            include_standard: Include standard objects
            batch_size: Number of records to process in each batch
        
        Returns:
            Dictionary with overall transfer statistics and dependency information
        """
        logger.info("Starting automatic object discovery and transfer...")
        
        # Discover all available objects
        all_objects = self.discover_all_objects()
        
        # Filter objects based on criteria
        filtered_objects = []
        for obj in all_objects:
            # Apply object name filter if provided
            if object_filter and obj not in object_filter:
                continue
            
            # Apply custom/standard filters
            is_custom = obj.endswith('__c')
            if is_custom and not include_custom:
                continue
            if not is_custom and not include_standard:
                continue
            
            filtered_objects.append(obj)
        
        logger.info(f"Filtered to {len(filtered_objects)} objects for transfer")
        
        # Build dependency graph
        dependency_graph = self.build_dependency_graph(filtered_objects)
        
        # Determine optimal transfer order using topological sort
        transfer_order = self.topological_sort(dependency_graph)
        
        # Prepare object configurations
        object_dependencies = []
        for obj_name in transfer_order:
            obj_config = {
                'object_name': obj_name
            }
            object_dependencies.append(obj_config)
            
            logger.info(f"{obj_name} -> Added to transfer queue")
        
        # Execute transfer in correct order
        results = self.transfer_objects_in_order(object_dependencies, batch_size)
        
        # Add dependency information to results
        results['dependency_graph'] = dependency_graph
        results['transfer_order'] = transfer_order
        results['total_objects'] = len(transfer_order)
        
        return results


def main():
    """Main execution function"""
    
    # Configuration for source and target sandboxes
    source_config = {
        'username': 'source_user@example.com',
        'password': 'source_password',
        'security_token': 'source_token',
        'domain': 'test'  # 'test' for sandbox, 'login' for production
    }
    
    target_config = {
        'username': 'target_user@example.com',
        'password': 'target_password',
        'security_token': 'target_token',
        'domain': 'test'
    }
    
    try:
        # Initialize data transfer
        transfer = SalesforceDataTransfer(source_config, target_config)
        
        # OPTION 1: Automatic transfer with dependency discovery
        # This will automatically discover objects, their relationships, 
        # and transfer them in the correct order using batch API
        
        # Example 1: Transfer all objects (custom and standard)
        results = transfer.auto_transfer_all_objects(
            include_custom=True,
            include_standard=True,
            batch_size=50
        )
        
        # Example 2: Transfer only specific objects
        # results = transfer.auto_transfer_all_objects(
        #     object_filter=['Account', 'Contact', 'Opportunity', 'Case'],
        #     batch_size=50
        # )
        
        # Example 3: Transfer only custom objects
        # results = transfer.auto_transfer_all_objects(
        #     include_custom=True,
        #     include_standard=False,
        #     batch_size=50
        # )
        
        # OPTION 2: Manual transfer with explicit object list
        # Define objects to transfer in dependency order (parents before children)
        # object_dependencies = [
        #     {
        #         'object_name': 'Account',
        #         # 'query_fields': ['Id', 'Name', 'BillingCity', 'BillingState']
        #     },
        #     {
        #         'object_name': 'Contact',
        #     },
        #     {
        #         'object_name': 'Opportunity',
        #     },
        # ]
        # results = transfer.transfer_objects_in_order(object_dependencies)
        
        # Save results to file
        with open('transfer_results.json', 'w') as f:
            json.dump(results, f, indent=2, default=str)
        
        logger.info("\nTransfer results saved to transfer_results.json")
        
        # Print summary
        logger.info("\n" + "="*60)
        logger.info("TRANSFER SUMMARY")
        logger.info("="*60)
        logger.info(f"Total Objects Transferred: {results.get('total_objects', 'N/A')}")
        logger.info(f"Total Records: {results.get('total_records', 0)}")
        logger.info(f"Successful: {results.get('total_success', 0)}")
        logger.info(f"Failed: {results.get('total_failed', 0)}")
        logger.info(f"Total Batches Processed: {results.get('batches_processed', 0)}")
        
        if 'transfer_order' in results:
            logger.info(f"\nTransfer Order:")
            for i, obj in enumerate(results['transfer_order'], 1):
                logger.info(f"  {i}. {obj}")
        
        logger.info("="*60)
        
    except Exception as e:
        logger.error(f"Transfer failed: {str(e)}")
        raise


if __name__ == '__main__':
    main()