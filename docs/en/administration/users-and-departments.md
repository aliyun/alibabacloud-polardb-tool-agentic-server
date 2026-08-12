# Users and departments

[简体中文](../../zh-cn/administration/users-and-departments.md)

Administrators manage human identities and organizational access in the Web
console. Agent identities are separate and are managed under **Agents**.

## Users

Open **Users** and choose **Create User** to create a built-in user. Enter a
unique login username, an administrator-set initial password of at least eight
characters, and a role; display name and email are optional. New users are
active immediately. SSO users are matched by the configured identity claim.
Administrators can enable, disable, reset, or delete users. Disabling a user
blocks new authentication and authorization.

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

## Safe administration

Use distinct administrator and database accounts, review bindings after staff
changes, and inspect Audit Logs after role, status, department, credential, or
instance-access updates.
