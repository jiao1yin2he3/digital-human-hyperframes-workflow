# Security Policy

## Supported Versions

Security fixes target the current `main` branch unless maintainers publish a
separate release policy.

## Reporting A Vulnerability

Open a private security advisory or contact the maintainers through the
repository security contact. Do not file public issues containing credentials,
cookies, private media, upload queue paths, or account-specific automation data.

## Sensitive Data

Never commit:

- reference voices, avatar photos, generated audio/video/image files
- model checkpoints or third-party engine checkouts
- `workflow.local.yaml` or other machine-specific config
- cookies, tokens, API keys, Bilibili queue data, or upload markers
- real production project folders with unreleased scripts or source materials

Run a dry-run add and secret scan before publishing:

```bash
git add -n .
rg -n --hidden -g '!node_modules/**' -g '!venv/**' -g '!*-venv/**' '/Users/|/home/|api[_-]?key|access[_-]?token|secret|cookie'
```
