# Playbooks

One file per site (`chase.md`, `amazon.md`, ...). Short and factual: where things are,
what worked, what to avoid, and the login and SSO hosts (one per line under a
`## Login hosts` heading; the guard allows them).

The browsing agent only proposes changes (`playbook_proposed.md` in the run folder).
`fin sync` shows the diff, and a playbook changes only when the user accepts it.
