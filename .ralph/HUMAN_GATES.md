# Human Gates — Not Ralph Chunks

Ralph builds **Genesis Agents CODE** only. Operational work stays here.

## OPERATIONAL (never auto-chunk)

- [ ] Provision Azure Container App (single replica Phase 1)
- [ ] Move secrets to Azure Key Vault; wire ACA secret refs
- [ ] Provision / attach shared Phoenix Container App (see FinanceOS Azure SPEC)
- [ ] DNS cutover from `swarmsync-agents.onrender.com` to ACA URL
- [ ] Keep Render live one week after cutover (rollback net)
- [ ] Live bwrap spike on ACA base image — Ben sign-off if only `process` tier works

## OUT-OF-REPO

| Work | Where |
|---|---|
| FinanceOS Azure Postgres + ACA | `Energy 4 Life\E4L Finance OS` ralph workspace |
| Cato HTTPS allowlist for FinanceOS | `vault/projects/My Github/Cato` |
| Genesis job Postgres host | SwarmSync.AI repo (Open Question 3) |

## Source specs

- `specs/SPEC-genesis-azure-phoenix-migration.md`
- Parent: `Energy 4 Life\E4L Finance OS\specs\SPEC-azure-phoenix-infrastructure-migration.md`
