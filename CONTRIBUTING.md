# Contributing

Thanks for improving HyperFrames Workflow.

## Development

```bash
npm install
npm test
npm run check
```

Use `workflow.local.yaml` for machine-specific paths. Do not commit it.

## Pull Request Checklist

- Keep changes scoped to workflow code, templates, docs, or sanitized examples.
- Add or update focused tests for behavior changes.
- Run `npm run check`.
- Confirm `git add -n .` does not include private media, generated outputs, model
  weights, local configuration, cookies, or upload queue data.
- Avoid committing real production projects under `projects/`; use `examples/`
  for public examples.

## Style

- Prefer configuration over hard-coded local paths.
- Keep scripts idempotent where possible and fail closed on validation errors.
- Write manifests or reports as JSON when downstream automation needs them.
