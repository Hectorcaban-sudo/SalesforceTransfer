import json
from simple_salesforce import Salesforce

# 1. Configuration
SF_USERNAME = 'your_username'
SF_PASSWORD = 'your_password'
SF_TOKEN = 'your_security_token'
SF_INSTANCE = 'login' 
CACHE_FILE = 'opp_automation_backup.json'

sf = Salesforce(username=SF_USERNAME, password=SF_PASSWORD, security_token=SF_TOKEN, domain=SF_INSTANCE)

def get_active_elements():
    """Finds only Opportunity-specific active automation."""
    cache = {"ValidationRules": [], "Triggers": [], "Flows": []}

    # --- 1. Validation Rules (Metadata API) ---
    print("Scanning Opportunity Validation Rules...")
    # Using list_metadata with a specific query for the type
    all_rules_list = sf.mdapi.list_metadata(queries=[{'type': 'ValidationRule'}])
    
    # Filter for Opportunity only
    opp_rule_names = [r['fullName'] for r in all_rules_list if r['fullName'].startswith('Opportunity.')]
    
    if opp_rule_names:
        rules_meta = sf.mdapi.ValidationRule.read(opp_rule_names)
        if isinstance(rules_meta, dict): rules_meta = [rules_meta]
        # Only cache those that are currently True
        cache["ValidationRules"] = [r['fullName'] for r in rules_meta if r.get('active') is True]

    # --- 2. Apex Triggers (Tooling API) ---
    print("Scanning Opportunity Apex Triggers...")
    # TableEnumOrId filters specifically for the Opportunity object
    trig_query = "SELECT Id, Name FROM ApexTrigger WHERE TableEnumOrId = 'Opportunity' AND Status = 'Active'"
    trig_res = sf.tooling_execute(trig_query)
    cache["Triggers"] = [{"id": r['Id'], "name": r['Name']} for r in trig_res.get('records', [])]

    # --- 3. Flows / Flow Triggers (Tooling API) ---
    print("Scanning Opportunity Flows...")
    # FlowDefinitionView is the most reliable way to filter by Trigger Object
    flow_query = (
        "SELECT Id, DeveloperName, ActiveVersionNumber FROM FlowDefinition "
        "WHERE DeveloperName IN (SELECT DeveloperName FROM FlowDefinitionView "
        "WHERE TriggerObjectOrEventId = 'Opportunity' AND Status = 'Active')"
    )
    flow_res = sf.tooling_execute(flow_query)
    cache["Flows"] = [{"id": r['Id'], "name": r['DeveloperName'], "version": r['ActiveVersionNumber']} for r in flow_res.get('records', [])]

    return cache

def disable_all():
    data = get_active_elements()
    if not any(data.values()):
        print("No active automation found to disable.")
        return

    with open(CACHE_FILE, 'w') as f:
        json.dump(data, f, indent=4)
    
    # Disable Validation Rules - Passing the object directly
    for v_name in data["ValidationRules"]:
        sf.mdapi.ValidationRule.update({"fullName": v_name, "active": False})
        print(f"Disabled Val Rule: {v_name}")

    # Disable Triggers
    for t in data["Triggers"]:
        sf.tooling_execute(method='PATCH', url=f"tooling/sobjects/ApexTrigger/{t['id']}", json={"Status": "Inactive"})
        print(f"Disabled Trigger: {t['name']}")

    # Disable Flows
    for f in data["Flows"]:
        sf.tooling_execute(method='PATCH', url=f"tooling/sobjects/FlowDefinition/{f['id']}", json={"Metadata": {"activeVersionNumber": 0}})
        print(f"Disabled Flow: {f['name']}")

def restore_all():
    try:
        with open(CACHE_FILE, 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        print("Backup file not found.")
        return

    # Restore Validation Rules
    for v_name in data["ValidationRules"]:
        sf.mdapi.ValidationRule.update({"fullName": v_name, "active": True})
        print(f"Restored Val Rule: {v_name}")

    # Restore Triggers
    for t in data["Triggers"]:
        sf.tooling_execute(method='PATCH', url=f"tooling/sobjects/ApexTrigger/{t['id']}", json={"Status": "Active"})
        print(f"Restored Trigger: {t['name']}")

    # Restore Flows
    for f in data["Flows"]:
        sf.tooling_execute(method='PATCH', url=f"tooling/sobjects/FlowDefinition/{f['id']}", json={"Metadata": {"activeVersionNumber": f['version']}})
        print(f"Restored Flow: {f['name']} (v{f['version']})")

# --- Execution ---
# disable_all()
# restore_all()