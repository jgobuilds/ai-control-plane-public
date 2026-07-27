# Consulting department scope

Practice-level content: engagement methodology, proposal templates, delivery
playbooks. Client scopes nest under this level (see `client-a/`, `client-b/` in
scopes.json) — each is siloed, conflict pairs are walled by the router, and in
silo mode a client's directory is mounted ONLY into that client's dedicated
runner (never into the shared pool runner).
