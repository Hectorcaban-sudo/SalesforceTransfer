# Salesforce Data Transfer Script - Enhanced with Auto Discovery

## 🚀 Overview

This Python script automates data transfer between Salesforce sandboxes with **automatic parent-child relationship discovery**, eliminating the need to manually determine object transfer order. It handles lookup fields, supports upsert operations, and ensures referential integrity.

## ✨ Key Features

### 🧠 **Intelligent Relationship Discovery**
- **Automatic dependency analysis** - Discovers all objects and their relationships
- **Topological sorting** - Automatically determines optimal transfer order (parents before children)
- **Lookup field mapping** - Identifies and resolves all reference and master-detail relationships
- **Circular dependency handling** - Detects and manages complex relationship scenarios

### 🔧 **Smart Data Transfer**
- **Upsert support** - Uses external ID fields to update existing records
- **Automatic external ID detection** - Finds the best external ID field for each object
- **Batch processing** - Configurable batch sizes for efficient transfers
- **ID mapping system** - Maintains source-to-target ID mappings for accurate lookups

### 📊 **Comprehensive Monitoring**
- **Detailed logging** - Step-by-step progress tracking
- **Error handling** - Detailed error tracking and reporting
- **Statistics dashboard** - Complete transfer statistics
- **JSON export** - Results saved for analysis and auditing

## 📋 Requirements

```bash
pip install simple_salesforce
```

## 🚀 Quick Start

### 1. Basic Automatic Transfer

The script will automatically discover all objects, their relationships, and transfer them in the correct order:

```python
from salesforce_data_transfer import SalesforceDataTransfer

# Configuration
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

# Initialize and transfer
transfer = SalesforceDataTransfer(source_config, target_config)
results = transfer.auto_transfer_all_objects()
```

### 2. Transfer Specific Objects Only

```python
# Transfer only specific objects (dependency order is still auto-discovered)
results = transfer.auto_transfer_all_objects(
    object_filter=['Account', 'Contact', 'Opportunity', 'Case']
)
```

### 3. Custom Objects Only

```python
# Transfer only custom objects
results = transfer.auto_transfer_all_objects(
    include_custom=True,
    include_standard=False
)
```

## 🛠️ Advanced Configuration

### Custom Batch Size

```python
results = transfer.auto_transfer_all_objects(
    batch_size=100  # Process 100 records at a time
)
```

### Manual Transfer (Legacy Mode)

If you need explicit control over object order:

```python
object_dependencies = [
    {'object_name': 'Account', 'external_id_field': 'External_ID__c'},
    {'object_name': 'Contact', 'external_id_field': 'External_ID__c'},
    {'object_name': 'Opportunity', 'external_id_field': 'External_ID__c'},
]

results = transfer.transfer_objects_in_order(object_dependencies)
```

## 📊 Understanding the Output

### Transfer Results Structure

```json
{
  "objects": {
    "Account": {
      "total": 1000,
      "success": 998,
      "failed": 2,
      "skipped": 0,
      "errors": [...]
    }
  },
  "total_records": 5000,
  "total_success": 4985,
  "total_failed": 15,
  "dependency_graph": {
    "Contact": ["Account"],
    "Opportunity": ["Account", "Contact"]
  },
  "transfer_order": [
    "Account",
    "Contact",
    "Opportunity"
  ],
  "total_objects": 3
}
```

### Log Output Example

```
2024-01-15 10:00:00 - INFO - Connecting to Source sandbox...
2024-01-15 10:00:01 - INFO - Successfully connected to Source sandbox
2024-01-15 10:00:01 - INFO - Connecting to Target sandbox...
2024-01-15 10:00:02 - INFO - Successfully connected to Target sandbox
2024-01-15 10:00:02 - INFO - Discovering all objects in Salesforce...
2024-01-15 10:00:05 - INFO - Discovered 45 transferable objects
2024-01-15 10:00:05 - INFO - Building dependency graph...
2024-01-15 10:00:08 - INFO - Contact depends on: Account
2024-01-15 10:00:08 - INFO - Opportunity depends on: Account, Contact
2024-01-15 10:00:08 - INFO - Determining optimal transfer order...
2024-01-15 10:00:08 - INFO - Optimal transfer order determined: Account -> Contact -> Opportunity
2024-01-15 10:00:08 - INFO - Account -> External ID: External_ID__c
2024-01-15 10:00:08 - INFO - Contact -> External ID: Contact_ID__c
2024-01-15 10:00:08 - INFO - Starting multi-object data transfer...
```

## 🎯 How Auto Discovery Works

### 1. Object Discovery
- Queries Salesforce metadata to find all accessible objects
- Filters out system objects, metadata objects, and non-creatable objects
- Identifies custom vs standard objects

### 2. Relationship Analysis
- Analyzes each object's fields for lookup relationships
- Identifies both reference and master-detail relationships
- Builds a dependency graph showing parent-child relationships

### 3. Transfer Order Calculation
- Uses topological sorting algorithm to determine optimal order
- Ensures all parent objects are transferred before children
- Handles circular dependencies gracefully

### 4. External ID Detection
- Scans for common external ID field patterns
- Checks for explicit external ID flags
- Prioritizes: `External_ID__c`, `ExternalId`, `ExternalID`, `Id`

## 🔍 Troubleshooting

### Common Issues

**Issue**: Circular dependencies detected
- **Solution**: Script automatically handles this by adding remaining objects at the end

**Issue**: No external ID field found
- **Solution**: Objects without external IDs will be inserted without upsert capability

**Issue**: Lookup field not resolved
- **Solution**: Ensure parent objects are transferred before children (handled automatically)

**Issue**: Rate limits exceeded
- **Solution**: Reduce batch size or add delays between batches

## 📝 Best Practices

1. **Test First**: Run with a small object filter before transferring all data
2. **Use External IDs**: Add external ID fields to enable upsert operations
3. **Monitor Logs**: Watch for warnings about missing ID mappings
4. **Backup**: Always backup target sandbox before large transfers
5. **Review Dependencies**: Check the generated dependency graph

## 🔐 Security Considerations

- Store credentials in environment variables or secure config files
- Never commit credentials to version control
- Use least-privileged API user accounts
- Consider using Salesforce Connected Apps with OAuth

## 📈 Performance Tips

- **Batch Size**: Start with 50-100 records per batch
- **Network**: Run on same network as Salesforce for best performance
- **Parallel Processing**: For large transfers, consider running multiple instances
- **Selective Transfer**: Transfer only necessary objects to save time

## 🤝 Contributing

Suggestions and improvements welcome! Key areas for enhancement:
- Parallel batch processing
- Delta transfer (only changed records)
- Progress bar for large transfers
- Web UI for monitoring

## 📄 License

This script is provided as-is for Salesforce data transfer purposes.

## 🆘 Support

For issues or questions:
1. Check the logs for detailed error messages
2. Review the transfer_results.json file
3. Verify API credentials and permissions
4. Ensure objects are accessible in both sandboxes