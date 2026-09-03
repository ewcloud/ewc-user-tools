# IAM Users Bulk Edition

Subroutines to simplify bulk edition of EWC IAM users.

## Functionality
> ✅ Combinations of all actions listed below can be applied within the same run.

* Adds new users
  * Emails new users with 1st-login action requirements (update password, etc.)
* Updates existing users
  * Updates first and/or last names 
  * Attaches new or replaces existing IAM roles
* Disables users
* Deletes users
* (Optional) Runs interactively within JupyterLab and from input data in CSV format

## Prerequisites

* Obtain a username and password from a IAM user with the `ewc-iam-tenant-admin` role.
* Verify Python version `>=3.10` is available on your working environment

## Usage

### Run interactively (Jupyter Notebook)

Start JupyterLab, copy the [example notebook](./notebooks/) and follow along to apply necessary IAM changes based on input data (CSV format).

### Run programmatically

#### 1. Setup working environment

  ```bash
  pip install -r requirements.txt
  ```

#### 2. Configure inputs

>💡 For complete information on required and optional input attributes, checkout the [templates/inputs.schema.json](./templates/inputs.schema.json) definition.

The included [input configuration](./vars/inputs.yml), in YAML format, exemplifies most common supported cases. Customize according to your needs:
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
  # These apply to every user unless overridden per-user above
  defaults:
    deletion_protection: true
    state: present
    enabled: true
    email_verified: true
    initial_login_actions:
      - UPDATE_PASSWORD
    roles:
      - name: ewc-iam-user
      - name: ewc-app-user
    roles_reconciliation_mode: replace
    first_name: Unknown
    last_name: Unknown

```


#### 3. Execute
>⚠️ You will be prompted to enter IAM username and password. This is required to make IAM changes on your behalf.

```bash
ansible-playbook iam-users-bulk-edition.yml
```


## Resources

* [EWC Identity and Access Management (IAM) Service](https://confluence.ecmwf.int/spaces/EWCLOUDKB/pages/439585127/EWC+Identity+and+Access+Management+IAM+Service)