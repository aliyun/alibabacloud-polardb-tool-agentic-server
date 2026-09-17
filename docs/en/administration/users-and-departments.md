# Users and departments

[简体中文](../../zh-cn/administration/users-and-departments.md)

The console groups people under **Access management → Personal accounts** and Agents under **Service accounts**. Use **Resources** for database grants and **Connect MCP** for personal access without an Agent. Existing advanced controls and Agent connections described below remain supported. See [Accounts and resources](accounts-and-resources.md).

Administrators manage human identities and organizational access in the Web
console. Agent identities are separate and are managed under **Agents**.

## Users

Open **Users** and choose **Create User** to create a built-in user. Enter a
unique login username, an administrator-set initial password of at least eight
characters, and a role; display name and email are optional. New users are
active immediately. SSO users are matched by the configured identity claim.
Administrators can enable, disable, reset, or delete users. Disabling a user
blocks new authentication and authorization.

The Users page searches and pages users on the server. It does not download the
complete user directory before filtering. Enterprise identity pickers likewise
page synchronized users and groups on the server, so large tenants do not block
initial page rendering. The user editor's department picker also searches the
backend instead of downloading every department, while retaining the user's
current selections when they are outside the returned page.

Per-instance user access selects a registered instance, an active
`direct_access` credential, `readonly` or `readwrite` permission, and explicit
capabilities. Grant only the databases and operations allowed by the MySQL
account; PAS does not elevate backend privileges.

Human Users use only administrator-assigned registered instances. They do not
claim auto-provisioning pool members, consume Agent purchase budgets, or trigger a
physical cold purchase. If no registered instance is assigned, the request
returns `NO_INSTANCE_ASSIGNED` and the administrator must register and assign
one.

## Departments

A department groups users and may reference one already registered
`multitenant` instance. Registration and credential rotation remain under
**Instances**. Department membership does not copy passwords or create a new
physical instance.

Department provisioning creates logical tenant resources through the selected
backend. Removing a department or binding is an administrative action; inspect
owned resources and cleanup state first.

Feishu department assignments follow the synchronized hierarchy: assigning an
Agent to a parent department includes members of descendant departments, but a
child assignment never grants access to its ancestors or siblings. The user's
ACL context expands upward from each direct department to its ancestors and
also includes synchronized enterprise groups.

## Safe administration

Use distinct administrator and database accounts, review bindings after staff
changes, and inspect Audit Logs after role, status, department, credential, or
instance-access updates.
