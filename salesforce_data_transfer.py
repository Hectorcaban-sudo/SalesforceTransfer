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
    
    def find_external_id_fields(self, object_name: str) -> str:
        """
        Find the best external ID field for an object
        
        Args:
            object_name: Name of the Salesforce object
        
        Returns:
            Name of external ID field or None if not found
        """
        try:
            describe = getattr(self.source, object_name).describe()
            
            # Priority order for external ID fields
            priority_fields = ['External_ID__c', 'ExternalId', 'ExternalID', 'Id']
            
            # Check for common external ID patterns
            for field in describe['fields']:
                field_name = field['name']
                
                # Check if it's explicitly marked as external ID
                if field.get('externalId'):
                    return field_name
                
                # Check for common naming patterns
                for priority in priority_fields:
                    if field_name.lower() == priority.lower():
                        return field_name
            
            # If no external ID found, return None
            logger.debug(f"No external ID field found for {object_name}")
            return None
            
        except Exception as e:
            logger.error(f"Error finding external ID field: {str(e)}")
            return None
    
    def prepare_record_for_insert(self, record: Dict, object_name: str, 
                                 lookup_fields: Dict, 
                                 external_id_field: str = None) -> Dict:
        """
        Prepare a record for insertion by handling lookup fields and IDs
        
        Args:
            record: Source record dictionary
            object_name: Name of the Salesforce object
            lookup_fields: Dictionary of lookup fields and their referenced objects
            external_id_field: External ID field to use for upsert (optional)
        
        Returns:
            Prepared record dictionary with updated lookup references
        """
        prepared_record = {}
        
        for field, value in record.items():
            if value is None:
                continue
                
            # Skip system fields and ID fields for new records
            if field in ['Id', 'CreatedDate', 'CreatedById', 
                        'LastModifiedDate', 'LastModifiedById']:
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
                    # No mapping found - you might want to log this or skip the record
                    logger.warning(f"No ID mapping found for {field} -> {value} (referencing {referenced_object})")
                    # Option 1: Skip the lookup field
                    # Option 2: Skip the entire record (uncomment below)
                    # return None
            else:
                # Regular field
                prepared_record[field] = value
        
        return prepared_record
    
    def transfer_records(self, object_name: str, batch_size: int = 50,
                        external_id_field: str = None, 
                        query_fields: List[str] = None,
                        skip_existing: bool = True) -> Dict:
        """
        Transfer records from source to target sandbox
        
        Args:
            object_name: Name of the Salesforce object to transfer
            batch_size: Number of records to process in each batch
            external_id_field: External ID field for upsert operations (optional)
            query_fields: Specific fields to query (optional)
            skip_existing: Skip records that already exist in target (for upsert)
        
        Returns:
            Dictionary with transfer statistics
        """
        logger.info(f"Starting data transfer for {object_name}...")
        
        # Initialize ID mappings for this object
        self.id_mappings[object_name] = {}
        
        stats = {
            'total': 0,
            'success': 0,
            'failed': 0,
            'skipped': 0,
            'errors': []
        }
        
        try:
            # Get lookup fields for this object
            lookup_fields = self.get_lookup_fields(self.source, object_name)
            
            # Retrieve source records
            source_records = self.get_all_records(
                self.source, object_name, query_fields
            )
            
            stats['total'] = len(source_records)
            
            # Process records in batches
            for i in range(0, len(source_records), batch_size):
                batch = source_records[i:i + batch_size]
                logger.info(f"Processing batch {i // batch_size + 1}/{(len(source_records) + batch_size - 1) // batch_size}")
                
                for record in batch:
                    try:
                        # Prepare record with lookup field mappings
                        prepared_record = self.prepare_record_for_insert(
                            record, object_name, lookup_fields, external_id_field
                        )
                        
                        if prepared_record is None:
                            stats['skipped'] += 1
                            continue
                        
                        # Insert or Upsert record
                        if external_id_field:
                            # Use upsert with external ID
                            external_id_value = prepared_record.get(external_id_field)
                            if external_id_value:
                                getattr(self.target, object_name).upsert(
                                    prepared_record,
                                    external_id_field
                                )
                                logger.debug(f"Upserted record with external ID: {external_id_value}")
                            else:
                                logger.warning(f"External ID field '{external_id_field}' not found in record, skipping upsert")
                                stats['skipped'] += 1
                                continue
                        else:
                            # Regular insert
                            getattr(self.target, object_name).create(prepared_record)
                        
                        stats['success'] += 1
                        
                        # Store ID mapping for future lookup field resolution
                        if 'Id' in record:
                            # Query the created record to get its new ID
                            # This is a simplified approach - in production, you might want to use a more efficient method
                            # For now, we'll store the mapping after successful insert
                            pass  # We'll need to get the created record's ID
                        
                    except Exception as e:
                        error_msg = f"Error transferring record: {str(e)}"
                        logger.error(error_msg)
                        stats['failed'] += 1
                        stats['errors'].append({
                            'record': record,
                            'error': str(e)
                        })
            
            # Get ID mappings after successful inserts
            # This is important for related objects that reference this one
            if stats['success'] > 0:
                self._update_id_mappings(object_name, source_records, external_id_field)
            
            logger.info(f"Transfer complete for {object_name}")
            logger.info(f"Total: {stats['total']}, Success: {stats['success']}, "
                       f"Failed: {stats['failed']}, Skipped: {stats['skipped']}")
            
            return stats
            
        except Exception as e:
            logger.error(f"Fatal error during transfer: {str(e)}")
            raise
    
    def _update_id_mappings(self, object_name: str, source_records: List[Dict],
                           external_id_field: str = None):
        """
        Update ID mappings after successful record creation
        
        Args:
            object_name: Name of the Salesforce object
            source_records: Original source records
            external_id_field: External ID field used for matching
        """
        logger.info(f"Updating ID mappings for {object_name}...")
        
        try:
            if external_id_field:
                # Use external ID to map records
                # Query target records with external IDs
                external_ids = [rec.get(external_id_field) for rec in source_records 
                               if rec.get(external_id_field)]
                
                if external_ids:
                    # Build SOQL query to get target records
                    external_ids_str = "', '".join([str(eid) for eid in external_ids])
                    soql = f"SELECT Id, {external_id_field} FROM {object_name} WHERE {external_id_field} IN ('{external_ids_str}')"
                    
                    result = self.target.query(soql)
                    target_records = result['records']
                    
                    # Create mapping: external_id -> target_id
                    external_id_mapping = {
                        rec[external_id_field]: rec['Id'] 
                        for rec in target_records
                    }
                    
                    # Map source IDs to target IDs
                    for source_record in source_records:
                        source_id = source_record.get('Id')
                        external_id_value = source_record.get(external_id_field)
                        
                        if source_id and external_id_value in external_id_mapping:
                            self.id_mappings[object_name][source_id] = external_id_mapping[external_id_value]
            else:
                # If no external ID, we need a different approach
                # This is more complex and might require querying based on multiple fields
                # For simplicity, we'll implement a basic version here
                logger.warning("ID mapping without external ID is limited - recommend using external IDs")
                
                # Create a mapping based on Name field (if it exists) for common objects
                name_fields = ['Name', 'FirstName', 'LastName', 'Email']
                for name_field in name_fields:
                    names = [rec.get(name_field) for rec in source_records 
                            if rec.get(name_field)]
                    if names:
                        try:
                            names_str = "', '".join([str(name) for name in names])
                            soql = f"SELECT Id, {name_field} FROM {object_name} WHERE {name_field} IN ('{names_str}')"
                            result = self.target.query(soql)
                            target_records = result['records']
                            
                            name_mapping = {
                                rec[name_field]: rec['Id'] 
                                for rec in target_records
                            }
                            
                            for source_record in source_records:
                                source_id = source_record.get('Id')
                                name_value = source_record.get(name_field)
                                
                                if source_id and name_value in name_mapping:
                                    self.id_mappings[object_name][source_id] = name_mapping[name_value]
                            break  # Use first available name field
                        except Exception as e:
                            logger.debug(f"Could not map using {name_field}: {str(e)}")
                            continue
            
            logger.info(f"Created {len(self.id_mappings.get(object_name, {}))} ID mappings for {object_name}")
            
        except Exception as e:
            logger.error(f"Error updating ID mappings: {str(e)}")
    
    def transfer_objects_in_order(self, object_dependencies: List[Dict],
                                batch_size: int = 50) -> Dict:
        """
        Transfer multiple objects respecting dependencies (parent before child)
        
        Args:
            object_dependencies: List of dictionaries with object transfer info
                Format: [
                    {'object_name': 'Account', 'external_id_field': 'External_ID__c'},
                    {'object_name': 'Contact', 'external_id_field': 'External_ID__c'},
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
            'total_failed': 0
        }
        
        for obj_config in object_dependencies:
            object_name = obj_config['object_name']
            external_id_field = obj_config.get('external_id_field')
            query_fields = obj_config.get('query_fields')
            
            logger.info(f"\n{'='*60}")
            logger.info(f"Transferring {object_name}")
            logger.info(f"{'='*60}")
            
            try:
                stats = self.transfer_records(
                    object_name=object_name,
                    batch_size=batch_size,
                    external_id_field=external_id_field,
                    query_fields=query_fields
                )
                
                overall_stats['objects'][object_name] = stats
                overall_stats['total_records'] += stats['total']
                overall_stats['total_success'] += stats['success']
                overall_stats['total_failed'] += stats['failed']
                
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
        logger.info("="*60)
        
        return overall_stats
    
    def auto_transfer_all_objects(self, object_filter: List[str] = None,
                                 include_custom: bool = True,
                                 include_standard: bool = True,
                                 batch_size: int = 50,
                                 external_id_priority: List[str] = None) -> Dict:
        """
        Automatically discover dependencies and transfer all objects in correct order
        
        Args:
            object_filter: List of specific objects to transfer (None = all available)
            include_custom: Include custom objects (__c suffix)
            include_standard: Include standard objects
            batch_size: Number of records to process in each batch
            external_id_priority: Priority list for external ID field names
        
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
        
        # Prepare object configurations with external ID detection
        object_dependencies = []
        for obj_name in transfer_order:
            # Try to find external ID field
            external_id_field = self.find_external_id_fields(obj_name)
            
            obj_config = {
                'object_name': obj_name,
                'external_id_field': external_id_field
            }
            object_dependencies.append(obj_config)
            
            logger.info(f"{obj_name} -> External ID: {external_id_field if external_id_field else 'None'}")
        
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
        # and transfer them in the correct order
        
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
        
        # OPTION 2: Manual transfer with explicit object list (legacy method)
        # Define objects to transfer in dependency order (parents before children)
        # object_dependencies = [
        #     {
        #         'object_name': 'Account',
        #         'external_id_field': 'External_ID__c',  # Use your external ID field
        #         # 'query_fields': ['Id', 'Name', 'External_ID__c', 'BillingCity', 'BillingState']
        #     },
        #     {
        #         'object_name': 'Contact',
        #         'external_id_field': 'External_ID__c',
        #     },
        #     {
        #         'object_name': 'Opportunity',
        #         'external_id_field': 'External_ID__c',
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