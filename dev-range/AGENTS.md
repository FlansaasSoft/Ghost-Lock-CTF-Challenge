# Ghost Lock dev range

This directory is a separate Linux training range. Read README.md before operating it.

- Never import, adopt, rebuild, resize, delete, retag, or change pre-existing cloud resources.
- `protected-resources.json` records the initial resources. The runtime baseline adds resources found before a deployment. Resource ownership requires Terraform state, deployment identity, and a matching cloud identity; a name or tag alone is insufficient.
- Use `python scripts/range.py` for cloud changes. It validates saved Terraform plans before applying them. Keep state, keys, passwords, and evidence in ignored `.runtime/`.
- Ordinary plans reject replacement. `plan-replace-analyst` / `replace-analyst` are the only explicit repair exception, and still require exact ownership plus all protected-ID checks. Review that plan and obtain any user-requested approval before using it.
- Gateway integration changes an existing service. It is a separate, explicitly authorized step. New peering to the existing VPC also requires explicit permission. Default deployment does neither.
- Ansible targets only the five NEW deployment IDs, never the existing gateway, relay, Ubuntu pilot, or Kali pilot.
- Keep the Sliver implant inside the training networks. Do not expose Sliver operators or implant listeners to the public Internet. Keep symbol obfuscation disabled for this prototype.
- Test protection policy changes with `python -m unittest discover -s tests -v`, validate Terraform, and perform a live isolated-network test before claiming a playtest passed.
- Record actual results and limitations in TEST-RESULTS.md. Do not claim local syntax checks prove a live callback or public-browser session.

