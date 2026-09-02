# IAM Users Bulk Edition

Subroutines to simplify bulk edition of EWC IAM users.

## Inputs
> 💡 For all available input options, checkout the [inputs.schema.json](./vars/inputs.schema.json) definition:

```yaml
# vars/inputs.yml
---
schema_version: 1
tenancy:

  # --- Tenancy Spec ---
  name: my-ewc-tenancy

  # --- User Spec ---
  users:
    - email: john.smith@example.com     # <- Adds or update user

    - email: ada.wong@example.com       # <- Adds or update user,
      enabled: false                    #    but disables login

    - email: carlos.perez@example.com  # <- Removes user, if exists
      state: absent
      deletion_protection: false

  # --- Defaults ---
  # These apply to every user unless overridden per-user within users section above
  defaults:
    deletion_protection: true
    state: present
    enabled: true
    email_verified: true
    required_actions:
      - UPDATE_PASSWORD
    roles:
      - name: ewc-iam-user
      - name: ewc-app-user
    roles_reconciliation_mode: merge

```


## Usage

### Interactive Mode
> ⚠️ When running in interactive mode, you will be prompts for your IAM username and password
```bash
ansible-playbook iam-users-bulk-edition.yml
```

### Non-interactive Mode

```bash
ansible-playbook \
  -e '{
        "iam_tenant_admin_username": "<redacted>",
        "iam_tenant_admin_password": "<redacted>"
      }' \
  iam-users-bulk-edition.yml
```
