import json
from simple_salesforce import Salesforce

# 1. Configuration
SF_USERNAME = 'your_username'
SF_PASSWORD = 'your_password'
SF_TOKEN = 'your_security_token'
SF_INSTANCE = 'login' 
CACHE_FILE = 'automation_cache.json'

sf = Salesforce(username=SF_USERNAME, password=SF_PASSWORD, security_token=SF_TOKEN, domain=SF_INSTANCE)

def get_active_automation():
    """
    Scans for active Triggers and Flows related to Opportunity.
    """
    cache_data = {"ApexTrigger": [], "Flow": []}
    
    # --- Part 1: Apex Triggers ---
    print("Searching for Apex Triggers...")
    triggers = sf.mdapi.list_metadata(queries=[{'type': 'ApexTrigger'}])
    # Filter for Opportunity triggers (requires reading metadata to check TableName)
    trigger_names = [t['fullName'] for t in triggers]
    if trigger_names:
        # Read in chunks of 10 to be safe with API limits
        details = sf.mdapi.ApexTrigger.read(trigger_names)
        if isinstance(details, dict): details = [details]
        
        for t in details:
            # Check if it belongs to Opportunity and is currently Active
            if t.get('entityId') == 'Opportunity' and t.get('status') == 'Active':
                cache_data["ApexTrigger"].append(t['fullName'])

    # --- Part 2: Flows (Includes Flow Triggers / Record-Triggered Flows) ---
    print("Searching for Flows...")
    # We use the Tooling API for Flows because it's much faster to filter by object
    flow_query = (
        "SELECT DeveloperName, Status FROM FlowDefinitionView "
        "WHERE TriggerObjectOrEventId = 'Opportunity' AND Status = 'Active'"
    )
    flow_results = sf.tooling_execute(flow_query)
    cache_data["Flow"] = [f['DeveloperName'] for f in flow_results.get('records', [])]

    return cache_data

def disable_automation():
    active_items = get_active_automation()
    
    with open(CACHE_FILE, 'w') as f:
        json.dump(active_items, f)
    
    # Disable Triggers
    for t_name in active_items["ApexTrigger"]:
        # We must provide the full body/content for triggers when updating status
        # This is why 'status' is often easier to toggle via the Tooling API
        sf.tooling_execute(f"SELECT Id FROM ApexTrigger WHERE Name = '{t_name}'")
        # Optimization: Using Tooling API for status toggles is less 'heavy' than MDAPI for code
        trigger_id = sf.tooling_execute(f"SELECT Id FROM ApexTrigger WHERE Name = '{t_name}'")['records'][0]['Id']
        sf.tooling_execute({
            'method': 'PATCH',
            'url': f"tooling/sobjects/ApexTrigger/{trigger_id}",
            'json': {"Status": "Inactive"}
        })
        print(f"Disabled Trigger: {t_name}")

    # Disable Flows
    for f_name in active_items["Flow"]:
        # Deactivating a flow is done by updating the FlowDefinition
        flow_def = sf.tooling_execute(f"SELECT Id FROM FlowDefinition WHERE DeveloperName = '{f_name}'")['records'][0]
        sf.tooling_execute({
            'method': 'PATCH',
            'url': f"tooling/sobjects/FlowDefinition/{flow_def['Id']}",
            'json': {"Metadata": {"activeVersionNumber": 0}}
        })
        print(f"Disabled Flow: {f_name}")

def restore_automation():
    with open(CACHE_FILE, 'r') as f:
        cache = json.load(f)

    # Restore Triggers
    for t_name in cache["ApexTrigger"]:
        trigger_id = sf.tooling_execute(f"SELECT Id FROM ApexTrigger WHERE Name = '{t_name}'")['records'][0]['Id']
        sf.tooling_execute({'method': 'PATCH', 'url': f"tooling/sobjects/ApexTrigger/{trigger_id}", 'json': {"Status": "Active"}})
        print(f"Restored Trigger: {t_name}")

    # Restore Flows (Resets to latest version)
    for f_name in cache["Flow"]:
        flow_def = sf.tooling_execute(f"SELECT Id FROM FlowDefinition WHERE DeveloperName = '{f_name}'")['records'][0]
        # Setting to null/omitting usually defaults to activating the latest version
        # Or you can cache the specific version number in the disable step
        sf.tooling_execute({
            'method': 'PATCH', 
            'url': f"tooling/sobjects/FlowDefinition/{flow_def['Id']}", 
            'json': {"Metadata": {"activeVersionNumber": None}} 
        })
        print(f"Restored Flow: {f_name}")

# --- Run ---
disable_automation()
# restore_automation()