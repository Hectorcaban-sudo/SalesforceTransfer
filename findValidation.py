import json
from simple_salesforce import Salesforce

# 1. Configuration
SF_USERNAME = 'your_username'
SF_PASSWORD = 'your_password'
SF_TOKEN = 'your_security_token'
SF_INSTANCE = 'login' 
CACHE_FILE = 'active_validation_rules_cache.json'

sf = Salesforce(username=SF_USERNAME, password=SF_PASSWORD, security_token=SF_TOKEN, domain=SF_INSTANCE)

def get_opportunity_validation_rules():
    """
    Uses mdapi.list to find all rules, then mdapi.read to get their details.
    """
    print("Searching for Validation Rules on Opportunity...")
    
    # Step 1: List all ValidationRules
    # 'queries' takes a list of dicts specifying the metadata type
    list_metadata = sf.mdapi.list_metadata(queries=[{'type': 'ValidationRule'}])
    
    # Step 2: Filter for Opportunity rules only
    # The 'fullName' for validation rules is always 'ObjectName.RuleName'
    opp_rule_names = [
        item['fullName'] for item in list_metadata 
        if item['fullName'].startswith('Opportunity.')
    ]
    
    if not opp_rule_names:
        print("No Opportunity validation rules found.")
        return []

    # Step 3: Read the actual metadata for these rules to check 'active' status
    # mdapi.read takes a list of fullNames (max 10 per call usually, but simple-salesforce handles mapping)
    rules_metadata = sf.mdapi.ValidationRule.read(opp_rule_names)
    
    # simple-salesforce returns a list if multiple, a dict if one
    if isinstance(rules_metadata, dict):
        rules_metadata = [rules_metadata]
        
    return rules_metadata

def disable_and_cache():
    all_rules = get_opportunity_validation_rules()
    active_rules_to_disable = [r for r in all_rules if r.get('active') is True]

    if not active_rules_to_disable:
        print("No active rules found to disable.")
        return

    # Cache the fullNames of currently active rules
    cache_names = [r['fullName'] for r in active_rules_to_disable]
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache_names, f)
    print(f"Cached {len(cache_names)} active rules to {CACHE_FILE}")

    # Disable them
    for rule in active_rules_to_disable:
        rule['active'] = False
        # FIX: Only 1 positional argument (the metadata dict)
        sf.mdapi.ValidationRule.update(rule)
        print(f"Disabled: {rule['fullName']}")

def restore_from_cache():
    try:
        with open(CACHE_FILE, 'r') as f:
            names_to_activate = json.load(f)
    except FileNotFoundError:
        print("No cache file found.")
        return

    print(f"Restoring {len(names_to_activate)} rules...")
    
    # Fetch the current state of these specific rules
    rules_to_fix = sf.mdapi.ValidationRule.read(names_to_activate)
    if isinstance(rules_to_fix, dict):
        rules_to_fix = [rules_to_fix]

    for rule in rules_to_fix:
        rule['active'] = True
        sf.mdapi.ValidationRule.update(rule)
        print(f"Restored: {rule['fullName']}")

# --- Execution ---
disable_and_cache()
# restore_from_cache()