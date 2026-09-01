# IAM Users Bulk Edition

Subroutines to simplify bulk edition of EWC IAM users.

## Inputs
> 💡 For all available input options, checkout the [inputs.schema.json](./vars/inputs.schema.json) definition:

```yaml
# vars/inputs.yml
---
schema_version: 1
tenancy:

  name: my-test-realm

  users:
    - username: john.smith
      state: present
      email: John.Smith@example.com
      first_name: John
      last_name: Smith
      roles:
        - name: ewc-iam-user
        - name: ewc-jhub-training-f7983a-attendee

  defaults:
    deletion_protection: true 
    required_actions:
      - UPDATE_PASSWORD
    roles_mode: merge
```


## Usage

```bash
ansible-playbook iam-users-bulk-edition.yml
```
