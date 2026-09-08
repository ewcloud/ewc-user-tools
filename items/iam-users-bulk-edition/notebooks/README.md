# IAM Users Bulk Edition via Jupyter
Step-by-step guide to simplify bulk edition of [EWC IAM](https://confluence.ecmwf.int/x/Z4kzGg) users via interactive Jupyter Notebook environments.


## Input

> ✅ The only required column is `email`.

> 💡 Default values for all optional columns are configurable as Jupyter Notebook global parameters. Defaults can also be overwritten on each row.

To get started, adapt the typical input example include in [users.csv](./users.csv):


email | state | enabled |username | first_name | last_name | roles | comment |
------|-------|---------|---------|------------|-----------|-------|---------|
"john.smith@example.com" | "present" | `true` | | | | | "Adds/updates user with most global defaults (email reused as username)." |
"ada.wong@example.com" | "present" | `false` | | | | | "Adds/updates user with most global defaults but disables login (email reused as username)."  |
"carlos.perez@example.com" | "absent" | | | | | | "Removes user, if exists." |
"philipp.mayer@example.com" | "present" | `true` | "pmayer" | "Philipp"  | "Mayer" | "ewc-iam-user:ewc-jhub-lab-f54924:ewc-jhub-lab-cfe2f3" | "Adds/updates user with overrides for optional username, optional first name, optional last name and optional roles to access their EWC IAM profile and EWC Jupyter Hub lab sessions cfe2f3 and f54924 (tree roles separated by colon)"  |


## Usage

Open the [iam-user-bulk-edition-via-jupyter.ipynb](./iam-users-bulk-edition-via-jupyter.ipynb) notebook, start the runtime, and execute cells top to bottom to apply access/permission changes.

## Workflow Stages

1. **Global Parameters**: tenancy name, global default values, `CSV`  path
2. **Dependencies Setup**: fetch dependencies, pin versions, and install
3. **Input Data Loading and Cleaning**: validate and normalize the input `CSV`
4. **Configuration Auto-generation**: generate and preview the equivalent `YAML` configuration changes to be applied on EWC IAM, based on input user `CSV` data
5. **Apply Configuration Changes**: Apply the necessary EWC IAM changes as per the generate `YAML` configuration

## Best Practices
1. Prefer a single input file per tenancy, to facilitate auditing tasks such as  ensuring a single config per user. Consider these edge-cases:

    * A user is marked as absent in one input config, the change is applied, and the entry is removed altogether right after. Yet their email appears on a second input config and reintroduced accidentally with login enabled by default.
    * A user requires access to multiple EWC Jupyter Hub environments, but necessary roles to are stated by different input configs, with the last one applied replacing the roles added by the former ones.

    There is risk of data loss or security breaching WHEN ANY OF THE ABOVE becomes true; the recommendation above mitigates said risk.

2. Keep the input config clean of users which are confirmed to have been marked as absent and for which said change was applied.

3. Backup logs or track your input file with GIT or similar version management tools, to better trace access changes applied by this tooling over time.