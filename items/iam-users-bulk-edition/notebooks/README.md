# IAM Users Bulk Edition via Jupyter
Step-by-step guide to simplify bulk edition of [EWC IAM](https://confluence.ecmwf.int/spaces/EWCLOUDKB/pages/439585127/EWC+Identity+and+Access+Management+IAM+Service) users via interactive Jupyter Notebook environments.


## Input

> ✅ The only required column is `email`.

> 💡 Default values for all optional  columns are configurable as Jupyter Notebook global parameters.

For simplicity, consider the input example of [users.csv](./users.csv):


email | state | enabled |username | first_name | last_name | roles | comment |
------|-------|---------|---------|------------|-----------|-------|---------|
"john.smith@example.com" | "present" | `true` | | | | | "Adds/updates user with most global defaults (email reused as username)." |
"ada.wong@example.com" | "present" | `false` | | | | | "Adds/updates user with most global defaults but disables login (email reused as username)."  |
"carlos.perez@example.com" | "absent" | | | | | | "Removes user, if exists." |
"philipp.mayer@example.com" | "present" | `true` | "pmayer" | "Philipp"  | "Mayer" | "ewc-jhub-lab-f54924:ewc-jhub-lab-cfe2f3" | "Adds/updates user with optional default overrides for `username`, `first_name`, `last_name` and `roles` (two roles, **colon-separated**)."  |


## Usage

Open [the Jupyter Notebook](./iam-users-bulk-edition.ipynb), start the runtime, and execute cells top to bottom to apply access/permission changes.

## Workflow Stages

1. **Global Parameters**: tenancy name, global default values, `CSV`  path
2. **Dependencies Setup**: fetch dependencies, pin versions, and install
3. **Input Data Loading and Cleaning**: validate and normalize the input `CSV`
4. **Input Data Transformation**: generate and preview the equivalent `YAML` configuration changes to be applied on EWC IAM, based on input user `CSV` data
5. **Apply Changes**: Apply the generated `YAML` configuration
