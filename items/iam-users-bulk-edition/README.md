# IAM Users Bulk Edition

Subroutines to simplify bulk edition of EWC IAM users.

## Functionality

* Adds new users
  * Emails new users with any required actions (verify email, update profile, etc.)
* Updates existing users
  * Updates first and/or last names 
  * Attaches new or replaces existing IAM roles
* Disables users
* Deletes users
* Combines all of the above actions in a single pass

## Prerequisites

* Obtain a username and password from a IAM user with the `ewc-iam-tenant-admin` role.
* Verify Python version `>=3.10` is available on your working environment

## Usage

### Run interactively via Jupyter Notebook


### Run programmatically via native tooling (Ansible)

#### 1. Setup working environment

  ```bash
  pip install -r requirements.txt
  ```

#### 2. Configure inputs

>💡 For complete information on required and optional input attributes, checkout the [templates/inputs.schema.json](./templates/inputs.schema.json) definition.

An [example configuration](./vars/inputs.yml) covering most common supported cases is available. Customize freely to suit your needs:
```yaml
# vars/inputs.yml
---
schema_version: 1
tenancy:

  # --- Tenancy Spec ---
  name: my-ewc-tenancy

  # --- User Spec ---
  users:
    - email: john.smith@example.com     # <- Adds/updates user with all defaults (email set as username)

    - email: ada.wong@example.com       # <- Adds/updates user with all defaults (email set as username)
      enabled: false                    #    and disables login

    - email: carlos.perez@example.com   # <- Removes user, if exists
      state: absent
      deletion_protection: false

    - email: philipp.mayer@example.com  # <- Adds/updates user with all defaults and
      username: pmayer                  #    optional username,
      first_name: Philipp               #    optional first name and
      last_name: Mayer                  #    optional last name

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
    roles_reconciliation_mode: replace
    first_name: Unknown
    last_name: Unknown

```


#### 3. Trigger execution
>⚠️ You will be prompted to enter IAM username and password. This is required to make IAM changes on your behalf.

```bash
ansible-playbook iam-users-bulk-edition.yml
```


## Resources

* [EWC Identity and Access Management (IAM) Service](https://confluence.ecmwf.int/spaces/EWCLOUDKB/pages/439585127/EWC+Identity+and+Access+Management+IAM+Service)