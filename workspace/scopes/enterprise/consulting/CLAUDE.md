# Consulting department scope

Practice-level content: engagement methodology, proposal templates, delivery
playbooks. Tenant scopes nest under this level (see `tenant-a/`, `tenant-b/` in
scopes.json) — each is siloed, conflict pairs are walled by the router, and in
silo mode a tenant's directory is mounted ONLY into that tenant's dedicated
runner (never into the shared pool runner).
