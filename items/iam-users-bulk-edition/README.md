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

Open the [iam-user-bulk-edition-via-jupyter.ipynb](https://github.com/ewcloud/ewc-user-tools/tree/1.2.0/items/iam-users-bulk-edition/notebooks) notebook, start the runtime, and execute cells top to bottom to apply access/permission changes.

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

    - email: philipp.mayer@example.com  # <- Adds/updates user with overrides for
      username: pmayer                  #    optional username,
      first_name: Philipp               #    optional first name,
      last_name: Mayer                  #    optional last name
      roles:                            #    and optional roles to
        - name: ewc-iam-user            #    access their EWC IAM profile
        - name: ewc-jhub-lab-cfe2f3     #    access EWC Jupyter Hub (lab session ID cfe2f3)  
        - name: ewc-jhub-lab-f54924     #    access EWC Jupyter Hub (lab session ID f54924) 

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
      - name: ewc-iam-user
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

## Best Practices

1. Prefer a single input file (`YAML` or `CSV`) per tenancy, to facilitate auditing tasks such as  ensuring a single config per user. Consider these edge-cases:

  * A user is marked as absent in one input config, the change is applied, and the entry is removed altogether right after. Yet their email appears on a second input config and rentroduced accidentally with login enabled by default.
  * A user requires access to multiple EWC Jupyter Hub environments, but necessary roles to are stated by different input configs, with the last one applied replacing the roles added by the former ones.

2. Keep the input config clean of users which are confirmed to have been marked as absent and for which said change was applied.

3. Backup logs or track your input file with GIT or similar version management tools, to better trace access changes applied by this tooling over time.

## Development

1. Fork this repository and change into the Item's subdirectory
```bash
git clone https://github.com/ewcloud/ewc-user-tools.git && cd ./ewc-user-tools/item/iam-users-bulk-edition
```

2. Install the development dependencies
```bash
pip install -r dev-requirements.yml
```

3. Modify the local code and test changes.

4. Push code to your fork and open a pull request.

## Code Styling
Execute all linting tests by running:

```bash
ansible-lint --offline .
```

## Resources

* [EWC Identity and Access Management (IAM) Service](https://confluence.ecmwf.int/spaces/EWCLOUDKB/pages/439585127/EWC+Identity+and+Access+Management+IAM+Service)