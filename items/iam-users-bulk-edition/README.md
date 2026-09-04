# IAM Users Bulk Edition

Subroutines to simplify bulk edition of [EWC IAM](https://confluence.ecmwf.int/spaces/EWCLOUDKB/pages/439585127/EWC+Identity+and+Access+Management+IAM+Service) users.

## Functionality
> ✅ Combinations of all features listed below are also supported.

* Adds new users
  * Emails new users with 1st-login action requirements (update password, etc.)
* Updates existing users
  * Updates first and/or last names 
  * Attaches new or replaces existing EWC IAM roles
* Disables users
* Deletes users
* (Optional) Runs step-by-step via Jupyter Notebook, on an arbitrary amount of tabular user data in `CSV` format

## Prerequisites

* Obtain a username and password from a EWC IAM user with the `ewc-iam-tenant-admin` role.
* Verify Python version `>=3.10` is available on your working environment

## Usage

### Run step-by-step via Jupyter Notebook

Open the [iam-user-bulk-edition-via-jupyter.ipynb](https://github.com/ewcloud/ewc-user-tools/tree/main/items/iam-users-bulk-edition/notebooks) notebook, start the runtime, and execute cells top to bottom to apply access/permission changes.

### Run programmatically

#### 1. Setup working environment

  ```bash
  pip install -r requirements.txt
  ```

#### 2. Configure inputs

>💡 For complete information on required and optional input attributes, checkout the [templates/inputs.schema.json](./templates/inputs.schema.json) definition.

The included [input configuration](./vars/inputs.yml), in `YAML` format, exemplifies most common supported cases. Customize according to your needs:
```yaml
# vars/inputs.yml
---
schema_version: 1
tenancy:

  # --- Tenancy Specification ---
  name: my-ewc-tenancy

  # --- User Specification ---
  users:
    - email: john.smith@example.com     # <- Adds/updates user with all defaults (email reused as username)

    - email: ada.wong@example.com       # <- Adds/updates user with all defaults (email reused as username)
      enabled: false                    #    and disables login

    - email: carlos.perez@example.com   # <- Removes user, if exists
      state: absent

    - email: philipp.mayer@example.com  # <- Adds/updates user with 6 out of 10 defaults,
      username: pmayer                  #    optional username,
      first_name: Philipp               #    optional first name,
      last_name: Mayer                  #    optional last name
      roles:                            #    and optional role overrides
        - name: ewc-jhub-lab-cfe2f3
        - name: ewc-jhub-lab-f54924

  # --- Defaults ---
  # These apply to every user unless overridden per-user above
  defaults:
    deletion_protection: false
    state: present
    enabled: true
    email_verified: true
    initial_login_actions:
      - UPDATE_PASSWORD
    roles:
      - name: ewc-jhub-lab-cfe2f3
    roles_reconciliation_mode: replace
    first_name: Unknown
    last_name: Unknown

```


#### 3. Execute
>⚠️ You will be prompted to enter EWC IAM tenancy admin username and password. This is required by the tooling to make changes on your behalf.

```bash
ansible-playbook iam-users-bulk-edition.yml
```


## Resources

* [EWC Identity and Access Management (IAM) Service](https://confluence.ecmwf.int/spaces/EWCLOUDKB/pages/439585127/EWC+Identity+and+Access+Management+IAM+Service)