import json
from simple_salesforce import Salesforce

# 1. Configuration
SF_USERNAME = 'your_username'
SF_PASSWORD = 'your_password'
SF_TOKEN = 'your_security_token'
SF_INSTANCE = 'login' 
CACHE_FILE = 'active_opportunity_rules.json'

sf = Salesforce(username=SF_USERNAME, password=SF_PASSWORD, security_token=SF_TOKEN, domain=SF_INSTANCE)

def disable_and_cache_rules():
    print("Fetching Opportunity metadata...")
    obj_metadata = sf.mdapi.CustomObject.read('Opportunity')
    
    if 'validationRules' not in obj_metadata:
        print("No validation rules found.")
        return

    rules = obj_metadata['validationRules']
    if isinstance(rules, dict): rules = [rules]

    # Find rules that are currently active
    active_rule_names = [r['fullName'] for r in rules if r.get('active') is True]
    
    if not active_rule_names:
        print("No active rules found to disable.")
        return

    # Save to local JSON cache
    with open(CACHE_FILE, 'w') as f:
        json.dump(active_rule_names, f)
    print(f"Saved {len(active_rule_names)} active rules to {CACHE_FILE}")

    # Set active to False for the rules we found
    for r in rules:
        if r['fullName'] in active_rule_names:
            r['active'] = False

    # Push update back to Salesforce
    sf.mdapi.CustomObject.update('Opportunity', obj_metadata)
    print("Successfully disabled all active validation rules.")

def restore_rules_from_cache():
    try:
        with open(CACHE_FILE, 'r') as f:
            rules_to_enable = json.load(f)
    except FileNotFoundError:
        print("Cache file not found. Nothing to restore.")
        return

    print(f"Restoring {len(rules_to_enable)} rules from cache...")
    obj_metadata = sf.mdapi.CustomObject.read('Opportunity')
    rules = obj_metadata['validationRules']
    if isinstance(rules, dict): rules = [rules]

    for r in rules:
        if r['fullName'] in rules_to_enable:
            r['active'] = True

    sf.mdapi.CustomObject.update('Opportunity', obj_metadata)
    print("Successfully restored rules to their original active state.")

# --- Execution ---
# Step 1: Run this before your data load
disable_and_cache_rules()

# Step 2: Run this after your data load
# restore_rules_from_cache()